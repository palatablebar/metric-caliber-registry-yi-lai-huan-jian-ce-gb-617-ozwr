"""算法 C（月结刷新编排）单测：下游闭包 / 依赖波次 / 环与缺失引用隔离 / 幂等键。"""
from __future__ import annotations

from src.services import refresh_analysis


def _metric(code, deps=None, fields=None, parse_status="ok"):
    return {
        "code": code,
        "name": code,
        "domain": "trade",
        "tier": "normal",
        "owner": "测试",
        "signature": f"sig-{code}",
        "parse_status": parse_status,
        "aggs": ["sum"],
        "fields": fields or [],
        "filters": [],
        "dims": [],
        "deps": deps or [],
        "expression": f"{code} = sum(@x)",
    }


def test_field_change_expands_all_layers_not_only_direct():
    metrics = [
        _metric("base", fields=["dwd.order.amount"]),
        _metric("mid", deps=["base"]),
        _metric("leaf", deps=["mid"]),
        _metric("unrelated", fields=["dwd.sku.id"]),
    ]
    plan = refresh_analysis.build_plan(metrics, [{"kind": "field", "ref": "dwd.order.amount"}])
    assert plan["direct"] == ["base"]
    assert plan["closure"] == ["base", "leaf", "mid"]
    assert "unrelated" not in plan["closure"]


def test_waves_follow_dependency_order():
    metrics = [
        _metric("base", fields=["f"]),
        _metric("mid", deps=["base"]),
        _metric("leaf", deps=["mid"]),
        _metric("sibling", deps=["base"]),
    ]
    plan = refresh_analysis.build_plan(metrics, [{"kind": "field", "ref": "f"}])
    waves = plan["waves"]
    assert [wave["metric_codes"] for wave in waves] == [["base"], ["mid", "sibling"], ["leaf"]]
    assert waves[1]["depends_on"] == ["base"]
    assert waves[2]["depends_on"] == ["mid"]
    # 第二三层都能解释“自己是因为哪个变更点被带进来的”
    assert waves[2]["reasons"]["leaf"]["change_points"] == ["field:f"]
    assert waves[2]["reasons"]["leaf"]["via"] == ["mid"]
    assert waves[2]["explanation"].startswith("第 3 波")


def test_cycle_only_isolates_its_branch():
    metrics = [
        _metric("base", fields=["f"]),
        _metric("good_down", deps=["base"]),
        _metric("cycle_a", deps=["cycle_b"]),
        _metric("cycle_b", deps=["cycle_a"]),
        _metric("cycle_down", deps=["cycle_a"]),
    ]
    plan = refresh_analysis.build_plan(metrics, [{"kind": "field", "ref": "f"}])
    # 环分支不在闭包里 -> 不影响正常分支
    assert plan["isolated"] == []
    assert [wave["metric_codes"] for wave in plan["waves"]] == [["base"], ["good_down"]]

    cycle_plan = refresh_analysis.build_plan(metrics, [{"kind": "metric", "ref": "cycle_a"}])
    isolated = {item["code"]: item for item in cycle_plan["isolated"]}
    assert set(isolated) == {"cycle_a", "cycle_b", "cycle_down"}
    assert isolated["cycle_a"]["reason"] == "cycle"
    assert isolated["cycle_b"]["detail"]["cycle_path"] == ["cycle_a", "cycle_b", "cycle_a"]
    assert isolated["cycle_down"]["reason"] == "upstream_isolated"
    assert cycle_plan["waves"] == []
    assert cycle_plan["summary"]["refreshable_count"] == 0


def test_missing_reference_isolates_only_affected_branch():
    metrics = [
        _metric("base", fields=["f"]),
        _metric("good_down", deps=["base"]),
        _metric("broken", deps=["not_registered"]),
        _metric("broken_down", deps=["broken"]),
    ]
    plan = refresh_analysis.build_plan(
        metrics, [{"kind": "field", "ref": "f"}, {"kind": "metric", "ref": "broken"}]
    )
    isolated = {item["code"]: item for item in plan["isolated"]}
    assert isolated["broken"]["reason"] == "missing_ref"
    assert isolated["broken"]["detail"]["missing_refs"] == ["not_registered"]
    assert isolated["broken_down"]["reason"] == "upstream_isolated"
    assert [wave["metric_codes"] for wave in plan["waves"]] == [["base"], ["good_down"]]


def test_parse_error_isolated():
    metrics = [_metric("base", fields=["f"], parse_status="error"), _metric("down", deps=["base"])]
    plan = refresh_analysis.build_plan(metrics, [{"kind": "field", "ref": "f"}])
    isolated = {item["code"]: item["reason"] for item in plan["isolated"]}
    assert isolated == {"base": "parse_error", "down": "upstream_isolated"}


def test_unknown_change_point_rejected():
    metrics = [_metric("base", fields=["f"])]
    try:
        refresh_analysis.build_plan(metrics, [{"kind": "field", "ref": "nope"}])
    except refresh_analysis.RefreshPlanError as exc:
        assert exc.code == "NOT_FOUND"
    else:  # pragma: no cover
        raise AssertionError("expected RefreshPlanError")


def test_change_key_is_order_and_duplicate_insensitive():
    first = refresh_analysis.normalize_change_points(
        [{"kind": "field", "ref": "f"}, {"kind": "metric", "ref": "base"}, {"kind": "field", "ref": "f"}]
    )
    second = refresh_analysis.normalize_change_points(
        [{"kind": "metric", "ref": "base"}, {"kind": "field", "ref": "f"}]
    )
    assert first == second
    assert refresh_analysis.change_key(first) == refresh_analysis.change_key(second)


def test_plan_is_deterministic():
    metrics = [
        _metric("base", fields=["f"]),
        _metric("mid", deps=["base"]),
        _metric("leaf", deps=["mid"]),
        _metric("cycle_a", deps=["cycle_b"]),
        _metric("cycle_b", deps=["cycle_a"]),
        _metric("missing", deps=["gone"]),
    ]
    points = [{"kind": "field", "ref": "f"}]
    assert refresh_analysis.build_plan(metrics, points) == refresh_analysis.build_plan(metrics, points)
