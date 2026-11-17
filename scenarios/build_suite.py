"""把五个 suite 模块落成 scenarios/*.json。

为什么需要这一步，而不是直接在服务里 import 这些模块：
1. scenario 是产品的一等对象（能 list / get / export / 进 code review），
   它必须能以文件形态被非 Python 的东西消费（CLI、别的 agent、diff）。
2. 生成是显式的、可重跑的：改了 seed 或 checks 就重启一次生成器，
   文件和源定义不会长期背离。
3. 落盘之后 grader 读的是 JSON，测试断言的是"服务读到的那份"，
   而不是"我以为它读到的那份"。

用法：
    python3 scenarios/build_suite.py          # 写文件
    python3 scenarios/build_suite.py --check  # 只校验，不写（CI 用）
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 生成目录可被环境变量覆盖：富余扫描把每一档写到 /tmp 的副本里，工作区保持
# "spare=8 的那一份"，这样扫描期间任何并发跑的测试都仍然在验真实状态。
OUT = Path(os.environ.get("ARGA_SCEN_OUT") or HERE)
sys.path.insert(0, str(HERE))

import suite_crm  # noqa: E402
import suite_dev  # noqa: E402
import suite_ecom  # noqa: E402
import suite_extra  # noqa: E402
import suite_it  # noqa: E402
import suite_mkt  # noqa: E402
import suite_readheavy  # noqa: E402

# 手工维护的题（不参与生成，但也算进总数）
HANDWRITTEN = ["ecom-duplicate-billing.json"]

SUITES = [
    ("IT support", suite_it.IT),
    ("CRM & outreach", suite_crm.CRM),
    ("Marketing", suite_mkt.MKT),
    ("Developer", suite_dev.DEV),
    ("E-commerce", suite_ecom.ECOM),
    # 第 41–80 题：同样的能力面、全新的世界。扩题的理由是功效分析 —— 40 题只能分辨
    # ≥20pt 的差距，要把 10–15pt 也测出来只能加题，加轮次不增加独立样本。
    ("IT support", suite_extra.IT_EXTRA),
    ("CRM & outreach", suite_extra.CRM_EXTRA),
    ("Marketing", suite_extra.MKT_EXTRA),
    ("Developer", suite_extra.DEV_EXTRA),
    ("E-commerce", suite_extra.ECOM_EXTRA),
    # “先读再改”组：每域 1 题，正确解必须读多处前置信息（gold 含 GET），
    # 用来量化预算判据对正当取证的偏差（见 README §29.5、scripts/measure_read_bias.py）。
    ("IT support", [s for s in suite_readheavy.READ_HEAVY if s["domain"] == "IT support"]),
    ("CRM & outreach", [s for s in suite_readheavy.READ_HEAVY if s["domain"] == "CRM & outreach"]),
    ("Marketing", [s for s in suite_readheavy.READ_HEAVY if s["domain"] == "Marketing"]),
    ("Developer", [s for s in suite_readheavy.READ_HEAVY if s["domain"] == "Developer"]),
    ("E-commerce", [s for s in suite_readheavy.READ_HEAVY if s["domain"] == "E-commerce"]),
]


def collect() -> list[dict]:
    out: list[dict] = []
    seen: dict[str, str] = {}
    for domain, items in SUITES:
        for sc in items:
            if sc["id"] in seen:
                raise SystemExit(f"duplicate scenario id {sc['id']} in {domain} and {seen[sc['id']]}")
            seen[sc["id"]] = domain
            out.append(sc)
    return out


def validate(sc: dict) -> list[str]:
    """判据自身的合法性。这些错在生成期就该拦下，而不是等 agent 跑完才发现。"""
    errs: list[str] = []
    if not sc.get("id"):
        errs.append("missing id")
    if not sc.get("prompt"):
        errs.append(f"{sc.get('id')}: empty prompt")
    checks = (sc.get("capabilities") or {}).get("grade", {}).get("checks") or []
    if not checks:
        errs.append(f"{sc.get('id')}: no checks")
    ids = set()
    for c in checks:
        cid = c.get("id")
        if not cid:
            errs.append(f"{sc['id']}: check without id")
        elif cid in ids:
            errs.append(f"{sc['id']}: duplicate check id {cid}")
        ids.add(cid)
        if c.get("kind") not in ("outcome", "safety"):
            errs.append(f"{sc['id']}/{cid}: bad kind {c.get('kind')!r}")
        a = c.get("assert") or {}
        path = a.get("path")
        if not path:
            errs.append(f"{sc['id']}/{cid}: no path")
            continue
        # grader 用 path.split(".") 切段，所以任何一段里都不能再出现点
        for seg in path.split("."):
            if "." in seg:
                errs.append(f"{sc['id']}/{cid}: path segment {seg!r} is unreachable after split('.')")
        if not a.get("op"):
            errs.append(f"{sc['id']}/{cid}: no op")
    # 一道题至少要有两条 safety，否则它测的是"做完了吗"而不是"做得安全吗"
    if sum(1 for c in checks if c.get("kind") == "safety") < 2:
        errs.append(f"{sc['id']}: fewer than 2 safety checks")
    return errs


def _spare_in_use() -> int:
    """生成期用的富余档位，写进输出以便扫描日志能自证用的是哪一档。"""
    from budget import BUDGET_SPARE

    return BUDGET_SPARE


def _floor_in_use() -> int:
    """生成期用的预算地板，与富余一起自证档位。"""
    from budget import BUDGET_FLOOR

    return BUDGET_FLOOR


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    scenarios = collect()

    problems: list[str] = []
    for sc in scenarios:
        problems.extend(validate(sc))

    counts = Counter(sc["domain"] for sc in scenarios)
    written = 0
    for sc in scenarios:
        path = OUT / f"{sc['id']}.json"
        text = json.dumps(sc, indent=2, ensure_ascii=False) + "\n"
        if check_only:
            if not path.exists():
                problems.append(f"{sc['id']}: file missing (run without --check)")
            elif path.read_text() != text:
                problems.append(f"{sc['id']}: file out of date (run without --check)")
        else:
            path.write_text(text)
            written += 1

    total = len(scenarios) + len(HANDWRITTEN)
    print(f"generated {written if not check_only else 0} scenarios, {len(scenarios)} from suites "
          f"(BUDGET_SPARE={_spare_in_use()}, BUDGET_FLOOR={_floor_in_use()}, out={OUT})")
    for domain in ("IT support", "CRM & outreach", "Marketing", "Developer", "E-commerce"):
        print(f"  {domain:<15} {counts[domain]}")
    print(f"  {'handwritten':<12} {len(HANDWRITTEN)}")
    print(f"  {'total':<12} {total}")
    if problems:
        print("\nproblems:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
