"""报表服务：登记列表 + 指标 -> 报表 反向索引（算法 B 的输入）。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import REPORT_WEIGHT_CORE, REPORT_WEIGHT_NORMAL
from src.models.metric import Metric
from src.models.report import Report
from src.utils.timeutil import iso_utc


def serialize(report: Report, snapshot: dict[str, dict] | None = None) -> dict:
    snapshot = snapshot or {}
    metric_codes = list(report.metric_codes or [])
    fresh = [code for code in metric_codes if code in snapshot]
    refresh_nos = sorted({snapshot[code]["refresh_no"] for code in fresh if snapshot[code].get("refresh_no")})
    refreshed_times = [snapshot[code]["refreshed_at"] for code in fresh if snapshot[code].get("refreshed_at")]
    return {
        "id": report.id,
        "code": report.code,
        "name": report.name,
        "owner": report.owner,
        "channel": report.channel,
        "is_core": report.is_core,
        "weight": report.weight,
        "metric_codes": metric_codes,
        "metric_count": len(metric_codes),
        "refresh_cron": report.refresh_cron,
        "created_at": iso_utc(report.created_at),
        # 报表数字是否对得上，取决于下游指标最近一次刷新批次
        "refreshed_metric_count": len(fresh),
        "stale_metric_codes": sorted(code for code in metric_codes if code not in snapshot),
        "refresh_nos": refresh_nos,
        "last_refreshed_at": max(refreshed_times) if refreshed_times else None,
    }


def list_reports(
    db: Session,
    *,
    core: bool | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    stmt = select(Report)
    count_stmt = select(func.count()).select_from(Report)
    if core is not None:
        stmt = stmt.where(Report.is_core.is_(core))
        count_stmt = count_stmt.where(Report.is_core.is_(core))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Report.name.ilike(like) | Report.code.ilike(like))
        count_stmt = count_stmt.where(Report.name.ilike(like) | Report.code.ilike(like))
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Report.code).limit(limit).offset(offset)).all()
    snapshot = refresh_snapshot(db)
    return {
        "items": [serialize(row, snapshot) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def reports_by_metric(db: Session) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for report in db.scalars(select(Report).order_by(Report.code)).all():
        weight = report.weight or (REPORT_WEIGHT_CORE if report.is_core else REPORT_WEIGHT_NORMAL)
        entry = {
            "code": report.code,
            "name": report.name,
            "is_core": bool(report.is_core),
            "weight": int(weight),
            "owner": report.owner,
            "channel": report.channel,
        }
        for metric_code in report.metric_codes or []:
            index.setdefault(metric_code, []).append(entry)
    return index


def refresh_snapshot(db: Session) -> dict[str, dict]:
    """指标编码 -> 最近一次刷新结果（批次、波次、指纹、时间）。"""
    from src.models.metric_refresh_result import MetricRefreshResult

    metrics = {metric.id: metric.code for metric in db.scalars(select(Metric)).all()}
    rows = db.scalars(select(MetricRefreshResult)).all()
    return {
        metrics[row.metric_id]: {
            "fingerprint": row.fingerprint,
            "refresh_no": row.source_refresh_no,
            "batch_id": row.source_batch_id,
            "wave_index": row.wave_index,
            "refreshed_at": iso_utc(row.refreshed_at),
        }
        for row in rows
        if row.metric_id in metrics
    }


def count_reports(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Report)) or 0
