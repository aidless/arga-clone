"""剖面作用域：一个"等不起"的剖面只能挂在点名的题上。

理由很实际：`stubborn` 的罚站是 20-60 秒，挂到全部 80 题会让一轮评测变成几小时，
而结论只需要两道题。判据的强度来自剖面本身，不来自题数。

这条测试守的是"作用域真的生效"，因为失效的形状是**静默的**：剖面照旧挂，
分数照旧出，只是墙钟多了几个小时 —— 谁也不会注意到。
"""

from __future__ import annotations

import pytest

from argaclone.server import App
from demo.agent_compare import CHAOS_SCOPED_SCENARIOS, apply_chaos


@pytest.fixture()
def app():
    return App("scenarios", data_dir=None)


def test_scoped_profile_only_attaches_to_named_tasks(app):
    view = app.provision({"twins": "stripe", "ttl": 900})
    assert CHAOS_SCOPED_SCENARIOS["stubborn"] == ("crm-01", "crm-02")

    off_task = apply_chaos(app, view["run_id"], "stubborn", "it-01-suspicious-vendor-download")
    assert off_task == 0
    assert not (app.store.get(view["run_id"]).extras.get("rate_limits") or [])

    view2 = app.provision({"twins": "stripe", "ttl": 900})
    on_task = apply_chaos(app, view2["run_id"], "stubborn", "crm-01-duplicate-lead-handoff")
    assert on_task == 1
    limits = app.store.get(view2["run_id"]).extras["rate_limits"]
    assert limits[0]["penalty_seconds"] == 20.0


def test_unscoped_profiles_are_unaffected_by_the_gate(app):
    view = app.provision({"twins": "stripe", "ttl": 900})
    # throttled 没进 CHAOS_SCOPED_SCENARIOS，所以任何题都挂
    assert apply_chaos(app, view["run_id"], "throttled", "it-01") == 2
    assert apply_chaos(app, view["run_id"], "throttled", "crm-01") == 2


def test_red_without_the_gate_the_expensive_profile_hits_every_task(app, monkeypatch):
    """反证：拿掉作用域，80 题里每一题都背上 20-60 秒的罚站。"""
    monkeypatch.setattr(
        "demo.agent_compare.CHAOS_SCOPED_SCENARIOS", {})
    view = app.provision({"twins": "stripe", "ttl": 900})
    assert apply_chaos(app, view["run_id"], "stubborn", "it-01") == 1, (
        "作用域拿掉之后假题也挂上了剖面 —— 这正是失效时没人会注意的那件事"
    )
