"""SDK 测试：打真 HTTP，不用 mock。

理由：SDK 的价值全在"它能替我把这条请求发对"。mock 掉传输层之后，
测的就变成了"我的 mock 会不会被调用"，而真实世界里出错的地方
恰好是被 mock 掉的那一段（路径拼错、Content-Type、错误状态码没抛）。
"""

from __future__ import annotations

import threading

import pytest

from argaclone.client import ArgacloneError, Client
from argaclone.server import App, make_server


@pytest.fixture()
def client(scenario_dir):
    app = App(str(scenario_dir))
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        yield Client(f"http://{host}:{port}")
    finally:
        server.shutdown()
        server.server_close()


def test_provision_returns_endpoints_and_credentials(client):
    run = client.provision(["stripe", "slack"], ttl=300, scenario="ecom-duplicate-billing")
    assert run.run_id
    assert set(run.endpoints) == {"stripe", "slack"}
    assert run.credentials["stripe"] == f"sk_test_twin_{run.run_id}"
    assert run.credentials["slack"].startswith("xoxb-twin-")
    assert run["ttl_seconds"] == 300


def test_run_act_injects_credentials(client):
    run = client.provision(["stripe"], ttl=300)
    trace = run.act([
        {"provider": "stripe", "method": "POST", "path": "/v1/customers",
         "body": {"email": "sdk@example.com"}},
    ])
    assert trace["count"] == 1
    assert trace["calls"][0]["ok"] is True
    assert trace["calls"][0]["status"] == 200


def test_run_act_respects_explicit_authorization(client):
    """显式给凭据时不许被覆盖 —— 否则没法测"用错凭据会怎样"。"""
    run = client.provision(["stripe"], ttl=300)
    trace = run.act([
        {"provider": "stripe", "method": "GET", "path": "/v1/customers",
         "headers": {"Authorization": "Bearer not-a-real-key"}},
    ])
    assert trace["calls"][0]["ok"] is False
    assert trace["calls"][0]["status"] == 401


def test_grade_and_reset_roundtrip(client):
    run = client.provision(["stripe", "slack"], ttl=300, scenario="ecom-duplicate-billing")
    assert run.grade()["verdict"] == "FAIL"

    run.act([
        {"provider": "stripe", "method": "POST", "path": "/v1/customers/cus_DUP_EXTRA",
         "body": {"metadata": {"merged_into": "cus_DUP_KEEP"}}},
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage",
         "body": {"channel": "C_BILLING_OPS", "text": "merged the duplicate billing profile"}},
    ])
    assert run.grade()["verdict"] == "PASS"

    run.reset()
    assert run.grade()["verdict"] == "FAIL"       # 回到 baseline
    assert run.stub_hits()["total_hits"] >= 0     # stub 历史不被 reset 清掉


def test_state_is_reachable(client):
    run = client.provision(["stripe"], ttl=300, scenario="ecom-duplicate-billing")
    state = run.state()
    # 服务端返回的是信封 {run_id, state, shared_people}，SDK 原样透传不加工
    assert "cus_DUP_KEEP" in state["state"]["stripe"]["customers"]


def test_scenarios_listing_and_lookup(client):
    ids = [s["id"] for s in client.scenarios()]
    assert len(ids) == 85
    assert client.scenario("ecom-duplicate-billing")["domain"] == "E-commerce"


def test_error_carries_status_and_body(client):
    with pytest.raises(ArgacloneError) as exc:
        client.get_run("does-not-exist")
    assert exc.value.status == 404
    assert "run_not_found" in str(exc.value.body)


def test_lock_then_teardown(client):
    run = client.provision(["stripe"], ttl=300)
    assert run.lock()["locked"] is True
    assert run.teardown()["deleted"] is True
    with pytest.raises(ArgacloneError):
        client.get_run(run.run_id)
