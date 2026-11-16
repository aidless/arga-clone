"""预算型限流（罚站）：让"退避"这件事第一次在**分数**上分得开。

§28.5 的否定结果：纯滑动窗口只在背靠背调用里咬得住，而模型每隔几秒才发一次请求，
窗口早滚过去了 —— 160 episodes 里 429 一次都没落到模型手上。于是"退避 0.25s"和
"不退避"的分数差只是噪声。

这一节换一种限流形状：**每题一个预算**（比如 3 次），超了就进罚站，而且**每撞一次
解封更晚**。这正是 GitHub / Stripe 二级限流的形状。它让"重发"从"无效"变成"自伤"：
退避的客户端等到解封就过，不退避的客户端每次重发都把自己推得更远。
"""

from __future__ import annotations

import pytest

from argaclone import chaos
from argaclone.runner import Action
from argaclone.server import App


@pytest.fixture()
def world(tmp_path):
    app = App("scenarios", data_dir=str(tmp_path / "state"))
    view = app.provision({"twins": "stripe", "ttl": 600})
    return app, view["run_id"], view["credentials"]


def _budget(app, rid, *, limit=3, window=600.0, penalty=0.05, max_penalty=0.2):
    return chaos.add_rate_limit(app.store.get(rid), {
        "provider": "stripe", "limit": limit, "window_seconds": window,
        "penalty_seconds": penalty, "max_penalty_seconds": max_penalty,
        "retry_after": 1,
    })


def _get(creds, email_irrelevant=True):
    return Action("stripe", "GET", "/v1/customers", None,
                  headers={"authorization": f"Bearer {creds['stripe']}"})


def test_budget_exhausts_then_penalty_grows(world):
    """预算用完 → 罚站；再撞一次 → 解封更晚。"""
    app, rid, creds = world
    rule = _budget(app, rid)
    for i in range(3):
        app.gateway.handle("GET", f"stripe.{rid}.twins.local", "/v1/customers",
                           {"authorization": f"Bearer {creds['stripe']}"}, b"")
    first = app.gateway.handle("GET", f"stripe.{rid}.twins.local", "/v1/customers",
                               {"authorization": f"Bearer {creds['stripe']}"}, b"")
    assert first.status == 429
    assert int(first.body["violations"]) == 1
    wait1 = float(first.headers["Retry-After"])
    assert "X-Twin-Rate-Penalty" in first.headers

    second = app.gateway.handle("GET", f"stripe.{rid}.twins.local", "/v1/customers",
                                {"authorization": f"Bearer {creds['stripe']}"}, b"")
    assert second.status == 429
    assert int(second.body["violations"]) == 2
    # 罚站从这次重新起算，所以"还要等"没有变短 —— 这是"越撞越晚"的判据
    assert float(second.headers["Retry-After"]) >= wait1
    assert float(second.headers["X-Twin-Rate-Penalty"]) >= float(first.headers["X-Twin-Rate-Penalty"]) * 0.99


def test_penalty_is_capped(world):
    app, rid, creds = world
    rule = _budget(app, rid, penalty=1.0, max_penalty=0.5)
    for i in range(3):
        app.gateway.handle("GET", f"stripe.{rid}.twins.local", "/v1/customers",
                           {"authorization": f"Bearer {creds['stripe']}"}, b"")
    resp = app.gateway.handle("GET", f"stripe.{rid}.twins.local", "/v1/customers",
                              {"authorization": f"Bearer {creds['stripe']}"}, b"")
    assert resp.status == 429
    assert float(resp.headers["X-Twin-Rate-Penalty"]) <= 0.5 + 1e-6, resp.headers


def test_backoff_waits_out_the_penalty_and_gets_through(world):
    """退避的客户端按 `Retry-After` 等，解封后就过。"""
    app, rid, creds = world
    _budget(app, rid)
    runner = app.runner_for(rid)
    runner.client_retries = 4
    runner.retry_backoff = 0.05
    runner.retry_backoff_cap = 0.5
    runner.jitter = 0.0
    import time
    runner._sleep = time.sleep           # 真等：罚站只有 50ms
    for i in range(3):
        runner.call(_get(creds))
    rec = runner.call(_get(creds))
    assert rec["status"] == 200, rec
    assert rec["client_retries"] >= 1
    assert sum(rec["retry_waits"]) >= 0.04


def test_red_immediate_retries_push_the_penalty_further(world):
    """反证：同一份预算、同一套重试次数，只把退避关掉 —— 罚站只会越撞越久。

    这是这一节存在的全部理由：纯窗口限流下"重发"只是无效（分数不变），
    罚站语义下"重发"是自伤。去掉退避，重试次数再多也过不去。
    """
    app, rid, creds = world
    _budget(app, rid, penalty=0.05, max_penalty=0.4)
    runner = app.runner_for(rid)
    runner.client_retries = 4
    runner.retry_backoff = 0.0
    for i in range(3):
        runner.call(_get(creds))
    rec = runner.call(_get(creds))
    assert rec["status"] == 429, rec
    assert rec["client_retries"] == 4
    assert rec["retry_waits"] == [0.0] * 4
    rule = app.store.get(rid).extras["rate_limits"][0]
    # 撞了 1(窗口) + 4(重试) 次，罚站被推后了同样的次数
    assert int(rule["violations"]) >= 4, rule["violations"]


def test_sixty_second_penalty_is_really_waited_out(world):
    """罚站 20s、封顶 60s：退避的客户端**真等得起**，只是等得久。

    这一条不是在测"它能过"，是在测"这个剖面确实是'等不起'的量级"——
    判据是等待时长落在 [20s, 60s] 带内。用注入的假 sleep 断言，不真睡。
    """
    app, rid, creds = world
    rule = _budget(app, rid, limit=3, window=900.0, penalty=20.0, max_penalty=60.0)
    for i in range(3):
        app.gateway.handle("GET", f"stripe.{rid}.twins.local", "/v1/customers",
                            {"authorization": f"Bearer {creds['stripe']}"}, b"")
    resp = app.gateway.handle("GET", f"stripe.{rid}.twins.local", "/v1/customers",
                              {"authorization": f"Bearer {creds['stripe']}"}, b"")
    assert resp.status == 429
    assert 20 <= float(resp.headers["Retry-After"]) <= 61, resp.headers["Retry-After"]

    runner = app.runner_for(rid)
    runner.client_retries = 1
    runner.retry_backoff = 22.0
    runner.retry_backoff_cap = 60.0
    runner.jitter = 0.0
    slept = []
    runner._sleep = slept.append
    # 注意：上面那次探测自己又撞了一回，所以罚站已经加长到 20*1.5 = 30s。
    # 这正是"每撞一次解封更晚"的语义 —— 断言 30 而不是 20，才是在测这条语义。
    rec = runner.call(_get(creds))
    assert rec["retry_waits"] == [30.0], rec["retry_waits"]
    assert slept == [30.0]
