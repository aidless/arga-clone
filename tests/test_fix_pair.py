"""修复前后对照读数器的守卫（README §31）。

这一轮的对照有一个独有的失效模式：**被修掉的那两道题必然从 FAIL 变 PASS**，
把它们混进配对里，就会把"判据改了"读成"模型行为变了"。所以下面每一条都
钉住一类会把两件事混起来的读法。

另一个失效模式是本仓已经踩过一次的：通道大面积 403 时，ERROR 若进了分母，
"看起来提升"可能只是这一臂扔掉的更多。所以 ERROR 过滤必须是入口行为，
而且两臂的丢弃数必须都印出来。
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import measure_fix_pair as m  # noqa: E402


def ep(sid, run, verdict, channel_error=False):
    return {"scenario": sid, "run": run, "verdict": verdict,
            "channel_error": channel_error}


# ---- 分母 ---------------------------------------------------------------

def test_errors_and_channel_breaks_are_dropped_before_counting():
    rows = [
        ep("a", 0, "PASS"),
        ep("a", 1, "FAIL"),
        ep("b", 0, "ERROR"),
        ep("b", 1, "FAIL", channel_error=True),
    ]
    ok = m.usable(rows)
    assert len(ok) == 2
    assert [r["verdict"] for r in ok] == ["PASS", "FAIL"]


def test_rate_block_reports_both_the_dropped_count_and_the_raw_total():
    rows = [ep("a", i, "PASS") for i in range(3)] + [ep("b", i, "ERROR") for i in range(7)]
    b = m.rate_block("after", rows)
    assert b["rows_total"] == 10
    assert b["n"] == 3
    assert b["dropped"] == 7
    assert b["rate"] == pytest.approx(1.0)


def test_red_errors_in_the_denominator_would_invent_a_regression():
    """承重反证：把 ERROR 算进分母，10 发里 3 发 PASS 会变成 30%。

    这不是"更保守"，是换了定义——本仓所有表都按 ERROR 不进分母读，
    这里若混进来，同一批数据在两张表里就是两个数。
    """
    rows = [ep("a", i, "PASS") for i in range(3)] + [ep("b", i, "ERROR") for i in range(7)]
    assert m.rate_block("x", rows)["rate"] == pytest.approx(1.0)
    naive = sum(1 for r in rows if r["verdict"] == "PASS") / len(rows)
    assert naive == pytest.approx(0.3)


# ---- 被修掉的两题不进配对 -------------------------------------------------

def test_fixed_pair_is_named_not_guessed():
    """两道被修的题必须写死在读数器里，不能"自动发现差异题"。"""
    assert m.FIXED == ("ecom-02-cancel-without-refund",
                       "dev-14-record-deployment-status")


def test_fixed_scenarios_are_excluded_from_the_paired_test():
    src = inspect.getsource(m.main)
    assert "if r.get(\"scenario\") in FIXED" in src
    assert src.count("in FIXED") >= 3  # episode 配对两处 + 多数票一处


def test_red_counting_the_fixed_pair_would_read_the_fix_as_skill():
    """承重反证：把两题算进配对，修复本身会被读成"模型变强"。

    构造 before 两题全 FAIL、after 两题全 PASS，其余题完全不变：
    排除后配对应当是 0 丢 0 赢，包含则是 0 丢 4 赢（p=0.125）。
    """
    before = [ep(s, r, "FAIL") for s in m.FIXED for r in range(2)]
    after = [ep(s, r, "PASS") for s in m.FIXED for r in range(2)]
    before += [ep("keep", r, "PASS") for r in range(4)]
    after += [ep("keep", r, "PASS") for r in range(4)]

    def pair(rows_b, rows_a, skip):
        bm = {(r["scenario"], r["run"]): r["verdict"] for r in m.usable(rows_b)
              if r["scenario"] not in skip}
        am = {(r["scenario"], r["run"]): r["verdict"] for r in m.usable(rows_a)
              if r["scenario"] not in skip}
        keys = set(bm) & set(am)
        return (sum(1 for k in keys if bm[k] == "PASS" and am[k] != "PASS"),
                sum(1 for k in keys if bm[k] != "PASS" and am[k] == "PASS"))

    assert pair(before, after, m.FIXED) == (0, 0)
    assert pair(before, after, ()) == (0, 4)


# ---- 多数票 ---------------------------------------------------------------

def test_majority_is_strict_not_half():
    """2 发里 1 发 PASS 不算多数票通过——1*2 > 2 为假。"""
    m2 = m.per_scenario([ep("a", 0, "PASS"), ep("a", 1, "FAIL")])
    assert m2["a"] == {"n": 2, "pass": 1}
    assert not (m2["a"]["pass"] * 2 > m2["a"]["n"])


def test_per_scenario_counts_only_usable_rows():
    rows = [ep("a", 0, "PASS"), ep("a", 1, "ERROR"), ep("a", 2, "FAIL", channel_error=True)]
    assert m.per_scenario(rows)["a"] == {"n": 1, "pass": 1}


# ---- 前缀参数化（跨模型复现不与首套读数互相覆盖） -----------------------

def test_arm_paths_follow_the_prefix():
    assert m.arm_paths("xm") == {"before": "demo/xm_before.json",
                                 "after": "demo/xm_after.json"}
    assert m.arm_paths("fixpair") == {"before": "demo/fixpair_before.json",
                                      "after": "demo/fixpair_after.json"}


def test_red_prefix_is_not_hardcoded_to_fixpair():
    """前缀若写死成 fixpair，跨模型读数会覆盖首套证据——这条必须红在那种实现上。"""
    src = inspect.getsource(m.arm_paths)
    assert "fixpair" not in src, "arm_paths 里出现写死的 fixpair 字面量"
    assert f"demo/{{prefix}}_before.json" in src, "路径必须由 prefix 拼出"


def test_main_accepts_prefix_flag():
    src = inspect.getsource(m.main)
    assert "--prefix" in src and "args.prefix" in src, "main 必须吃 --prefix 并用它取数"


# ---- 可靠性闸（§29.9 同款：通道塌方时不能印配对 p 值） --------------------

def test_rate_block_flags_an_unreliable_arm():
    """判据与 §29.9 的 measure_spare_sweep.py 同口径，两臂必须一致。"""
    clean = [ep("a", i, "PASS") for i in range(30)]
    assert m.rate_block("clean", clean)["reliable"] is True

    dead = [ep("a", i, "PASS") for i in range(20)] + [ep("b", i, "ERROR") for i in range(320)]
    assert m.rate_block("dead", dead)["reliable"] is False, "320/340 ERROR 必须判不可信"

    thin = [ep("a", i, "PASS") for i in range(5)]
    assert m.rate_block("thin", thin)["reliable"] is False, "样本不足 30 必须判不可信"


def test_red_a_dead_route_would_still_print_a_paired_p_value(tmp_path, monkeypatch, capsys):
    """承重反证：没有这道闸时，321/340 ERROR 的批次仍会印出配对 p 值。

    本轮真踩到（sidrune 中途 402、qft2xf5j 空 content）。缺闸的实现必须被这条
    判红，否则"两臂都印了 p 值"会被人当成跨模型复现的结论读。
    """
    before = [ep("a", i, "PASS") for i in range(20)]
    after = [ep("a", i, "PASS") for i in range(19)] + [ep("b", i, "ERROR") for i in range(321)]
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "xm_before.json").write_text(json.dumps(before))
    (tmp_path / "demo" / "xm_after.json").write_text(json.dumps(after))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["measure_fix_pair.py", "--prefix", "xm"])
    m.main()
    out = capsys.readouterr().out
    assert "拒绝配对" in out
    assert "mcnemar_exact" not in out, "不可信时绝不能印配对 p 值"

    payload = json.loads((tmp_path / "demo" / "xm_summary.json").read_text())
    assert payload["reliable"] is False
    assert payload["pair"] is None
    assert payload["majority"] is None

