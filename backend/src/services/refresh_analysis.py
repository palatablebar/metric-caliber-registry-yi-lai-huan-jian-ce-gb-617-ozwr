"""算法 C｜月结刷新编排：下游闭包 → 依赖分层波次 → 环/缺失引用隔离。

设计目标（与校验运行 graph_analysis 解耦，输入仍是纯 dict，便于单测）：

1. **完整下游影响**：从变更点（上游字段 / 基础指标）出发，沿 `指标 -> 被引用指标`
   的反向边走到底，闭包里既包含直接命中，也包含第二、三层间接下游；
2. **按依赖顺序**：把闭包按「依赖深度」分层 —— 只依赖已完成波次的指标进入下一波，
   保证上游先重算、下游后重算；
3. **只隔离受影响分支**：依赖环（SCC）与缺失引用只让所在分支及其下游变成
   `isolated`，其余无环、引用完整的分支照常生成波次并继续执行；
4. **可解释**：每个指标带来源（直接变更点 / 哪个上游把它带进闭包），
   每个波次带上游依赖说明，每条隔离带原因、环路径或缺失引用明细。

确定性：输入相同则输出逐字相同（节点、波次、隔离均按编码排序）。
"""
from __future__ import annotations

import hashlib
import json
from collections import deque

from src.constants.enums import ChangePointKind, RefreshReason
from src.services import graph_analysis


class RefreshPlanError(ValueError):
    """变更点无法展开为刷新计划（未知类型 / 未知目标 / 空变更点）。"""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


_KIND_VALUES = {kind.value for kind in ChangePointKind}


def normalize_change_points(points: list[dict] | None) -> list[dict]:
    """去重 + 排序，让同一批变更（顺序不同 / 重复提交）得到同一个变更键。"""
    seen: dict[tuple[str, str], dict] = {}
    for point in points or []:
        kind = (point.get("kind") or ChangePointKind.METRIC.value).strip().lower()
        ref = str(point.get("ref") or "").strip()
        if kind not in _KIND_VALUES:
            raise RefreshPlanError("VALIDATION", f"未知变更点类型：{kind}", {"kind": kind})
        if not ref:
            raise RefreshPlanError("VALIDATION", "变更点缺少 ref", {"point": point})
        seen[(kind, ref)] = {"kind": kind, "ref": ref}
    if not seen:
        raise RefreshPlanError("VALIDATION", "至少需要一个变更点", {"field": "change_points"})
    return [seen[key] for key in sorted(seen)]


def change_key(points: list[dict]) -> str:
    """变更批次的幂等键：同一批变更重跑复用同一条刷新记录。"""
    payload = json.dumps(points, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dep_maps(metrics: list[dict]) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    dep_map: dict[str, list[str]] = {}
    reverse: dict[str, set[str]] = {}
    for metric in sorted(metrics, key=lambda item: item["code"]):
        code = metric["code"]
        deps = sorted({dep for dep in (metric.get("deps") or []) if dep})
        dep_map[code] = deps
        for dep in deps:
            reverse.setdefault(dep, set()).add(code)
    return dep_map, reverse


def _direct_codes(metrics: list[dict], change_points: list[dict]) -> dict[str, list[str]]:
    """变更点直接命中的指标；值为触发它的变更点标签（如 field:dwd.order.amount）。"""
    known = {metric["code"] for metric in metrics}
    referencing: dict[str, list[str]] = {}
    for metric in metrics:
        for dep in metric.get("deps") or []:
            if dep not in known:
                referencing.setdefault(dep, []).append(metric["code"])
    direct: dict[str, list[str]] = {}
    for point in change_points:
        label = f"{point['kind']}:{point['ref']}"
        if point["kind"] == ChangePointKind.METRIC.value:
            if point["ref"] in known:
                direct.setdefault(point["ref"], []).append(label)
            else:
                # 变更点本身是缺失引用：引用它的分支直接受影响，随后被隔离
                for code in referencing.get(point["ref"], []):
                    direct.setdefault(code, []).append(label)
            continue
        for metric in metrics:
            if point["ref"] in (metric.get("fields") or []):
                direct.setdefault(metric["code"], []).append(label)
    return {code: sorted(set(reasons)) for code, reasons in direct.items()}


def _validate_change_points(metrics: list[dict], change_points: list[dict]) -> None:
    known = {metric["code"] for metric in metrics}
    referenced: set[str] = set()
    fields: set[str] = set()
    for metric in metrics:
        fields.update(metric.get("fields") or [])
        referenced.update(metric.get("deps") or [])
    missing = []
    for point in change_points:
        if point["kind"] == ChangePointKind.METRIC.value and point["ref"] not in known | referenced:
            missing.append({"kind": point["kind"], "ref": point["ref"], "reason": "metric_not_registered"})
        if point["kind"] == ChangePointKind.FIELD.value and point["ref"] not in fields:
            missing.append({"kind": point["kind"], "ref": point["ref"], "reason": "field_not_referenced"})
    if missing:
        raise RefreshPlanError(
            "NOT_FOUND",
            "变更点没有命中任何指标，无法展开下游影响",
            {"change_points": missing},
        )


def _closure(direct: list[str], reverse: dict[str, set[str]]) -> list[str]:
    closure = set(direct)
    queue: deque[str] = deque(sorted(direct))
    while queue:
        code = queue.popleft()
        for dependent in sorted(reverse.get(code, ())):
            if dependent not in closure:
                closure.add(dependent)
                queue.append(dependent)
    return sorted(closure)


def dep_maps(metrics: list[dict]) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """公开入口：正向依赖图与反向（谁引用我）索引，供页面选项与调度复用。"""
    return _dep_maps(metrics)


def downstream_closure(codes: list[str], metrics: list[dict]) -> list[str]:
    """公开入口：从给定指标出发的完整下游闭包（含直接与间接下游）。"""
    _, reverse = _dep_maps(metrics)
    return _closure(sorted(set(codes)), reverse)


def _propagate_isolation(
    isolated: dict[str, dict],
    closure: set[str],
    reverse: dict[str, set[str]],
) -> None:
    """被隔离指标的所有受影响下游一起隔离；不触碰其他分支。"""
    queue: deque[str] = deque(sorted(isolated))
    while queue:
        code = queue.popleft()
        for dependent in sorted(reverse.get(code, ())):
            if dependent not in closure:
                continue
            existing = isolated.get(dependent)
            if existing is None:
                isolated[dependent] = {
                    "code": dependent,
                    "reason": RefreshReason.UPSTREAM_ISOLATED.value,
                    "detail": {"blocked_by": [code]},
                }
                queue.append(dependent)
                continue
            if existing["reason"] == RefreshReason.UPSTREAM_ISOLATED.value:
                blocked = set(existing["detail"].get("blocked_by") or [])
                if code not in blocked:
                    existing["detail"]["blocked_by"] = sorted(blocked | {code})


def _cycle_isolation(
    closure: set[str],
    dep_map: dict[str, list[str]],
    isolated: dict[str, dict],
) -> None:
    """在完整依赖图上求 SCC：闭包内处于环上的指标一律隔离。

    只看闭包子图会漏掉“环的另一半不在本次闭包里”的情况，导致半个环被当成
    可排序节点继续往下跑；用完整图才能把环上的受影响指标全部摘出来。
    """
    for component in graph_analysis.tarjan_scc(dep_map):
        is_cycle = len(component) > 1 or any(code in dep_map.get(code, []) for code in component)
        if not is_cycle:
            continue
        members = set(component)
        path = graph_analysis.shortest_cycle(min(component), dep_map, members)
        for code in component:
            if code not in closure or code in isolated:
                continue
            isolated[code] = {
                "code": code,
                "reason": RefreshReason.CYCLE.value,
                "detail": {"cycle_path": path, "members": sorted(members)},
            }


def _isolate(
    closure: list[str],
    dep_map: dict[str, list[str]],
    metrics_by_code: dict[str, dict],
    reverse: dict[str, set[str]],
) -> dict[str, dict]:
    known = set(metrics_by_code)
    isolated: dict[str, dict] = {}
    # 1) 缺失引用：引用了未登记的指标 -> 该分支无法重算
    for code in closure:
        missing = [dep for dep in dep_map.get(code, []) if dep not in known]
        if missing:
            isolated[code] = {
                "code": code,
                "reason": RefreshReason.MISSING_REF.value,
                "detail": {"missing_refs": sorted(missing)},
            }
    # 2) 最新口径解析失败：没有可用表达式
    for code in closure:
        if code in isolated:
            continue
        if metrics_by_code[code].get("parse_status") != "ok":
            isolated[code] = {
                "code": code,
                "reason": RefreshReason.PARSE_ERROR.value,
                "detail": {"code": code},
            }
    # 3) 依赖环：环成员无法排序，整个 SCC 隔离
    _cycle_isolation(set(closure), dep_map, isolated)
    # 4) 隔离向受影响下游扩散（不越出闭包）
    _propagate_isolation(isolated, set(closure), reverse)
    return isolated


def _waves(
    remaining: list[str],
    dep_map: dict[str, list[str]],
    direct: dict[str, list[str]],
) -> list[dict]:
    """Kahn 分层：同一波内互不依赖，下一波只依赖前面波次。"""
    remaining_set = set(remaining)
    deps_in = {code: [dep for dep in dep_map.get(code, []) if dep in remaining_set] for code in remaining}
    dependents_in: dict[str, list[str]] = {}
    for code in remaining:
        for dep in deps_in[code]:
            dependents_in.setdefault(dep, []).append(code)
    for key in dependents_in:
        dependents_in[key].sort()

    indegree = {code: len(deps_in[code]) for code in remaining}
    depth: dict[str, int] = {}
    ready = deque(sorted(code for code in remaining if indegree[code] == 0))
    for code in ready:
        depth[code] = 0
    while ready:
        code = ready.popleft()
        for dependent in dependents_in.get(code, ()):
            indegree[dependent] -= 1
            depth[dependent] = max(depth.get(dependent, 0), depth[code] + 1)
            if indegree[dependent] == 0:
                ready.append(dependent)

    leftovers = sorted(code for code in remaining if code not in depth)
    if leftovers:  # 防御：SCC 已隔离后不应出现，仍按环处理而不是拖垮整批
        raise RefreshPlanError("INTERNAL", "依赖排序未完成，存在未隔离的环", {"codes": leftovers})

    grouped: dict[int, list[str]] = {}
    for code, level in depth.items():
        grouped.setdefault(level, []).append(code)
    waves: list[dict] = []
    for level in sorted(grouped):
        codes = sorted(grouped[level])
        upstream = sorted({dep for code in codes for dep in deps_in[code]})
        reasons = {}
        for code in codes:
            if code in direct:
                reasons[code] = {"origin": "change_point", "change_points": direct[code]}
            else:
                reasons[code] = {"origin": "downstream", "via": deps_in[code]}
        if level == 0:
            explanation = f"第 {level + 1} 波 · {len(codes)} 个指标；没有待刷新的闭包内上游，可直接重算"
        else:
            explanation = (
                f"第 {level + 1} 波 · {len(codes)} 个指标；依赖已完成的上游 "
                f"{'、'.join(upstream) if upstream else '—'}，按依赖顺序重算"
            )
        waves.append(
            {
                "index": level,
                "metric_codes": codes,
                "reasons": reasons,
                "depends_on": upstream,
                "explanation": explanation,
            }
        )
    return waves


def _origin_labels(
    closure: list[str],
    direct: dict[str, list[str]],
    reverse: dict[str, set[str]],
) -> dict[str, list[str]]:
    """每个指标最终受哪些变更点影响（直接变更点 + 通过反向边传播的祖先变更点）。"""
    closure_set = set(closure)
    origins: dict[str, set[str]] = {code: set(direct.get(code, [])) for code in closure}
    changed = True
    while changed:
        changed = False
        for code in closure:
            for dependent in reverse.get(code, ()):
                if dependent not in closure_set:
                    continue
                new = origins[code] - origins[dependent]
                if new:
                    origins[dependent] |= new
                    changed = True
    return {code: sorted(origins[code]) for code in closure}


def _isolated_entries(isolated: dict[str, dict]) -> list[dict]:
    entries = []
    for code in sorted(isolated):
        item = isolated[code]
        reason = item["reason"]
        detail = item.get("detail") or {}
        if reason == RefreshReason.CYCLE.value:
            path = detail.get("cycle_path") or []
            explanation = f"{code} 位于依赖环 {' → '.join(path)}，环上指标无法排序，已隔离"
        elif reason == RefreshReason.MISSING_REF.value:
            explanation = f"{code} 引用了未登记的指标 {', '.join(detail.get('missing_refs') or [])}，已隔离"
        elif reason == RefreshReason.PARSE_ERROR.value:
            explanation = f"{code} 的最新口径解析失败，没有可用表达式，已隔离"
        else:
            explanation = f"{code} 的上游 {', '.join(detail.get('blocked_by') or [])} 被隔离，本分支整体跳过"
        entries.append(
            {
                "code": code,
                "reason": reason,
                "explanation": explanation,
                "detail": detail,
            }
        )
    return entries


def _report_impact(codes: list[str], reports_by_metric: dict[str, list[dict]]) -> dict:
    found: dict[str, dict] = {}
    for code in codes:
        for report in reports_by_metric.get(code, []):
            found.setdefault(report["code"], report)
    reports = [found[key] for key in sorted(found)]
    payload = graph_analysis.impact_payload(reports)
    payload.update({"metric_codes": list(codes), "metric_count": len(codes)})
    return payload


def build_plan(
    metrics: list[dict],
    change_points: list[dict],
    reports_by_metric: dict[str, list[dict]] | None = None,
) -> dict:
    """展开变更点的完整下游影响，按依赖顺序生成可解释的刷新波次。"""
    points = normalize_change_points(change_points)
    _validate_change_points(metrics, points)
    metrics_by_code = {metric["code"]: metric for metric in metrics}
    dep_map, reverse = _dep_maps(metrics)

    direct = _direct_codes(metrics, points)
    direct_codes = sorted(direct)
    closure = _closure(direct_codes, reverse)
    isolated = _isolate(closure, dep_map, metrics_by_code, reverse)
    remaining = [code for code in closure if code not in isolated]
    waves = _waves(remaining, dep_map, direct)

    origins = _origin_labels(closure, direct, reverse)
    for wave in waves:
        for code in wave["metric_codes"]:
            wave["reasons"][code]["change_points"] = origins.get(code, [])

    isolated_entries = _isolated_entries(isolated)
    impact = _report_impact(remaining, reports_by_metric or {})
    return {
        "change_points": points,
        "change_key": change_key(points),
        "direct": direct_codes,
        "closure": closure,
        "waves": waves,
        "isolated": isolated_entries,
        "metrics_total": len(closure),
        "refreshable_total": len(remaining),
        "isolated_total": len(isolated_entries),
        "reports": impact,
        "summary": {
            "direct_count": len(direct_codes),
            "closure_count": len(closure),
            "wave_count": len(waves),
            "refreshable_count": len(remaining),
            "isolated_count": len(isolated_entries),
            "report_count": impact["report_count"],
            "core_report_count": impact["core_count"],
            "impact_score": impact["score"],
            "impact_severity": impact["severity"],
        },
    }
