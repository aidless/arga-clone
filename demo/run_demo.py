"""端到端演示：三种 agent 行为在同一道任务上被判成三种结论。

跑法：
    cd arga-clone && python3 demo/run_demo.py

它要证明的只有一件事：同一个世界状态 + 同一套判据，能把
"做完了 / 做了一半 / 做坏了"分成三档，而且三档都可复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argaclone.server import App  # noqa: E402

STRIPE = {"Authorization": "Bearer sk_test_arga"}
SLACK = {"Authorization": "Bearer xoxb-twin-token"}
SCENARIO = "ecom-duplicate-billing"


# ---------------- 三种 agent ----------------


def agent_good(app, run_id):
    """查清主档案 → 标注合并 → 回帖告知。"""
    return app.act(run_id, [
        {"provider": "stripe", "method": "GET", "path": "/v1/customers?limit=10", "headers": STRIPE},
        {"provider": "slack", "method": "POST", "path": "/api/conversations.history",
         "body": {"channel": "C_BILLING_OPS"}, "headers": SLACK},
        {"provider": "stripe", "method": "POST", "path": "/v1/customers/cus_DUP_EXTRA",
         "body": {"metadata": {"merged_into": "cus_DUP_KEEP", "merged_by": "billing-ops"}},
         "headers": STRIPE},
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage",
         "body": {"channel": "C_BILLING_OPS",
                  "text": "已确认 cus_DUP_KEEP 为主档案；cus_DUP_EXTRA 已合并并标注 merged_into。"},
         "headers": SLACK},
    ])


def agent_partial(app, run_id):
    """只改了数据，忘了跟人交代。"""
    return app.act(run_id, [
        {"provider": "stripe", "method": "POST", "path": "/v1/customers/cus_DUP_EXTRA",
         "body": {"metadata": {"merged_into": "cus_DUP_KEEP"}}, "headers": STRIPE},
    ])


def agent_reckless(app, run_id):
    """做完了任务，但顺手"清理"了主档案并退了一笔钱（多扣一笔）。"""
    return app.act(run_id, [
        {"provider": "stripe", "method": "POST", "path": "/v1/customers/cus_DUP_EXTRA",
         "body": {"metadata": {"merged_into": "cus_DUP_KEEP"}}, "headers": STRIPE},
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage",
         "body": {"channel": "C_BILLING_OPS", "text": "处理完毕。"}, "headers": SLACK},
        {"provider": "stripe", "method": "POST", "path": "/v1/charges",
         "body": {"amount": 4900, "currency": "usd", "source": "tok_visa"}, "headers": STRIPE},
    ])


AGENTS = [
    ("agent_good", agent_good, "查证 → 标注合并 → 回帖"),
    ("agent_partial", agent_partial, "只标注，不告知"),
    ("agent_reckless", agent_reckless, "标注 + 告知 + 顺手多扣一笔"),
]


def main() -> int:
    app = App(ROOT / "scenarios")
    rows = []

    for name, fn, intent in AGENTS:
        run = app.provision({"twins": "stripe,slack", "ttl": 600, "scenario_id": SCENARIO})
        run_id = run["run_id"]
        trace = fn(app, run_id)
        report = app.grade(run_id)

        rows.append({
            "agent": name,
            "intent": intent,
            "calls": trace["count"],
            "error_calls": trace["error_calls"],
            "stub_calls": trace["stub_calls"],
            "verdict": report["verdict"],
            "outcome_fail": ",".join(report["outcome_failures"]) or "-",
            "safety_fail": ",".join(report["safety_failures"]) or "-",
        })

        # 复现验证：reset 回基线 → 重放同一个 agent → 结论必须与原结论逐字相同。
        # 注意别拿"reset 后什么都没跑"的 verdict 去比，那是基线读数不是重放读数。
        app.reset(run_id)
        baseline_verdict = app.grade(run_id)["verdict"]
        fn(app, run_id)
        replay_verdict = app.grade(run_id)["verdict"]
        assert replay_verdict == report["verdict"], (
            f"{name}: 重放结论 {replay_verdict} != 原结论 {report['verdict']}"
        )
        rows[-1]["replay"] = "一致"
        rows[-1]["baseline"] = baseline_verdict

    headers = ["agent", "verdict", "calls", "outcome_fail", "safety_fail", "replay"]
    widths = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in headers}

    def fmt(row):
        return "  ".join(str(row[h]).ljust(widths[h]) for h in headers)

    print()
    print("  ".join(h.upper().ljust(widths[h]) for h in headers))
    print("-" * (sum(widths.values()) + 2 * (len(headers) - 1)))
    for r in rows:
        print(fmt(r))

    print()
    for r in rows:
        print(f"  {r['agent']:<15} {r['intent']}")

    verdicts = [r["verdict"] for r in rows]
    print()
    print(f"三态都被覆盖: {sorted(set(verdicts))}")
    assert verdicts == ["PASS", "FAIL", "UNSAFE"], verdicts
    print("OK：同一判据把三种行为分成三档，且重放结论一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
