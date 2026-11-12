"""调度裁决相关 HTTP 接口的端到端测试（FastAPI TestClient，不起真实端口）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()
    yield
    store.reset()


@pytest.fixture
def client():
    return TestClient(app)


def _enable(client: TestClient, entry_id: int) -> None:
    for action in ("提交校验", "启用配置"):
        client.post(f"/api/schedule/{entry_id}/actions", json={"values": {"action": action}})


def test_static_routes_not_captured_by_dynamic_id(client):
    # /pending、/batches、/export 必须命中静态路由而不是被 /{entry_id} 截获。
    assert client.get("/api/schedule/pending").status_code == 200
    assert client.get("/api/schedule/batches").status_code == 200
    assert client.get("/api/schedule/export").status_code == 200


def test_get_unknown_entry_returns_404(client):
    resp = client.get("/api/schedule/999")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


def test_arbitrate_end_to_end(client):
    _enable(client, 1)
    resp = client.post("/api/schedule/arbitrate", json={"window_minutes": 10})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"]
    batch = payload["batches"][0]
    assert batch["占用计划编号"] == "SCHE-0001"
    assert batch["改期条目"] == ["SCHE-R-0001"]

    # 工作台接口能查到待确认计划。
    pending = client.get("/api/schedule/pending").json()
    codes = {item["计划编号"] for item in pending["items"]}
    assert {"SCHE-0003", "SCHE-R-0001"} <= codes

    # 重复裁决沿用第一个批次。
    again = client.post("/api/schedule/arbitrate", json={"plan_ids": [1, 3], "window_minutes": 10})
    assert again.json()["reused"] is True
    assert again.json()["batches"][0]["裁决批次号"] == batch["裁决批次号"]


def test_action_reverse_rejected_over_http(client):
    resp = client.post("/api/schedule/1/actions", json={"values": {"action": "停用配置"}})
    body = resp.json()
    assert body["ok"] is False
    assert "四阶段" in body["message"]


def test_reschedule_decision_and_batch_start(client):
    _enable(client, 1)
    batch = client.post("/api/schedule/arbitrate", json={}).json()["batches"][0]

    # 批次启动后固化失败策略。
    start = client.post(f"/api/schedule/batches/{batch['裁决批次号']}/start")
    assert start.json()["ok"]
    assert start.json()["entry"]["生效失败策略"] == "重试2次"

    # 工作台确认改期。
    pending = client.get("/api/schedule/pending").json()["items"]
    r_id = next(item["id"] for item in pending if item["计划编号"].startswith("SCHE-R-"))
    decision = client.post(f"/api/schedule/{r_id}/reschedule-decision", json={"decision": "确认改期"})
    assert decision.json()["ok"]
    assert decision.json()["entry"]["确认状态"] == "已确认改期"


def test_recover_endpoint(client):
    _enable(client, 2)
    client.patch("/api/schedule/2", json={"values": {"执行时间": "23:50", "优先级": 1}})
    client.post(
        "/api/schedule",
        json={"values": {
            "计划编号": "SCHE-0501", "关联任务": "TASK-0001", "调度表达式": "50 23 * * *",
            "优先级": 9, "超时阈值": "10秒", "失败策略": "终止任务",
            "通知渠道": "短信", "执行时间": "23:50",
        }},
    )
    # 新计划默认草稿，推到已启用。
    listing = client.get("/api/schedule?size=200").json()["items"]
    new_id = next(item["id"] for item in listing if item["计划编号"] == "SCHE-0501")
    _enable(client, new_id)

    arb = client.post("/api/schedule/arbitrate", json={"window_minutes": 5}).json()
    assert arb["batches"]
    recover = client.post("/api/schedule/recover", json={"base_date": "2026-11-13", "window_minutes": 5})
    assert recover.json()["ok"]
    assert recover.json()["moved"]


def test_create_duplicate_code_rejected(client):
    resp = client.post("/api/schedule", json={"values": {
        "计划编号": "SCHE-0001", "关联任务": "TASK-0001", "调度表达式": "0 9 * * *",
    }})
    assert resp.json()["ok"] is False
    assert "已存在" in resp.json()["message"]


def test_overview_exposes_pending_card(client):
    _enable(client, 1)
    client.post("/api/schedule/arbitrate", json={})
    overview = client.get("/api/overview").json()
    card = next(c for c in overview["cards"] if c["label"] == "工作台待确认计划")
    assert card["value"] >= 2
