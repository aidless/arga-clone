"""对照报表的判据：差值是配对的，不是两个总率相减。

这里守三件事：
- 只有"同题同轮"的变化才计入 McNemar 的两个计数（lost / gained）；
- 只在一边出现的模型不参与对照（否则会拿 A 的题比 B 的题）；
- 注入次数必须一起报 —— 注入为 0 的那一行，其实是温和读数。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from resilience_report import build_rows, by_model  # noqa: E402


def _row(model, sid, verdict, injected=0, recovered=0):
    return {
        "model": model, "scenario": sid, "verdict": verdict,
        "injected_failures": injected, "recovered": recovered, "unrecovered": 0,
    }


def test_paired_delta_and_mcnemar_counts():
    calm = [_row("m", f"t{i}", "PASS") for i in range(10)]
    flaky = (
        [_row("m", f"t{i}", "PASS") for i in range(7)]
        + [_row("m", f"t{i}", "FAIL") for i in range(7, 10)]
    )
    # 注入只记在 flaky 那侧，且必须能对上 recovered
    flaky = [dict(r, injected_failures=1, recovered=1) for r in flaky]

    rows, missing = build_rows(calm, flaky)
    assert missing == []
    r = rows[0]
    assert (r["calm_pass"], r["flaky_pass"], r["n"]) == (10, 7, 10)
    assert r["delta"] == -0.3
    assert (r["lost"], r["gained"]) == (3, 0)
    assert r["injected"] == 10 and r["recovered"] == 10


def test_model_only_on_one_side_is_reported_not_compared():
    calm = [_row("m", "t1", "PASS"), _row("other", "t1", "PASS")]
    flaky = [_row("m", "t1", "PASS")]
    rows, missing = build_rows(calm, flaky)
    assert [r["model"] for r in rows] == ["m"]
    assert missing == ["other"]


def test_majority_vote_across_runs_and_tie_is_conservative():
    rows = [
        _row("m", "t1", "PASS"), _row("m", "t1", "PASS"), _row("m", "t1", "FAIL"),
        _row("m", "t2", "PASS"), _row("m", "t2", "FAIL"),
    ]
    decided = by_model(rows)["m"]
    assert decided["t1"] == "PASS"
    assert decided["t2"] == "FAIL"  # 平票不判过


def test_red_comparing_raw_rates_would_hide_the_pairing():
    """反证：两边总率都是 50%，配对后却全是"只掉不涨"。

    这正是为什么要配对 —— 总率相减会得到 Δ=0pt，看起来"环境没影响"。
    """
    calm = [_row("m", "a", "PASS"), _row("m", "b", "FAIL")]
    flaky = [_row("m", "a", "FAIL"), _row("m", "b", "PASS")]
    rows, _ = build_rows(calm, flaky)
    r = rows[0]
    assert r["delta"] == 0.0          # 总率相同
    assert (r["lost"], r["gained"]) == (1, 1)  # 但配对后能看见两个方向都有变化
