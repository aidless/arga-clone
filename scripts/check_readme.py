#!/usr/bin/env python3
"""核对 README 里那些"能数出来的数字"是否与代码一致。

为什么需要这个：README 的数字是给外面看的承诺，而它最典型的腐化方式不是写错，
是**改完代码忘了改文档**。11 个 twin 的路由表、测试数、"十一层验收"这些一旦漂移，
读者看到的是一份看起来很精确、实际上过期的自述 —— 那比不写数字更坏。

只核对**可复算**的项（twin 数、路由数、场景数、gold 数、测试数、验收层数）。
不核对叙述性判断（"难复刻的是哪一层"），那是人写的，脚本没有资格改。

`--selftest` 用注入样本证明三件事（这条是从工作区三闸的纪律搬来的：门要能红、
也要证明不会乱红）：① 数字漂移必须报；② 干净 README 必须放行；
③ 别的表里长得像的行（模型分数表、补齐前后表）**不许**被当成 twin 行。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}

HEADER = re.compile(
    r"(\d+) 个 twin / (\d+) 条路由 / (\d+) 道五域任务 / (\d+) 道 gold 可解性证明 / "
    r"(\d+) 个测试全绿"
)
# 终止符加 `\Z`：节尾没有下一个标题（或就是文末）时也要能切出来，
# 否则"合规文本"会被误判成"表解析为空"。
SECTION = re.compile(r"个 twin 的规模.*?\n\n(.*?)(?=\n#{2,3} |\n---|\Z)", re.S)
ROW = re.compile(r"^\| (\w+) \| (\d+) \|", re.M)


def _table_only(body: str) -> str:
    """只保留这一段里的**第一张表**。

    光切到节边界还不够：如果 §12 是全文最后一节（或后面紧跟的不是标题），
    节尾会一直延伸到文末，把后面别的表也吃进来。只在"从第一行 `|` 开始、
    遇到第一个非表格行就停"的范围内解析，断言域才真的等于那张表。
    """
    lines = body.splitlines()
    out: list[str] = []
    for ln in lines:
        if ln.lstrip().startswith("|"):
            out.append(ln)
        elif out:
            break
    return "\n".join(out)
LAYERS = re.compile(r"([一二三四五六七八九十]+)层验收")


def actual() -> dict:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    from argaclone.server import build_registry

    reg = build_registry()
    routes = {name: len(reg.get(name).routes) for name in reg.names()}

    import verify_solvable as vs  # noqa: E402

    # `-o addopts=`：仓库的 pytest.ini 已带 `-q`，再传一个 -q 会变成 -qq，
    # 那时 pytest 连"共收集多少"那行都不打印 —— 于是统计恒为 -1。
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-o", "addopts="],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    m = re.search(r"(\d+) tests? collected", collected)
    tests = int(m.group(1)) if m else -1

    verify = (ROOT / "scripts" / "verify.sh").read_text()
    layers = len(re.findall(r'^echo "== \d+/\d+ ', verify, re.M))

    return {
        "twins": len(reg.names()),
        "routes": sum(routes.values()),
        "routes_by_twin": routes,
        "scenarios": len(list((ROOT / "scenarios").glob("*.json"))),
        "gold": len(vs.GOLD),
        "tests": tests,
        "layers": layers,
    }


def compare(text: str, got: dict) -> list[str]:
    """纯函数：给定 README 文本与实测数字，返回不一致项。空列表 = 一致。

    抽成纯函数是为了能对"注入过错误的文本"跑同一套判据 —— 门要能红，
    就得能在不碰真 README 的前提下被喂坏输入。
    """
    problems: list[str] = []

    m = HEADER.search(text)
    if not m:
        problems.append("头部汇总行找不到（是不是改了措辞？改了就同步更新匹配式）")
    else:
        for label, written, real in [
            ("twin", int(m.group(1)), got["twins"]),
            ("路由", int(m.group(2)), got["routes"]),
            ("任务", int(m.group(3)), got["scenarios"]),
            ("gold", int(m.group(4)), got["gold"]),
            ("测试", int(m.group(5)), got["tests"]),
        ]:
            if written != real:
                problems.append(f"头部 {label}：README 写 {written}，实测 {real}")

    # §12 的 per-twin 路由表。必须先切出这一节再解析：README 里还有别的表格也是
    # `| 名字 | 数字 |`（模型分数表、补齐前后的端点对照表），全文扫会把 `hy4free | 1`
    # 当成 twin、把 §16 的"补齐前 28 条"当成当前值。第一版就是这么误报的。
    section = SECTION.search(text)
    body = section.group(1) if section else ""
    rows = ROW.findall(_table_only(body))
    if not rows:
        problems.append("§12 的 twin 规模表解析为空")
    for name, written in rows:
        real = got["routes_by_twin"].get(name)
        if real is None:
            problems.append(f"§12 表里有 `{name}`，但注册表里没有这个 twin")
        elif int(written) != real:
            problems.append(f"§12 `{name}`：README 写 {written} 条路由，实测 {real}")

    m = LAYERS.search(text)
    if not m:
        problems.append("找不到验收层数（「N 层验收」）")
    else:
        raw = m.group(1)
        written = CN_NUM.get(raw, -1)
        if written != got["layers"]:
            problems.append(f"验收层数：README 写 {raw}（{written}），verify.sh 实际 {got['layers']} 层")

    return problems


def _load_episodes(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return data["episodes"] if isinstance(data, dict) else data


def _stats(rows: list[dict]) -> dict:
    from collections import Counter

    c = Counter(r["verdict"] for r in rows)
    n = len(rows)
    calls = sum(r.get("calls", 0) for r in rows) / n if n else 0.0
    return {
        "pass": c["PASS"], "n": n,
        "rate": round(100 * c["PASS"] / n, 1) if n else 0.0,
        "unsafe": c["UNSAFE"], "error": c["ERROR"],
        "avg_calls": round(calls, 1),
    }


# README §15 的模型结果行：| 模型 | PASS | n | **94.2%** | [lo, hi] | UNSAFE | ERROR | avg |
MODEL_ROW = re.compile(
    # 百分比后面可能还有注解（`0.8%（作废）`），所以不能要求紧跟 ` |`
    r"^\| ([\w@./\-]+) \| (\d+) \| (\d+) \| [^|]*?([\d.]+)%[^|]* "
    r"\| \[([\d.]+)%, ([\d.]+)%\] \| (\d+) \| (\d+) \| ([\d.]+) \|",
    re.M,
)
BALLPARK = re.compile(
    r"ballpark 是 (\S+) ([\d.]+)% / (\S+) ([\d.]+)% / (\S+) ([\d.]+)%"
)


def compare_results(text: str, demo: Path) -> list[str]:
    """README 里贴的 benchmark 数字必须能从落盘 JSON 重算。

    这是全仓最该被守的数字面：外人引用这套东西时抄的就是这几个百分数，
    而它最容易在"重跑一轮、忘了改表"之后静默过期——结果文件换了、README 没换，
    或者只改了其中一个模型那一格。所以逐格核，不核总数。
    """
    problems: list[str] = []

    f480 = demo / "agent_compare_480.json"
    if not f480.exists():
        problems.append("找不到 demo/agent_compare_480.json，§15 的 480 episodes 表无法复算")
    else:
        rows = _load_episodes(f480)
        by_model: dict[str, list[dict]] = {}
        for r in rows:
            by_model.setdefault(r["model"], []).append(r)
        seen = set()
        for m in MODEL_ROW.finditer(text):
            model, p_, n_, rate, lo, hi, unsafe, error, avg = m.groups()
            if model not in by_model:
                problems.append(f"§15 表里的模型 `{model}` 不在 agent_compare_480.json 里")
                continue
            seen.add(model)
            real = _stats(by_model[model])
            for label, written, actual in [
                ("PASS", int(p_), real["pass"]),
                ("n", int(n_), real["n"]),
                ("pass 率", float(rate), real["rate"]),
                ("UNSAFE", int(unsafe), real["unsafe"]),
                ("ERROR", int(error), real["error"]),
                ("avg calls", float(avg), real["avg_calls"]),
            ]:
                if written != actual:
                    problems.append(f"§15 `{model}` {label}：README 写 {written}，重算 {actual}")
            # Wilson 区间也重算：区间是这张表最容易被手抄错的列
            sys.path.insert(0, str(ROOT / "scripts"))
            from stats import wilson  # noqa: E402

            wlo, whi = wilson(real["pass"], real["n"])
            for label, written, actual in [
                ("CI 下界", float(lo), round(100 * wlo, 1)),
                ("CI 上界", float(hi), round(100 * whi, 1)),
            ]:
                if abs(written - actual) > 0.15:      # 表里只保留一位小数
                    problems.append(f"§15 `{model}` {label}：README 写 {written}，重算 {actual}")
        missing = set(by_model) - seen
        if missing:
            problems.append(f"agent_compare_480.json 里的模型没进 §15 表：{sorted(missing)}")

    f120 = demo / "agent_compare_endpoints.json"
    bb = BALLPARK.search(text)
    if f120.exists() and bb:
        rows120 = _load_episodes(f120)
        by_model = {}
        for r in rows120:
            by_model.setdefault(r["model"], []).append(r)
        for i in range(0, 6, 2):
            model, rate = bb.group(i + 1), float(bb.group(i + 2))
            # README 正文用的是短名（sensenova / glm / deepseek），JSON 里是全名
            # （sensenova/sensenova-6.8-flash）。按"短名出现在全名里"配对，
            # 但要求唯一命中——两可时宁可报错也不要猜。
            hits = [k for k in by_model if model in k]
            if len(hits) != 1:
                problems.append(
                    f"§16 ballpark 里的 `{model}` 在 agent_compare_endpoints.json 里"
                    f"{'命中多个：' + str(hits) if hits else '找不到'}"
                )
                continue
            model = hits[0]
            real = _stats(by_model[model])["rate"]
            if abs(rate - real) > 0.05:
                problems.append(f"§16 ballpark `{model}`：README 写 {rate}%，重算 {real}%")

    return problems


def _selftest() -> int:
    """注入 6 个样本：3 个必须红、3 个必须绿（其中 2 个是"不许乱红"）。"""
    got = {
        "twins": 11, "routes": 236, "scenarios": 40, "gold": 39,
        "tests": 406, "layers": 11,
        "routes_by_twin": {"github": 51, "slack": 22, "stripe": 39},
    }
    good = (
        "11 个 twin / 236 条路由 / 40 道五域任务 / 39 道 gold 可解性证明 / 406 个测试全绿 /\n"
        "\n"
        "十一个 twin 的规模（路由数取自各自 `coverage()`）：\n"
        "\n"
        "| twin | 路由 | 说明 |\n"
        "|---|---|---|\n"
        "| stripe | 39 | x |\n"
        "| github | 51 | y |\n"
        "| slack | 22 | z |\n"
        "\n"
        "十一层验收全过。\n"
    )

    cases: list[tuple[str, str, bool, str]] = []   # (名字, 文本, 期望有错, 说明)

    cases.append(("干净文本", good, False, "合规输入必须放行"))
    cases.append(("头部 twin 漂移", good.replace("11 个 twin", "12 个 twin"), True, "数字漂移必须报"))
    cases.append(("§12 行漂移",
                  good.replace("| github | 51 |", "| github | 50 |"), True, "per-twin 路由数漂移必须报"))
    cases.append(("层数漂移", good.replace("十一层验收", "十层验收"), True, "层数漂移必须报"))

    # 不许乱红之一：别处的同名数字表（模型分数表）不能被当成 twin 行
    noisy = good.replace(
        "十一层验收全过。",
        "| hy4free | 1 | 模型分数表，不是 twin |\n"
        "\n"
        "| github | 28 | 51 | `GET /contents` |\n"   # §16 的"补齐前/后"表
        "\n"
        "十一层验收全过。",
    )
    cases.append(("别的表里也有 | 名字 | 数字 |", noisy, False, "§12 之外的表格不许被当 twin 行"))

    # 不许乱红之二：注册表里没有但不在 §12 的 twin 名（比如正文提到 asana）不该报
    prose = good.replace("十一层验收全过。", "对比对象还有 asana、twilio 等 33 个服务。\n\n十一层验收全过。")
    cases.append(("正文提到没实现的 provider", prose, False, "正文提及不等于表内声明"))

    fails: list[str] = []

    # 结果表的两条：真实 README 必须与落盘 JSON 一致；改掉一格必须报
    real = README.read_text()
    demo = ROOT / "demo"
    if not compare_results(real, demo):
        print("  ✓ 结果表（真实 README vs demo/*.json）：放行 —— 480 表与 ballpark 全部重算一致")
    else:
        fails_pre = compare_results(real, demo)
        fails.append(f"结果表（真实 README 与落盘 JSON 不一致：{fails_pre[:2]}）")

    # 用 MODEL_ROW 自己定位要改的那一格，而不是靠字符串猜——曾在 §11 的
    # 另一张表上改到同名行，于是"注入成功但没被检查到"，看起来像假绿。
    mrow = MODEL_ROW.search(real)
    if mrow is None:
        fails.append("结果表：README 里找不到任何模型结果行")
        mutated = real
    else:
        start, end = mrow.span(2)
        mutated = real[:start] + str(int(mrow.group(2)) - 1) + real[end:]
    if mutated != real and compare_results(mutated, demo):
        print("  ✓ 结果表 PASS 格注入 97：红了 —— benchmark 数字漂移必须报")
    else:
        fails.append("结果表 PASS 格注入 97（期望红，实际绿）")

    for name, text, expect_bad, why in cases:
        problems = compare(text, got)
        if bool(problems) != expect_bad:
            fails.append(f"{name}（期望{'红' if expect_bad else '绿'}，实际 {problems or '绿'}；{why}）")
        else:
            print(f"  ✓ {name}：{'红了' if expect_bad else '放行'} —— {why}")

    if fails:
        print(f"\n✗ selftest {len(fails)} 条不通过：")
        for f in fails:
            print("  " + f)
        return 1
    red = len([c for c in cases if c[2]]) + 1     # +1 = 结果表 PASS 格注入
    green = len([c for c in cases if not c[2]]) + 1  # +1 = 结果表真实一致性
    print(f"\n✓ selftest 全通过（{red + green} 例：{red} 条必红 + {green} 条必绿）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核对 README 数字是否与代码一致")
    ap.add_argument("--selftest", action="store_true", help="非空性反证自测")
    ap.add_argument("--readme", default=str(README))
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    got = actual()
    text = Path(args.readme).read_text()
    print("实测：" + json.dumps(
        {k: v for k, v in got.items() if k != "routes_by_twin"}, ensure_ascii=False
    ))
    problems = compare(text, got) + compare_results(text, ROOT / "demo")
    if problems:
        print("\nREADME 与代码不一致：")
        for p in problems:
            print("  ✗ " + p)
        return 1
    print("README 数字全部可复算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
