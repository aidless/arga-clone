"""40 题 suite 的自检。

这套断言不是在测"agent 会不会做对"（那是 demo/agent_compare 的事），
而是在测**题目本身是不是能判**。一道题的判据路径写错、或者判据描述了一个
seed 里不存在的世界，agent 再强也永远判不过 —— 这种缺陷必须在跑 agent 之前发现，
否则一次 40 题 × N 模型的评测得出的所有数字都是零信息。

具体断言：
1. 每题都能 provision（seed 结构与 apply_scenario 的约定一致）
2. 空动作下 grade 不出现 PATH_ERROR（每条 path 都能解析到）
3. 空动作下 verdict 必须是 FAIL 而不是 PASS（判据不能空放行）
4. 空动作下不得有任何 safety 失败（safety 判的是"没做坏事"，空动作没做坏事）
5. 同题两次 provision 的空动作判分逐字相同（可复现）
6. 每题至少 2 条 safety（"做完了吗"和"做得安全吗"必须分开问）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argaclone.server import App

SCEN_DIR = Path(__file__).resolve().parent.parent / "scenarios"
FILES = sorted(p for p in SCEN_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _twins_for(sc: dict) -> str:
    seed = sc.get("seed") or {}
    names = [k for k in ("stripe", "slack", "github") if k in seed]
    return ",".join(names) or "slack"


# 题目总数是契约的一部分：`scripts/check_readme.py` 与 README 头部也核这个数，
# 三处必须一起改。扩题（40 → 80）的理由见 `scenarios/suite_extra.py` 的模块说明。
assert len(FILES) == 80, f"expected 80 scenarios, found {len(FILES)}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_scenario_is_gradeable(tmp_path, path):
    sc = _load(path)
    app = App(str(SCEN_DIR))
    view = app.provision({"twins": _twins_for(sc), "ttl": 600, "scenario_id": sc["id"]})
    run_id = view["run_id"]

    report = app.grade(run_id)
    verdict = report["verdict"]
    checks = report["checks"]

    # 2. 判据本身必须可解析
    path_errors = [c for c in checks if any(str(c.get("actual", "")).startswith("PATH_ERROR") for c in [c])]
    assert not path_errors, f"{sc['id']}: unreachable check paths: {[(c['id'], c['actual']) for c in path_errors]}"

    # 3. 什么都不做绝不能 PASS
    assert verdict == "FAIL", f"{sc['id']}: empty action graded {verdict}; a task nobody did must not pass"

    # 4. 什么都不做也不该 SAFETY 失败
    safety_fail = [c["id"] for c in checks if c["kind"] == "safety" and not c["passed"]]
    assert not safety_fail, f"{sc['id']}: empty action tripped safety checks {safety_fail}"

    # 6. 两类判据都要有
    assert sum(1 for c in checks if c["kind"] == "safety") >= 2, f"{sc['id']}: needs >=2 safety checks"
    assert any(c["kind"] == "outcome" for c in checks), f"{sc['id']}: needs outcome checks"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_scenario_is_reproducible(tmp_path, path):
    sc = _load(path)
    app = App(str(SCEN_DIR))
    twins = _twins_for(sc)

    reports = []
    for _ in range(2):
        run_id = app.provision({"twins": twins, "ttl": 600, "scenario_id": sc["id"]})["run_id"]
        reports.append(app.grade(run_id))

    def shape(r):
        return [(c["id"], c["passed"], json.dumps(c["actual"], sort_keys=True, default=str)) for c in r["checks"]]

    assert shape(reports[0]) == shape(reports[1]), f"{sc['id']}: two provisions disagree"


def test_suite_has_five_domains():
    domains = {}
    for p in FILES:
        d = _load(p).get("domain")
        domains[d] = domains.get(d, 0) + 1
    assert domains == {
        "IT support": 16,
        "CRM & outreach": 16,
        "Marketing": 16,
        "Developer": 16,
        "E-commerce": 16,
    }, domains


def test_red_path_error_is_visible(tmp_path):
    """RED：把一条判据的路径改坏，必须出现 PATH_ERROR 而不是静默通过。"""
    sc = _load(FILES[0])
    sc["id"] = "red-broken-path"
    sc["capabilities"]["grade"]["checks"] = [
        {"id": "broken", "kind": "outcome", "desc": "d", "assert": {"path": "slack.channels.count.deep", "op": "eq", "value": 1}}
    ]
    sc["seed"] = {"slack": {"channels": [], "users": [], "messages": []}}
    (tmp_path / "red-broken-path.json").write_text(json.dumps(sc))

    app = App(str(tmp_path))
    run_id = app.provision({"twins": "slack", "ttl": 60, "scenario_id": "red-broken-path"})["run_id"]
    report = app.grade(run_id)
    assert report["verdict"] == "FAIL"
    assert str(report["checks"][0]["actual"]).startswith("PATH_ERROR")
