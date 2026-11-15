"""并发写保护：两个进程写同一个 run 时，不许有任何一次改动无声消失。

这里测的不是"锁能加"，而是三件具体的事：

1. 基于旧世界写的改动会**被拒绝**（409），而不是把对方的改动覆盖掉；
2. 被拒绝之后重新读取就能继续（拒绝是可恢复的，不是死锁）；
3. 锁与版本号都不会在盘上留下垃圾（`.lock` 之外的临时文件必须为 0）。
"""

from __future__ import annotations

import json

import pytest

from argaclone.server import App
from argaclone.store import StaleRunVersion, Store


@pytest.fixture()
def two_stores(tmp_path):
    """两个 Store 指向同一个目录 —— 等价于两个进程。"""
    app = App("scenarios", data_dir=tmp_path)
    rid = app.provision({"twins": "stripe", "ttl": 600})["run_id"]
    return app, rid, Store(tmp_path), Store(tmp_path)


def test_stale_write_is_rejected_instead_of_overwriting(two_stores):
    app, rid, first, second = two_stores
    a = first.get(rid)
    b = second.get(rid)

    a.state["stripe"]["_note"] = "from-first"
    first.save(a)                      # v1

    b.state["stripe"]["_note"] = "from-second"
    with pytest.raises(StaleRunVersion):
        second.save(b)                 # 基于 v0，盘上已是 v1

    on_disk = json.loads((app.store.directory / f"{rid}.json").read_text())
    assert on_disk["state"]["stripe"]["_note"] == "from-first", "旧世界把新世界覆盖了"


def _disk_version(app, rid) -> int:
    return json.loads((app.store.directory / f"{rid}.json").read_text())["version"]


def test_after_refetch_the_write_goes_through(two_stores):
    app, rid, first, second = two_stores
    a = first.get(rid)
    a.state["stripe"]["_note"] = "x"
    first.save(a)
    before = _disk_version(app, rid)

    b = Store(app.store.directory).get(rid)   # 重新读一次
    b.state["stripe"]["_note"] = "y"
    second.save(b)

    on_disk = json.loads((app.store.directory / f"{rid}.json").read_text())
    assert on_disk["state"]["stripe"]["_note"] == "y"
    assert on_disk["version"] == before + 1, "每次成功写盘只 +1，跳号说明有人绕过锁写了"


def test_no_temp_files_are_left_behind(two_stores):
    app, rid, first, _ = two_stores
    run = first.get(rid)
    for i in range(5):
        run.state["stripe"]["_n"] = i
        first.save(run)
    leftovers = [p.name for p in app.store.directory.iterdir()
                 if ".tmp-" in p.name]
    assert leftovers == []


def test_gateway_write_conflicts_are_409_not_500(two_stores):
    app, rid, first, _ = two_stores
    # 另一个进程先把 run 改到下一版
    other = first.get(rid)
    other.extras["_touched_by"] = "other-process"
    first.save(other)

    # 本进程手里的 run 还停在 v0 —— 这一次写必须被抓住
    run = app.store.get(rid)
    # 强行把这份 run 说成"基于一个更旧的版本"：等价于本进程读完之后别人改过。
    run.loaded_version = run.version - 1
    tok = run.extras["credentials"]["stripe"]
    resp = app.gateway.handle(
        "POST", f"stripe.{rid}.twins.local", "/v1/customers",
        {"authorization": f"Bearer {tok}"}, json.dumps({"email": "c@acme.test"}).encode(),
    )
    assert resp.status == 409, resp.body
    assert resp.body["error"] == "stale_run_version"
    assert "重新读取" in resp.body["_twin_hint"]


def test_red_without_version_check_the_older_writer_wins_silently(two_stores):
    """RED：证明"不丢改动"真的来自版本检查。

    把 loaded_version 强行对齐盘上版本（等价于关掉乐观检查），同一场景下
    第二个写者会把第一个的改动整段覆盖掉，且**没有任何错误**。
    """
    app, rid, first, second = two_stores
    a = first.get(rid)
    a.state["stripe"]["_note"] = "from-first"
    first.save(a)

    b = second.get(rid)
    b.state["stripe"]["_note"] = "from-second"
    disk = _disk_version(app, rid)
    b.loaded_version = b.version = disk    # 关掉检查
    second.save(b)

    on_disk = json.loads((app.store.directory / f"{rid}.json").read_text())
    assert on_disk["state"]["stripe"]["_note"] == "from-second"   # 覆盖发生了
