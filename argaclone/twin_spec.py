"""Twin 工厂：把"一个 SaaS 的 REST 面"从手写代码变成声明。

为什么需要工厂：手写 33 个 twin 是 33 倍线性劳动，而线性劳动追不平一个做了
一年的团队。工厂不省"想清楚这个 SaaS 怎么工作"的时间（那部分必须人做），
它省的是**每次都要重写一遍的骨架**：路由注册、id 生成、必填校验、分页游标、
错误形状、归属过滤、created/updated 时间戳。

工厂做对了的标志只有一条：新增 twin 的 diff 里**不出现手写的 CRUD 样板**。
所以 `tests/test_factory.py` 会直接检查 jira/notion 的源码里没有
`def list_`/`def create_` 这类逐资源的重复实现。

三个可组合件：
  RestResource  声明一个资源族（list/create/get/update/delete 自动生成）
  ScopeFamily   声明式 scope 继承（父授子），一个闭包搞定
  Pager         分页风格（cursor / offset / none），三种主流形状

刻意不做的：GraphQL。Linear 那种 `query { ... }` 不是"资源 CRUD"能描述的，
硬塞进工厂会得到一个只能跑 demo 的假解析器。它的 dispatch 自己写，
但错误形状、scope、分页仍然复用这里的件。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .twins.base import Request, Response, Twin


def now() -> int:
    return int(time.time())


# ------------------------------------------------------------------ 错误形状


@dataclass
class ErrorShape:
    """provider 的错误信封。

    为什么值得单独抽出来：错误形状是最容易被"差不多就行"糊过去的一层，
    而它恰恰是 agent 判断"该重试还是该停手"的唯一依据。一个返回 200 的失败
    会让 agent 一路做下去。Jira 是 `{"errorMessages":[...],"errors":{}}`，
    Notion 是 `{"object":"error","code":...,"message":...}`，
    Linear 是 GraphQL 的 `{"errors":[{"message","extensions":{"type"}}]}`。
    """

    not_found: Callable[[str, str], Response]
    bad_request: Callable[[str, str | None], Response]
    forbidden: Callable[[str], Response]
    unauthorized: Callable[[str], Response]


def jira_errors() -> ErrorShape:
    def not_found(kind: str, ident: str) -> Response:
        return Response(
            404,
            {"errorMessages": [f"Issue Does Not Exist or You Do Not Have Permission to See It"],
             "errors": {}},
        )

    def bad_request(detail: str, fld: str | None = None) -> Response:
        return Response(400, {"errorMessages": [], "errors": {fld or "": detail}})

    def forbidden(detail: str) -> Response:
        return Response(403, {"errorMessages": [detail], "errors": {}})

    def unauthorized(detail: str) -> Response:
        return Response(401, {"errorMessages": [detail], "errors": {}})

    return ErrorShape(not_found, bad_request, forbidden, unauthorized)


def notion_errors() -> ErrorShape:
    def box(status: int, code: str, message: str) -> Response:
        return Response(status, {"object": "error", "status": status, "code": code, "message": message})

    return ErrorShape(
        not_found=lambda kind, ident: box(404, "object_not_found", f"Could not find {kind} with ID: {ident}"),
        bad_request=lambda detail, fld=None: box(400, "validation_error", detail),
        forbidden=lambda detail: box(403, "restricted_resource", detail),
        unauthorized=lambda detail: box(401, "unauthorized", detail),
    )


# ------------------------------------------------------------------ scope

@dataclass
class ScopeFamily:
    """声明式 scope 继承。

    `parents` 里写"A 授 B"，expand 做传递闭包。Jira 的实测行为：
    `write:jira-work` 授 `read:jira-work`；`manage:jira-project` 授 `read:jira-work`；
    `manage:jira-configuration` 授 `read:jira-work` + `manage:jira-project`。
    手写 if-else 会在加第三个 scope 时出错，闭包不会。
    """

    parents: dict[str, list[str]] = field(default_factory=dict)

    def expand(self, granted: list[str]) -> set[str]:
        if "*" in granted:
            return {"*"}
        out: set[str] = set()
        stack = list(granted)
        while stack:
            s = stack.pop()
            if s in out:
                continue
            out.add(s)
            stack.extend(self.parents.get(s, []))
        return out

    def allows(self, granted: list[str], needed: str) -> bool:
        eff = self.expand(granted)
        return "*" in eff or needed in eff


# ------------------------------------------------------------------ 分页

@dataclass
class Pager:
    """三种主流分页形状。形状用错，客户端会拿到空列表然后以为"没有数据"。"""

    style: str = "cursor"  # cursor | offset | after | none
    page_size: int = 50

    def apply(self, req: Request, items: list[dict], *, key: str = "results") -> dict:
        if self.style == "none":
            return {key: items}
        if self.style == "after":
            # HubSpot：游标藏在 `paging.next.after`，且**没有 has_more**——
            # 客户端只能靠"有没有 paging 键"判断还有没有下一页。
            # 用 cursor 那套 `next_cursor`/`has_more` 回它，代码会一直读到第一页。
            cursor = req.query_one("after", "")
            start = int(cursor) if cursor.isdigit() else 0
            size = min(int(req.query_one("limit", str(self.page_size)) or self.page_size), 100)
            body: dict[str, Any] = {key: items[start : start + size]}
            if start + size < len(items):
                body["paging"] = {"next": {"after": str(start + size), "link": ""}}
            return body
        if self.style == "offset":
            start = int(req.query_one("startAt", "0") or 0)
            size = int(req.query_one("maxResults", str(self.page_size)) or self.page_size)
            window = items[start : start + size]
            return {
                "startAt": start,
                "maxResults": size,
                "total": len(items),
                "isLast": start + size >= len(items),
                key: window,
            }
        # cursor：游标就是下一页起点在排序后列表里的位置，够确定、够可断言
        cursor = req.query_one("cursor", "")
        start = int(cursor) if cursor.isdigit() else 0
        size = int(req.query_one("limit", str(self.page_size)) or self.page_size)
        window = items[start : start + size]
        nxt = str(start + size) if start + size < len(items) else ""
        return {
            key: window,
            "next_cursor": nxt,
            "has_more": bool(nxt),
        }


# ------------------------------------------------------------------ 资源

@dataclass
class Field:
    name: str
    default: Any = None
    required: bool = False
    readonly: bool = False


@dataclass
class Resource:
    """一个资源族的声明。

    `collection` / `item` 用 `{name}` 占位；`parent_keys` 里的名字会从路径里取出来，
    既用于归属过滤（list 只回本 parent 下的），也用于写进实体（`{"project": "ABC"}`）。
    """

    name: str
    collection: str
    item: str
    id_field: str = "id"
    id_style: str = "prefix"  # prefix | increment | uuid
    id_prefix: str = ""
    parent_keys: tuple[str, ...] = ()
    fields: list[Field] = field(default_factory=list)
    sort_key: str = "created"
    pager_style: str = "cursor"
    hooks: dict[str, Callable] = field(default_factory=dict)
    """钩子：normalize_in（写入前整理字段，如 ADF 转换）、decorate（读出前补派生字段）。"""


class SpecTwin(Twin):
    """用声明建起来的 twin。子类只写"这个 SaaS 特有的事"。"""

    name = "spec"
    errors: ErrorShape

    def __init__(self) -> None:
        self.resources: list[Resource] = []
        self._by_name: dict[str, Resource] = {}
        super().__init__()

    # ---- 声明 ----

    def declare(self, resource: Resource) -> Resource:
        self.resources.append(resource)
        self._by_name[resource.name] = resource
        return resource

    def mount(self) -> None:
        """把已声明的资源挂成路由。由子类的 `_build_routes` 调一次。"""
        for r in self.resources:
            self.route("GET", r.collection, self._make_list(r))
            self.route("POST", r.collection, self._make_create(r))
            self.route("GET", r.item, self._make_get(r))
            self.route("PATCH", r.item, self._make_update(r))
            self.route("DELETE", r.item, self._make_delete(r))

    # ---- 状态访问 ----

    def bucket(self, req: Request, resource: Resource) -> dict[str, Any]:
        state = req.run.state.setdefault(self.name, {})
        return state.setdefault(resource.name, {})

    def new_id(
        self,
        resource: Resource,
        bucket: dict[str, Any],
        params: dict[str, str] | None = None,
        entity: dict[str, Any] | None = None,
    ) -> str | int:
        if resource.id_style == "increment":
            return len(bucket) + 1
        if resource.id_style == "uuid":
            return str(uuid.uuid4())
        if resource.id_style == "jira_key":
            # Jira 的 issue 用 `PROJ-123` 寻址，序号在项目内自增。
            # 这个形状没法用前缀+随机，所以单列一种 style，而不是让每个 twin 重写 create。
            # 优先从实体取：create 走的是 collection 路由，路径里没有 project，
            # 只有 body 里那个 `{"fields":{"project":{"key":"OPS"}}}` 才知道归属。
            project = (
                (entity or {}).get("project")
                or (params or {}).get("project")
                or "GEN"
            )
            n = sum(1 for e in bucket.values() if e.get("project") == project) + 1
            return f"{project}-{n}"
        return f"{resource.id_prefix}{uuid.uuid4().hex[:12]}"

    # ---- 默认 handler ----

    def _scoped(self, resource: Resource, params: dict[str, str], entity: dict) -> bool:
        return all(entity.get(k) == v for k, v in params.items() if k in resource.parent_keys)

    def _make_list(self, r: Resource):
        def handler(req: Request, params: dict[str, str]) -> Response:
            bucket = self.bucket(req, r)
            items = [e for e in bucket.values() if self._scoped(r, params, e)]
            items.sort(key=lambda e: e.get(r.sort_key) or 0)
            # 类上的 pager 带着这个 SaaS 的 page_size（HubSpot 是 10，Jira 是 50）。
            # 每次 new 一个默认 Pager 会把 page_size 打回 50，分页断言永远测不到边界。
            pager = self.pager if getattr(self, "pager", None) and self.pager.style == r.pager_style else Pager(r.pager_style)
            body = pager.apply(req, items)
            decorate = r.hooks.get("decorate")
            if decorate:
                body = {k: ([decorate(self, req, e) for e in v] if isinstance(v, list) else v) for k, v in body.items()}
            return Response(200, body)

        return handler

    def _make_create(self, r: Resource):
        def handler(req: Request, params: dict[str, str]) -> Response:
            body = req.json()
            normalize = r.hooks.get("normalize_in")
            if normalize:
                body = normalize(self, req, body)
            missing = [f.name for f in r.fields if f.required and not body.get(f.name)]
            if missing:
                return self.errors.bad_request(
                    f"missing required fields: {', '.join(missing)}", missing[0]
                )
            bucket = self.bucket(req, r)
            entity: dict[str, Any] = {}
            for f in r.fields:
                if f.readonly:
                    continue
                entity[f.name] = body.get(f.name, f.default)
            for k in r.parent_keys:
                if k in params:
                    entity[k] = params[k]
            entity[r.id_field] = self.new_id(r, bucket, params, entity)
            entity.setdefault("created", now())
            entity["updated"] = entity["created"]
            key = str(entity[r.id_field])
            bucket[key] = entity
            decorate = r.hooks.get("decorate")
            out = decorate(self, req, entity) if decorate else entity
            return Response(201, out)

        return handler

    def _make_get(self, r: Resource):
        def handler(req: Request, params: dict[str, str]) -> Response:
            bucket = self.bucket(req, r)
            entity = bucket.get(str(params["id"]))
            if entity is None or not self._scoped(r, params, entity):
                return self.errors.not_found(r.name, str(params["id"]))
            decorate = r.hooks.get("decorate")
            return Response(200, decorate(self, req, entity) if decorate else entity)

        return handler

    def _make_update(self, r: Resource):
        def handler(req: Request, params: dict[str, str]) -> Response:
            bucket = self.bucket(req, r)
            entity = bucket.get(str(params["id"]))
            if entity is None or not self._scoped(r, params, entity):
                return self.errors.not_found(r.name, str(params["id"]))
            body = req.json()
            normalize = r.hooks.get("normalize_in")
            if normalize:
                body = normalize(self, req, body)
            for f in r.fields:
                if f.readonly or f.name not in body:
                    continue
                entity[f.name] = body[f.name]
            entity["updated"] = now()
            decorate = r.hooks.get("decorate")
            return Response(200, decorate(self, req, entity) if decorate else entity)

        return handler

    def _make_delete(self, r: Resource):
        def handler(req: Request, params: dict[str, str]) -> Response:
            bucket = self.bucket(req, r)
            key = str(params["id"])
            if key not in bucket:
                return self.errors.not_found(r.name, key)
            del bucket[key]
            return Response(204, None)

        return handler


# ------------------------------------------------------------------ 小工具

def adf(text: Any) -> dict:
    """把纯字符串包成最小 ADF 文档。

    Jira 的 comment/description 是 Atlassian Document Format，不是字符串。
    真实 Jira 对纯字符串输入会自动包一层（这是它的宽容行为），所以 twin 也照做 ——
    否则照文档写 `{"body": "some text"}` 的客户端会被判 400，而那是真 Jira 接受的东西。
    """
    if isinstance(text, dict) and text.get("type") == "doc":
        return text
    # None 必须变成空文本：`str(None)` 会写出字面量 "None"，在 issue 描述里
    # 就是一段真实的垃圾文本，而且 agent 会把它当成"作者写了 None"
    body = "" if text is None else str(text)
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
    }


def adf_text(node: Any) -> str:
    """从 ADF 里抽纯文本，用于断言与回读。"""
    if not isinstance(node, dict):
        return str(node or "")
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(adf_text(c) for c in node.get("content") or [])


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()
