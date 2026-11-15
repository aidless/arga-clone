"""HubSpot CRM v3 twin（用工厂声明出来的，不含手写 CRUD）。

这个 twin 的价值不在"又一个 SaaS"，而在它**逼工厂长出第四种分页形状**：
HubSpot 用 `paging.next.after`，既没有 `has_more` 也没有 `next_cursor`。
照 cursor 那套回它，客户端永远停在第一页 —— 而这种错不会报错，只会让 agent
以为"这个 CRM 里只有 3 个联系人"，然后基于这个错误事实做完整套决策。

另一层要复刻的是**对象是壳、数据在 properties 里**：
`{"id": "123", "properties": {"email": "..."}, "createdAt", "updatedAt"}`。
把字段摊平在顶层会让所有按文档写的客户端拿到空值（它们读 `properties.email`）。
"""

from __future__ import annotations

import time
from typing import Any

from ..twin_spec import ErrorShape, Field, Pager, Resource, SpecTwin
from .base import Response, bearer

HUBSPOT_ERRORS = ErrorShape(
    not_found=lambda kind, ident: _err(404, f"{kind} object {ident} not found", "OBJECT_NOT_FOUND"),
    bad_request=lambda detail, fld=None: _err(400, detail, "VALIDATION_ERROR"),
    forbidden=lambda detail: _err(403, detail, "MISSING_SCOPES"),
    unauthorized=lambda detail: _err(401, detail, "INVALID_AUTHENTICATION"),
)


def _err(status: int, message: str, category: str) -> Response:
    return Response(
        status,
        {
            "status": "error",
            "message": message,
            "category": category,
            "correlationId": f"twin-{int(time.time() * 1000)}",
        },
    )


def _iso(ts: Any) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(int(ts or 0)))


def _wrap(twin, req, entity: dict) -> dict:
    """把扁平存储包成 HubSpot 的对象壳。

    `properties` 里只放业务字段；`id`/`created`/`updated` 留在顶层。
    另外支持 `?properties=email,firstname` 的投影 —— 真 HubSpot 只回你要的字段，
    全回会让"我只要 email"的客户端误以为自己拿到了全部属性。
    """
    props = {k: v for k, v in entity.items() if k not in ("id", "created", "updated") and v is not None}
    want = (req.query_one("properties") or "").strip()
    if want:
        keep = [p.strip() for p in want.split(",") if p.strip()]
        props = {k: v for k, v in props.items() if k in keep}
    return {
        "id": str(entity["id"]),
        "properties": props,
        "createdAt": _iso(entity.get("created")),
        "updatedAt": _iso(entity.get("updated")),
        "archived": False,
    }


def _flatten(twin, req, body: dict) -> dict:
    """写请求进来时可能是 `{"properties": {...}}`，也可能是扁平 dict。

    真 HubSpot 只接受前者，但**宽容这一层是必要的**：不同 SDK 版本对
    "创建联系人"的封装不一样，把合法请求判 400 会让 agent 学到一个错误的事实
    ——"这个 API 不能建联系人"。宽容读、严格写，方向不能反。
    """
    if isinstance(body.get("properties"), dict):
        return dict(body["properties"])
    return {k: v for k, v in (body or {}).items() if k not in ("id", "createdAt", "updatedAt")}


def _token(req) -> str:
    return bearer(req)


class HubSpotTwin(SpecTwin):
    name = "hubspot"
    errors = HUBSPOT_ERRORS
    pager = Pager("after", page_size=10)

    def initial_state(self) -> dict:
        return {self.name: {"contacts": {}, "companies": {}, "deals": {}}}

    # ---- 声明 ----

    def _build_routes(self) -> None:
        common = dict(
            id_field="id",
            id_style="increment",
            hooks={"normalize_in": _flatten, "decorate": _wrap},
        )
        for obj, extra in (
            ("contacts", [Field("email"), Field("firstname"), Field("lastname"), Field("lifecyclestage")]),
            ("companies", [Field("name", required=True), Field("domain"), Field("industry")]),
            ("deals", [Field("dealname", required=True), Field("dealstage"), Field("amount")]),
        ):
            self.declare(
                Resource(
                    name=obj,
                    collection=f"/crm/v3/objects/{obj}",
                    item=f"/crm/v3/objects/{obj}/{{id}}",
                    fields=extra,
                    pager_style="after",
                    **common,
                )
            )
        # 顺序是有意义的：`GET /crm/v3/objects/{obj}/{id}` 必须在 mount() **之前**挂，
        # 因为 contacts 允许用 email 当 id（真 HubSpot 就这样），而工厂生成的默认
        # item GET 只按主键查。挂晚了会被默认 handler 先匹配，email 一律 404。
        self.route("POST", "/crm/v3/objects/{obj}/search", self.search)
        self.route("GET", "/crm/v3/objects/{obj}/{id}", self.get_object)
        self.route("GET", "/account-info/v3/details", self.account_info)
        self.mount()

    # ---- 特有行为 ----

    def authenticate(self, req):
        if req.path.startswith("/admin"):
            return None
        if not _token(req):
            return _err(401, "Authentication credentials are missing or invalid.", "INVALID_AUTHENTICATION")
        return None

    def dispatch(self, req):
        """鉴权放在路由匹配**之前**。

        基类的 `authenticate` 只在"没有路由命中"时兜底调用，对已实现端点根本不跑 ——
        于是无 token 的请求能正常读到全部联系人。这不是 HubSpot 的行为（它一律 401），
        而且是那种"只在坏路径上设防、好路径全开"的典型漏洞。
        """
        if not req.path.startswith("/admin"):
            bad = self.authenticate(req)
            if bad is not None:
                return bad
        return super().dispatch(req)

    def search(self, req, params):
        """`POST /crm/v3/objects/{obj}/search`：filterGroups 只支持等值与 contains。

        不认识的算子明确 400，不静默返回空结果 —— 空结果会被 agent 读成
        "这个 CRM 里没有这家公司"，而真相是"这个 twin 不会算那个算子"。
        """
        obj = params["obj"]
        if obj not in ("contacts", "companies", "deals"):
            return _err(404, f"No object type named {obj}", "OBJECT_NOT_FOUND")
        body = req.json() or {}
        bucket = req.run.state.setdefault(self.name, {}).setdefault(obj, {})
        rows = list(bucket.values())
        for group in body.get("filterGroups") or []:
            for f in group.get("filters") or []:
                prop = f.get("propertyName") or ""
                op = (f.get("operator") or "").upper()
                val = (f.get("value") or "")
                if op in ("EQ", "="):
                    rows = [r for r in rows if str(r.get(prop, "")) == str(val)]
                elif op in ("CONTAINS_TOKEN", "CONTAINS"):
                    rows = [r for r in rows if str(val).lower() in str(r.get(prop, "")).lower()]
                else:
                    return _err(400, f"Unsupported search operator: {op}", "VALIDATION_ERROR")
        rows.sort(key=lambda r: r.get("created") or 0)
        out = self.pager.apply(req, [_wrap(self, req, r) for r in rows])
        out["total"] = len(rows)
        return Response(200, out)

    def get_object(self, req, params):
        """按 id 或按 email 取（HubSpot 的 contacts 也吃 email 作为 id）。"""
        obj = params["obj"]
        bucket = req.run.state.setdefault(self.name, {}).setdefault(obj, {})
        ident = params["id"]
        entity = bucket.get(ident)
        if entity is None and obj == "contacts":
            entity = next((e for e in bucket.values() if e.get("email") == ident), None)
        if entity is None:
            return _err(404, f"{obj} object {ident} not found", "OBJECT_NOT_FOUND")
        return Response(200, _wrap(self, req, entity))

    def account_info(self, req):
        return Response(
            200,
            {
                "portalId": 24000000,
                "accountType": "STANDARD",
                "timeZone": "America/Los_Angeles",
                "companyCurrency": "USD",
                "_twin": True,
            },
        )

    def handle_admin(self, req):
        if req.path.endswith("/admin/seed"):
            body = req.json() or {}
            state = req.run.state.setdefault(self.name, {})
            state.setdefault("contacts", {})
            state.setdefault("companies", {})
            state.setdefault("deals", {})
            for obj in ("contacts", "companies", "deals"):
                bucket = state[obj]
                for row in body.get(obj) or []:
                    entity = {k: v for k, v in row.items() if k not in ("id", "created", "updated")}
                    ident = str(row.get("id") or (len(bucket) + 1))
                    entity["id"] = ident
                    entity.setdefault("created", int(time.time()))
                    entity["updated"] = entity["created"]
                    bucket[ident] = entity
            return Response(200, {"ok": True})
        return super().handle_admin(req)
