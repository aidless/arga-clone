"""HTTP 层：真起一个 server 再打它。

进程内直调 gateway 测不到的东西都在这里：Host 头传递、JSON 编解码、
admin 路由匹配、异常到状态码的映射。
"""

from __future__ import annotations

import json
import threading
from urllib import error, request

import pytest

from argaclone.server import make_server


@pytest.fixture()
def base_url(app):
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
    except error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None), dict(exc.headers)


def test_healthz(base_url):
    status, body, _ = http("GET", f"{base_url}/healthz")
    assert status == 200 and body["ok"] is True


def test_admin_provision_and_twin_call_over_http(base_url):
    status, run, _ = http("POST", f"{base_url}/admin/runs",
                          {"twins": "stripe,slack", "ttl": 300,
                           "scenario_id": "ecom-duplicate-billing"})
    assert status == 201
    run_id = run["run_id"]
    assert run["endpoints"]["stripe"] == f"http://stripe.{run_id}.twins.local"

    # 关键：只用 Host 头就把同一个端口当 Stripe 用
    status, body, headers = http(
        "GET", f"{base_url}/v1/customers",
        headers={"Host": f"stripe.{run_id}.twins.local",
                 "Authorization": "Bearer sk_test_arga"},
    )
    assert status == 200
    # 明确断言形状：写 `if "data" in body else True` 等于什么都没测
    assert body["object"] == "list"
    assert len(body["data"]) == 2


def test_admin_grade_over_http(base_url):
    _, run, _ = http("POST", f"{base_url}/admin/runs",
                     {"twins": "stripe,slack", "scenario_id": "ecom-duplicate-billing"})
    run_id = run["run_id"]

    status, trace, _ = http("POST", f"{base_url}/admin/runs/{run_id}/actions",
                            {"actions": [
                                {"provider": "stripe", "method": "POST",
                                 "path": "/v1/customers/cus_DUP_EXTRA",
                                 "body": {"metadata": {"merged_into": "cus_DUP_KEEP"}},
                                 "headers": {"Authorization": "Bearer sk_test_arga"}},
                                {"provider": "slack", "method": "POST",
                                 "path": "/api/chat.postMessage",
                                 "body": {"channel": "C_BILLING_OPS", "text": "done"},
                                 "headers": {"Authorization": "Bearer xoxb-twin-token"}},
                            ]})
    assert status == 200
    assert trace["count"] == 2 and trace["error_calls"] == 0

    status, report, _ = http("POST", f"{base_url}/admin/runs/{run_id}/grade", {})
    assert status == 200
    assert report["verdict"] == "PASS"


def test_expired_run_is_410_over_http(base_url, app):
    _, run, _ = http("POST", f"{base_url}/admin/runs", {"twins": "stripe", "ttl": 60})
    app.store.get(run["run_id"], allow_expired=True).created_at -= 120

    status, body, _ = http(
        "GET", f"{base_url}/v1/customers",
        headers={"Host": f"stripe.{run['run_id']}.twins.local",
                 "Authorization": "Bearer sk_test_arga"},
    )
    assert status == 410
    assert body["error"] == "run_expired"


def test_admin_scenario_listing(base_url):
    status, body, _ = http("GET", f"{base_url}/admin/scenarios")
    assert status == 200
    ids = [s["id"] for s in body["scenarios"]]
    # 目录里现在有整套 80 题，断言"包含"而不是"等于某一份"
    assert "ecom-duplicate-billing" in ids
    assert len(ids) == 80


def test_bad_json_body_is_400(base_url):
    req = request.Request(f"{base_url}/admin/runs", data=b"{not json", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=5) as resp:
            status = resp.status
            body = json.loads(resp.read())
    except error.HTTPError as exc:
        status = exc.code
        body = json.loads(exc.read())
    assert status == 400
    assert body["error"] == "invalid_json"
