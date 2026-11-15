"""HTTP 服务：一个进程同时充当 admin API 与全部 twin。

一个进程、一个端口、按 Host 分流，是这个复刻品能跑起来的关键：
- 本地 `curl -H "Host: stripe.<run>.twins.local"` 就能当 Stripe 用；
- 生产里换成 `*.twins.example.com` 通配解析即可，代码不用改。

admin 面故意不鉴权：它只在 127.0.0.1 上监听，且内容本身就是仿真世界的东西。
真要做多租户，第一件事是把 admin 加 token——但那是另一件事，别在这里假装做完了。
"""

from __future__ import annotations

import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import chaos, webhooks
from .gateway import Gateway
from .graders import grade
from .runner import Runner, empty_trace
from .scenarios import Scenario, ScenarioStore, apply_scenario, generate_from_prompt
from .store import RunExpired, RunNotFound, StaleRunVersion, Store
from .twins.base import TwinRegistry
from .twins.github_twin import GitHubTwin
from .twins.hubspot_twin import HubSpotTwin
from .twins.calendar_twin import CalendarTwin
from .twins.drive_twin import DriveTwin
from .twins.gmail_twin import GmailTwin
from .twins.jira_twin import JiraTwin
from .twins.linear_twin import LinearTwin
from .twins.notion_twin import NotionTwin
from .twins.slack_twin import SlackTwin
from .twins.salesforce_twin import SalesforceTwin
from .twins.stripe_twin import StripeTwin


def build_registry() -> TwinRegistry:
    reg = TwinRegistry()
    reg.register(StripeTwin())
    reg.register(SlackTwin())
    reg.register(GitHubTwin())
    reg.register(HubSpotTwin())
    reg.register(SalesforceTwin())
    # 这三个是用 twin_spec 工厂建的（声明式资源 + scope 家族 + 分页 + 错误形状），
    # 不是手写 CRUD —— 见 README「工厂」一节
    reg.register(JiraTwin())
    reg.register(LinearTwin())
    reg.register(NotionTwin())
    # Google Workspace 三件套：共享 run-scoped 文件状态与 people 身份
    reg.register(GmailTwin())
    reg.register(DriveTwin())
    reg.register(CalendarTwin())
    return reg


def run_public_view(run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "twins": run.twins,
        "ttl_seconds": run.ttl_seconds,
        "remaining_seconds": run.remaining(),
        "created_at": run.created_at,
        "locked": run.locked,
        "expired": run.expired(),
        "endpoints": {
            t: f"http://{t}.{run.run_id}.twins.local" for t in run.twins
        },
        "scenario_id": run.extras.get("scenario_id"),
        "credentials": run.extras.get("credentials", {}),
        # 严格凭据模式要能从 run 视图里看到：客户端靠它判断"该不该用 provision
        # 下发的那个 token"，否则它只能靠试错发现。
        "strict_tokens": bool(run.extras.get("strict_tokens")),
    }


class App:
    def __init__(self, scenario_dir: Path, admin_token: str | None = None,
                 data_dir: Path | str | None = None) -> None:
        # admin_token=None 是刻意的默认：本机单人调试时不该为了看一眼 run 列表去配令牌。
        # 一旦要给别人连（或多租户），这就是必须拧的那颗螺丝 —— serve --admin-token。
        self.admin_token = admin_token
        self.store = Store(data_dir)
        self.registry = build_registry()
        self.gateway = Gateway(self.store, self.registry)
        self.scenarios = ScenarioStore(scenario_dir)
        self.runners: dict[str, Runner] = {}
        self._runners_lock = threading.Lock()

    # ---------- run 操作 ----------

    def provision(self, body: dict[str, Any]) -> dict[str, Any]:
        twins = body.get("twins") or ["stripe"]
        if isinstance(twins, str):
            twins = [t.strip() for t in twins.split(",") if t.strip()]
        unknown = [t for t in twins if not self.registry.has(t)]
        if unknown:
            raise ValueError(f"unknown twins: {unknown}; available: {self.registry.names()}")

        ttl = int(body.get("ttl") or body.get("ttl_seconds") or 600)
        if ttl < 1:
            raise ValueError("ttl must be >= 1")
        run = self.store.create(twins, ttl, strict_tokens=bool(body.get("strict_tokens")))
        # 没有 scenario 的 run 也必须有可用的世界：twin 的初始状态在创建时就落地，
        # 否则任何一次请求都会在 state[provider] 上 KeyError。
        for t in twins:
            run.state[t] = self.registry.get(t).initial_state()

        scenario: Scenario | None = None
        sid = body.get("scenario_id")
        if sid:
            scenario = self.scenarios.get(sid)
            if scenario is None:
                raise ValueError(f"scenario not found: {sid}")
        elif body.get("prompt"):
            scenario = Scenario(
                id="",
                name=body.get("name") or "inline",
                seed=generate_from_prompt(body["prompt"], twins),
                prompt=body["prompt"],
                generation_mode=body.get("generation_mode") or "fast",
            )
            self.scenarios.save(scenario)

        if scenario is not None:
            apply_scenario(scenario, run, self.store, self.registry)
            run.extras["scenario_id"] = scenario.id

        # 关键时序：所有 seed 落完之后才打基线。早一步打，reset 就会把 seed 弄丢。
        self.store.snapshot(run.run_id)
        return run_public_view(self.store.get(run.run_id))

    def get_run(self, run_id: str) -> dict[str, Any]:
        return run_public_view(self.store.get(run_id))

    def list_runs(self) -> dict[str, Any]:
        return {"runs": [run_public_view(r) for r in self.store.all_runs()], "stub_reports": {}}

    def reset(self, run_id: str) -> dict[str, Any]:
        self.store.reset(run_id)
        # 重置是一次新尝试：动作历史必须清空，否则上一次的写请求会跟着这次判分走，
        # "重放同一 agent 得到同一结论"就变成了"上一次的痕迹让这次判过"。
        self.runners.pop(run_id, None)
        return run_public_view(self.store.get(run_id))

    def extend(self, run_id: str, ttl: int) -> dict[str, Any]:
        self.store.extend(run_id, ttl)
        return run_public_view(self.store.get(run_id))

    def lock(self, run_id: str) -> dict[str, Any]:
        self.store.lock(run_id)
        return run_public_view(self.store.get(run_id))

    def teardown(self, run_id: str) -> dict[str, Any]:
        self.store.drop(run_id)
        self.runners.pop(run_id, None)
        return {"deleted": True, "run_id": run_id}

    def state(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id, allow_expired=True)
        return {
            "run_id": run.run_id,
            "state": run.state,
            "shared_people": run.state.get("shared", {}).get("people", {}),
        }

    def stub_hits(self, run_id: str, provider: str | None = None) -> dict[str, Any]:
        return self.store.stub_report(run_id, provider)

    # ---------- 动作执行与判分 ----------

    def runner_for(self, run_id: str) -> Runner:
        """拿这个 run 的 runner，必要时创建。

        为什么必须按 run 复用一个 runner 而不是每次新建：runner 持有全部
        calls，而判据里的 `trace.writes.count`、`trace.path_sequence` 判的是
        "这一整轮做过什么"。每次 act 都新建 runner 会让 trace 只剩最后一批
        动作 —— 一个分三步干活的 agent 会被判成"只发了最后一条消息"，
        而它前面发的那两条（包括发错频道的）全部消失。这是最隐蔽的假绿。
        """
        runner = self.runners.get(run_id)
        if runner is None:
            # ThreadingHTTPServer：同一个 run 的两个并发请求会同时走到这里。
            # 没有锁的话两次都可能看到 None 并各自建一个 runner，于是 trace 被劈成
            # 两半 —— 每个都"看着完整"，判据只看得到最后被写回的那个。这跟"每次
            # act 新建 runner"是同一个 bug 的并发版本，且更难复现。
            with self._runners_lock:
                runner = self.runners.get(run_id)
                if runner is None:
                    runner = Runner(self.gateway, run_id)
                    self.runners[run_id] = runner
        return runner

    def commit_trace(self, run_id: str) -> dict[str, Any]:
        """把 runner 累积的 trace 落到 run 上，供 grade 使用。

        逐步驱动的 agent（真模型 tool-call 循环）不能走 act —— 它每步都要
        看返回再决定。所以那条路径需要显式提交一次 trace，否则 grade 看到的是
        空 trace，判据里所有 trace.* 都会读成 None。
        """
        trace = self.runner_for(run_id).trace()
        self.store.get(run_id, allow_expired=True).extras["last_trace"] = trace
        return trace

    def act(self, run_id: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        self.store.get(run_id)  # 过期就抛
        trace = self.runner_for(run_id).run(actions)
        run = self.store.get(run_id, allow_expired=True)
        run.extras["last_trace"] = trace
        return trace

    def grade(self, run_id: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
        run = self.store.get(run_id, allow_expired=True)
        the_spec = spec
        if the_spec is None:
            sid = run.extras.get("scenario_id")
            scenario = self.scenarios.get(sid) if sid else None
            if scenario is not None:
                the_spec = (scenario.capabilities or {}).get("grade")
        if not the_spec:
            return {"verdict": "ERROR", "error": "no grade spec provided and none in scenario"}
        trace = run.extras.get("last_trace") or empty_trace()
        report = grade(the_spec, run.state, trace)
        out = report.to_dict()
        out["trace"] = trace
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: App  # 由 serve() 注入

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        # 静音：测试里几百个请求刷屏没价值
        return

    # ---- 工具 ----

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        if isinstance(payload, (bytes, bytearray)):
            raw = bytes(payload)
        elif payload is None:
            raw = b""
        else:
            raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _headers_lower(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.headers.items()}

    # ---- 方法 ----

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    # ---- admin 鉴权 ----

    def _admin_scoped(self, path: str, headers: dict[str, str]) -> bool:
        """这个请求是否属于"管理面"（需要 admin 令牌）。

        三类都算：服务级 /admin/*、twin 级 /admin/*（三种寻址都算）、以及 /mcp
        —— MCP 的 provision/act/grade 与被保护的 admin 路由是同一份权力，
        只挡 HTTP 那一半等于没挡。
        数据面（/v1/...、/api/...）不在此列，它有自己的凭据体系。
        """
        if path == "/mcp":
            return True
        if path.startswith("/__twin/"):
            m = re.match(r"^/__twin/[^/]+/[^/]+(/.*)?$", path)
            return bool(m and (m.group(1) or "").startswith("/admin"))
        return path.startswith("/admin")

    def _admin_token_ok(self, headers: dict[str, str]) -> bool:
        token = self.app.admin_token
        if not token:
            return True
        given = (headers.get("x-admin-token") or "").strip()
        if not given:
            auth = headers.get("authorization") or ""
            scheme, _, value = auth.partition(" ")
            if scheme.lower() == "bearer":
                given = value.strip()
        # 定长比较：令牌比较不该泄露前缀匹配长度
        return bool(given) and hmac.compare_digest(given, token)

    def _dispatch(self, method: str) -> None:
        body = self._read_body()
        path = urlparse(self.path).path

        headers = self._headers_lower()
        if self.app.admin_token and self._admin_scoped(path, headers) and not self._admin_token_ok(headers):
            self._send(
                401,
                {
                    "error": "admin_auth_required",
                    "detail": "send Authorization: Bearer <admin token> or X-Admin-Token",
                },
                {"WWW-Authenticate": 'Bearer realm="argaclone-admin"'},
            )
            return

        try:
            if path == "/healthz":
                self._send(200, {"ok": True})
                return
            if path == "/mcp":
                self._mcp(method, body)
                return
            if path.startswith("/admin") and not self.app.gateway.is_twin_target(
                self.headers.get("Host", ""), self._headers_lower()
            ):
                self._admin(method, path, body)
                return

            resp = self.app.gateway.handle(
                method, self.headers.get("Host", ""), self.path, self._headers_lower(), body
            )
            self._send(resp.status, resp.body, resp.headers)
        except RunNotFound as exc:
            self._send(404, {"error": "run_not_found", "detail": str(exc)})
        except RunExpired as exc:
            self._send(410, {"error": "run_expired", "detail": str(exc)})
        except StaleRunVersion as exc:
            # 409 而不是 500：这是"你的改动基于旧世界"，调用方重读一次就能继续。
            # 报 500 会让调用方以为服务坏了，然后重试同一份旧世界 —— 无限循环。
            self._send(409, {"error": "stale_run_version", "detail": str(exc),
                             "_twin_hint": "重新 GET /admin/runs/<id>/state 再改"})
        except Exception as exc:  # noqa: BLE001 - 服务端不该因为一个坏请求整个炸掉
            self._send(500, {"error": "internal", "detail": f"{type(exc).__name__}: {exc}"})

    # ---- MCP ----

    def _mcp(self, method: str, body: bytes) -> None:
        """MCP 走 POST /mcp，JSON-RPC 2.0。

        延迟 import：mcp_server 需要 App 的类型，server 需要 McpEndpoint，
        顶部 import 会成环。这里放在方法里避免。
        """
        from .mcp_server import McpEndpoint

        if method != "POST":
            self._send(405, {"error": "method_not_allowed", "detail": "use POST /mcp"})
            return
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"Parse error: {exc}"}})
            return
        out = McpEndpoint(self.app).handle(payload)
        if out is None:
            # 通知：202 Accepted，无 body（协议要求）
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, out)

    # ---- admin ----
    def _admin(self, method: str, path: str, body: bytes) -> None:
        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid_json"})
                return

        parts = [p for p in path.strip("/").split("/") if p]

        # /admin/scenarios[...]
        if parts[:2] == ["admin", "scenarios"]:
            self._admin_scenarios(method, parts[2:], payload)
            return

        # /admin/runs[...]
        if parts[:2] == ["admin", "runs"]:
            self._admin_runs(method, parts[2:], payload)
            return

        self._send(404, {"error": "no such admin route", "path": path})

    def _admin_runs(self, method: str, rest: list[str], payload: dict[str, Any]) -> None:
        app = self.app

        if not rest:
            if method == "GET":
                self._send(200, app.list_runs())
                return
            if method == "POST":
                try:
                    self._send(201, app.provision(payload))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                return
            self._send(405, {"error": "method not allowed"})
            return

        run_id = rest[0]
        action = rest[1] if len(rest) > 1 else ""

        if len(rest) == 1:
            if method == "GET":
                self._send(200, app.get_run(run_id))
                return
            if method == "DELETE":
                self._send(200, app.teardown(run_id))
                return
            self._send(405, {"error": "method not allowed"})
            return

        if action == "reset" and method == "POST":
            self._send(200, app.reset(run_id))
        elif action == "extend" and method == "POST":
            self._send(200, app.extend(run_id, int(payload.get("ttl") or 600)))
        elif action == "lock" and method == "POST":
            self._send(200, app.lock(run_id))
        elif action == "state" and method == "GET":
            self._send(200, app.state(run_id))
        elif action == "stub-hits":
            if method == "GET":
                self._send(200, app.stub_hits(run_id, payload.get("provider")))
            elif method == "POST":
                self.app.store.clear_stub_hits(run_id)
                self._send(200, {"cleared": True})
            else:
                self._send(405, {"error": "method not allowed"})
        elif action == "actions" and method == "POST":
            self._send(200, app.act(run_id, payload.get("actions") or []))
        elif action == "grade" and method == "POST":
            self._send(200, app.grade(run_id, payload.get("spec")))
        elif action == "faults":
            # 故障注入：GET 看规则与命中数，POST 加一条，DELETE 清空。
            # 挂在 run 上而不是服务上，因为"这次重试题要坏一次"是 run 的剧本，
            # 服务级的规则会污染同时跑的其它 episode。
            run = app.store.get(run_id)
            if method == "GET":
                self._send(200, chaos.report(run))
            elif method == "POST":
                try:
                    fault = chaos.add_fault(run, payload)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                app.store.save(run)
                self._send(201, fault)
            elif method == "DELETE":
                chaos.clear(run, "faults")
                app.store.save(run)
                self._send(200, {"cleared": "faults"})
            else:
                self._send(405, {"error": "method not allowed"})
        elif action == "webhooks":
            # 事件订阅：注册 → 世界变了自动 POST → 投递记录可查。
            run = app.store.get(run_id)
            if method == "GET":
                self._send(200, webhooks.report(run))
            elif method == "POST":
                try:
                    hook = webhooks.register(run, payload)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                app.store.save(run)
                # 密钥**只在注册这一次**明文回：之后报告里只给前 8 位。
                self._send(201, hook)
            elif method == "DELETE":
                hook_id = str(payload.get("id") or (rest[2] if len(rest) > 2 else ""))
                removed = webhooks.remove(run, hook_id)
                app.store.save(run)
                self._send(200 if removed else 404, {"removed": removed})
            else:
                self._send(405, {"error": "method not allowed"})
        elif action == "rate-limit":
            run = app.store.get(run_id)
            if method == "GET":
                self._send(200, chaos.report(run))
            elif method == "POST":
                try:
                    rule = chaos.add_rate_limit(run, payload)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                app.store.save(run)
                self._send(201, rule)
            elif method == "DELETE":
                chaos.clear(run, "rate_limits")
                app.store.save(run)
                self._send(200, {"cleared": "rate_limits"})
            else:
                self._send(405, {"error": "method not allowed"})
        else:
            self._send(404, {"error": "no such run action", "action": action})

    def _admin_scenarios(self, method: str, rest: list[str], payload: dict[str, Any]) -> None:
        store = self.app.scenarios
        if not rest:
            if method == "GET":
                self._send(200, {"scenarios": [s.to_dict() for s in store.list()]})
                return
            if method == "POST":
                scenario = Scenario.from_dict(payload)
                store.save(scenario)
                self._send(201, scenario.to_dict())
                return
            self._send(405, {"error": "method not allowed"})
            return

        sid = rest[0]
        if method == "GET":
            s = store.get(sid)
            if s is None:
                self._send(404, {"error": "scenario_not_found", "id": sid})
                return
            self._send(200, s.to_dict())
        elif method == "DELETE":
            self._send(200, {"deleted": store.delete(sid), "id": sid})
        else:
            self._send(405, {"error": "method not allowed"})


def make_server(app: App, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(app: App, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    server = make_server(app, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
