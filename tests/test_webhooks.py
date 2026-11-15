"""Webhook 投递：三条判据 —— 真发出去了、签名能验、没发出的原因查得到。

反面样本比正面样本重要：一个"注册了就一定收到"的实现，在真环境里等于
"通知永远不会丢"，而 agent 学不到"收不到就得自己查"。所以这里有一条测试
专门证明"没声明的变更不发通知"，还有一条证明"对方 500 时重试到上限就停，
并留下 attempts=3 的记录"。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from argaclone import webhooks
from argaclone.server import App


class _Sink:
    """本地接收端：记下每条请求的头与 body，可以按需回错误码。"""

    def __init__(self, status=200):
        self.received: list[dict] = []
        self.status = status
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                sink.received.append({"headers": dict(self.headers), "raw": raw})
                self.send_response(sink.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):  # 静音
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/hook"


@pytest.fixture()
def run(tmp_path):
    app = App("scenarios", data_dir=tmp_path)
    view = app.provision({"twins": "stripe,github", "ttl": 600})
    return app, view["run_id"]


def _call(app, rid, provider, method, path, body=None):
    tok = app.store.get(rid).extras["credentials"][provider]
    raw = json.dumps(body).encode() if body is not None else b""
    return app.gateway.handle(
        method, f"{provider}.{rid}.twins.local", path,
        {"authorization": f"Bearer {tok}"}, raw,
    )


def test_delivery_arrives_signed_and_verifiable(run):
    app, rid = run
    with _Sink() as sink:
        hook = webhooks.register(app.store.get(rid),
                                 {"url": sink.url, "provider": "stripe"})
        resp = _call(app, rid, "stripe", "POST", "/v1/customers",
                     {"email": "wh@acme.test"})
        assert resp.status == 200

    assert len(sink.received) == 1
    got = sink.received[0]
    assert got["headers"]["X-Argaclone-Event"] == "customer.created"
    # 判据一：签名能被接收方用共享密钥独立验过。
    assert webhooks.verify(hook["secret"], got["headers"]["X-Argaclone-Signature"],
                           got["raw"])
    # 篡改 body 必须验不过（否则签名只是个装饰性字符串）。
    assert not webhooks.verify(hook["secret"], got["headers"]["X-Argaclone-Signature"],
                               got["raw"] + b" ")

    log = webhooks.report(app.store.get(rid))["log"]
    assert log[0]["ok"] and log[0]["status"] == 200 and log[0]["attempts"] == 1


def test_failed_delivery_is_retried_and_recorded(run):
    app, rid = run
    with _Sink(status=500) as sink:
        webhooks.register(app.store.get(rid),
                          {"url": sink.url, "provider": "stripe", "max_attempts": 3})
        _call(app, rid, "stripe", "POST", "/v1/customers", {"email": "x@acme.test"})

    assert len(sink.received) == 3, "失败必须重试到上限，而不是发一次就算"
    log = webhooks.report(app.store.get(rid))["log"]
    assert log[0]["attempts"] == 3 and log[0]["ok"] is False and log[0]["status"] == 500


def test_undeclared_changes_emit_nothing(run):
    """没在 EVENT_MAP 里声明的变更不发通知 —— 不糊一条 `something.changed`。

    糊一条的代价：订阅方会以为自己覆盖了全部事件，而实际上漏掉的那些
    永远查不出来（因为"没收到"和"没发生"长得一样）。
    """
    app, rid = run
    with _Sink() as sink:
        webhooks.register(app.store.get(rid), {"url": sink.url, "provider": "stripe"})
        # PATCH /v1/customers/{id} 没声明 → 世界变了但不发事件。
        _call(app, rid, "stripe", "POST", "/v1/customers", {"email": "a@acme.test"})
        assert len(sink.received) == 1      # 只有 create 那条
        _call(app, rid, "stripe", "POST", "/v1/customers/cus_x", {"metadata": {"a": "b"}})
        assert len(sink.received) == 1, "未声明的事件不该发"


def test_failed_request_emits_nothing(run):
    app, rid = run
    with _Sink() as sink:
        webhooks.register(app.store.get(rid), {"url": sink.url, "provider": "stripe"})
        # 400：世界没变，不该通知。
        bad = _call(app, rid, "stripe", "POST", "/v1/subscriptions", {"customer": "nope"})
        assert bad.status >= 400
        assert sink.received == []


def test_event_filter_is_respected(run):
    app, rid = run
    state = app.store.get(rid).state["github"]
    state["repos"]["acme/svc"] = {"id": 1, "name": "svc", "full_name": "acme/svc",
                                  "private": False, "stargazers_count": 0,
                                  "default_branch": "main"}
    state["issues"]["acme/svc"] = {}
    with _Sink() as sink:
        webhooks.register(app.store.get(rid),
                          {"url": sink.url, "provider": "github",
                           "events": ["issues.opened"]})
        # 未订阅的事件：发了也不该收到
        _call(app, rid, "github", "POST", "/repos/acme/svc/pulls",
              {"title": "p", "head": "feature", "base": "main"})
        assert sink.received == [], "订阅了 A 事件却收到 B 事件"
        _call(app, rid, "github", "POST", "/repos/acme/svc/issues",
              {"title": "new", "body": "b"})
        assert [r["headers"]["X-Argaclone-Event"] for r in sink.received] == ["issues.opened"]


def test_github_patch_issue_derives_event_from_body(run):
    """同一个端点按 body 发不同事件：PATCH issue 关/开不是同一件事。"""
    app, rid = run
    state = app.store.get(rid).state["github"]
    state["repos"]["acme/svc"] = {"id": 1, "name": "svc", "full_name": "acme/svc",
                                  "private": False, "stargazers_count": 0,
                                  "default_branch": "main"}
    # issue 表的键是**字符串** number（update_issue 用 str(number) 查），
    # 塞 int 键会让"issue 明明在"的请求 404。
    state["issues"]["acme/svc"] = {"7": {"number": 7, "title": "t", "state": "open"}}

    with _Sink() as sink:
        webhooks.register(app.store.get(rid), {"url": sink.url, "provider": "github"})
        _call(app, rid, "github", "PATCH", "/repos/acme/svc/issues/7", {"state": "closed"})
        _call(app, rid, "github", "PATCH", "/repos/acme/svc/issues/7", {"title": "x"})

    events = [r["headers"]["X-Argaclone-Event"] for r in sink.received]
    assert events == ["issues.closed", "issues.edited"]


def test_report_masks_secrets(run):
    app, rid = run
    hook = webhooks.register(app.store.get(rid),
                             {"url": "http://127.0.0.1:9/x", "provider": "stripe",
                              "secret": "whsec_supersecret_value"})
    assert hook["secret"] == "whsec_supersecret_value"   # 注册时明文一次性回给调用方
    masked = webhooks.report(app.store.get(rid))["webhooks"][0]["secret"]
    assert masked.startswith("whsec_su") and masked.endswith("…")
    assert "supersecret" not in masked


def test_red_without_the_gateway_hook_nothing_is_delivered(run, monkeypatch):
    """RED：证明投递真的挂在网关那一支上，而不是别处恰好发了。"""
    app, rid = run
    monkeypatch.setattr(webhooks, "deliver", lambda *a, **k: [])
    with _Sink() as sink:
        webhooks.register(app.store.get(rid), {"url": sink.url, "provider": "stripe"})
        _call(app, rid, "stripe", "POST", "/v1/customers", {"email": "n@acme.test"})
        assert sink.received == []
