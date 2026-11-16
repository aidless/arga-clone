"""故障注入与限流：让环境会失败。

为什么单独一个模块、而不是塞进各个 twin：

- 这属于"环境的行为"，不是"provider 的协议细节"。写进 twin 意味着 12 个 twin 各写
  一遍，而漏掉的那个会静默变成"永不失败"的假环境 —— 这种缺陷在报表里完全看不见。
- 插在网关里才能覆盖所有 provider，包括以后新加的。

为什么必须有这个东西：没有故障注入的环境里，"重试 / 幂等 / 回滚"这三类逻辑一次
都不会被执行到，agent 的分数里也就没有它们的位置。分数看起来只跟"会不会用 API"
有关，其实是因为环境从不刁难。
"""

from __future__ import annotations

import time
from typing import Any

from .twins.base import Response, _segments_match

# 故障模式：
#   status   —— 直接回指定状态码（模拟 500/503/429）
#   timeout  —— 等 delay_ms（默认 0）后回 504，模拟上游超时
#   truncate —— 200 但 body 是半截 JSON，模拟"连上了、读一半断了"
#   commit_then_fail —— **先真的写进状态，再把响应换成失败**。这是重复扣款、
#       重复建单、重复发消息的唯一成因：调用方看到失败，于是重试，而第一次已经生效。
#       它与上面三种不同：必须在 twin 处理**之后**才生效，所以单独一个桶。
MODES = ("status", "timeout", "truncate", "commit_then_fail")
PRE_DISPATCH_MODES = ("status", "timeout", "truncate")


def _faults(run) -> list[dict[str, Any]]:
    return run.extras.setdefault("faults", [])


def _after_faults(run) -> list[dict[str, Any]]:
    """dispatch 之后才生效的故障（`commit_then_fail`）单独放一个桶。

    混进 `faults` 会让 `_match_fault` 在 dispatch 之前就把它们消耗掉，
    表现是"注入了但状态没变" —— 而这一模式的全部意义就是状态**变了**。
    """
    return run.extras.setdefault("faults_after", [])


def _limits(run) -> list[dict[str, Any]]:
    return run.extras.setdefault("rate_limits", [])


def _used(run) -> dict[str, list[float]]:
    return run.extras.setdefault("rate_used", {})


def add_fault(run, spec: dict[str, Any]) -> dict[str, Any]:
    """登记一条故障规则。spec 里的字段一律宽容缺省，但 mode 必须认识。"""
    mode = str(spec.get("mode") or "status")
    if mode not in MODES:
        raise ValueError(f"unknown fault mode: {mode}（可选 {', '.join(MODES)}）")
    fault = {
        "id": str(spec.get("id") or f"f{len(_faults(run)) + 1}"),
        "provider": str(spec.get("provider") or "*"),
        "method": str(spec.get("method") or "*").upper(),
        "path": str(spec.get("path") or "*"),
        "mode": mode,
        "status": int(spec.get("status") or (504 if mode == "timeout" else 500)),
        "times": int(spec.get("times") if spec.get("times") is not None else 1),
        "delay_ms": int(spec.get("delay_ms") or 0),
        "message": str(spec.get("message") or ""),
        "hits": 0,
        # times=0 表示"一直坏"。默认 1 是刻意的：默认永久坏会让第一次评测就整片
        # 掉进 500，读的人会以为环境坏了；"坏一次再看你怎么办"才是要测的东西。
    }
    if mode in PRE_DISPATCH_MODES:
        _faults(run).append(fault)
    else:
        _after_faults(run).append(fault)
    return fault


def add_rate_limit(run, spec: dict[str, Any]) -> dict[str, Any]:
    limit = int(spec.get("limit") or 0)
    if limit <= 0:
        raise ValueError("rate limit 必须 > 0（想关掉就调清空接口）")
    rule = {
        "provider": str(spec.get("provider") or "*"),
        "limit": limit,
        "window_seconds": float(spec.get("window_seconds") or 60),
        # 429 时的 Retry-After：真 provider 给的是"窗口何时滚出"，不给整数秒
        # 会让调用方无从退避。
        "retry_after": int(spec.get("retry_after") or 1),
        # 罚站（penalty）语义：超限之后进入冷却，**从最后一次违规重新计时**。
        # 这是 GitHub / Stripe 二级限流的真实形状：不是"等一会儿就好"，而是
        # "你越撞，越晚解封"。0 = 纯滑动窗口（旧行为）。
        #
        # 为什么要它：纯窗口限流只在**背靠背**的调用里咬得住，而模型每隔几秒才发
        # 一次请求，窗口早滚过去了（§28.5 实测：160 episodes 里 429 一次都没落到
        # 模型手上）。罚站把"撞"这个动作本身变成代价，于是退避与不退避在**分数**上
        # 才分得开 —— 前者等到解封就过，后者每次重发都把自己推得更远。
        "penalty_seconds": float(spec.get("penalty_seconds") or 0),
        "max_penalty_seconds": float(spec.get("max_penalty_seconds") or 0),
        "violations": 0,
        "cooldown_until": 0.0,
    }
    _limits(run).append(rule)
    return rule


def _penalty_remaining(rule: dict[str, Any], now: float) -> float:
    """还在罚站里就返回剩余秒数，否则 0。"""
    return max(0.0, float(rule.get("cooldown_until") or 0.0) - now)


def _enter_penalty(rule: dict[str, Any], now: float) -> float:
    """撞了一次限流：把冷却从**现在**重新起算，并按违规次数加长。

    加长而不是固定：固定冷却会让"连续重发"和"等一会儿再发"得到同一个结果，
    于是"退避"这个词在分数上没有含义。
    """
    rule["violations"] = int(rule.get("violations") or 0) + 1
    grow = float(rule["penalty_seconds"]) * (1.5 ** (rule["violations"] - 1))
    cap = float(rule.get("max_penalty_seconds") or 0.0)
    if cap > 0:
        grow = min(grow, cap)
    rule["cooldown_until"] = now + grow
    return grow


def clear(run, what: str = "all") -> None:
    if what in ("all", "faults"):
        run.extras["faults"] = []
        run.extras["faults_after"] = []
    if what in ("all", "rate_limits"):
        run.extras["rate_limits"] = []
    if what in ("all", "rate_used"):
        run.extras["rate_used"] = {}


def report(run) -> dict[str, Any]:
    return {
        "faults": [dict(f) for f in _faults(run)],
        "faults_after": [dict(f) for f in _after_faults(run)],
        "rate_limits": [dict(r) for r in _limits(run)],
        "rate_used": {k: len(v) for k, v in _used(run).items()},
    }


def _match_fault(run, provider: str, method: str, path: str,
                 bucket=None) -> dict[str, Any] | None:
    """找出第一条命中且还有余额的故障规则，并消耗一次。

    顺序 = 登记顺序，不做"最具体优先"的智能匹配：规则少的时候顺序就是可预测的，
    而"猜哪条更具体"会让复现实验变成猜谜。
    """
    for fault in (bucket or _faults)(run):
        if fault["provider"] not in ("*", provider):
            continue
        if fault["method"] not in ("*", method):
            continue
        if not _segments_match(fault["path"], path):
            continue
        if fault["times"] and fault["hits"] >= fault["times"]:
            continue
        fault["hits"] += 1
        return fault
    return None


def _fault_response(fault: dict[str, Any]) -> Response:
    headers = {"X-Twin-Fault": fault["mode"], "X-Twin-Fault-Id": fault["id"]}
    if fault["delay_ms"]:
        time.sleep(fault["delay_ms"] / 1000.0)
    if fault["mode"] == "timeout":
        return Response(
            fault["status"] or 504,
            {"error": "upstream_timeout", "_twin_fault": fault["id"],
             "_twin_hint": "重试要退避；同一个请求重试成功才算真的处理了"},
            headers,
        )
    if fault["mode"] == "truncate":
        # 真正的截断不是"合法 JSON 里少个字段"，是"连上了、body 读一半就断了"。
        # 所以这里直接返回半截字节：客户端必须能处理 JSONDecodeError，
        # 而不是以为字段缺失。
        partial = b'{"id": "obj_truncated", "object": '
        return Response(200, partial, {**headers, "Content-Type": "application/json"})
    return Response(
        fault["status"] or 500,
        {"error": "injected_failure", "_twin_fault": fault["id"],
         "message": fault["message"] or "injected by /admin/runs/<id>/faults"},
        headers,
    )


def _too_many(rule: dict[str, Any], provider: str, now: float, grow: float,
              *, window: float) -> Response:
    """429 的形状。`Retry-After` 报的是**还要等多久**，不是配置里的那个整数。

    这一点很要紧：罚站语义下剩余时间每次都在变，报旧值会让"等对了的客户端"
    白等（它按 2s 等，实际还要 3s），于是性别退避也无从判断。
    """
    remaining = _penalty_remaining(rule, now)
    wait = max(1, int(remaining + 0.999)) if remaining > 0 else int(rule["retry_after"])
    # reset 报的是"还要多少秒才可能放行"：罚站时报冷却，纯窗口时报窗口滚出的时间
    reset = int(remaining + 0.999) if remaining > 0 else int(rule["retry_after"])
    return Response(
        429,
        {"error": "rate_limited", "provider": provider, "limit": rule["limit"],
         "violations": int(rule.get("violations") or 0),
         "_twin_hint": f"退避 {wait}s 后重试；窗口 {int(window)}s"
                       + ("（罚站中，每撞一次解封更晚）" if grow else "")},
        {
            "Retry-After": str(wait),
            "X-RateLimit-Limit": str(rule["limit"]),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(reset),
            "X-Twin-Rate-Limited": "true",
            **({"X-Twin-Rate-Penalty": str(round(remaining, 3))} if remaining > 0 else {}),
        },
    )


def _check_rate(run, provider: str, now: float) -> Response | None:
    rules = [r for r in _limits(run) if r["provider"] in ("*", provider)]
    if not rules:
        return None
    rule = rules[0]  # 同一 provider 只认第一条，避免两条规则叠加出无法复现的阈值

    # 罚站优先于窗口：只要还在冷却里，任何请求都是 429，而且每撞一次都往后推。
    # 这一条的顺序让"重发"变成**自伤**，而不退还只是无效 —— 退避因此有了可量的价值。
    if float(rule.get("penalty_seconds") or 0) > 0:
        remaining = _penalty_remaining(rule, now)
        if remaining > 0:
            grow = _enter_penalty(rule, now)
            return _too_many(rule, provider, now, grow, window=float(rule["window_seconds"]))
        if int(rule.get("violations") or 0) > 0:
            # 罚站解封：违规计数留着（用来观察"它撞了几次才停"），窗口账本清空
            rule["violations"] = 0
            _used(run)[provider] = []

    used = _used(run).setdefault(provider, [])
    window = rule["window_seconds"]
    used[:] = [t for t in used if now - t < window]
    if len(used) >= rule["limit"]:
        grow = _enter_penalty(rule, now) if float(rule.get("penalty_seconds") or 0) > 0 else 0.0
        return _too_many(rule, provider, now, grow, window=window)
    used.append(now)
    return None


def after_response(run, provider: str, method: str, path: str,
                   resp: Response) -> Response:
    """网关在 twin 处理完、写盘之前调用：把"其实已经成功"的那一发伪装成失败。

    只替换 2xx：一个 404 被换成 500 只是噪声，而 2xx→500 才是要测的场景。
    """
    if path.startswith("/admin"):
        return resp
    fault = _match_fault(run, provider, method.upper(), path, bucket=_after_faults)
    if fault is None:
        return resp
    if not (200 <= resp.status < 300):
        return resp
    return Response(
        fault["status"] or 500,
        {"error": "committed_but_failed", "_twin_fault": fault["id"],
         "message": fault["message"] or (
             "服务端已经处理了这次请求，但响应失败了 —— 重发会不会重复生效，"
             "取决于这次调用是不是幂等的")},
        {"X-Twin-Fault": "commit_then_fail", "X-Twin-Fault-Id": fault["id"]},
    )


def guard(run, provider: str, method: str, path: str) -> Response | None:
    """网关在交给 twin 之前调用。返回 None 表示这一发照常处理。"""
    if path.startswith("/admin"):
        # admin 面（覆盖率、stub 报表、清空故障）不能被自己注入的故障挡住，
        # 否则"注入了故障之后没法关掉"就是死锁。
        return None
    fault = _match_fault(run, provider, method.upper(), path)
    if fault is not None:
        return _fault_response(fault)
    return _check_rate(run, provider, time.time())
