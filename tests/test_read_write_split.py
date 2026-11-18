"""读/写拆分：`trace.reads` 与 `trace.writes` 必须是真实列表，且读/写判定只准走 `_is_write`。

为什么这条轴要单独存在：`within-budget` 数的是 `trace.calls.count`，它分不清
"读得多"与"写得多"。于是同一个超预算判决可以来自两种完全不同的行为 ——
把现状反复读十遍（效率问题）与把同一件事写了十遍（正确性问题）。

这里守四件事：
- 两个列表都是**真实条目**，不是现算的整数（`count` 作用其上即长度）；
- 读/写的分界是 `_is_write`，不是 `method != GET` —— Slack 的读接口也是 POST；
- 两个列表加起来等于 `calls`（没有条目会在拆分里消失）；
- `empty_trace()` 也带这两个键（否则空 trace 上的判据会走 `PATH_ERROR` 而不是给出 0）。

顺带钉住一条**否定结论**：`reads >= gold_reads` 这种"读够了"判据是永真的，
不能写进题库 —— 见 `test_reads_sufficient_would_be_vacuous_on_the_corpus`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argaclone.graders import resolve
from argaclone.runner import Action, Runner, _is_write, empty_trace
from argaclone.server import App


@pytest.fixture()
def world(tmp_path):
    app = App("scenarios", data_dir=str(tmp_path / "state"))
    view = app.provision({"twins": "stripe,slack", "ttl": 600})
    return app, view["run_id"], view["credentials"]


def _runner(app, rid) -> Runner:
    return app.runner_for(rid)


def _stripe_read(creds):
    return Action("stripe", "GET", "/v1/customers",
                  headers={"authorization": f"Bearer {creds['stripe']}"})


def _stripe_write(creds, email="split@read.write"):
    return Action("stripe", "POST", "/v1/customers", {"email": email},
                  headers={"authorization": f"Bearer {creds['stripe']}"})


def _slack_read(creds):
    # Slack 的读接口是 POST —— 这正是 `method != GET` 会判错的那一格
    return Action("slack", "POST", "/api/conversations.history", {"channel": "C_BILLING_OPS"},
                  headers={"authorization": f"Bearer {creds['slack']}"})


def _slack_write(creds):
    return Action("slack", "POST", "/api/chat.postMessage",
                  {"channel": "C_BILLING_OPS", "text": "读完了，处理完了。"},
                  headers={"authorization": f"Bearer {creds['slack']}"})


def test_reads_and_writes_are_real_lists(world):
    app, rid, creds = world
    runner = _runner(app, rid)
    runner.call(_stripe_read(creds))
    runner.call(_stripe_write(creds))
    runner.call(_slack_read(creds))
    runner.call(_slack_write(creds))
    trace = runner.trace()

    assert len(trace["reads"]) == 2
    assert len(trace["writes"]) == 2
    # 每一条都是带 provider/method/path 的条目，不是一个数字
    assert all({"provider", "method", "path"} <= set(r) for r in trace["reads"])
    assert [r["method"] for r in trace["reads"]] == ["GET", "POST"]
    assert resolve("trace.reads.count", {}, trace) == 2
    assert resolve("trace.writes.count", {}, trace) == 2


def test_the_split_adds_up_to_the_call_list(world):
    app, rid, creds = world
    runner = _runner(app, rid)
    for action in (_stripe_read(creds), _stripe_write(creds),
                   _slack_read(creds), _slack_write(creds)):
        runner.call(action)
    trace = runner.trace()

    # 拆分不许吞掉条目：否则"读 0 写 0"会被读成"什么都没做"
    assert len(trace["reads"]) + len(trace["writes"]) == len(trace["calls"])
    assert resolve("trace.calls.count", {}, trace) == 4


def test_slack_read_is_a_read_not_a_write(world):
    """承重反证：把判定换成 `method != GET`，这一条立刻红。

    Slack 的 `conversations.history` / `users.list` 都是 POST。用 HTTP 方法判读写
    会把"翻聊天记录"记成"发消息"，于是写计数虚高、读计数虚低 —— 一个只读的
    episode 会看起来像在写东西。
    """
    app, rid, creds = world
    runner = _runner(app, rid)
    runner.call(_slack_read(creds))
    trace = runner.trace()

    assert _is_write("slack", "POST", "/api/conversations.history") is False
    assert _is_write("slack", "POST", "/api/chat.postMessage") is True
    assert len(trace["reads"]) == 1
    assert len(trace["writes"]) == 0


def test_empty_trace_carries_the_split():
    trace = empty_trace()
    assert trace["reads"] == []
    assert trace["writes"] == []
    # 空 trace 上取 count 必须得到 0，而不是 PATH_ERROR（键不存在会被读成 None）
    assert resolve("trace.reads.count", {}, trace) == 0
    assert resolve("trace.writes.count", {}, trace) == 0


def test_reads_sufficient_would_be_vacuous_on_the_corpus():
    """`reads >= gold_reads` 是装饰，不许写进题库。

    永真的真正理由不是"模型爱读"，而是**大多数题的 gold 一次读都不做**：
    84 题里 77 题 `gold_reads == 0`，于是 `reads >= gold_reads` 对它们恒真。
    剩下的 5 道 read-heavy 题实测也没有任何一发读到少于 3 次。
    一条永真判据比没有判据更坏 —— 它会让人以为那一格测过了。
    """
    from argaclone.runner import _is_write as is_write

    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "demo").glob("*.json"))
    assert files, "demo 下没有读数文件，这条反证失去意义"

    read_heavy = {
        "it-17-correlate-alerts-before-triage",
        "crm-17-identify-correct-refund-after-reads",
        "dev-17-verify-fix-before-merge-handoff",
        "mkt-17-rollback-to-correct-source-after-reads",
        "ecom-17-refund-duplicate-after-reads",
    }
    checked = 0
    heavy_checked = 0
    for path in files:
        try:
            rows = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(rows, list):
            continue
        for ep in rows:
            if not isinstance(ep, dict) or ep.get("verdict") == "ERROR":
                continue
            detail = ep.get("calls_detail")
            if not detail:
                continue
            reads = sum(
                1 for c in detail
                if not is_write(c.get("provider", ""), (c.get("method") or "").upper(),
                                c.get("path") or "")
            )
            checked += 1
            if ep.get("scenario") in read_heavy:
                heavy_checked += 1
                # 这 5 道题的 gold_reads 最小是 3（it-17/dev-17/mkt-17）
                assert reads >= 3, f"{path.name} {ep.get('scenario')} 只读了 {reads} 次"

    assert checked > 1000, f"只检查到 {checked} 发，样本太少不足以支撑结论"
    assert heavy_checked >= 10, f"read-heavy 只检查到 {heavy_checked} 发"
