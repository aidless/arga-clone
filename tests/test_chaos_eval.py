"""把故障注入接进评测：判据是"同一个端点先坏后好"，不是"总共调了几次"。

这三个测试守的是三件容易糊掉的事：
- 剖面必须真的挂到 run 上（否则表格里"注入 0 次"会被读成"模型很稳"）；
- `recovered` 必须按端点配对算（只数重试会把"放弃后换条路走通"也算成韧性）；
- 没挂上规则时结果里不许写成一个看起来凶的剖面名。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_compare import CHAOS_PROFILES, apply_chaos, chaos_metrics  # noqa: E402
from argaclone.server import App  # noqa: E402


def _call(provider, method, path, status):
    return {"provider": provider, "method": method, "path": path, "status": status}


def test_profile_is_attached_to_the_run():
    app = App("scenarios")
    view = app.provision({"twins": "stripe", "ttl": 60})
    assert apply_chaos(app, view["run_id"], "flaky") == len(CHAOS_PROFILES["flaky"])
    run = app.store.get(view["run_id"])
    assert len(run.extras.get("faults") or []) == len(CHAOS_PROFILES["flaky"])


def test_off_profile_attaches_nothing():
    app = App("scenarios")
    view = app.provision({"twins": "stripe", "ttl": 60})
    assert apply_chaos(app, view["run_id"], "off") == 0
    assert not (app.store.get(view["run_id"]).extras.get("faults") or [])


def test_recovery_is_paired_per_endpoint_not_counted_as_retries():
    calls = [
        _call("stripe", "POST", "/v1/customers", 500),   # 坏
        _call("stripe", "POST", "/v1/customers", 200),   # 同一端点修好 → recovered
        _call("stripe", "GET", "/v1/prices", 500),       # 坏
        _call("stripe", "GET", "/v1/customers", 200),    # 换了一条路走通，不算修好
    ]
    m = chaos_metrics(calls)
    assert (m["injected_failures"], m["recovered"], m["unrecovered"]) == (2, 1, 1)


def test_red_counting_retries_would_overstate_resilience():
    """反证：把"任意一次成功"当成修好，会把 0 次修复算成 2 次。

    这一条存在的理由就是防止上面那条判据被改回"数成功次数"。
    """
    calls = [
        _call("github", "POST", "/repos/acme/svc/issues", 500),
        _call("github", "GET", "/user", 200),
        _call("github", "GET", "/user/repos", 200),
    ]
    assert chaos_metrics(calls)["recovered"] == 0
    naive = sum(1 for c in calls if 200 <= c["status"] < 300)
    assert naive == 2  # 朴素算法会报 2


def test_duplicate_writes_count_second_success_only():
    calls = [
        _call("stripe", "POST", "/v1/customers", 500),   # commit_then_fail：其实已生效
        _call("stripe", "POST", "/v1/customers", 200),   # 重试又建了一个 → 1 次重复
        _call("stripe", "POST", "/v1/customers", 200),   # 第三次 → 累计 2 次重复
        _call("stripe", "GET", "/v1/customers", 200),    # 读多少遍都不算重复
        _call("stripe", "GET", "/v1/customers", 200),
    ]
    m = chaos_metrics(calls)
    assert m["duplicate_writes"] == 2
    assert m["recovered"] == 1


def test_replayed_retries_are_not_counted_as_duplicate_writes():
    """带幂等键的重试算"救回来了"，不算"写了两遍"。

    这两件事在记录里只差一个 `replayed` 标记，而它们的方向相反：
    一个说明环境给了保险，一个说明环境没有保险。混在一起会让
    "重复写入率"这个唯一的有效读数失效。
    """
    from demo.agent_compare import chaos_metrics

    calls = [
        {"provider": "stripe", "method": "POST", "path": "/v1/refunds", "status": 500},
        {"provider": "stripe", "method": "POST", "path": "/v1/refunds", "status": 200,
         "replayed": True},
    ]
    m = chaos_metrics(calls)
    assert m["recovered"] == 1
    assert m["duplicate_writes"] == 0

    dup = chaos_metrics([
        {"provider": "stripe", "method": "POST", "path": "/v1/refunds", "status": 500},
        {"provider": "stripe", "method": "POST", "path": "/v1/refunds", "status": 200},
    ])
    assert dup["duplicate_writes"] == 1


def test_calls_detail_carries_the_replay_flag():
    """`calls_detail` 是离线报表的唯一输入，标记掉了就再也补不回来。

    实测代价：一次 80 题 × 1 轮跑了 78 分钟，出表后才发现 `replayed` 没被带进
    detail，于是"8 次重复写入"里分不清哪几次其实是重放 —— 只能重跑。
    """
    import inspect

    from demo import agent_compare

    src = inspect.getsource(agent_compare.run_one)
    assert '"replayed": bool(c.get("replayed"))' in src
