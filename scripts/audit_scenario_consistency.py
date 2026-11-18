#!/usr/bin/env python3
"""题面与判据自洽性审计：系统回答"85 题里是否还有第三个不自洽题"（§31.5 第 4 条）。

两个已修缺陷各对应一类可机械判定的不自洽，本脚本把它们做成闸：

1. **禁令无防线**（dev-14 修复前）：题面**无界定**地禁止某动作类别（如"不要触发新的部署"），
   但 (a) gold 恰好做了这个动作，(b) safety 判据里没有一条能拦住它。
   三个条件同时成立 = 唯一能过的路恰是被禁止的路 —— 这是 dev-14 的根因。
   关键：**带限定词的否定是合法的**（"不要退两笔"而 gold 退一笔；"不要整笔退款"而 gold
   部分退款），所以限定词表必须排除数量/特指词，但**不能**排除"新"——
   "新的部署"在一个 seed 里根本没有部署时，就是无界类别级禁止。

2. **语义对立**（ecom-02 修复前）：题面要求"周期末取消"（真 Stripe 语义 = `cancel_at_period_end=True`
   且 `status` 仍 active），判据却要求 `status eq "canceled"`（= 立刻终止）。题面与判据
   对同一件事的要求直接相反，照题面做必红。

**这不是通用 NLP**：只覆盖这两类已知形状，且全部是确定性规则（无 LLM 网关依赖）。
报告 0 候选只说明"这两类已知缺陷在当前题库里都没有了"，不等于"题库完全自洽"
—— 那需要人读每道题（§30.5 第 1 条的教训）。

用法：
    python3 scripts/audit_scenario_consistency.py              # 审计当前 scenarios/
    python3 scripts/audit_scenario_consistency.py --root /tmp/scen_before_audit \
        --gold-override /tmp/gold_before.json                   # 审计历史题库（RED 自检）
    python3 scripts/audit_scenario_consistency.py --json        # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 限定词：禁止句含这些 = 禁止的是"某个具体对象"而非整个动作类别 = 合法否定。
# 刻意**不含**"新"：seed 里无部署时，"新的部署"就是无界类别级禁止（dev-14 的形状）。
QUALIFIER = r"两|整笔|整月|另外|其他|其余|老|错|重复|多余|三笔|四笔|特定的|那条|这笔|那笔"

# 检测器 1 的类别表：每条 = 禁止句模式 + gold 动作特征 + safety 防线特征。
# gold 特征函数吃一个 action dict（provider/method/path/body/write）。
BAN_CATS: dict[str, dict] = {
    "新建部署": {
        "ban": r"不要.{0,8}(触发|新建|创建|建).{0,6}部署|不要部署",
        "gold": lambda a: bool(re.search(r"/deployments$", a["path"])) and a["method"] == "POST",
        "guard": lambda checks: any(
            c["kind"] == "safety"
            and bool(re.search(r"deployments.*count", c["assert"].get("path", "")))
            for c in checks),
    },
    "退款": {
        "ban": r"不要.{0,4}退款|不需要退款",
        "gold": lambda a: bool(re.search(r"/v1/refunds$", a["path"])) and a["method"] == "POST",
        "guard": lambda checks: any(
            c["kind"] == "safety" and "refund" in c["id"].lower() for c in checks),
    },
}


def _bans(prompt: str) -> list[str]:
    return [seg.strip() for seg in re.split(r"[。；\n]", prompt)
            if re.search(r"(不要|不得|禁止|别|切勿|严禁)", seg)]


def audit_ban_consistency(root: pathlib.Path, gold_map: dict[str, list]) -> list[dict]:
    """检测器 1：无界定禁止 + gold 照做 + safety 无防线。"""
    hits: list[dict] = []
    for p in sorted(root.glob("*.json")):
        sid = p.stem
        d = json.loads(p.read_text())
        checks = d["capabilities"]["grade"]["checks"]
        acts = gold_map.get(sid, [])
        if not acts:
            continue  # 没有 gold（如手写题）→ 无从判断"gold 是否照做"
        for cat, rule in BAN_CATS.items():
            if not any(a.get("write") and rule["gold"](a) for a in acts):
                continue  # gold 没做这个动作 → 不是矛盾
            if rule["guard"](checks):
                continue  # safety 有防线 → 自洽
            for ban in _bans(d["prompt"]):
                if not re.search(rule["ban"], ban):
                    continue
                if re.search(QUALIFIER, ban):
                    continue  # 有限定词 = 合法否定
                hits.append({"scenario": sid, "kind": "ban_without_guard",
                             "category": cat, "ban": ban})
    return hits


def audit_semantic_opposition(root: pathlib.Path) -> list[dict]:
    """检测器 2：题面要周期末取消，判据却要立刻终止（status eq canceled）。

    周期末取消在真 Stripe 里的语义是 `cancel_at_period_end=True` 且 `status` 仍是
    active；判据却要求 `status eq "canceled"`（立刻终止）——两者对同一件事对立，
    照题面做必红。这就是 ecom-02 修复前的形状（15/15 全 FAIL）。

    不需要题面额外说"不要立刻终止"："周期末取消"本身就蕴含 status 应保持 active。
    反向（题面要立刻终止、判据要 active）在当前题库没有形状，且极易把否定句
    "不要立刻终止"误读成肯定句，故不实现——宁可漏报也不误伤自洽的题。
    """
    hits: list[dict] = []
    for p in sorted(root.glob("*.json")):
        d = json.loads(p.read_text())
        period_end = bool(re.search(r"周期末取消|按周期末|period.end", d["prompt"]))
        if not period_end:
            continue
        for c in d["capabilities"]["grade"]["checks"]:
            a = c["assert"]
            path, val = a.get("path", ""), str(a.get("value", "")).lower()
            if re.search(r"subscriptions\..*\.status$", path) and val == "canceled":
                hits.append({"scenario": p.stem, "kind": "semantic_opposition",
                             "check": c["id"], "value": val})
    return hits


def audit(root: pathlib.Path, gold_map: dict[str, list]) -> list[dict]:
    return audit_ban_consistency(root, gold_map) + audit_semantic_opposition(root)


def load_gold() -> dict[str, list]:
    """从 verify_solvable.GOLD 现算 gold 动作（不读缓存 JSON）。"""
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(ROOT / "scenarios"))
    import verify_solvable as vs  # noqa: E402
    from argaclone.server import App  # noqa: E402
    from argaclone.runner import _is_write  # noqa: E402

    app = App(vs.SCENARIOS)
    out: dict[str, list] = {}
    for sid in sorted(vs.GOLD):
        run = app.provision({"scenario_id": sid, "twins": "stripe,slack,github", "ttl": 600})
        try:
            acts = vs.GOLD[sid](run)
            out[sid] = [
                {"provider": a.provider, "method": a.method, "path": a.path,
                 "body": a.body, "write": _is_write(a.provider, a.method.upper(), a.path)}
                for a in acts
            ]
        finally:
            app.teardown(run["run_id"])
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT / "scenarios"))
    ap.add_argument("--gold-override", default="", help="历史题库的 gold JSON（RED 自检用）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root)
    gold = (json.loads(pathlib.Path(args.gold_override).read_text())
            if args.gold_override else load_gold())
    hits = audit(root, gold)

    if args.json:
        print(json.dumps({"scenarios": len(list(root.glob("*.json"))),
                          "gold": len(gold), "hits": hits}, ensure_ascii=False, indent=2))
    else:
        print(f"审计 {len(list(root.glob('*.json')))} 题（gold {len(gold)} 题）："
              f"发现 {len(hits)} 处潜在不自洽")
        for h in hits:
            if h["kind"] == "ban_without_guard":
                print(f"  {h['scenario']}: 禁止「{h['ban']}」但 gold 做了「{h['category']}」"
                      f"且 safety 无防线")
            else:
                print(f"  {h['scenario']}: 题面与判据语义对立（{h['check']}={h['value']}）")
        if not hits:
            print("  （两类已知缺陷形状均未命中；不代表题库完全自洽，见模块 docstring）")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
