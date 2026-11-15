"""月结刷新服务：变更点 → 完整下游闭包 → 依赖波次 → 幂等执行。

幂等规则：
- 一批变更（周期 + 变更点集合）只有一条 `RefreshBatch`；重复提交返回同一条记录，
  `created=false`、`reused=true`，不新增刷新记录；
- 已 `completed` 的波次不重复执行；`running/failed` 的波次可续跑，其余未开始的波次按顺序执行；
- 每次执行结束汇总 `metrics`（复跑 / 新执行 / 跳过）与 `log_text`，供页面与导出解释。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.constants.enums import (
    REFRESH_REASON_LABELS,
    RefreshStatus,
    RefreshWaveStatus,
    TriggerKind,
)
from src.models.formula import Formula
from src.models.metric import Metric
from src.models.metric_refresh_result import MetricRefreshResult
from src.models.refresh import RefreshBatch, RefreshWave
from src.services import formula_service, graph_service, refresh_analysis
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, local_now, utcnow


def serialize_wave(wave: RefreshWave) -> dict:
    return {
        "id": wave.id,
        "index": wave.index,
        "status": wave.status,
        "metric_codes": list(wave.metric_codes or []),
        "depends_on": list(wave.depends_on or []),
        "reasons": wave.reasons or {},
        "explanation": wave.explanation,
        "result": list(wave.result or []),
        "error_text": wave.error_text,
        "started_at": iso_utc(wave.started_at),
        "finished_at": iso_utc(wave.finished_at),
    }


def serialize(batch: RefreshBatch, waves: list[RefreshWave] | None = None) -> dict:
    plan = batch.plan or {}
    return {
        "id": batch.id,
        "refresh_no": batch.refresh_no,
        "period": batch.period,
        "trigger": batch.trigger,
        "status": batch.status,
        "operator_email": batch.operator_email,
        "change_points": list(batch.change_points or []),
        "change_key": batch.dedupe_key,
        "summary": plan.get("summary") or {},
        "direct": plan.get("direct") or [],
        "closure": plan.get("closure") or [],
        "isolated": plan.get("isolated") or [],
        "reports": plan.get("reports") or {},
        "metrics": batch.metrics or {},
        "log_text": batch.log_text,
        "waves": [serialize_wave(wave) for wave in sorted(waves or [], key=lambda item: item.index)],
        "started_at": iso_utc(batch.started_at),
        "finished_at": iso_utc(batch.finished_at),
    }


def next_refresh_no(db: Session, period: str) -> str:
    prefix = f"RF{period}"
    count = db.scalar(select(func.count()).select_from(RefreshBatch).where(RefreshBatch.refresh_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:03d}"


def normalize_period(period: str | None) -> str:
    if period:
        text = str(period).strip()
        if len(text) == 7 and text[4] == "-":
            year, month = text.split("-")
            if year.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
                return text
        raise AppError("VALIDATION", 422, {"field": "period", "message": "周期格式应为 YYYY-MM"})
    return f"{local_now():%Y-%m}"


def plan_for(db: Session, change_points: list[dict] | None) -> dict:
    """只算不落库：页面预览 / 变更单影响面都能复用。"""
    metrics, reports_index, _ = graph_service.collect_inputs(db)
    try:
        return refresh_analysis.build_plan(metrics, change_points or [], reports_index)
    except refresh_analysis.RefreshPlanError as exc:
        if exc.code == "NOT_FOUND":
            raise AppError("NOT_FOUND", 404, exc.detail, message=exc.message) from exc
        raise AppError("VALIDATION", 422, exc.detail, message=exc.message) from exc


def dedupe_key_for(period: str, points: list[dict]) -> str:
    """幂等键 = 周期 + 归一后的变更点集合（顺序 / 重复不影响）。

    同一批变更在同一个周期内重跑复用同一条记录；换月则视为新一期刷新。
    """
    return f"{period}:{refresh_analysis.change_key(points)}"


def _plan_metrics(plan: dict) -> dict:
    return {
        "direct_count": plan["summary"]["direct_count"],
        "closure_count": plan["summary"]["closure_count"],
        "wave_count": plan["summary"]["wave_count"],
        "refreshable_count": plan["summary"]["refreshable_count"],
        "isolated_count": plan["summary"]["isolated_count"],
        "report_count": plan["summary"]["report_count"],
        "core_report_count": plan["summary"]["core_report_count"],
        "impact_score": plan["summary"]["impact_score"],
        "isolated_reasons": {
            reason: sum(1 for item in plan["isolated"] if item["reason"] == reason)
            for reason in sorted(REFRESH_REASON_LABELS)
        },
    }


def _upsert_waves(db: Session, batch: RefreshBatch, plan: dict) -> list[RefreshWave]:
    existing = {
        wave.index: wave
        for wave in db.scalars(select(RefreshWave).where(RefreshWave.batch_id == batch.id)).all()
    }
    waves: list[RefreshWave] = []
    for item in plan["waves"]:
        wave = existing.get(item["index"])
        if wave is None:
            wave = RefreshWave(batch_id=batch.id, index=item["index"], status=RefreshWaveStatus.PENDING.value)
            db.add(wave)
        # 已完成 / 已跳过的波次保留原执行记录；计划本身总是以最新一次为准
        wave.metric_codes = item["metric_codes"]
        wave.depends_on = item["depends_on"]
        wave.reasons = item["reasons"]
        wave.explanation = item["explanation"]
        waves.append(wave)
    stale = [wave for index, wave in existing.items() if index >= len(plan["waves"])]
    for wave in stale:
        db.delete(wave)
    db.flush()
    return waves


def _load_metrics(db: Session, codes: list[str]) -> list[Metric]:
    if not codes:
        return []
    return db.scalars(select(Metric).where(Metric.code.in_(codes)).order_by(Metric.code)).all()


def _recompute_metric(
    db: Session,
    metric: Metric,
    latest: dict[int, Formula],
    parsed: dict[str, dict],
    state: dict[str, dict],
) -> dict:
    """重算一个指标：用当前上游口径重新解析自己的 DSL，并算出一个可传播的结果指纹。

    `state` 是本次刷新内各指标的最新结果（上游波次会覆盖旧结果），
    指纹 = 自身签名 + 闭包内上游的结果指纹，因此第二三层只会在上游真的变化后变化。
    """
    formula: Formula | None = latest.get(metric.id)
    if formula is None:
        raise AppError("VALIDATION", 422, {"metric": metric.code, "message": "缺少口径公式"})
    result = parsed.get(metric.code) or formula_service.parse_preview(formula.dsl_text, metric.code)
    if result.get("status") != "ok":
        raise AppError("VALIDATION", 422, {"metric": metric.code, "message": "当前口径解析失败"})
    # 上游取全部已登记指标，而不是只看本波：第二三层必须拿到前面波次已更新的结果指纹
    deps = sorted(dep for dep in (result.get("deps") or []) if dep in state)
    upstream_state = {dep: (state.get(dep) or {}).get("fingerprint", "") for dep in deps}
    payload = {"signature": result.get("signature") or "", "upstream": upstream_state}
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "code": metric.code,
        "version": formula.version,
        "signature": formula.signature,
        "fingerprint": fingerprint,
        "aggs": result.get("aggs") or [],
        "fields": result.get("fields") or [],
        "filters": result.get("filters") or [],
        "deps": result.get("deps") or [],
        "upstream": deps,
        "upstream_fingerprints": upstream_state,
        "expression": result.get("expression") or "",
    }


def _base_state(db: Session, latest: dict[int, Formula], metrics: list[Metric]) -> dict[str, dict]:
    """刷新前的“旧结果”：优先取上次刷新快照的指纹，没有快照时用当前签名打底。"""
    snapshots = {row.metric_id: row for row in db.scalars(select(MetricRefreshResult)).all()}
    state: dict[str, dict] = {}
    for metric in metrics:
        snapshot = snapshots.get(metric.id)
        formula = latest.get(metric.id)
        if snapshot is not None and snapshot.fingerprint:
            state[metric.code] = {"fingerprint": snapshot.fingerprint}
        else:
            state[metric.code] = {"fingerprint": formula.signature if formula else ""}
    return state


def _save_results(
    db: Session,
    batch: RefreshBatch,
    wave: RefreshWave,
    results: list[dict],
) -> None:
    """落指标结果快照：报表与下游读取这里，避免沿用旧结果。"""
    if not results:
        return
    codes = [item["code"] for item in results]
    metrics = {metric.code: metric for metric in _load_metrics(db, codes)}
    existing = {
        row.metric_id: row
        for row in db.scalars(
            select(MetricRefreshResult).where(
                MetricRefreshResult.metric_id.in_([metric.id for metric in metrics.values()])
            )
        ).all()
    }
    for item in results:
        metric = metrics.get(item["code"])
        if metric is None:
            continue
        row = existing.get(metric.id)
        if row is None:
            row = MetricRefreshResult(metric_id=metric.id)
            db.add(row)
        row.fingerprint = item["fingerprint"]
        row.signature = item["signature"]
        row.source_refresh_no = batch.refresh_no
        row.source_batch_id = batch.id
        row.wave_index = wave.index
        row.upstream = item.get("upstream_fingerprints") or {}
        row.refreshed_at = utcnow()


def execute(db: Session, batch: RefreshBatch) -> RefreshBatch:
    """按波次顺序执行；已完成的波次跳过，环/缺失引用分支根本不进波次。"""
    waves = sorted(
        db.scalars(select(RefreshWave).where(RefreshWave.batch_id == batch.id)).all(),
        key=lambda item: item.index,
    )
    batch.status = RefreshStatus.RUNNING.value
    if batch.started_at is None:
        batch.started_at = utcnow()
    executed: list[int] = []
    skipped: list[int] = []
    failed: list[int] = []
    latest = formula_service.latest_map(db)
    all_metrics = db.scalars(select(Metric).order_by(Metric.code)).all()
    state = _base_state(db, latest, all_metrics)
    for wave in waves:
        if wave.status == RefreshWaveStatus.COMPLETED.value:
            skipped.append(wave.index)
            # 已完成波次的结果直接回填到传播状态，保证续跑时下游指纹一致
            for item in wave.result or []:
                state[item["code"]] = {"fingerprint": item.get("fingerprint", "")}
            continue
        blockers = [
            other
            for other in waves
            if other.index < wave.index
            and other.status not in (RefreshWaveStatus.COMPLETED.value,)
        ]
        if blockers:
            wave.status = RefreshWaveStatus.SKIPPED.value
            wave.error_text = "上游波次未完成：" + "、".join(str(item.index + 1) for item in blockers)
            failed.append(wave.index)
            continue
        wave.status = RefreshWaveStatus.RUNNING.value
        wave.started_at = utcnow()
        db.flush()
        savepoint = db.begin_nested()
        try:
            codes = list(wave.metric_codes or [])
            metrics = _load_metrics(db, codes)
            parsed = {
                metric.code: formula_service.parse_preview(
                    (latest.get(metric.id).dsl_text if latest.get(metric.id) else ""), metric.code
                )
                for metric in metrics
            }
            results = [_recompute_metric(db, metric, latest, parsed, state) for metric in metrics]
            # 全部重算成功后才落库；单波失败整体回滚，不留半成品
            for metric in metrics:
                formula = latest.get(metric.id)
                if formula is not None and metric.code in parsed:
                    formula_service.apply_parse(formula, parsed[metric.code])
            for item in results:
                state[item["code"]] = {"fingerprint": item["fingerprint"]}
            _save_results(db, batch, wave, results)
            wave.result = results
            wave.error_text = None
            wave.status = RefreshWaveStatus.COMPLETED.value
            wave.finished_at = utcnow()
            savepoint.commit()
            executed.append(wave.index)
        except Exception as exc:  # noqa: BLE001 - 单波失败要留痕，后续波次按依赖跳过
            savepoint.rollback()
            wave.status = RefreshWaveStatus.FAILED.value
            wave.error_text = str(exc)[:300]
            wave.finished_at = utcnow()
            failed.append(wave.index)
        db.flush()

    done = all(wave.status == RefreshWaveStatus.COMPLETED.value for wave in waves)
    batch.status = RefreshStatus.COMPLETED.value if done else RefreshStatus.FAILED.value
    batch.finished_at = utcnow()
    refreshed = [item["code"] for wave in waves if wave.status == RefreshWaveStatus.COMPLETED.value for item in (wave.result or [])]
    plan = batch.plan or {}
    isolated = plan.get("isolated") or []
    batch.metrics = {
        **_plan_metrics(plan),
        "executed_waves": [item + 1 for item in executed],
        "skipped_waves": [item + 1 for item in skipped],
        "failed_waves": [item + 1 for item in failed],
        "refreshed_count": len(refreshed),
        "refreshed_metrics": refreshed,
        "skipped_completed_waves": [item + 1 for item in skipped],
        "run_executed": bool(executed or failed),
    }
    batch.log_text = (
        f"刷新 {batch.period}：变更点 {len(batch.change_points or [])} 个，"
        f"下游闭包 {plan.get('metrics_total', 0)} 个指标；"
        f"执行波次 {len(executed)}/{len(waves)}，跳过已完成 {len(skipped)}，失败 {len(failed)}；"
        f"隔离 {len(isolated)} 个（环 {sum(1 for item in isolated if item['reason'] == 'cycle')} / "
        f"缺失引用 {sum(1 for item in isolated if item['reason'] == 'missing_ref')}）；"
        f"重算 {len(refreshed)} 个指标"
    )
    if not executed and not failed and skipped:
        batch.log_text += "（同一批变更已全部完成，本次未重复执行）"
    db.flush()
    return batch


def create_or_reuse(
    db: Session,
    *,
    period: str | None,
    change_points: list[dict] | None,
    trigger: str = TriggerKind.MANUAL.value,
    operator_email: str = "system",
) -> tuple[RefreshBatch, bool]:
    """同一批变更只保留一条刷新记录；已执行完的直接返回，不重复执行。"""
    normalized = refresh_analysis.normalize_change_points(change_points)
    period_value = normalize_period(period)
    key = dedupe_key_for(period_value, normalized)
    existing = db.scalar(select(RefreshBatch).where(RefreshBatch.dedupe_key == key))
    if existing is not None:
        return existing, False
    plan = plan_for(db, normalized)
    batch = RefreshBatch(
        refresh_no=next_refresh_no(db, period_value.replace("-", "")),
        dedupe_key=key,
        period=period_value,
        trigger=trigger,
        status=RefreshStatus.PENDING.value,
        operator_email=operator_email,
        change_points=normalized,
        plan={**plan, "summary": plan["summary"]},
        metrics={},
    )
    try:
        with db.begin_nested():
            db.add(batch)
            db.flush()
    except IntegrityError:
        # 并发提交同一批变更：让唯一约束兜底，返回先落库的那一条
        existing = db.scalar(select(RefreshBatch).where(RefreshBatch.dedupe_key == key))
        if existing is not None:
            return existing, False
        raise
    _upsert_waves(db, batch, plan)
    batch.metrics = {**_plan_metrics(plan), "executed_waves": [], "skipped_waves": [], "failed_waves": []}
    db.flush()
    return batch, True


def refresh(
    db: Session,
    *,
    period: str | None,
    change_points: list[dict] | None,
    trigger: str = TriggerKind.MANUAL.value,
    operator_email: str = "system",
) -> tuple[RefreshBatch, bool]:
    batch, created = create_or_reuse(
        db,
        period=period,
        change_points=change_points,
        trigger=trigger,
        operator_email=operator_email,
    )
    if batch.status == RefreshStatus.COMPLETED.value:
        return batch, created
    execute(db, batch)
    return batch, created


def status_counts(db: Session) -> dict:
    """总览/页面用：刷新批次总数与各状态计数。"""
    counts = {status.value: 0 for status in RefreshStatus}
    for status, count in db.execute(
        select(RefreshBatch.status, func.count()).group_by(RefreshBatch.status)
    ).all():
        counts[status] = count
    counts["total"] = sum(counts.values())
    return counts


def _waves_of(db: Session, batch_id: int) -> list[RefreshWave]:
    return db.scalars(select(RefreshWave).where(RefreshWave.batch_id == batch_id)).all()


def list_batches(db: Session, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(RefreshBatch)
    count_stmt = select(func.count()).select_from(RefreshBatch)
    if status:
        stmt = stmt.where(RefreshBatch.status == status)
        count_stmt = count_stmt.where(RefreshBatch.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(RefreshBatch.id.desc()).limit(limit).offset(offset)).all()
    return {
        "items": [serialize(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_batch(db: Session, batch_id: int) -> dict:
    batch = db.get(RefreshBatch, batch_id)
    if batch is None:
        raise not_found("refresh_batch", batch_id)
    return serialize(batch, _waves_of(db, batch.id))


def change_options(db: Session) -> dict:
    """页面下拉用：可作变更点的上游字段与基础指标（附下游计数，便于选影响面大的）。"""
    metrics, _, _ = graph_service.collect_inputs(db)
    direct_fields: dict[str, list[str]] = {}
    for metric in metrics:
        for field in metric.get("fields") or []:
            direct_fields.setdefault(field, []).append(metric["code"])
    field_items = []
    for field in sorted(direct_fields):
        codes = sorted(direct_fields[field])
        closure = refresh_analysis.downstream_closure(codes, metrics)
        field_items.append(
            {
                "ref": field,
                "metric_codes": codes,
                "direct_count": len(codes),
                "closure_count": len(closure),
            }
        )
    metric_items = []
    for metric in metrics:
        code = metric["code"]
        closure = refresh_analysis.downstream_closure([code], metrics)
        metric_items.append(
            {
                "ref": code,
                "name": metric.get("name"),
                "tier": metric.get("tier"),
                "parse_status": metric.get("parse_status"),
                "direct_count": 1,
                "closure_count": len(closure),
            }
        )
    return {"fields": field_items, "metrics": metric_items}
