"""落盘：进程重启之后，那个世界还在不在。

为什么这件事值得单独一组测试：
"状态可信、可复现"是这套东西的全部卖点，而纯内存的实现里，一次 `systemctl restart`
或一个 OOM kill 就会让所有 run 消失——所有正在跑的评测一起死，而且没有任何痕迹。
更坏的是它**看起来没事**：进程起来了，`/healthz` 是 200，只是列表空了。

所以这里的断言都围绕"重启后同一件事还成立"，而不是"文件被写出来了"：
文件存在但读不回来，等于没落盘。
"""

from __future__ import annotations

import json

import pytest

from argaclone.gateway import Gateway
from argaclone.server import App, build_registry
from argaclone.store import Store

STRIPE_KEY = "sk_test_arga"


def _stripe(app: App, run_id: str, method: str, path: str, body: dict | None = None):
    gw = Gateway(app.store, app.registry)
    headers = {
        "authorization": f"Bearer {STRIPE_KEY}",
        "host": f"stripe.{run_id}.twins.local",
        "content-type": "application/json",
    }
    return gw.handle(method, f"stripe.{run_id}.twins.local", path, headers,
                     json.dumps(body).encode() if body else b"")


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def _app(scenario_dir, data_dir, **kw):
    return App(scenario_dir, data_dir=data_dir, **kw)


def test_state_survives_restart(scenario_dir, data_dir):
    a = _app(scenario_dir, data_dir)
    run_id = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    resp = _stripe(a, run_id, "POST", "/v1/customers", {"email": "keep@example.com"})
    assert resp.status == 200, resp.body

    b = _app(scenario_dir, data_dir)          # 新进程 = 新 Store
    got = b.state(run_id)
    cid = resp.body["id"]
    assert cid in got["state"]["stripe"]["customers"]
    assert got["state"]["stripe"]["customers"][cid]["email"] == "keep@example.com"


def test_baseline_survives_so_reset_still_means_something(scenario_dir, data_dir):
    """只有 state 存活而 baseline 丢了，reset 会把世界清空——比不落盘更糟。"""
    a = _app(scenario_dir, data_dir)
    run_id = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    _stripe(a, run_id, "POST", "/v1/customers", {"email": "temp@example.com"})

    b = _app(scenario_dir, data_dir)
    before = b.state(run_id)["state"]["stripe"]["customers"]
    assert len(before) == 1
    b.reset(run_id)
    after = b.state(run_id)["state"]["stripe"]["customers"]
    assert after == {}, "reset 必须回到 provision 时的空世界，而不是清空一切之后的样子"


def test_scenario_seed_survives_restart(scenario_dir, data_dir):
    a = _app(scenario_dir, data_dir)
    run_id = a.provision(
        {"twins": "stripe,slack", "ttl": 600, "scenario_id": "ecom-duplicate-billing"}
    )["run_id"]
    b = _app(scenario_dir, data_dir)
    state = b.state(run_id)["state"]
    assert "cus_DUP_KEEP" in state["stripe"]["customers"]
    assert "C_BILLING_OPS" in state["slack"]["channels"]


def test_stub_counters_survive_restart(scenario_dir, data_dir):
    a = _app(scenario_dir, data_dir)
    run_id = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    _stripe(a, run_id, "GET", "/v1/tax_rates")

    b = _app(scenario_dir, data_dir)
    report = b.stub_hits(run_id)
    assert report["total_hits"] == 1
    assert report["summary"][0]["endpoint"].endswith("/v1/tax_rates")


def test_expiry_is_still_410_after_restart(scenario_dir, data_dir):
    """过期要落成 410 而不是 404——重启不该改变这条语义。"""
    a = _app(scenario_dir, data_dir)
    run_id = a.provision({"twins": "stripe", "ttl": 1})["run_id"]
    path = data_dir / f"{run_id}.json"
    payload = json.loads(path.read_text())
    payload["created_at"] -= 10               # 不 sleep，直接把时间推回去
    path.write_text(json.dumps(payload))

    b = _app(scenario_dir, data_dir)
    from argaclone.store import RunExpired

    with pytest.raises(RunExpired):
        b.store.get(run_id)


def test_teardown_removes_the_file(scenario_dir, data_dir):
    a = _app(scenario_dir, data_dir)
    run_id = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    assert (data_dir / f"{run_id}.json").exists()
    a.teardown(run_id)
    assert not (data_dir / f"{run_id}.json").exists()
    b = _app(scenario_dir, data_dir)
    assert b.list_runs()["runs"] == [], "删掉的 run 不许在重启后复活"


def test_locked_flag_survives(scenario_dir, data_dir):
    a = _app(scenario_dir, data_dir)
    run_id = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    a.lock(run_id)
    b = _app(scenario_dir, data_dir)
    assert b.get_run(run_id)["locked"] is True


def test_corrupt_file_is_reported_not_silently_skipped(scenario_dir, data_dir):
    """坏文件必须能被看见。静默跳过会让人以为"那个 run 从来不存在"。"""
    a = _app(scenario_dir, data_dir)
    good = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    (data_dir / "broken.json").write_text('{"run_id": "broken", "state": ')

    b = _app(scenario_dir, data_dir)
    assert [r["run_id"] for r in b.list_runs()["runs"]] == [good]
    assert b.store.load_errors and "broken.json" in b.store.load_errors[0]


def test_no_data_dir_writes_nothing(scenario_dir, tmp_path):
    """默认纯内存：批量评测时不该在磁盘上留几百份世界状态。"""
    a = App(scenario_dir)
    a.provision({"twins": "stripe", "ttl": 600})
    assert list(tmp_path.glob("*.json")) == []
    assert a.store.directory is None


def test_save_is_atomic_leaves_no_temp_files(scenario_dir, data_dir):
    a = _app(scenario_dir, data_dir)
    run_id = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    for i in range(5):
        _stripe(a, run_id, "POST", "/v1/customers", {"email": f"t{i}@example.com"})
    leftovers = [p.name for p in data_dir.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_red_without_save_the_restart_loses_everything(scenario_dir, data_dir, monkeypatch):
    """RED：证明"重启还在"这件事是 _save 干的，不是别的路径顺手写出来的。"""
    a = _app(scenario_dir, data_dir)
    monkeypatch.setattr(Store, "_save", lambda self, run: None)
    run_id = a.provision({"twins": "stripe", "ttl": 600})["run_id"]
    _stripe(a, run_id, "POST", "/v1/customers", {"email": "gone@example.com"})

    b = _app(scenario_dir, data_dir)
    assert b.list_runs()["runs"] == [], "关掉落盘后必须丢掉状态，否则这条 RED 不成立"
