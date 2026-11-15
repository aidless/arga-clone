"""MCP 端点：把 twin 的四个动作暴露成 MCP 工具。

为什么要有这一层：agent 框架里能自己发 HTTP 的 agent 是少数，能调 MCP 工具的
是多数。Arga 的四通道（Web/API/CLI/MCP）里，MCP 才是 agent 真正会用上的那个 ——
因为 agent 不需要知道 twin 的 host 规则、不需要自己拼 Authorization 头，
它只需要"给我一个能用的 Slack"。

协议按 MCP 2024-11-05 的 Streamable HTTP 子集实现：
  POST /mcp  body = JSON-RPC 2.0
  initialize → 服务能力
  tools/list → 工具清单（带 inputSchema）
  tools/call → 执行
  通知（没有 id 的消息）→ 不返回 body

刻意**不实现**的部分：SSE 流式、resources、prompts、sampling。
理由：这一层的价值是"agent 能拿到能用的 twin"，不是"实现完整的 MCP"。
多实现一层协议就多一层与真实客户端不一致的表面。
"""

from __future__ import annotations

import json
from typing import Any

from .server import App

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "argaclone"
SERVER_VERSION = "0.1.0"


def _text(payload: Any, *, error: bool = False) -> dict:
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload, ensure_ascii=False)
    else:
        body = str(payload)
    return {"content": [{"type": "text", "text": body}], "isError": error}


def tool_specs() -> list[dict]:
    """工具清单。`inputSchema` 必须是真 JSON Schema，客户端会拿它做校验与提示。"""
    return [
        {
            "name": "list_twins",
            "description": "列出可用的 SaaS twin 及其已实现的端点数。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "provision",
            "description": (
                "起一个隔离的 twin run，返回各 provider 的 base URL 与 run-scoped 凭据。"
                "把应用里的 base URL 与密钥换成返回值即可，不需要改代码。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "twins": {
                        "oneOf": [
                            {"type": "string", "description": "逗号分隔，如 stripe,slack"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "ttl_seconds": {"type": "integer", "minimum": 1},
                    "scenario_id": {"type": "string", "description": "用已保存的 scenario 做初始状态"},
                },
                "required": ["twins"],
                "additionalProperties": False,
            },
        },
        {
            "name": "act",
            "description": "在 run 内执行一批动作（provider/method/path/body），返回累积 trace。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "provider": {"type": "string"},
                                "method": {"type": "string"},
                                "path": {"type": "string"},
                                "body": {"type": ["object", "null"]},
                                "query": {"type": ["object", "null"]},
                            },
                            "required": ["provider", "path"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["run_id", "actions"],
                "additionalProperties": False,
            },
        },
        {
            "name": "grade",
            "description": "按 scenario 的判据给 run 判分（PASS / FAIL / UNSAFE / ERROR）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "spec": {"type": "object", "description": "可选：覆盖 scenario 自带的判据"},
                },
                "required": ["run_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "stub_hits",
            "description": "查哪些端点落进了 stub（未实现）。这是保真度唯一的观测口。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "provider": {"type": "string"},
                },
                "required": ["run_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "coverage",
            "description": "自述某个 twin 实现了哪些端点（observed coverage，不是 fidelity）。",
            "inputSchema": {
                "type": "object",
                "properties": {"provider": {"type": "string"}},
                "required": ["provider"],
                "additionalProperties": False,
            },
        },
    ]


class McpEndpoint:
    def __init__(self, app: App) -> None:
        self.app = app

    # ---- 工具实现 ----

    def _with_credentials(self, run_id: str, actions: list) -> list:
        """给没带凭据的动作补上 run-scoped 的 Authorization。

        为什么必须自动补：MCP 这一层的全部价值就是"agent 不用管接入细节"。
        如果 agent 调 act 还得自己去 provision 的返回值里翻 token 再拼头，
        那 MCP 只是个 HTTP 转发器，还不如直接打 API。

        显式给了 Authorization 的动作**不覆盖** —— 测试"用错凭据会怎样"
        是这个工具唯一的用途之一，自动补会把它变成永远正确。
        """
        creds = self.app.store.get(run_id).extras.get("credentials") or {}
        out = []
        for a in actions:
            a = dict(a)
            headers = dict(a.get("headers") or {})
            if not any(k.lower() == "authorization" for k in headers):
                token = creds.get(a.get("provider"))
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            a["headers"] = headers
            out.append(a)
        return out

    def _call_tool(self, name: str, args: dict) -> dict:
        try:
            if name == "list_twins":
                return _text({
                    "twins": [
                        {
                            "provider": p,
                            "implemented_endpoints": self.app.registry.get(p).coverage().body["unique_endpoints"],
                        }
                        for p in self.app.registry.names()
                    ]
                })
            if name == "provision":
                return _text(self.app.provision({
                    "twins": args["twins"],
                    "ttl_seconds": args.get("ttl_seconds", 600),
                    **({"scenario_id": args["scenario_id"]} if args.get("scenario_id") else {}),
                }))
            if name == "act":
                return _text(self.app.act(args["run_id"], self._with_credentials(args["run_id"], args.get("actions") or [])))
            if name == "grade":
                return _text(self.app.grade(args["run_id"], args.get("spec")))
            if name == "stub_hits":
                return _text(self.app.stub_hits(args["run_id"], args.get("provider")))
            if name == "coverage":
                twin = self.app.registry.get(args["provider"])
                if twin is None:
                    return _text(f"unknown provider: {args['provider']}", error=True)
                return _text(twin.coverage().body)
        except Exception as exc:  # noqa: BLE001
            # 工具失败要作为工具结果返回，不能抛成 JSON-RPC 错误：
            # agent 需要看到"这一步失败了"才能换做法，而协议层错误通常会中断整轮。
            return _text(f"{type(exc).__name__}: {exc}", error=True)
        return _text(f"unknown tool: {name}", error=True)

    # ---- JSON-RPC ----

    def handle(self, payload: dict) -> dict | None:
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        method = payload.get("method")
        rid = payload.get("id")
        params = payload.get("params") or {}

        # 通知没有 id，按协议不返回 body
        if rid is None and method:
            return None

        if method == "initialize":
            return _ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "tools/list":
            return _ok(rid, {"tools": tool_specs()})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return _error(rid, -32602, "arguments must be an object")
            return _ok(rid, self._call_tool(name, args))
        if method == "ping":
            return _ok(rid, {})
        return _error(rid, -32601, f"Method not found: {method}")


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
