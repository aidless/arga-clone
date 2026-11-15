"""幂等键：让"重试"这件事变得安全。

这条能力的动因很具体：`commit_then_fail` 之后调用方看到 500，**重试是正确行为**，
但没有幂等键的重试会把同一件事做两遍（§25.3 实测 16%–34%）。
所以"重试安不安全"不是客户端的自觉，而是环境有没有提供这道保险。

这里守五件事：
- 同一把钥匙重放同一份响应，世界只变一次；
- 钥匙配不同的货要拒，不能静默返回旧响应；
- **缓存的是 twin 自己那份响应**（chaos 遮成 500 之前）——否则重试拿回的还是 500；
- 没带钥匙时行为与从前完全一致（这条能力必须是可选的，否则量不出差别）；
- 5xx 不缓存（瞬时故障不该被固化成永久故障）。
"""

from __future__ import annotations

import json

import pytest

from argaclone import chaos
from argaclone.server import App


@pytest.fixture()
def world(tmp_path):
    app = App("scenarios", data_dir=str(tmp_path / "state"))
    view = app.provision({"twins": "stripe", "ttl": 600})
    return app, view["run_id"], view["credentials"]


def call(app, rid, creds, method, path, body=None, key=None):
    headers = {"authorization": f"Bearer {creds['stripe']}"}
    if key:
        headers["idempotency-key"] = key
    raw = json.dumps(body).encode() if body is not None else b""
    return app.gateway.handle(method, f"stripe.{rid}.twins.local", path, headers, raw)


def customers(app, rid, creds):
    return call(app, rid, creds, "GET", "/v1/customers").body["data"]


def test_same_key_replays_and_the_world_changes_once(world):
    app, rid, creds = world
    first = call(app, rid, creds, "POST", "/v1/customers", {"email": "a@idem.test"}, key="k1")
    second = call(app, rid, creds, "POST", "/v1/customers", {"email": "a@idem.test"}, key="k1")

    assert first.status == second.status == 200
    assert first.body["id"] == second.body["id"]
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert len([c for c in customers(app, rid, creds) if c.get("email") == "a@idem.test"]) == 1


def test_same_key_with_different_payload_is_rejected(world):
    app, rid, creds = world
    call(app, rid, creds, "POST", "/v1/customers", {"email": "b@idem.test"}, key="k2")
    resp = call(app, rid, creds, "POST", "/v1/customers", {"email": "other@idem.test"}, key="k2")

    assert resp.status == 400
    # 形状归 provider：Stripe 的错误体是扁平三键，不是通用的 {"error": "..."}。
    assert resp.body["error"]["code"] == "idempotency_error"
    # 静默返回旧响应会让调用方以为新请求生效了 —— 那比报错更难查。
    emails = [c.get("email") for c in customers(app, rid, creds)]
    assert "other@idem.test" not in emails


def test_without_a_key_duplicates_still_happen(world):
    """对照组：这条能力是可选的，否则量不出它买到了什么。"""
    app, rid, creds = world
    for _ in range(2):
        call(app, rid, creds, "POST", "/v1/customers", {"email": "dup@idem.test"})
    n = len([c for c in customers(app, rid, creds) if c.get("email") == "dup@idem.test"])
    assert n == 2


def test_the_whole_point_commit_then_fail_plus_key_is_safe(world):
    """写生效、响应变 500、客户端带同一把钥匙重试 → 只有一份，且拿到第一次的 200。"""
    app, rid, creds = world
    run = app.store.get(rid)
    chaos.add_fault(run, {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "status": 500, "times": 1})

    first = call(app, rid, creds, "POST", "/v1/customers", {"email": "safe@idem.test"}, key="k3")
    assert first.status == 500, "故障没生效，这条测试就没在测它要测的东西"
    assert first.headers.get("X-Twin-Fault") == "commit_then_fail"

    second = call(app, rid, creds, "POST", "/v1/customers", {"email": "safe@idem.test"}, key="k3")
    assert second.status == 200
    assert second.headers.get("Idempotent-Replayed") == "true"
    n = len([c for c in customers(app, rid, creds) if c.get("email") == "safe@idem.test"])
    assert n == 1, "重试又建了一份 —— 缓存的不是 twin 那份响应，而是被遮成 500 的那份"


def test_5xx_is_not_cached(world):
    app, rid, creds = world
    first = call(app, rid, creds, "POST", "/v1/customers", {"email": "x@idem.test"}, key="k4")
    assert first.status == 200
    # 人为塞一条 5xx 进表，验证 _remember 根本不收它
    run = app.store.get(rid)
    from argaclone.twins.base import Response
    app.gateway._remember(run, "stripe", "k5", "fp", "POST", "/v1/customers", Response(503, {"e": 1}))
    table = run.extras.get("idempotency", {}).get("stripe", {})
    assert "k5" not in table


def test_keys_are_scoped_per_run(tmp_path):
    app = App("scenarios", data_dir=str(tmp_path / "state"))
    a = app.provision({"twins": "stripe", "ttl": 600})
    b = app.provision({"twins": "stripe", "ttl": 600})
    call(app, a["run_id"], a["credentials"], "POST", "/v1/customers",
         {"email": "s@idem.test"}, key="shared")
    resp = call(app, b["run_id"], b["credentials"], "POST", "/v1/customers",
                {"email": "s@idem.test"}, key="shared")
    # 同一把钥匙在另一个 run 里必须是全新的：run 之间不共享世界。
    assert "Idempotent-Replayed" not in resp.headers


def test_red_without_remember_the_retry_writes_twice(world, monkeypatch):
    """反证：把 _remember 停掉，同一把钥匙的重试就会真的写第二遍。"""
    app, rid, creds = world
    monkeypatch.setattr(app.gateway, "_remember", lambda *a, **k: None)
    call(app, rid, creds, "POST", "/v1/customers", {"email": "r@idem.test"}, key="k6")
    call(app, rid, creds, "POST", "/v1/customers", {"email": "r@idem.test"}, key="k6")
    n = len([c for c in customers(app, rid, creds) if c.get("email") == "r@idem.test"])
    assert n == 2


def test_no_twin_reads_the_key_itself():
    """幂等只有一层：网关。twin 里再读一次 `idempotency-key` 就有两条路径。

    双通道的后果是本仓已经踩过的：一条有测试、另一条没有，而报表上看起来
    "功能存在"。这里用源码扫描钉住，而不是靠人记得。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "argaclone" / "twins"
    import re

    # 只抓"真的去读这个头"的写法；文档字符串里提到它是允许的。
    pattern = re.compile(r'(?:header|headers(?:\.get)?\(|headers\[)\s*\(?\s*["\']idempotency-key', re.I)
    offenders = []
    for path in sorted(root.glob("*_twin.py")):
        if pattern.search(path.read_text()):
            offenders.append(path.name)
    assert offenders == [], f"这些 twin 自己解析幂等键，机制该留在网关：{offenders}"
