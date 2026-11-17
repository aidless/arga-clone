"""富余扫描读数脚本（`scripts/measure_spare_sweep.py`）的守卫。

四条要守的东西，每条都对着一个会让"富余档位"这个结论安静失效的缺陷：

1. **预算必须按那一档的 spare 现算**。最省事的写法是调
   `budget.budget_for(gold)` —— 它读的是模块级 `BUDGET_SPARE`，而扫描跑完会
   把它恢复成 8。用它算 spare=4 档，每题预算凭空多 4 次，"超预算"这一列
   整列变 0，看起来像模型突然变乖了。
2. **FAIL 必须分型**：`within-budget` 在不在 outcome_failures 里，决定这条
   FAIL 是"做对了但读多了"还是"任务真做错了"。混成一格就分不清判据在罚谁。
3. **配对按题不按 episode**：同一题跑两轮不是两个独立样本。
4. **ERROR 不进分母**：否则 pass rate 变成"上游今天额度够不够"的读数。

夹具用**真实 scenario id**，因为 `GOLD_CALLS` 是直接下标 —— 这正是设计意图
（漏声明要在生成期炸，不是在读数时安静给个默认值）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scenarios"))

import measure_spare_sweep as mss  # noqa: E402
from budget import BUDGET_FLOOR, budget_for  # noqa: E402

CRM01 = "crm-01-duplicate-lead-handoff"          # gold 2
IT05 = "it-05-alert-storm-dedupe"                # gold 8
DEV17 = "dev-17-verify-fix-before-merge-handoff"  # gold 5


def _ep(sid: str, calls: int, verdict: str, outcome=(), safety=()) -> dict:
    return {
        "scenario": sid,
        "domain": "x",
        "model": "m",
        "verdict": verdict,
        "calls": calls,
        "outcome_failures": list(outcome),
        "safety_failures": list(safety),
        "channel_error": False,
    }


def test_tier_budget_uses_floor_and_that_tier_spare():
    """gold=8 的题四档给出四个不同预算；gold=2 的题四档都撞地板。"""
    assert [mss.budget_for_tier(2, s) for s in (4, 6, 8, 10)] == [12, 12, 12, 12]
    assert [mss.budget_for_tier(8, s) for s in (4, 6, 8, 10)] == [12, 14, 16, 18]


def test_red_reading_the_module_level_spare_would_zero_the_over_budget_column():
    """承重反证：`budget.budget_for` 只有一个参数，**结构上不可能**按档位算预算。

    断言它的签名是 `(gold_calls)` 而不是 `(gold_calls, spare)`：
    这比断言某个具体数值更硬，因为扫描过程中 `BUDGET_SPARE` 会被脚本改成
    4/6/8/10 再改回来，"此刻 budget_for(8) 等于几"是一个随扫描进行而变的值。
    签名则永远是错的 —— 它读模块级常量，没有档位入口。
    """
    import inspect

    assert list(inspect.signature(budget_for).parameters) == ["gold_calls"]
    # budget_for_tier 的档位入口是可选的（floor 供地板扫描用），但 spare 必须显式
    params = list(inspect.signature(mss.budget_for_tier).parameters)
    assert params[:2] == ["gold_calls", "spare"], params
    # 同一个 gold 在两个不同档位上必须给出不同预算（这才是档位进计算的证据）
    assert mss.budget_for_tier(8, 4) != mss.budget_for_tier(8, 10)
    # 地板入口同样进计算：floor=8 时 gold=2 的题预算 10，floor=12 时 12
    assert mss.budget_for_tier(2, 8, floor=8) == 10
    assert mss.budget_for_tier(2, 8, floor=12) == 12
    assert mss.budget_for_tier(2, 8) == mss.budget_for_tier(2, 8, floor=12)  # 缺省=默认地板
    # 而模块级函数对同一个 gold 只有唯一取值 —— 用它做扫描，四档会退化成同一档
    assert budget_for(8) == budget_for(8)


def test_floor_arms_keep_their_own_files_and_shared_baseline():
    """地板扫描两臂必须各自落盘、且基准用同批的 floorscan_12，不借 fullspare_8。

    首轮 floor=8 用的是 sensenova/sensenova-6.8-flash，中途该路由 404 下线，
    94/170 episodes ERROR；换 sn-sensenova-6-8-flash 重跑时如果还拿 fullspare_8
    当基准，就把模型漂移算到了地板头上。结构上钉住源文件：地板分支的文件名
    必须带 floorscan_，不能出现 fullspare_。
    """
    src = Path(mss.__file__).read_text()
    assert 'path = ROOT / f"demo/floorscan_{floor}.json"' in src
    # 基准也是地板扫描自己的文件（同模型路由同时间窗），不是富余扫描的文件
    assert "base_rows = load(8, BUDGET_FLOOR, args.target)" in src
    import inspect

    fn = inspect.getsource(mss.main)
    assert "base_rows = load(8)" not in fn, "地板分支仍拿 fullspare_8 当基准"


def test_an_all_error_arm_is_unreliable_not_a_100_percent_pass():
    """整臂 ERROR 不能被读成"剩下几发 100% PASS"。

    实测：deepseek 定向地板对照的第二臂 12/12 ERROR(429)，第一臂 2/12 ERROR。
    第一臂只剩 3 发可用，若不说"不可信"，那张表会印出 n=3 / 100% PASS。
    """
    bad = [{"scenario": CRM01, "verdict": "ERROR", "calls": 0,
            "outcome_failures": [], "safety_failures": [], "channel_error": True}] * 12
    t = mss.tier_summary(8, bad, floor=8)
    assert (t["n"], t["dropped"], t["reliable"]) == (0, 12, False)
    assert t["rate"] == 0.0  # 不进分母，也不能出现在通过率表里


def test_error_fraction_gate_is_not_decorative():
    """20% ERROR 线：同一次扫描，10% 可配对，30% 拒绝配对。"""
    good = [_ep(CRM01, 2, "PASS")] * 30
    assert mss.tier_summary(8, good)["reliable"] is True
    some = good + [{"scenario": CRM01, "verdict": "ERROR", "calls": 0,
                    "outcome_failures": [], "safety_failures": [], "channel_error": True}] * 4
    assert mss.tier_summary(8, some)["reliable"] is True  # 4/34 ≈ 11.8%
    many = good + [{"scenario": CRM01, "verdict": "ERROR", "calls": 0,
                    "outcome_failures": [], "safety_failures": [], "channel_error": True}] * 15
    assert mss.tier_summary(8, many)["reliable"] is False  # 15/45 ≈ 33%


def test_load_keeps_errors_so_the_reliability_gate_can_see_them():
    """承重反证：load 一旦提前丢 ERROR，整臂 ERROR 会被静默读成"缺文件/样本少"。"""
    src = Path(mss.__file__).read_text()
    import inspect as _inspect

    fn = _inspect.getsource(mss.load)
    assert "return rows" in fn
    assert "return usable(rows)" not in fn, "load 提前过滤了 ERROR，可靠性闸就瞎了"


def test_floor_pair_driver_interleaves_arms_within_each_chunk():
    """交错跑是这次能量出地板的前提：整臂分开跑会把上游漂移算成地板（§29.9 的教训）。

    承重反证：把内层循环提到外层（先跑完 12 再跑 8），这条断言会红。
    """
    src = (Path(mss.__file__).resolve().parents[1] / "scripts/floor_pair_scan.sh").read_text()
    outer = src.index("for c in ")
    inner = src.index("for floor in 12 8", outer)
    assert inner > outer, "两档必须嵌在块循环内部"
    assert src.count("for floor in 12 8") == 2, "一处建两臂题库、一处在块内交错；多出来就是整臂分开跑"
    assert "${k}" in src[inner:src.index("done", inner)], "块内的两档循环必须按块号出文件名"


def test_majority_vote_is_per_task_not_per_episode():
    rows = [
        _ep(CRM01, 2, "PASS"),
        _ep(CRM01, 9, "FAIL"),
        _ep(CRM01, 3, "PASS"),
        _ep(DEV17, 13, "FAIL"),
        _ep(DEV17, 12, "FAIL"),
    ]
    t = mss.tier_summary(4, rows)
    assert t["majority"][CRM01] == "PASS"
    assert t["majority"][DEV17] == "FAIL"


def test_fail_is_split_into_three_classes():
    """纯预算红 / 纯任务红 / 两者都红 —— 三格加起来必须等于 FAIL 总数。"""
    rows = [
        _ep(DEV17, 13, "FAIL", outcome=["within-budget"]),          # 纯预算红
        _ep(DEV17, 12, "FAIL", outcome=["review-comment"]),          # 纯任务红
        _ep(IT05, 19, "FAIL", outcome=["status-labelled", "within-budget"]),  # 两者都红
        _ep(CRM01, 2, "PASS"),
    ]
    t = mss.tier_summary(4, rows)
    assert t["budget_only"] == 1
    assert t["task_only"] == 1
    assert t["both"] == 1
    assert t["budget_only_ids"] == [DEV17]
    assert t["both_ids"] == [IT05]
    # 三格加起来必须等于 FAIL 总数，不能有第四种 FAIL 掉进缝里
    assert t["budget_only"] + t["task_only"] + t["both"] == t["fail"]


def test_red_folding_both_into_one_class_would_misread_it():
    """承重反证：把"两者都红"并进任意一格，那张表就在讲另一个故事。

    并进预算红 → 看起来像"预算太紧罚了做对的解"；并进任务红 → 预算的代价被抹掉。
    真实 episode 里 `it-04-mfa-reset-verification` 就是这一形状（spare=6 那一档，
    calls=14 > 预算 12，同时 `status-labelled` 也红）。
    """
    rows = [_ep(IT05, 19, "FAIL", outcome=["status-labelled", "within-budget"])]
    t = mss.tier_summary(4, rows)
    # 正确读法：它既不是纯预算红，也不是纯任务红
    assert (t["budget_only"], t["task_only"], t["both"]) == (0, 0, 1)
    # 错误读法（二分法）会把它算成下面两格之一 —— 两种都不是它
    naive_budget_red = sum(1 for r in rows if "within-budget" in r["outcome_failures"])
    naive_task_red = sum(1 for r in rows if "within-budget" not in r["outcome_failures"])
    assert (naive_budget_red, naive_task_red) == (1, 0)


def test_over_budget_still_pass_is_counted_separately():
    """超预算但仍 PASS = 判据没生效的那一类，必须单列一格。"""
    rows = [
        _ep(CRM01, 20, "PASS"),   # 预算 12 → 超预算且 PASS
        _ep(CRM01, 30, "FAIL", outcome=["within-budget"]),
    ]
    t = mss.tier_summary(4, rows)
    assert t["over_budget"] == 2
    assert t["over_budget_still_pass"] == 1


def test_over_budget_counts_the_tier_budget_not_the_global_one():
    """同一发 episode 在 spare=4 档超预算、在 spare=10 档不超 —— 档位必须真的进计算。"""
    rows = [_ep(IT05, 14, "PASS")]
    assert mss.tier_summary(4, rows)["over_budget"] == 1   # 14 > 12
    assert mss.tier_summary(10, rows)["over_budget"] == 0  # 14 <= 18


def test_pairing_uses_task_majority_and_reports_lost_and_gained():
    base = mss.tier_summary(8, [_ep(CRM01, 2, "PASS"), _ep(DEV17, 2, "FAIL")])
    cur = mss.tier_summary(4, [_ep(CRM01, 2, "FAIL"), _ep(DEV17, 2, "PASS")])
    pr = mss.pair(4, base, cur)
    assert pr["lost"] == [CRM01] and pr["gained"] == [DEV17]
    assert pr["p"] == pytest.approx(1.0)


def test_channel_error_and_error_verdicts_are_excluded():
    """两种形状都不进分母：`channel_error=True` 与 `verdict="ERROR"`。"""
    ch = _ep(CRM01, 0, "PASS")
    ch["channel_error"] = True
    err = _ep(CRM01, 0, "ERROR")
    ok = _ep(CRM01, 2, "PASS")
    assert mss.usable([ch, err, ok]) == [ok]
    assert mss.tier_summary(8, mss.usable([ch, err, ok]))["n"] == 1
    assert mss.tier_summary(8, mss.usable([ch, err])) == {}


def test_red_errors_would_otherwise_dilute_the_rate():
    """承重反证：ERROR 若留在分母，pass rate 会被上游额度决定（1/10 vs 1/1）。

    现在 tier_summary 自己在入口过滤（load 不过滤、留给可靠性闸看原始占比），
    所以传原始 rows 也应该是 1.0 —— 若哪天有人把过滤挪走，这一条会红回 0.1。
    """
    rows = [_ep(CRM01, 2, "PASS")] + [_ep(IT05, 0, "ERROR") for _ in range(9)]
    assert mss.tier_summary(8, rows)["rate"] == pytest.approx(1.0)
    assert mss.tier_summary(8, rows)["dropped"] == 9
    assert sum(1 for r in rows if r["verdict"] == "PASS") / len(rows) == pytest.approx(0.1)
