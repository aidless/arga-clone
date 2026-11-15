"""`commit_then_fail`：写已经生效，但调用方看到的是失败。

这是重复扣款/重复建单/重复发消息的唯一成因，也是温和环境里**永远测不到**的一类：
第一次就成功，没有"再发一遍"的时刻。

这里守四件事：
- 状态真的变了（判据必须去读列表，不能只看响应码）；
- 只把 2xx 换成失败（把 404 换成 500 只是噪声）；
- 事件照发（世界变了就该通知，哪怕调用方看到 500）；
- 一次用完就失效（否则后续正常请求全被污染，失败会被归因到错的地方）。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import pytest

from argaclone import chaos, webhooks
from argaclone.server import App


@pytest.fixture()
def world(tmp_path):
    app = App("scenarios", data_dir=str(tmp_path / "state"))
    view = app.provision({"twins": "stripe", "ttl": 600})
    return app, view["run_id"], view["credentials"], app.store.get(view["run_id"])


def call(app, run_id, creds, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    return app.gateway.handle(
        method,
        f"stripe.{run_id}.twins.local",
        path,
        {"authorization": f"Bearer {creds['stripe']}"},
        raw,
    )


def test_write_lands_even_though_the_caller_sees_failure(world):
    app, rid, creds, run = world
    chaos.add_fault(run, {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "status": 500, "times": 1})

    resp = call(app, rid, creds, "POST", "/v1/customers", {"email": "ghost@commit.test"})
    assert resp.status == 500
    assert resp.headers.get("X-Twin-Fault") == "commit_then_fail"

    listed = call(app, rid, creds, "GET", "/v1/customers")
    emails = [c.get("email") for c in listed.body["data"]]
    assert "ghost@commit.test" in emails, "写没生效，那这条故障就不是在测幂等"


def test_only_success_is_masked(world):
    app, rid, creds, run = world
    chaos.add_fault(run, {"provider": "stripe", "method": "POST", "path": "/**",
                          "mode": "commit_then_fail", "times": 1})
    resp = call(app, rid, creds, "POST", "/v1/refunds", {"charge": "ch_missing"})
    # 原响应本来就是 4xx：不该被换成 500
    assert resp.status != 500
    assert "X-Twin-Fault" not in resp.headers


def test_fault_is_consumed_then_the_retry_succeeds(world):
    app, rid, creds, run = world
    chaos.add_fault(run, {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "times": 1})
    call(app, rid, creds, "POST", "/v1/customers", {"email": "a@commit.test"})
    second = call(app, rid, creds, "POST", "/v1/customers", {"email": "a@commit.test"})
    assert second.status == 200
    # 两次都真的建了 —— 这就是重复档案的来源
    listed = call(app, rid, creds, "GET", "/v1/customers")
    dupes = [c for c in listed.body["data"] if c.get("email") == "a@commit.test"]
    assert len(dupes) == 2


def test_event_is_delivered_because_the_world_changed(world):
    app, rid, creds, run = world
    received: list[tuple[dict, bytes]] = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received.append((dict(self.headers), raw))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        webhooks.register(run, {"url": f"http://127.0.0.1:{srv.server_port}/h",
                                "provider": "stripe"})
        chaos.add_fault(run, {"provider": "stripe", "method": "POST",
                              "path": "/v1/customers", "mode": "commit_then_fail",
                              "times": 1})
        resp = call(app, rid, creds, "POST", "/v1/customers", {"email": "ev@commit.test"})
        assert resp.status == 500
    finally:
        srv.shutdown()

    assert len(received) == 1, "世界变了却没通知 —— 订阅方会永久少一条事件"
    headers, raw = received[0]
    assert headers["X-Argaclone-Event"] == "customer.created"


def test_red_without_after_response_the_write_never_looks_failed(world, monkeypatch):
    """反证：把 after_response 变成恒等函数，同一请求就变回 200。

    这一条证明上面那些断言测的是这个机制本身，而不是"twin 恰好会返回 500"。
    """
    app, rid, creds, run = world
    monkeypatch.setattr(chaos, "after_response", lambda run, p, m, path, resp: resp)
    chaos.add_fault(run, {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "times": 1})
    resp = call(app, rid, creds, "POST", "/v1/customers", {"email": "red@commit.test"})
    assert resp.status == 200


def test_admin_surface_is_never_masked(world):
    app, rid, creds, run = world
    chaos.add_fault(run, {"provider": "*", "method": "*", "path": "/**",
                          "mode": "commit_then_fail", "times": 0})
    report = call(app, rid, creds, "GET", "/admin/coverage")
    assert report.status == 200
