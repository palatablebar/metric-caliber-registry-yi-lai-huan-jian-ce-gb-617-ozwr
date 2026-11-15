"""月结刷新 API 集成测试：同批变更幂等、波次顺序、隔离分支、权限。

用 SQLite 临时库替换 SessionLocal，避免依赖 PostgreSQL / Redis。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import src.config.app as appmod
import src.config.database as dbmod
from src.config.app import create_app
from src.models import Base, MetricRefreshResult, RefreshBatch, User
from src.services import formula_service, metric_service
from src.utils.security import hash_password


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'refresh.db'}",
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", factory)
    monkeypatch.setattr(dbmod, "wait_for_db", lambda *args, **kwargs: True)
    monkeypatch.setattr(dbmod, "ping_db", lambda: True)
    # 不跑启动钩子的 PostgreSQL/seed 逻辑，表结构由测试直接建
    monkeypatch.setattr(appmod, "_bootstrap_database", lambda: None)
    monkeypatch.setattr(appmod, "_run_seed_once", lambda: None)
    Base.metadata.create_all(bind=engine)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client, factory


def _seed_graph(factory) -> None:
    with factory() as db:
        users = [
            ("admin@metricreg.io", "admin"),
            ("analyst@metricreg.io", "analyst"),
            ("viewer@metricreg.io", "viewer"),
        ]
        for email, role in users:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password("Demo@2026"),
                    display_name=email,
                    role=role,
                )
            )

        def add(code: str, dsl: str) -> None:
            metric = metric_service.create_metric(db, {"code": code, "name": code}, author="test@x")
            formula_service.save_version(db, metric, dsl_text=dsl, author_email="test@x")

        add("gmv", "gmv = sum(dwd.order.amount) where order_status = 'paid'")
        add("mid", "mid = sum(@gmv)")
        add("leaf", "leaf = sum(@mid)")
        add("leaf_branch", "leaf_branch = sum(dwd.sku.price) where sku_status = 'on_sale'")
        add("cycle_a", "cycle_a = sum(@cycle_b)")
        add("cycle_b", "cycle_b = sum(@cycle_a)")
        db.commit()


def _token(client: TestClient, email: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": "Demo@2026"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_same_batch_reuses_record_and_layers_all_get_new_result(client):
    test_client, factory = client
    _seed_graph(factory)
    headers = _token(test_client, "analyst@metricreg.io")
    points = [{"kind": "metric", "ref": "gmv"}]

    first = test_client.post("/api/refreshes", json={"period": "2026-11", "change_points": points}, headers=headers)
    assert first.status_code == 201
    assert first.json()["created"] is True
    batch = first.json()["refresh"]
    assert batch["status"] == "completed"

    second = test_client.post("/api/refreshes", json={"period": "2026-11", "change_points": points}, headers=headers)
    assert second.status_code == 201
    assert second.json()["reused"] is True
    assert second.json()["refresh"]["id"] == batch["id"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(RefreshBatch)) == 1

    # 第二三层都必须拿到上游波次的新结果，而不是沿用旧结果
    waves = batch["waves"]
    assert [wave["metric_codes"] for wave in waves] == [["gmv"], ["mid"], ["leaf"]]
    assert waves[2]["result"][0]["upstream_fingerprints"]["mid"] == waves[1]["result"][0]["fingerprint"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(MetricRefreshResult)) == 3


def test_source_change_flows_through_second_and_third_layer(client):
    test_client, factory = client
    _seed_graph(factory)
    headers = _token(test_client, "analyst@metricreg.io")
    points = [{"kind": "metric", "ref": "gmv"}]
    first = test_client.post("/api/refreshes", json={"period": "2026-11", "change_points": points}, headers=headers).json()
    assert first["created"] is True
    with factory() as db:
        fingerprints = {
            row.metric_id: row.fingerprint
            for row in db.scalars(select(MetricRefreshResult)).all()
        }
    with factory() as db:
        from src.models import Metric

        metric = db.scalar(select(Metric).where(Metric.code == "gmv"))
        formula_service.save_version(db, metric, dsl_text="gmv = sum(dwd.order.amount) - sum(dwd.refund.amount)", author_email="test@x")
        db.commit()
    refreshed = test_client.post("/api/refreshes", json={"period": "2026-12", "change_points": points}, headers=headers).json()
    assert refreshed["created"] is True
    waves = refreshed["refresh"]["waves"]
    assert [wave["metric_codes"] for wave in waves] == [["gmv"], ["mid"], ["leaf"]]
    with factory() as db:
        updated = {
            row.metric_id: row.fingerprint
            for row in db.scalars(select(MetricRefreshResult)).all()
        }
    assert updated != fingerprints, "上游口径变了，第二三层结果指纹必须一起变"
    # 第三层拿到的上游指纹必须等于第二层这次的新指纹
    assert waves[2]["result"][0]["upstream_fingerprints"]["mid"] == waves[1]["result"][0]["fingerprint"]
    assert refreshed["refresh"]["status"] == "completed"


def test_cycle_and_missing_ref_isolate_only_their_branch(client):
    test_client, factory = client
    _seed_graph(factory)
    headers = _token(test_client, "analyst@metricreg.io")
    response = test_client.post(
        "/api/refreshes",
        json={"period": "2026-11", "change_points": [{"kind": "metric", "ref": "cycle_a"}]},
        headers=headers,
    )
    batch = response.json()["refresh"]
    codes = {item["code"]: item["reason"] for item in batch["isolated"]}
    assert codes == {"cycle_a": "cycle", "cycle_b": "cycle"}
    assert batch["waves"] == []
    assert batch["status"] == "completed"

    missing = test_client.post(
        "/api/refreshes",
        json={"period": "2026-11", "change_points": [{"kind": "field", "ref": "not.a.field"}]},
        headers=headers,
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


def test_refresh_permissions(client):
    test_client, factory = client
    _seed_graph(factory)
    viewer = _token(test_client, "viewer@metricreg.io")
    assert test_client.get("/api/refreshes", headers=viewer).status_code == 200
    assert (
        test_client.post(
            "/api/refreshes", json={"change_points": [{"kind": "metric", "ref": "gmv"}]}, headers=viewer
        ).status_code
        == 403
    )
    assert test_client.get("/api/refreshes").status_code == 401
    analyst = _token(test_client, "analyst@metricreg.io")
    assert test_client.get("/api/exports/refreshes.csv", headers=analyst).status_code == 200
    assert test_client.get("/api/exports/refreshes.csv", headers=viewer).status_code == 403


def test_failed_wave_skips_dependents_but_other_branch_continues(client, monkeypatch):
    from src.services import refresh_service

    test_client, factory = client
    _seed_graph(factory)
    headers = _token(test_client, "analyst@metricreg.io")

    original = refresh_service._recompute_metric

    def flaky(db, metric, latest, parsed, state):
        if metric.code == "mid":
            raise RuntimeError("上游结果写入失败")
        return original(db, metric, latest, parsed, state)

    monkeypatch.setattr(refresh_service, "_recompute_metric", flaky)
    response = test_client.post(
        "/api/refreshes",
        json={
            "period": "2026-11",
            "change_points": [{"kind": "metric", "ref": "gmv"}, {"kind": "metric", "ref": "leaf_branch"}],
        },
        headers=headers,
    )
    batch = response.json()["refresh"]
    states = {code: wave["status"] for wave in batch["waves"] for code in wave["metric_codes"]}
    assert states["gmv"] == "completed"
    assert states["mid"] == "failed"
    assert states["leaf"] == "skipped"
    # 另一条分支不受影响
    assert states["leaf_branch"] == "completed"
    assert batch["status"] == "failed"

    # 续跑：已完成的波次（含另一条分支）不重复执行，失败波次及下游继续
    monkeypatch.setattr(refresh_service, "_recompute_metric", original)
    resumed = test_client.post(
        "/api/refreshes",
        json={
            "period": "2026-11",
            "change_points": [{"kind": "metric", "ref": "gmv"}, {"kind": "metric", "ref": "leaf_branch"}],
        },
        headers=headers,
    ).json()
    assert resumed["reused"] is True
    assert resumed["refresh"]["status"] == "completed"
    assert resumed["refresh"]["metrics"]["skipped_waves"] == [1]
    assert resumed["refresh"]["metrics"]["executed_waves"] == [2, 3]
    # 续跑没有新增记录
    assert resumed["refresh"]["id"] == batch["id"]
