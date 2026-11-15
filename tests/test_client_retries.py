"""客户端重试层：把"重试"从模型的自选动作，变成 SDK 的内建行为。

为什么这条轴必须单独存在：§26 的读数里剩下的 8 次重复写入，全部长同一个样子 ——
`commit_then_fail` 让模型看到 500，模型重试，但重试时**重新组织了一遍请求**
（措辞、字段都可能变），于是幂等键换了，世界被写了两次。

关键不是"模型笨"，而是**客户端建错了**：真实 SDK 的传输级重试发生在
"一次逻辑操作"内部，重发的是同一个已经序列化好的请求，钥匙和 body 都不变。
这个参数量的就是那句话 —— 客户端建对之后，这个环境能不能给出恰好一次。

这里守六件事：
- 传输级失败（5xx）由客户端重试，重试成功后世界只有一份；
- 4xx 是语义拒绝，不重试（重发只会拿到同一个 4xx，那是日志噪声）；
- 读接口不重试（重试读只会让报表里的调用数虚高）；
- 重试次数有上界，用完仍失败就如实把失败交还给调用方；
- 没有幂等键时的对照：同一套重试会写两遍（证明这份保险是钥匙给的，不是重试给的）；
- `client_retries` 与 `replayed` 分开记（药和病不能共用一个字段）。
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


def _runner(app, rid, *, idem: bool, retries: int):
    runner = app.runner_for(rid)
    runner.idempotent_writes = idem
    runner.client_retries = retries
    return runner


def _emails(app, rid) -> list[str]:
    state = app.state(rid)["state"]["stripe"]
    return [c.get("email") for c in state["customers"].values()]


def _create(creds):
    return Action("stripe", "POST", "/v1/customers", {"email": "retry@commit.test"},
                  headers={"authorization": f"Bearer {creds['stripe']}"})


def _read(creds, path="GET /v1/customers"):
    return Action("stripe", "GET", "/v1/customers",
                  headers={"authorization": f"Bearer {creds['stripe']}"})


def test_transport_failure_is_retried_and_world_moves_once(world):
    app, rid, creds = world
    # commit_then_fail：世界真的变了，但响应被遮成 500
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "status": 500, "times": 1})
    runner = _runner(app, rid, idem=True, retries=2)
    rec = runner.call(_create(creds))

    assert rec["status"] == 200, rec
    assert rec["ok"] is True
    assert rec["replayed"] is True          # 客户端拿回的是第一次那份响应
    assert rec["client_retries"] == 1
    assert _emails(app, rid).count("retry@commit.test") == 1


def test_without_the_client_layer_the_caller_sees_a_500(world):
    app, rid, creds = world
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "status": 500, "times": 1})
    runner = _runner(app, rid, idem=True, retries=0)
    rec = runner.call(_create(creds))

    assert rec["status"] == 500
    assert rec["ok"] is False
    assert rec["client_retries"] == 0
    # 写已经落地了，只是调用方不知道 —— 这正是需要幂等键的那个瞬间
    assert _emails(app, rid).count("retry@commit.test") == 1


def test_semantic_rejection_is_not_retried(world):
    app, rid, creds = world
    runner = _runner(app, rid, idem=True, retries=3)
    # 不存在的 charge：4xx 是语义拒绝，重试没有意义
    rec = runner.call(Action("stripe", "POST", "/v1/refunds", {"charge": "ch_NOPE"},
                             headers={"authorization": f"Bearer {creds['stripe']}"}))
    assert rec["status"] >= 400
    assert rec["client_retries"] == 0


def test_reads_are_not_retried(world):
    app, rid, creds = world
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "GET", "path": "/v1/customers",
                          "mode": "status", "status": 500, "times": 1})
    runner = _runner(app, rid, idem=True, retries=3)
    rec = runner.call(_read(creds))
    assert rec["status"] == 500
    assert rec["client_retries"] == 0


def test_retries_are_bounded_and_the_failure_is_returned(world):
    app, rid, creds = world
    # 两次故障、只允许重试一次：重试一次仍失败，如实交还
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "status", "status": 503, "times": 2})
    runner = _runner(app, rid, idem=True, retries=1)
    rec = runner.call(_create(creds))
    assert rec["status"] == 503
    assert rec["client_retries"] == 1
    assert _emails(app, rid).count("retry@commit.test") == 0


def test_red_the_retry_alone_does_not_make_it_exactly_once(world):
    """反证：同样的重试层，只是不带钥匙 —— 世界被写两遍。

    这条断言是整份文件的承重点。它证明"恰好一次"是**钥匙 + 重试**合起来买到的，
    不是重试这一件事买到的；去掉钥匙，同一段代码就把重复写入造出来了。
    """
    app, rid, creds = world
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "status": 500, "times": 1})
    runner = _runner(app, rid, idem=False, retries=2)
    rec = runner.call(_create(creds))

    assert rec["client_retries"] == 1
    assert _emails(app, rid).count("retry@commit.test") == 2


def test_trace_counts_client_retries_separately(world):
    app, rid, creds = world
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST", "path": "/v1/customers",
                          "mode": "commit_then_fail", "status": 500, "times": 1})
    runner = _runner(app, rid, idem=True, retries=2)
    runner.call(_create(creds))
    trace = runner.trace()
    assert trace["client_retries"] == 1
    assert trace["writes"][0]["client_retries"] == 1
    # 世界只有一份：重放不是又写了一次
    assert trace["writes"][0]["replayed"] is True
