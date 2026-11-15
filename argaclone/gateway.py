"""网关：把 Host 解析成 (provider, run_id)，再交给对应 twin。

为什么用 Host 而不是路径前缀（这是复刻 Arga 时最该抄的架构决定）：
官方 SDK 只认 base URL 的 host。把租户编进 hostname，客户端就真的能"只改一行
.env"接入，不需要改写任何 URL 拼接逻辑。用路径前缀的话，所有 SDK 都会在拼
`/v1/customers` 时把前缀吃掉。

本地调试时 Host 不方便设置，所以额外支持 `X-Twin-Host` 头和 `__twin` 路径前缀。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from . import chaos, webhooks
from .store import RunExpired, RunNotFound, StaleRunVersion, Store
from .twins.base import Request, Response, TwinRegistry


@dataclass
class Target:
    provider: str
    run_id: str
    path: str
    query: dict[str, list[str]]


class Gateway:
    def __init__(self, store: Store, registry: TwinRegistry) -> None:
        self.store = store
        self.registry = registry

    # ---------- 解析 ----------

    def resolve(self, host: str, raw_path: str, headers: dict[str, str]) -> Target:
        parsed = urlparse(raw_path)
        path = parsed.path or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)

        # 1) X-Twin-Host: slack.abc123.twins.local
        override = headers.get("x-twin-host")
        if override:
            t = self._from_host(override)
            if t:
                return Target(t[0], t[1], path, query)

        # 2) 路径前缀 /__twin/<provider>/<run_id>/<rest>
        m = re.match(r"^/__twin/([^/]+)/([^/]+)(/.*)?$", path)
        if m:
            rest = m.group(3) or "/"
            return Target(m.group(1), m.group(2), rest, query)

        # 3) 标准 Host
        host_only = host.split(":")[0]
        t = self._from_host(host_only)
        if t:
            return Target(t[0], t[1], path, query)

        raise RunNotFound(f"cannot resolve twin from host={host!r} path={path!r}")

    def is_twin_target(self, host: str, headers: dict[str, str]) -> bool:
        """这个请求是打给某个 twin 的，还是打给服务本身的？

        只给路由用：`/admin/coverage` 从服务面看是"没有这个 admin 路由"，
        从 twin 面看是"这个 twin 的覆盖度"。Host 决定它是哪一个。
        """
        if (headers or {}).get("x-twin-host"):
            return True
        return self._from_host((host or "").split(":")[0]) is not None

    def _from_host(self, host: str) -> tuple[str, str] | None:
        host = host.split(":")[0]
        parts = host.split(".")
        # 形如 <provider>.<run_id>.twins.local（允许 provider 前面还有标签）
        for i in range(len(parts) - 3):
            if parts[i + 2] == "twins" and parts[i + 3] == "local":
                return parts[i], parts[i + 1]
        # 两标签简写只在第一段是已知 provider 时才成立。
        # 不加这个条件，example.com 这种"域名配错了"会被报成 unknown_twin，
        # 把真正的问题（base URL 没指过来）掩盖成"provider 拼错了"。
        if len(parts) == 2 and parts[0] and parts[1] and self.registry.has(parts[0]):
            return parts[0], parts[1]
        return None

    def _strict_credential_check(
        self, run, provider: str, headers: dict[str, str]
    ) -> Response | None:
        """strict 模式下校验 token 逐字等于 provision 时下发的值。返回 None 表示放行。

        为什么默认关：twin 不维护 token 表时，任何**形状合法**的字符串都会被放行
        （`Bearer definitely-not-valid` 在 jira/linear/notion 上能过）。这在手工
        调试和单元测试里很方便，但它意味着"孪生校验了凭据"这句话是假的。
        打开以后这句话变真：值不对就是 401，与真实 SaaS 一致。

        为什么没带 token 时不在这里拦：401 的**错误体形状**也是保真的一部分
        （Salesforce 回数组、Jira 回 errorMessages），交给 provider 自己给。
        """
        if not run.extras.get("strict_tokens"):
            return None

        raw = headers.get("authorization", "")
        token = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if not token:
            return None

        issued = (run.extras.get("credentials") or {}).get(provider)
        if not issued or token == issued:
            return None

        return Response(
            401,
            {
                "error": "invalid_credential",
                "provider": provider,
                "run_id": run.run_id,
                "_twin_hint": (
                    "this run has strict_tokens on: only the credential issued by "
                    "provision is accepted"
                ),
            },
        )

    # ---------- 处理 ----------

    def handle(
        self,
        method: str,
        host: str,
        raw_path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> Response:
        # 统一小写化 header 名。HTTP 头本来就不区分大小写，但这个入口被
        # HTTP handler、SDK、MCP、测试四条通道共用，它们的 dict 大小写各不相同。
        # 不归一就会出现"HTTP 通道能过、SDK 通道 401"这种只在某条通道上复现的怪事 ——
        # 而 401 会被误读成凭据问题，浪费的是排查时间。
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        try:
            target = self.resolve(host, raw_path, headers)
        except RunNotFound as exc:
            return Response(404, {"error": "unresolvable_target", "detail": str(exc)})

        if not self.registry.has(target.provider):
            return Response(
                404,
                {
                    "error": "unknown_twin",
                    "provider": target.provider,
                    "available": self.registry.names(),
                },
            )

        try:
            run = self.store.get(target.run_id)
        except RunNotFound:
            return Response(404, {"error": "run_not_found", "run_id": target.run_id})
        except RunExpired:
            return Response(
                410,
                {
                    "error": "run_expired",
                    "run_id": target.run_id,
                    "_twin_hint": "provision a new run, or reset with a longer TTL",
                },
            )

        if target.provider not in run.twins:
            return Response(
                404,
                {
                    "error": "twin_not_provisioned",
                    "provider": target.provider,
                    "provisioned": run.twins,
                },
            )

        # 严格凭据模式：只认本次 run 签发的那一个 token。
        # 放在 twin 之前，是因为这属于"run 的访问控制"而不是"provider 的协议细节"——
        # 12 个 twin 各写一遍同样的比对，必然漏一个。
        denied = self._strict_credential_check(run, target.provider, headers)
        if denied is not None:
            return denied

        twin = self.registry.get(target.provider)
        run.state.setdefault(target.provider, twin.initial_state())

        upper = method.upper()
        idem = self._idem_key(headers, upper)
        fingerprint = self._fingerprint(upper, target.path, body)
        # 幂等键在 dispatch 之前判定：命中就直接重放，世界一动不动。
        replayed = self._replay(run, twin, target.provider, idem, fingerprint, upper, target.path)
        if replayed is not None:
            return replayed

        if run.locked and not headers.get("authorization") and not target.path.startswith("/admin"):
            return Response(403, {"error": "run_locked", "_twin_hint": "this run is public-access-locked"})

        # 故障注入与限流：插在 twin 之前，因为这是"环境的行为"不是"provider 的
        # 协议"。放在凭据校验之后 —— 认证失败是调用方的问题，不该被 500 盖住，
        # 否则调试时会把"token 错了"读成"服务在抽风"。
        chaos_resp = chaos.guard(run, target.provider, method.upper(), target.path)
        if chaos_resp is not None:
            if (conflict := self._save(run)) is not None:
                return conflict
            return chaos_resp

        req = Request(
            method=upper,
            path=target.path,
            query=target.query,
            headers=headers,
            body=body,
            run=run,
            run_id=run.run_id,
            store=self.store,
        )
        try:
            resp = twin.dispatch(req)
        except StaleRunVersion as exc:
            # twin 内部（stub 计数、seed 落盘）也可能写盘，同样可能撞版本。
            # 在同一个地方收口，调用方看到的语义才一致。
            return self._conflict(exc)
        self._remember(run, target.provider, idem, fingerprint, upper, target.path, resp)
        # 到这里世界已经变了（twin 的写是在 dispatch 里做的）。"已提交但响应失败"
        # 必须在此时才生效：早一步就变成"根本没提交"，晚一步（写盘之后）就变成
        # "响应成功但状态没存" —— 两种都不是要测的那个事故。
        resp = chaos.after_response(run, target.provider, method.upper(), target.path, resp)
        # 事件投递：只在"世界真的变了"时发（写方法 + 2xx）。失败/403/404 都算
        # 没发生变更，发通知等于告诉订阅方一件没发生的事。
        # 同步投递、有超时与重试上限：异步会把"通知丢了"变成无法复现的偶发问题。
        # "世界变了"不等于"调用方看到成功"：commit_then_fail 下这次写已经生效，
        # 接收方理应收到事件，而调用方拿到的是 500。这个不对齐正是要测的东西。
        committed = (
            200 <= resp.status < 300
            or resp.headers.get("X-Twin-Fault") == "commit_then_fail"
        )
        if method.upper() not in ("GET", "HEAD") and committed:
            webhooks.deliver(run, target.provider, method.upper(), target.path, req.json())
        # 写请求改完世界就落盘（配了 directory 才有动作）。读请求不落盘：
        # GET 除了 stub 计数以外不改状态，而 stub 计数在 record_stub 里已经存过。
        if method.upper() not in ("GET", "HEAD"):
            if (conflict := self._save(run)) is not None:
                return conflict
        return resp

    # ---------- 幂等键 ----------
    #
    # 为什么 twin 需要它：`commit_then_fail` 之后调用方看到 500，重试是**正确行为**，
    # 但没有幂等键的重试会把同一件事做两遍。所以"重试"这件事的安全与否，
    # 取决于服务端认不认幂等键 —— 这不是客户端的锅，是环境的性质。
    #
    # 只对写方法生效，且**缓存的是 twin 自己那份响应**（在 chaos 遮成 500 之前），
    # 这样"提交成功但响应丢了"的重试会拿回第一次的 200，而不是再来一遍。

    @staticmethod
    def _idem_key(headers: dict[str, str], method: str) -> str:
        if method in ("GET", "HEAD"):
            return ""
        return (headers.get("idempotency-key") or "").strip()

    @staticmethod
    def _fingerprint(method: str, path: str, body: bytes) -> str:
        return hashlib.sha256(method.encode() + b"\n" + path.encode() + b"\n" + (body or b"")).hexdigest()[:32]

    def _replay(self, run, twin, provider: str, key: str, fingerprint: str, method: str, path: str) -> Response | None:
        if not key:
            return None
        table = (run.extras.get("idempotency") or {}).get(provider) or {}
        seen = table.get(key)
        if not seen:
            return None
        if seen["fingerprint"] != fingerprint:
            # 同一把钥匙配不同的货，真实 Stripe 也是拒的。静默返回旧响应会让
            # 调用方以为新请求生效了 —— 那是更难查的一类事故。
            return twin.idempotency_conflict(
                "Keys for idempotent requests can only be used with the same parameters "
                "they were first used with.",
                f"key 首次用于 {seen['method']} {seen['path']}，本次是 {method} {path}",
            )
        cached = seen
        return Response(
            cached["status"],
            cached["body"],
            {**cached.get("headers", {}), "Idempotent-Replayed": "true"},
        )

    def _remember(self, run, provider: str, key: str, fingerprint: str, method: str, path: str, resp: Response) -> None:
        if not key:
            return
        # 只记住"有结论"的响应：2xx 与 4xx 都是服务端给出的答复，重放它是对的；
        # 5xx 不该被记住 —— 那正是"再来一次可能就好了"的情形，缓存 500 会把
        # 一次瞬时故障变成永久故障。
        if not (200 <= resp.status < 500):
            return
        table = run.extras.setdefault("idempotency", {}).setdefault(provider, {})
        table[key] = {
            "fingerprint": fingerprint,
            "method": method,
            "path": path,
            "status": resp.status,
            "body": resp.body,
            "headers": {k: v for k, v in (resp.headers or {}).items() if k.lower() != "idempotent-replayed"},
        }

    def _save(self, run) -> Response | None:
        """写盘失败时把 409 回给调用方，而不是让异常穿到 HTTP 层当 500。

        为什么回 409 而不是重试一次：重试意味着"我替调用方决定它的改动可以
        基于旧世界"——那正是要防的事。调用方自己重读、自己重放，才是可解释的。
        """
        try:
            self.store.save(run)
        except StaleRunVersion as exc:
            return self._conflict(exc)
        return None

    @staticmethod
    def _conflict(exc: StaleRunVersion) -> Response:
        return Response(409, {"error": "stale_run_version", "detail": str(exc),
                              "_twin_hint": "重新读取 run 后再改"})
