"""退避与 `Retry-After`：重试的**节奏**也是环境的一部分。

上一节的客户端重试层把重复写入从 8 压到 0，但它连着轰：失败之后立刻重发。
真实 SDK 不会这么做 —— 它指数退避、带抖动，并且**服务端说等多久就等多久**。

为什么这不是锦上添花：这个环境里有固定窗口限流（§23）。客户端不退避时，
一次 429 之后的重发会撞在同一个窗口里，把"稍后再来"变成一串 429；
而客户端退避得对时，同一个窗口过去，第二次就过。

所以这里守五件事：
- 等待按指数增长，且**封顶**是硬上界（抖动不会把等待推到 cap 之上）；
- 抖动落在 [ceil*(1-jitter), ceil] 内，不是"再加一个随机量"；
- `Retry-After` 优先于指数退避（窗口是服务端定的，客户端猜短的只会更糟）；
- 429 与 5xx 可重试，400/402/404 不重试；
- `retry_waits` 如实记下每次等了多久 —— 不记就分不出"真退避"与"碰巧第二次成功"。
"""

from __future__ import annotations

import json

import pytest

from argaclone import chaos
from argaclone.runner import Action
from argaclone.server import App


@pytest.fixture()
def world(tmp_path):
    app = App("scenarios", data_dir=str(tmp_path / "state"))
    view = app.provision({"twins": "stripe", "ttl": 600})
    return app, view["run_id"], view["credentials"]


class _Clock:
    """假 sleep：真实等待会让这些测试变成秒级，而我们要断言的是"等了多少"。"""

    def __init__(self):
        self.slept: list[float] = []

    def __call__(self, secs: float) -> None:
        self.slept.append(secs)


def _runner(app, rid, *, retries=2, backoff=0.0, cap=2.0, jitter=0.5, clock=None):
    clock = clock or _Clock()
    runner = app.runner_for(rid)
    runner.idempotent_writes = True
    runner.client_retries = retries
    runner.retry_backoff = backoff
    runner.retry_backoff_cap = cap
    runner.jitter = jitter
    runner._sleep = clock
    return runner, clock


def _create(creds):
    return Action("stripe", "POST", "/v1/customers", {"email": "backoff@test.dev"},
                  headers={"authorization": f"Bearer {creds['stripe']}"})


def test_backoff_grows_exponentially_and_is_capped(world):
    app, rid, creds = world
    # 三次连续 500、允许重试三次 → 等待应为 base, 2*base, 4*base（jitter=0 时精确）
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                         "path": "/v1/customers", "mode": "status",
                                         "status": 500, "times": 4})
    runner, clock = _runner(app, rid, retries=3, backoff=0.1, cap=0.3, jitter=0.0)
    rec = runner.call(_create(creds))

    assert rec["status"] == 500           # 用完重试仍失败：如实交还，不假装成功
    assert rec["client_retries"] == 3
    assert clock.slept == [0.1, 0.2, 0.3], clock.slept   # 第三档被 cap 截住
    assert rec["retry_waits"] == [0.1, 0.2, 0.3]


def test_jitter_stays_inside_the_band(world):
    app, rid, creds = world
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                         "path": "/v1/customers", "mode": "status",
                                         "status": 500, "times": 4})
    runner, clock = _runner(app, rid, retries=4, backoff=0.2, cap=1.0, jitter=0.5)
    runner.call(_create(creds))

    for i, waited in enumerate(clock.slept, start=1):
        ceiling = min(0.2 * 2 ** (i - 1), 1.0)
        assert ceiling * 0.5 - 1e-9 <= waited <= ceiling + 1e-9, (i, waited, ceiling)


def test_retry_after_beats_the_exponential_guess(world):
    """限流窗口是服务端定的。客户端自己猜一个更短的间隔，只会把 429 变成更多 429。"""
    app, rid, creds = world
    chaos.add_rate_limit(app.store.get(rid), {"provider": "stripe", "limit": 1,
                                              "window_seconds": 1, "retry_after": 1})
    runner, clock = _runner(app, rid, retries=2, backoff=0.01, cap=30.0, jitter=0.0)

    first = runner.call(_create(creds))          # 第一发过
    second = runner.call(Action("stripe", "POST", "/v1/customers",
                                {"email": "second@test.dev"},
                                headers={"authorization": f"Bearer {creds['stripe']}"}))
    assert first["status"] == 200
    assert second["status"] == 429, second
    assert second["client_retries"] == 2
    # 等的是服务端给的 1s，不是退避算出来的 0.01s / 0.02s
    assert second["retry_waits"] == [1.0, 1.0], second["retry_waits"]


def test_throttled_write_goes_through_after_waiting(world):
    """限流 + 退避 = 等到窗口过去就成功；不退避会在同一窗口里连撞。"""
    app, rid, creds = world
    run = app.store.get(rid)
    chaos.add_rate_limit(run, {"provider": "stripe", "limit": 1,
                               "window_seconds": 0.02})
    runner, _ = _runner(app, rid, retries=3, backoff=0.05, cap=0.05, jitter=0.0)
    # 真 sleep 这一次：窗口只有 50ms，代价可接受，换来的是"真的等过去了"
    runner._sleep = __import__("time").sleep

    runner.call(_create(creds))
    rec = runner.call(Action("stripe", "POST", "/v1/customers", {"email": "later@test.dev"},
                             headers={"authorization": f"Bearer {creds['stripe']}"}))
    assert rec["status"] == 200, rec
    assert rec["client_retries"] >= 1
    emails = [c.get("email") for c in app.store.get(rid).state["stripe"]["customers"].values()]
    assert emails.count("later@test.dev") == 1


def test_semantic_rejections_are_never_retried(world):
    app, rid, creds = world
    runner, clock = _runner(app, rid, retries=3, backoff=0.1)
    rec = runner.call(Action("stripe", "POST", "/v1/refunds", {"charge": "ch_NOPE"},
                             headers={"authorization": f"Bearer {creds['stripe']}"}))
    assert rec["status"] >= 400 and rec["status"] not in (429, 500, 502, 503, 504)
    assert rec["client_retries"] == 0
    assert clock.slept == []


def test_zero_backoff_keeps_the_old_behaviour(world):
    """退避默认关闭：不传参数时，等待为 0（老行为，测试与评测都不变慢）。"""
    app, rid, creds = world
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                         "path": "/v1/customers", "mode": "commit_then_fail",
                                         "status": 500, "times": 1})
    runner, clock = _runner(app, rid, retries=2, backoff=0.0)
    rec = runner.call(_create(creds))
    assert rec["status"] == 200 and rec["replayed"] is True
    assert clock.slept == [] and rec["retry_waits"] == [0.0]


def test_red_hammering_without_backoff_keeps_hitting_the_window(world):
    """反证：同一限流规则下不退避，同一秒内重发只会拿到更多 429。

    这条钉住"退避"这个词的具体含义：它是**时间**上的让步，不是次数上的。
    去掉它，上面的 `test_throttled_write_goes_through_after_waiting` 会红，
    而所有只数"重试了几次"的断言仍然绿 —— 那是这一节存在的理由。
    """
    app, rid, creds = world
    chaos.add_rate_limit(app.store.get(rid), {"provider": "stripe", "limit": 1,
                                              "window_seconds": 30, "retry_after": 0})
    runner, clock = _runner(app, rid, retries=3, backoff=0.0)
    runner.call(_create(creds))
    rec = runner.call(Action("stripe", "POST", "/v1/customers", {"email": "hammer@test.dev"},
                             headers={"authorization": f"Bearer {creds['stripe']}"}))
    assert rec["status"] == 429
    assert rec["client_retries"] == 3
    assert clock.slept == [], "退避关掉时不该有任何等待 —— 这正是要反证的那件事"


def test_retry_after_is_not_clamped_by_our_own_cap(world):
    """cap 管的是**我们自己猜的**指数曲线，不是服务端给的指令。

    实测代价：罚站 `Retry-After: 20` 曾被默认 `cap=2.0` 截成 2s，于是"尊重
    Retry-After"的客户端 2 秒后就回来继续撞 —— 看起来像它没退避。
    """
    app, rid, creds = world
    chaos.add_rate_limit(app.store.get(rid), {"provider": "stripe", "limit": 1,
                                              "window_seconds": 900, "retry_after": 20})
    runner, clock = _runner(app, rid, retries=1, backoff=0.01, cap=2.0)
    runner.call(Action("stripe", "GET", "/v1/customers", None,
                       headers={"authorization": f"Bearer {creds['stripe']}"}))
    rec = runner.call(Action("stripe", "GET", "/v1/customers", None,
                             headers={"authorization": f"Bearer {creds['stripe']}"}))
    assert rec["status"] == 429
    assert rec["retry_waits"] == [20.0], rec["retry_waits"]


def test_reads_retry_on_429_but_not_on_500(world):
    """读遇到限流要重试，遇到 5xx 不要 —— 两条都是刻意的。

    429 是"稍后再问"，重发一次读不改世界；5xx 的读则留给模型决定
    （替它决定会把 §23 那批"它会不会重试"的读数抹平）。
    """
    app, rid, creds = world
    chaos.add_rate_limit(app.store.get(rid), {"provider": "stripe", "limit": 1,
                                              "window_seconds": 30, "retry_after": 1})
    runner, clock = _runner(app, rid, retries=2, backoff=0.01, cap=30.0)
    get = Action("stripe", "GET", "/v1/customers", None,
                 headers={"authorization": f"Bearer {creds['stripe']}"})
    runner.call(get)                       # 消耗掉窗口里那一个配额
    rec = runner.call(get)
    assert rec["status"] == 429
    assert rec["client_retries"] == 2      # 读也重试了
    assert rec["retry_waits"] == [1.0, 1.0]

    # 5xx 的读不重试
    app2, rid2, creds2 = world
    chaos.add_fault(app2.store.get(rid2), {"provider": "stripe", "method": "GET",
                                           "path": "/v1/customers", "mode": "status",
                                           "status": 500, "times": 5})
    r2, clock2 = _runner(app2, rid2, retries=3, backoff=0.1)
    rec2 = r2.call(Action("stripe", "GET", "/v1/customers", None,
                          headers={"authorization": f"Bearer {creds2['stripe']}"}))
    assert rec2["status"] == 500 and rec2["client_retries"] == 0
    assert clock2.slept == []
