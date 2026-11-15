"""Webhook 投递：让"世界变了会有人告诉你"这件事真的发生。

为什么值得单独做：现在 11 个 twin 全都是"你查得到"，没有一个是"它会通知你"。
而真实的多系统任务里，事件是第二个入口 —— 一个只会轮询的 agent 和一个会订阅的
agent，在同一个环境里的行为差别，恰恰是这套评测最想量出来的东西。

三条判据（这三条决定它是不是装饰）：

1. **签名可验**：接收方必须能用共享密钥验证"这条通知真的是你发的"。
2. **投递有记录**：失败了几次、最后一次什么状态码，必须能在 admin 面查到。
   否则"通知丢了"和"我根本没注册"长得一样。
3. **不假装覆盖全部变更**：这里只声明最常被用到的几种事件；**没声明到的变更
   不产生事件**，而不是发一条 `something.changed` 糊过去。查不出来的地方
   要能被查出来。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .twins.base import _segments_match

# ---------------------------------------------------------------- 事件表

# (方法, 路径段模式, 事件名或 判定函数)。判定函数给 (body) 返回事件名或 None，
# 用于"同一个端点因 body 不同发不同事件"的情形（PATCH issue 关/开）。
Rule = tuple[str, str, "str | Callable[[dict], str | None]"]

EVENT_MAP: dict[str, list[Rule]] = {
    "stripe": [
        ("POST", "/v1/customers", "customer.created"),
        ("POST", "/v1/refunds", "charge.refunded"),
        ("POST", "/v1/subscriptions", "customer.subscription.created"),
        ("POST", "/v1/subscriptions/*", "customer.subscription.updated"),
        ("DELETE", "/v1/subscriptions/*", "customer.subscription.deleted"),
        ("POST", "/v1/payment_intents", "payment_intent.succeeded"),
    ],
    "github": [
        ("POST", "/repos/*/*/issues", "issues.opened"),
        ("PATCH", "/repos/*/*/issues/*",
         lambda body: "issues.closed" if body.get("state") == "closed" else "issues.edited"),
        ("POST", "/repos/*/*/issues/*/comments", "issue_comment.created"),
        ("POST", "/repos/*/*/pulls", "pull_request.opened"),
        ("POST", "/repos/*/*/pulls/*/reviews", "pull_request_review.submitted"),
        ("POST", "/repos/*/*/deployments", "deployment.created"),
        ("POST", "/repos/*/*/releases", "release.published"),
    ],
    "slack": [
        ("POST", "/api/chat.postMessage", "message.posted"),
        ("POST", "/api/conversations.create", "channel.created"),
    ],
}


def events_for(provider: str, method: str, path: str, body: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for rule_method, pattern, event in EVENT_MAP.get(provider, []):
        if rule_method != method.upper():
            continue
        if not _segments_match(pattern, path):
            continue
        if callable(event):
            name = event(body)
            if name:
                out.append(name)
        else:
            out.append(event)
    return out


# ---------------------------------------------------------------- 签名

def sign(secret: str, timestamp: int, raw: bytes) -> str:
    """Stripe 风格：`t=<ts>,v1=<hex>`，签名内容是 `"{ts}.{body}"`。

    签时间戳而不只签 body，是为了让接收方可以拒绝重放（我们不校验窗口，
    但接收方要能校验）—— 一条能被无限重放的 `charge.refunded` 会让"收到通知"
    这件事失去信息量。
    """
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify(secret: str, header: str, raw: bytes) -> bool:
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    if "t" not in parts or "v1" not in parts:
        return False
    try:
        ts = int(parts["t"])
    except ValueError:
        return False
    return hmac.compare_digest(sign(secret, ts, raw), header)


# ---------------------------------------------------------------- 注册与投递

def _hooks(run) -> list[dict[str, Any]]:
    return run.extras.setdefault("webhooks", [])


def _log(run) -> list[dict[str, Any]]:
    return run.extras.setdefault("webhook_log", [])


def register(run, spec: dict[str, Any]) -> dict[str, Any]:
    url = str(spec.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise ValueError("webhook url 必须 http(s):// 开头")
    hook = {
        "id": str(spec.get("id") or f"wh_{len(_hooks(run)) + 1}"),
        "url": url,
        "provider": str(spec.get("provider") or "*"),
        "events": list(spec.get("events") or ["*"]),
        # 没给密钥就现发一个，并**原样回给注册方**：让它自己编一个会让签名校验
        # 变成"两边都用了错的密钥、都以为自己对"。
        "secret": str(spec.get("secret") or f"whsec_{run.run_id[:12]}_{len(_hooks(run)) + 1}"),
        "max_attempts": int(spec.get("max_attempts") or 3),
        "timeout": float(spec.get("timeout") or 2.0),
    }
    _hooks(run).append(hook)
    return hook


def remove(run, hook_id: str) -> bool:
    hooks = _hooks(run)
    keep = [h for h in hooks if h["id"] != hook_id]
    run.extras["webhooks"] = keep
    return len(keep) != len(hooks)


def _wants(hook: dict[str, Any], provider: str, event: str) -> bool:
    if hook["provider"] not in ("*", provider):
        return False
    return "*" in hook["events"] or event in hook["events"]


def deliver(run, provider: str, method: str, path: str, body: dict[str, Any]) -> list[dict]:
    """把这一发变更变成通知，逐条投递并记账。返回本次的投递记录。"""
    hooks = [h for h in _hooks(run) if h["provider"] in ("*", provider)]
    if not hooks:
        return []
    events = events_for(provider, method, path, body)
    if not events:
        return []

    records: list[dict[str, Any]] = []
    for hook in hooks:
        for event in events:
            if not _wants(hook, provider, event):
                continue
            payload = {
                "id": f"evt_{int(time.time() * 1000)}_{len(_log(run))}",
                "type": event,
                "provider": provider,
                "run_id": run.run_id,
                "created": int(time.time()),
                # data 里给的是"变更的原始事实"而不是"变更后的世界"：
                # 拿到对象就得再查一次，这正是真实的 webhook 消费者要做的事。
                "data": {"method": method.upper(), "path": path, "request": body},
            }
            raw = json.dumps(payload, sort_keys=True).encode()
            ts = int(time.time())
            record = {
                "hook": hook["id"], "event": event, "url": hook["url"],
                "attempts": 0, "status": None, "ok": False,
                "signature": sign(hook["secret"], ts, raw),
            }
            for attempt in range(1, hook["max_attempts"] + 1):
                record["attempts"] = attempt
                try:
                    request = urllib.request.Request(
                        hook["url"], data=raw, method="POST",
                        headers={
                            "Content-Type": "application/json",
                            "X-Argaclone-Signature": record["signature"],
                            "X-Argaclone-Event": event,
                            "X-Argaclone-Delivery": payload["id"],
                        },
                    )
                    with urllib.request.urlopen(request, timeout=hook["timeout"]) as resp:
                        record["status"] = resp.status
                    record["ok"] = 200 <= record["status"] < 300
                except urllib.error.HTTPError as exc:
                    record["status"] = exc.code
                    record["error"] = f"HTTP {exc.code}"
                except Exception as exc:  # 连不上、超时、DNS 失败
                    record["status"] = 0
                    record["error"] = f"{type(exc).__name__}: {exc}"
                if record["ok"]:
                    break
            _log(run).append(record)
            records.append(record)
    return records


def report(run) -> dict[str, Any]:
    hooks = [{k: v for k, v in h.items()} for h in _hooks(run)]
    # 密钥只回前 8 位：报告会被贴进 README 和日志，整串密钥进去等于公布了它。
    for hook in hooks:
        hook["secret"] = hook["secret"][:8] + "…"
    return {"webhooks": hooks, "log": list(_log(run))}
