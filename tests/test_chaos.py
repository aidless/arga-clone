"""故障注入与限流：让环境会失败。

这一组测试的判据都不是"状态码多少"，而是"这一次失败有没有留下可查的痕迹"：
`X-Twin-Fault` 头、命中计数、`Retry-After`。没有这些，一次 500 与网关自己崩了
长得一模一样，而评测里最需要区分的正是这两件事。
"""

from __future__ import annotations

import json

import pytest

from argaclone import chaos
from argaclone.server import App


@pytest.fixture()
def run(tmp_path):
    app = App("scenarios", data_dir=tmp_path)
    view = app.provision({"twins": "stripe,github", "ttl": 600})
    return app, view["run_id"]


def _call(app, run_id, provider, method, path, body=None):
    tok = app.store.get(run_id).extras["credentials"][provider]
    host = f"{provider}.{run_id}.twins.local"
    raw = json.dumps(body).encode() if body is not None else b""
    return app.gateway.handle(method, host, path, {"authorization": f"Bearer {tok}"}, raw)


def _fault(app, run_id, **spec):
    run = app.store.get(run_id)
    return chaos.add_fault(run, spec)


def test_injected_failure_is_visible_and_bounded(run):
    app, rid = run
    _fault(app, rid, provider="stripe", method="POST", path="/v1/refunds",
           mode="status", status=500, times=1)

    first = _call(app, rid, "stripe", "POST", "/v1/refunds", {"charge": "x"})
    assert first.status == 500
    # 判据一：这次失败是"注入的"，不是"服务崩了"。
    assert first.headers["X-Twin-Fault"] == "status"
    assert first.body["_twin_fault"] == "f1"

    # 判据二：times=1 意味着"坏一次"，第二次必须真的执行。
    second = _call(app, rid, "stripe", "POST", "/v1/refunds", {"charge": "x"})
    assert second.status != 500
    assert "X-Twin-Fault" not in second.headers

    # 判据三：命中数留在 run 上，报表能回答"这条规则被撞了几次"。
    report = chaos.report(app.store.get(rid))
    assert report["faults"][0]["hits"] == 1


def test_fault_is_scoped_by_provider_method_and_path(run):
    app, rid = run
    _fault(app, rid, provider="stripe", method="POST", path="/v1/refunds", times=0)

    # 同 provider 但方法不同：不命中
    assert _call(app, rid, "stripe", "GET", "/v1/refunds").status != 500
    # 同 provider、同方法、路径不同：不命中
    assert _call(app, rid, "stripe", "POST", "/v1/customers",
                 {"email": "a@b.test"}).status != 500
    # 别的 provider：不命中
    assert _call(app, rid, "github", "GET", "/user").status != 500
    # 完全命中：坏
    assert _call(app, rid, "stripe", "POST", "/v1/refunds", {"charge": "x"}).status == 500


def test_times_zero_means_always_broken(run):
    app, rid = run
    _fault(app, rid, provider="*", method="GET", path="/user", times=0)
    for _ in range(3):
        assert _call(app, rid, "github", "GET", "/user").status == 500


def test_timeout_mode_has_its_own_status_and_header(run):
    app, rid = run
    _fault(app, rid, provider="github", method="GET", path="/user", mode="timeout")
    resp = _call(app, rid, "github", "GET", "/user")
    assert resp.status == 504
    assert resp.headers["X-Twin-Fault"] == "timeout"


def test_truncate_mode_returns_half_a_json_document(run):
    app, rid = run
    _fault(app, rid, provider="stripe", method="GET", path="/v1/customers",
           mode="truncate")
    resp = _call(app, rid, "stripe", "GET", "/v1/customers")
    assert resp.status == 200
    raw = resp.to_bytes()
    # 截断不是"少个字段"，是"解析不出来"。客户端必须能处理 JSONDecodeError。
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_admin_surface_is_never_fault_injected(run):
    app, rid = run
    # 把 /admin/** 也纳入"永远坏"的规则：如果守住了 admin 面，这里仍然 200。
    _fault(app, rid, provider="*", method="*", path="/admin/**", times=0)
    resp = _call(app, rid, "stripe", "GET", "/__twin/stripe/%s/admin/coverage" % rid)
    assert resp.status == 200
    # 而数据面已经坏了，证明规则本身是生效的（不是"没写对因此都不命中"）。
    _fault(app, rid, provider="*", method="*", path="/v1/**", times=0)
    assert _call(app, rid, "stripe", "GET", "/v1/customers").status == 500


def test_rate_limit_returns_429_with_retry_headers(run):
    app, rid = run
    rule = chaos.add_rate_limit(app.store.get(rid),
                                {"provider": "stripe", "limit": 2, "window_seconds": 60})
    assert rule["limit"] == 2

    assert _call(app, rid, "stripe", "GET", "/v1/customers").status == 200
    assert _call(app, rid, "stripe", "GET", "/v1/customers").status == 200

    third = _call(app, rid, "stripe", "GET", "/v1/customers")
    assert third.status == 429
    assert third.headers["Retry-After"] == "1"
    assert third.headers["X-RateLimit-Limit"] == "2"
    assert third.headers["X-RateLimit-Remaining"] == "0"
    assert third.headers["X-Twin-Rate-Limited"] == "true"


def test_rate_limit_is_per_provider(run):
    app, rid = run
    chaos.add_rate_limit(app.store.get(rid),
                         {"provider": "stripe", "limit": 1, "window_seconds": 60})
    assert _call(app, rid, "stripe", "GET", "/v1/customers").status == 200
    assert _call(app, rid, "stripe", "GET", "/v1/customers").status == 429
    # 配额按 provider 记账：stripe 打满了不该连带 github。
    assert _call(app, rid, "github", "GET", "/user").status == 200


def test_no_rules_means_no_interference(run):
    """没登记规则时必须完全空转 —— 否则"默认就坏"会让所有旧读数失效。"""
    app, rid = run
    for _ in range(5):
        resp = _call(app, rid, "stripe", "GET", "/v1/customers")
        assert resp.status == 200
        assert "X-Twin-Fault" not in resp.headers
    assert chaos.report(app.store.get(rid)) == {
        "faults": [], "faults_after": [], "rate_limits": [], "rate_used": {},
    }


def test_red_without_the_guard_the_injected_failure_never_happens(run, monkeypatch):
    """RED：证明保证来自 chaos.guard，而不是别的什么东西。

    把 guard 换成空转，同一发请求必须 200 —— 否则"注入成功"可能只是别的层
    恰好也回了 500（那这条测试就在测空气）。
    """
    app, rid = run
    _fault(app, rid, provider="stripe", method="GET", path="/v1/customers", times=0)
    assert _call(app, rid, "stripe", "GET", "/v1/customers").status == 500

    monkeypatch.setattr(chaos, "guard", lambda *a, **k: None)
    assert _call(app, rid, "stripe", "GET", "/v1/customers").status == 200


def test_unknown_mode_is_rejected(run):
    app, rid = run
    with pytest.raises(ValueError):
        chaos.add_fault(app.store.get(rid), {"mode": "explode"})
