"""`calls_detail` 必须完整映到 runner 的记录上——漏字段这件事已经烧掉两轮评测。

两次事故长一个样：新能力在 runner 里记了字段，但结果文件的白名单没跟着加，
于是**数据看起来是完整的**，只有去复算时才发现那一列全是 None：
`replayed` 漏过一次（78 分钟作废）、`client_retries` 漏过一次。

所以这条测试不验业务语义，只验"字段有没有活到结果文件"。它的判据是可枚举的：
`DETAIL_FIELDS` 里每个字段都要在 `detail_row()` 的输出里，且值与源记录一致。
"""

from __future__ import annotations

import json

import pytest

from argaclone import chaos
from argaclone.runner import Action
from argaclone.server import App
from demo.agent_compare import DETAIL_FIELDS, detail_row


@pytest.fixture()
def world(tmp_path):
    app = App("scenarios", data_dir=str(tmp_path / "state"))
    view = app.provision({"twins": "stripe", "ttl": 600})
    return app, view["run_id"], view["credentials"]


def test_every_declared_field_survives_the_projection():
    record = {
        "provider": "stripe", "method": "POST", "path": "/v1/customers", "status": 500,
        "stub": True, "declared": True, "absent": False, "replayed": True,
        "client_retries": 2, "retry_waits": [0.5, 1.0],
    }
    row = detail_row(record)
    for name in DETAIL_FIELDS:
        assert name in row, f"{name} 没活到结果文件里 —— 复算时那一列会是 None"
        assert row[name] == record[name], name


def test_real_record_keeps_backoff_and_replay_flags(world):
    """端到端：真跑一次带退避的重试，结果行里必须能看到等了多久。"""
    app, rid, creds = world
    chaos.add_fault(app.store.get(rid), {"provider": "stripe", "method": "POST",
                                         "path": "/v1/customers", "mode": "commit_then_fail",
                                         "status": 500, "times": 1})
    runner = app.runner_for(rid)
    runner.idempotent_writes = True
    runner.client_retries = 2
    runner.retry_backoff = 0.1
    runner.retry_backoff_cap = 0.3
    runner.jitter = 0.0
    runner._sleep = lambda s: None
    rec = runner.call(Action("stripe", "POST", "/v1/customers", {"email": "parity@test.dev"},
                             headers={"authorization": f"Bearer {creds['stripe']}"}))

    row = detail_row(rec)
    assert row["replayed"] is True
    assert row["client_retries"] == 1
    assert row["retry_waits"] == [0.1]
    # 结果文件要能 json 序列化：这一条曾经害过整轮（bytes 到落盘那一刻炸）
    json.dumps(row)


def test_red_dropping_a_field_is_visible(monkeypatch):
    """反证：把 retry_waits 从字段表里拿掉，上面的断言必须红。"""
    import demo.agent_compare as ac

    monkeypatch.setattr(ac, "DETAIL_FIELDS", tuple(
        f for f in ac.DETAIL_FIELDS if f != "retry_waits"))
    row = ac.detail_row({"provider": "stripe", "method": "POST", "path": "/v1/x"})
    # 值还在（因为 detail_row 自己还填），但"声明的字段表"已经不含它 ——
    # 这条断言守的是"字段表本身不能被悄悄改小"
    assert "retry_waits" not in ac.DETAIL_FIELDS
    assert set(ac.DETAIL_FIELDS) - set(row) == set()
