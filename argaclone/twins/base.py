"""Twin 基类：路由、鉴权、stub 降级。

这里有一条故意反直觉的设计，抄自 Arga 的文档：**未实现的端点要破坏 schema**。
数组响应会被包成 `{"_twin_stub": true, "_twin_warning": ..., "data": [...]}`，
而不是直接返回数组。

理由：如果 stub 跟真响应长得一样，测试会静默通过，而"通过"是假的。破坏 schema
会让任何依赖真实结构的断言当场炸掉——把假货变成可见的失败，而不是无声的绿色。
这是整套东西里最值得搬走的一条纪律。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..store import Run


@dataclass
class Response:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        if isinstance(self.body, (bytes, bytearray)):
            return bytes(self.body)
        if self.body is None:
            return b""
        return json.dumps(self.body).encode()


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    run: Run
    run_id: str
    # 注入 Store，让 stub 计数能落到 run 上。显式字段比 __dict__ 里偷摸塞干净。
    store: Any = None

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return {}

    def header(self, name: str, default: str = "") -> str:
        key = name.lower()
        if key in self.headers:
            return self.headers[key]
        # 网关会把 header 归一成小写，但进程内的调用方（测试、MCP、SDK、twin 之间的
        # 互相调用）不经过网关。多这一层兜底，省掉"HTTP 通道能过、进程内 401"这类
        # 只在某一层出现的怪异失败 —— 它读起来像凭据问题，实际是大小写。
        for k, v in self.headers.items():
            if k.lower() == key:
                return v
        return default

    def query_one(self, name: str, default: str = "") -> str:
        vals = self.query.get(name)
        return vals[0] if vals else default


Handler = Callable[[Request, dict[str, str]], Response]


def bearer(req: "Request") -> str:
    """取出 Bearer 令牌；没有头、空值、非 Bearer 方案一律返回 `""`。

    这段解析原来在 8 个 twin 里各写一遍，而"空令牌放行"这个缺陷当年在 5 个 twin 上
    **同时**存在：每个实现都只判"头在不在"，没人试过 `Authorization: Bearer `（后面为空）。
    抽成一处之后，这条约束只有一处可能被改坏，测试也只需要盯一处。

    注意不要用 `header[7:]`：那个 7 是 `"bearer "` 的长度，硬编码之后任何方案名改动
    都会静默错位一格。`partition` 按第一个空格切，方案名比较不区分大小写。
    """
    header = req.header("authorization") or ""
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    template: str  # 用于 stub 日志的可读模板，如 "GET /v1/customers/{id}"


class Twin:
    """一个 SaaS 的仿真器。

    子类只需实现 `_build_routes()` 并把业务 handler 注册进去。
    dispatch 负责：路径匹配 → 404/stub → 调 handler。
    """

    name: str = "base"

    # 声明式"这些端点我不打算实现"：(method, 路径模式, 理由)。
    # 路径模式按 segment 逐段比，`*` 匹配任意一段（`/repos/*/*/actions/secrets`）。
    # 不用前缀是因为前缀太粗：`/repos/` 会把整个仓库 API 都吞成"边界"，
    # 于是一条真欠着的 `/repos/o/r/hooks` 会被洗成已经想清楚的不做。
    # 为什么需要它：stub 报表原来只有一张清单，"还没补的端点"和"想清楚不做的端点"
    # 同名。于是两类完全不同的东西被加在一起：前者是欠的工程债，后者是画好的边界
    # （比如"密钥明文不许进孪生环境"）。把它们混成一列，看报表的人会以为
    # 边界也是待办，然后去"补"一个本来就不该存在的东西。
    declared_stubs: tuple[tuple[str, str, str], ...] = ()

    # 第三类：**真实 provider 里不存在的端点**：(method 或 "*", 路径模式, 理由)。
    # 为什么需要它：agent 会发明端点（`POST /repos/o/r/issues/56`——真 GitHub 更新
    # issue 用 PATCH），也会把 A 家的路径打到 B 家的 Host 上（`/api/conversations.list`
    # 打 github）。对这些请求回 200 + `_twin_stub`，等于告诉它"改成功了"，
    # 而判据那边读到的是没改 —— 于是失分被记成"模型能力不足"，实际是我给了假成功。
    # 真实 API 对这些路径会 404，twin 照做才叫保真。
    absent_paths: tuple[tuple[str, str, str], ...] = ()

    def __init__(self) -> None:
        self.routes: list[Route] = []
        self._build_routes()

    # ---- 子类实现 ----

    def _build_routes(self) -> None:
        raise NotImplementedError

    def initial_state(self) -> dict[str, Any]:
        """该 provider 在一个空 run 里的初始状态。"""
        return {}

    # ---- 注册工具 ----

    def route(self, method: str, path: str, handler: Handler) -> None:
        """path 用 `{name}` 占位，转成正则捕获组。"""
        template = f"{method} {path}"
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path)
        self.routes.append(
            Route(
                method=method.upper(),
                pattern=re.compile("^" + regex + "$"),
                handler=handler,
                template=template,
            )
        )

    # ---- 分发 ----

    def dispatch(self, req: Request) -> Response:
        # 尾斜杠一律吃掉。真实 provider 对此是宽容的（GitHub 301 到无斜杠版本、
        # Slack 两种都认），而 twin 严格匹配会让 `GET /repos/o/r/contents/` 掉进 stub——
        # 实测 480+120 episodes 里有 8 次就是这样丢的：agent 拿到了假数据却以为是空目录。
        path = (req.path.rstrip("/") or "/") if len(req.path) > 1 else req.path

        # admin 面优先，避免被 provider 路由误吃
        if path.startswith("/admin"):
            # coverage 由基类统一提供：任何一个 twin 都自动能被问"你实现了什么"。
            # 放在这里而不是每个子类的 handle_admin 里，是因为漏掉一个 twin
            # 就会让保真报表悄悄少一行 —— 这种缺口最难发现。
            if path == "/admin/coverage":
                return self.coverage()
            # stub 报表同样由基类统一提供。原来 12 个 twin 里只有 6 个手写了它，
            # 另外 6 个（gmail/drive/calendar/hubspot/salesforce）问不到自己的 stub 命中 ——
            # 而"这个 twin 到底有多少请求掉进 stub"正是保真报表的分子分母。
            # 这种"漏一个就悄悄少一行"的东西必须放进基类，不能靠每个 twin 记得写。
            if path.startswith("/admin/stub-hits"):
                return self._admin_stub_hits(req, path)
            return self.handle_admin(req)

        for r in self.routes:
            if r.method != req.method.upper():
                continue
            m = r.pattern.match(path)
            if not m:
                continue
            return r.handler(req, m.groupdict())

        # 未命中的路径也要先过鉴权。
        # 这一条是契约测试逼出来的真缺陷：stripe 的 /v1/terminal/readers 未实现时，
        # 用**坏 token** 打过去会直接走到 stub 分支返回 200 —— 鉴权根本没跑。
        # 真实 SaaS 不会这样：未实现路径不该泄露"存不存在"，更不该给未授权方假数据。
        bad = self.authenticate(req)
        if bad is not None:
            return bad

        # 端点不存在（真实 provider 也没有）：必须 404，不能给假成功。
        # 放在鉴权之后：不泄露"这个路径存不存在"，也不让未授权方看到理由。
        endpoint = f"{req.method.upper()} {path}"
        hit = self.absent_reason(req)
        if hit is not None:
            return self.absent(req, hit[0], hit[1])

        # 没有任何路由命中：这是 stub，不是 404。
        # 真 SaaS 对不存在的路径回 404，但如果一个 endpoint 我们没实现，
        # 回 404 会让客户端以为"资源不存在"而走错分支。所以给 schema-valid 的假数据 + 显式标记。
        return self.stub(req, endpoint)

    def idempotency_conflict(self, message: str, detail: str) -> Response:
        """同一把幂等键配了不同的货时回什么。

        幂等本身归网关（覆盖全部 twin、且在 dispatch 之前判定），但**错误体形状**
        归各 provider —— Stripe 是 `{"error": {"type", "code", "message"}}`，
        Slack 是 `{"ok": false, "error"}`。让网关自己拼一份通用形状，
        就会出现"这条错误在任何 SDK 里都解析不出来"。
        """
        return Response(400, {"error": "idempotency_error", "message": message, "_twin_hint": detail})

    def authenticate(self, req: Request) -> Response | None:
        """未命中路由时的兜底鉴权。返回 None 表示放行。

        默认不鉴权（admin 面与无鉴权 twin）；有 token 概念的 twin 必须覆写它，
        否则 stub 分支会变成一条绕过鉴权的后门。
        """
        return None

    def _admin_stub_hits(self, req: Request, path: str) -> Response:
        """GET 出报表，POST 清空。两种后缀都认（`/admin/stub-hits` 与 `.../clear`），
        因为早期六个 twin 各写了其中一种，客户端已经两种都在用了。"""
        method = req.method.upper()
        if method == "GET" and path == "/admin/stub-hits":
            if req.store is None:
                return Response(503, {"error": "no store bound to this request"})
            return Response(200, req.store.stub_report(req.run_id, self.name))
        if method == "POST" and path in ("/admin/stub-hits", "/admin/stub-hits/clear"):
            if req.store is None:
                return Response(503, {"error": "no store bound to this request"})
            req.store.clear_stub_hits(req.run_id)
            return Response(200, {"cleared": True})
        return Response(
            404,
            {
                "error": "no such admin route",
                "path": path,
                "_twin_hint": "GET /admin/stub-hits 或 POST /admin/stub-hits/clear",
            },
        )

    def handle_admin(self, req: Request) -> Response:
        return Response(404, {"error": "no admin routes"})

    # ---- 覆盖度自述 ----

    def coverage(self) -> Response:
        """自述"我实现了哪些端点"。

        为什么需要这个：Arga 的 twin 除 GitHub 外都没有可量化的"我实现了多少"接口，
        于是它们的 `100% fidelity` 无法被证伪。把已实现端点清单暴露成一个端点，
        覆盖率的分母就不再依赖人工统计脚本，而是 twin 自己报出来的东西。

        ⚠️ 名字必须叫对：这是 **observed coverage（被观测到的端点覆盖）**，
        不是 fidelity（保真度）。保真度还牵着状态语义、错误形状、时序，
        端点清单只是它的一个下界。报告里混用两个词就是重复 Arga 的错。
        """
        implemented = sorted({self._normalize(r.template) for r in self.routes})
        by_method: dict[str, int] = {}
        for r in self.routes:
            by_method[r.method] = by_method.get(r.method, 0) + 1
        return Response(
            200,
            {
                "provider": self.name,
                "implemented_routes": len(self.routes),
                "unique_endpoints": len(implemented),
                "by_method": dict(sorted(by_method.items())),
                "endpoints": implemented,
                # 声明的边界也一并自述：报表拿它把 stub 命中拆成两列，
                # 而不是靠读源码猜哪些是故意的。
                "absent_paths": [
                    {"method": m.upper(), "prefix": prefix, "reason": reason}
                    for m, prefix, reason in self.absent_paths
                ],
                "declared_stubs": [
                    {"method": m.upper(), "prefix": prefix, "reason": reason}
                    for m, prefix, reason in self.declared_stubs
                ],
            },
        )

    # ---- stub ----

    def declared_stub_reason(self, req: Request) -> str | None:
        """这个请求是否命中一条"声明过不实现"的端点。命中返回理由。

        按**原始路径前缀**匹配，不按归一化模板：声明是写给读代码的人看的
        （`/repos/` 后面的 owner/repo 对分类没有意义），而且归一化会把
        owner 换成 `{id}`，两边永远对不上。
        """
        method, path = req.method.upper(), req.path
        for declared_method, pattern, reason in self.declared_stubs:
            if method == declared_method.upper() and _segments_match(pattern, path):
                return reason
        return None

    def absent_reason(self, req: Request) -> tuple[str, str] | None:
        """这个请求是否命中"真实 API 里不存在"的路径。命中返回理由。

        与 `declared_stub_reason` 共用同一套段匹配；区别只在处置：
        声明过的端点仍然回 stub（数据是假的，但端点是真的），
        缺席的端点必须回 404 —— 因为真 API 就没有它。
        """
        method, path = req.method.upper(), req.path
        for rule_method, pattern, reason in self.absent_paths:
            if rule_method.upper() in ("*", method) and _segments_match(pattern, path):
                return pattern, reason
        return None

    def absent(self, req: Request, pattern: str, reason: str) -> Response:
        """按真实 provider 的形状说"没有"。

        子类可以覆写它来换错误体（GitHub 的 `{"message": "Not Found"}`、
        Slack 的 `{"ok": false, "error": "unknown_method"}` 不是一回事）。
        但默认实现必须自带可检出标记：没有标记的话，报表就分不出
        "agent 编了端点"与"我的路由漏了"。
        """
        # 计数按**规则模式**归并，不按具体路径：agent 编出来的端点每次 owner/repo
        # 都不一样，按具体路径记会得到一长串各 1 次的条目，看不出"它反复在试同一类错"。
        label = f"{req.method.upper()} {pattern}".replace("/*", "/*")
        if req.store is not None:
            req.store.record_absent(req.run_id, self.name, label)
        return Response(
            404,
            {
                "message": "Not Found",
                "_twin_absent": True,
                "_twin_reason": reason,
                "_twin_hint": "这个端点在你的 provider 里不存在；检查 method 与 Host。",
            },
            {"X-Twin-Absent": "true", "X-Twin-Provider": self.name},
        )

    def stub(self, req: Request, endpoint: str) -> Response:
        """未实现端点：返回 schema 合法的空数据，但必须留下可检测的痕迹。

        如果这条端点属于 `declared_stubs`，响应里额外带 `_twin_declared` 与
        `_twin_reason`：调用方拿到的是"我们决定不仿真这个"，而不是"孪生还没做完"。
        这两句话对 agent 的处置完全不同 —— 前者应当走真服务或换路，后者值得重试。
        """
        reason = self.declared_stub_reason(req)
        if req.store is not None:
            req.store.record_stub(
                req.run_id, self.name, self._normalize(endpoint), planned=reason is not None
            )

        warning = (
            f"This endpoint ({endpoint}) is stubbed and returns mock data. "
            "Do not rely on this response in production tests."
        )
        # 故意破坏 schema：数组包成对象，对象加两个下划线前缀字段。
        payload: dict[str, Any] = {
            "_twin_stub": True,
            "_twin_warning": warning,
            "data": [],
        }
        headers = {"X-Twin-Stub": "true", "X-Twin-Provider": self.name}
        if reason is not None:
            payload["_twin_declared"] = True
            payload["_twin_reason"] = reason
            headers["X-Twin-Declared"] = "true"
        return Response(status=200, body=payload, headers=headers)

    @staticmethod
    def _normalize(endpoint: str) -> str:
        """把 /v1/customers/cus_abc123 归一成 /v1/customers/{id}，让统计能聚合。

        同时把路由模板里的占位符（`{cid}` / `{id}` / `{number}`）也归成 `{id}`，
        否则 coverage 报 `GET /v1/customers/{cid}`、stub 报 `GET /v1/customers/{id}`，
        两边永远对不上，覆盖率算出来全是 0。
        """
        method, _, path = endpoint.partition(" ")
        parts = []
        for seg in path.split("/"):
            if not seg:
                parts.append(seg)
                continue
            # 路由模板的占位符与真实 id 用同一套记号
            if re.match(r"^\{\w+\}$", seg):
                parts.append("{id}")
                continue
            # 长 id（含下划线前缀，或 >= 12 位且**含数字**的混合串）视为标识符。
            # 必须要求含数字：纯字母的长单词（collaborators 正好 13 位）不是 id，
            # 早期版本只看长度，把 "collaborators" 也归一成 {id}，
            # 导致 stub 报表把两个完全不同的端点合并成一条。
            if re.match(r"^(cus|pi|ch|re|prod|price|sub|in|pm|evt)_", seg) or (
                re.match(r"^[0-9A-Za-z]{12,}$", seg) and any(c.isdigit() for c in seg)
            ):
                parts.append("{id}")
            else:
                parts.append(seg)
        return f"{method} {'/'.join(parts)}"


def _segments_match(pattern: str, path: str) -> bool:
    """逐段比较：`*` 匹配任意一段，结尾的 `**` 匹配剩余任意段。

    为什么需要 `**`：前缀类规则（"这个 provider 不会有 /api/... 路径"）必须能盖住
    任意深度。第一版只写 `*`（一段），结果 `GET /api/v1/customers/search` 四段
    匹配不上两段的规则，静默掉进 stub —— 又是一次"规则写了但没生效"。
    但**不用**前缀匹配：`/repos/` 前缀会把 `/repos/o/r/hooks` 一起吞掉，
    那是真端点。`*` 与 `**` 的差别就是"精确到段"与"从这里往下都算"。
    """
    want = pattern.strip("/").split("/")
    got = path.strip("/").split("/")
    if want and want[-1] == "**":
        head = want[:-1]
        if len(got) < len(head):
            return False
        return all(w == "*" or w == g for w, g in zip(head, got))
    if len(want) != len(got):
        return False
    return all(w == "*" or w == g for w, g in zip(want, got))


class TwinRegistry:
    """provider 名 → Twin 实例。twin 是无状态的，run 的状态在 Store 里。"""

    def __init__(self) -> None:
        self._twins: dict[str, Twin] = {}

    def register(self, twin: Twin) -> None:
        self._twins[twin.name] = twin

    def get(self, name: str) -> Twin:
        return self._twins[name]

    def has(self, name: str) -> bool:
        return name in self._twins

    def names(self) -> list[str]:
        return sorted(self._twins)
