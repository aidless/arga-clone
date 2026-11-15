"""Notion twin：capability 三档 + block 树的 provider。

保真清单（照 Arga 文档实测行为对齐）：
  ✅ 必须带 `Notion-Version` 头，否则 400 `missing_version`
  ✅ 三档 capability，**读用户信息是唯一受它管的能力**：
       read_user_information_with_email  读得到，且带 email
       no_email                          读得到，但 email 被抹掉
       no_user_information               一律 403 restricted_resource
     这一层最容易被做成"全有或全无"，而真实 Notion 是分档的 —— 分档才能在
     agent 场景里造出"人有权限看名字但看不到邮箱"这种真实故障。
  ✅ 错误信封 `{"object":"error","status":..,"code":..,"message":..}`
  ✅ page 的 properties 是 dict，block 有 has_children / type / 类型专属字段
  ✅ `PATCH /v1/blocks/{id}/children` 追加子块（不是替换）
  ✅ `POST /v1/databases/{id}/query` 支持 filter（property + title/rich_text contains）
  ❌ 不做：富文本 rich_text 的完整 annotation 体系（只保留 plain_text 与 type）
"""

from __future__ import annotations

import uuid
from typing import Any

from ..twin_spec import (
    Field,
    Pager,
    Resource,
    SpecTwin,
    notion_errors,
    now,
)
from .base import Request, Response, bearer

# Notion 的 capability 不是 scope 继承关系，而是三档互斥。用 ScopeFamily 会误导。
CAPABILITIES = (
    "read_user_information_with_email",
    "no_email",
    "no_user_information",
)


def _rich(text: str) -> list[dict]:
    """把纯字符串包成 rich_text 数组，并保留 plain_text（客户端断言主要读它）。"""
    return [
        {
            "type": "text",
            "plain_text": text,
            "text": {"content": text},
            "annotations": {
                "bold": False,
                "italic": False,
                "strikethrough": False,
                "underline": False,
                "code": False,
                "color": "default",
            },
        }
    ]


def _rich_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(
            (v.get("plain_text") or (v.get("text") or {}).get("content") or "")
            if isinstance(v, dict)
            else str(v)
            for v in value
        )
    return str(value or "")


class NotionTwin(SpecTwin):
    name = "notion"

    # 真 Notion 的列表分页是 cursor 风格（results / next_cursor / has_more）。
    # 交给 Pager，twin 只负责把 object="list" 补上。
    pager = Pager("cursor")

    def _list_page(self, req: Request, rows: list[dict], **extra) -> Response:
        body = self.pager.apply(req, rows)
        body["object"] = "list"
        body.update(extra)
        return Response(200, body)
    errors = notion_errors()

    REQUIRED_VERSION = "2022-06-28"

    def _build_routes(self) -> None:
        self.declare(
            Resource(
                name="pages",
                collection="/v1/pages",
                item="/v1/pages/{id}",
                id_field="id",
                id_prefix="page-",
                fields=[
                    Field("parent", default={}),
                    Field("properties", default={}),
                    Field("archived", default=False),
                    Field("url"),
                ],
                pager_style="cursor",
                hooks={"decorate": _page_out},
            )
        )
        self.declare(
            Resource(
                name="blocks",
                collection="/v1/blocks",
                item="/v1/blocks/{id}",
                id_field="id",
                id_prefix="blk-",
                fields=[
                    Field("type", default="paragraph"),
                    Field("parent", default={}),
                    Field("children", default=[]),
                    Field("archived", default=False),
                ],
                pager_style="cursor",
            )
        )
        self.declare(
            Resource(
                name="databases",
                collection="/v1/databases",
                item="/v1/databases/{id}",
                id_field="id",
                id_prefix="db-",
                fields=[Field("title", default=[]), Field("properties", default={})],
                pager_style="cursor",
            )
        )
        self.mount()
        # 追加子块是 PATCH 到 children，不是 PATCH 到 block 本身
        self.route("PATCH", "/v1/blocks/{id}/children", self._append_children)
        self.route("GET", "/v1/blocks/{id}/children", self._list_children)
        self.route("POST", "/v1/databases/{id}/query", self._query_database)
        self.route("GET", "/v1/users/me", self._me)
        self.route("GET", "/v1/users", self._users)
        self.route("GET", "/v1/users/{id}", self._user)

    def initial_state(self) -> dict[str, Any]:
        return {
            "notion": {
                "pages": {},
                "blocks": {},
                "databases": {},
                "users": {
                    "usr_twin_me": {
                        "object": "user",
                        "id": "usr_twin_me",
                        "name": "Twin Bot",
                        "type": "person",
                        "person": {"email": "twin@example.com"},
                    }
                },
                "capability": "read_user_information_with_email",
            }
        }

    # ---- 鉴权与版本 ----

    def dispatch(self, req: Request) -> Response:
        if req.path.startswith("/admin"):
            return super().dispatch(req)
        token = bearer(req)
        if not token:
            return self.errors.unauthorized("API token is invalid.")
        version = req.header("notion-version")
        if not version:
            # 真 Notion 对缺版本头回 400 且 code 是 missing_version，不是 401
            return Response(
                400,
                {
                    "object": "error",
                    "status": 400,
                    "code": "missing_version",
                    "message": "Notion-Version header failed validation: Notion-Version header should be defined, "
                    f"instead was `{version}`.",
                },
            )
        return super().dispatch(req)

    def capability(self, req: Request) -> str:
        return req.run.state.get("notion", {}).get("capability", "read_user_information_with_email")

    def _capability_guard(self, req: Request) -> Response | None:
        if self.capability(req) == "no_user_information":
            return self.errors.forbidden(
                "Insufficient permissions for this endpoint. "
                "This integration lacks the 'read user information' capability."
            )
        return None

    # ---- users ----

    def _mask(self, req: Request, user: dict) -> dict:
        cap = self.capability(req)
        if cap == "read_user_information_with_email":
            return dict(user)
        # no_email：读得到人，但 person.email 被抹掉（不是整个 person 消失）
        out = dict(user)
        if out.get("type") == "person":
            out["person"] = {}
        out["name"] = out.get("name") or ""
        return out

    def _me(self, req: Request, params: dict[str, str]) -> Response:
        bad = self._capability_guard(req)
        if bad:
            return bad
        return Response(200, self._mask(req, {"object": "user", "id": "usr_twin_me", "name": "Twin Bot", "type": "person", "person": {"email": "twin@example.com"}}))

    def _users(self, req: Request, params: dict[str, str]) -> Response:
        bad = self._capability_guard(req)
        if bad:
            return bad
        rows = [self._mask(req, u) for u in req.run.state["notion"]["users"].values()]
        return self._list_page(req, rows)

    def _user(self, req: Request, params: dict[str, str]) -> Response:
        bad = self._capability_guard(req)
        if bad:
            return bad
        u = req.run.state["notion"]["users"].get(params["id"])
        if not u:
            return self.errors.not_found("user", params["id"])
        return Response(200, self._mask(req, u))

    # ---- 装饰 ----

    def _append_children(self, req: Request, params: dict[str, str]) -> Response:
        state = req.run.state["notion"]
        parent = params["id"]
        body = req.json()
        created = []
        for item in body.get("children") or []:
            bid = item.get("id") or f"blk-{uuid.uuid4().hex[:10]}"
            block = {
                "object": "block",
                "id": bid,
                "type": item.get("type") or "paragraph",
                "has_children": bool(item.get("children")),
                "archived": False,
                "created": now(),
                "parent": parent,
                "children": [],
            }
            # 类型专属 payload 原样保留（paragraph.rich_text / to_do.checked / …）
            if isinstance(item.get("paragraph"), dict):
                block["paragraph"] = item["paragraph"]
            elif isinstance(item.get("to_do"), dict):
                block["to_do"] = item["to_do"]
            elif isinstance(item.get("heading_1"), dict):
                block["heading_1"] = item["heading_1"]
            if "rich_text" in item:
                # 客户端的简写：直接给 rich_text，按 type 放进去
                block[block["type"]] = {"rich_text": item["rich_text"]}
            state["blocks"][bid] = block
            created.append(block)
            for sub in item.get("children") or []:
                sub_id = sub.get("id") or f"blk-{uuid.uuid4().hex[:10]}"
                state["blocks"][sub_id] = {
                    "object": "block",
                    "id": sub_id,
                    "type": sub.get("type") or "paragraph",
                    "has_children": False,
                    "archived": False,
                    "created": now(),
                    "parent": bid,
                    "children": [],
                    **(
                        {sub["type"]: {"rich_text": sub.get("rich_text") or []}}
                        if sub.get("type")
                        else {}
                    ),
                }
                block["children"].append(sub_id)
        if parent in state["blocks"]:
            state["blocks"][parent]["has_children"] = True
            state["blocks"][parent].setdefault("children", [])
            state["blocks"][parent]["children"].extend(b["id"] for b in created)
        return self._list_page(req, [_block_out(b) for b in created])

    def _list_children(self, req: Request, params: dict[str, str]) -> Response:
        state = req.run.state["notion"]
        parent = params["id"]
        rows = [b for b in state["blocks"].values() if b.get("parent") == parent]
        rows.sort(key=lambda b: b.get("created") or 0)
        return Response(200, Pager("cursor").apply(req, [_block_out(b) for b in rows]))

    # ---- databases ----

    def _query_database(self, req: Request, params: dict[str, str]) -> Response:
        state = req.run.state["notion"]
        db = state["databases"].get(params["id"])
        if db is None:
            return self.errors.not_found("database", params["id"])
        body = req.json()
        rows = [p for p in state["pages"].values() if (p.get("parent") or {}).get("database_id") == params["id"]]
        flt = body.get("filter")
        if flt:
            prop = flt.get("property")
            cond = flt.get("title") or flt.get("rich_text") or {}
            needle = (cond.get("contains") or "") if isinstance(cond, dict) else ""
            if prop and needle:
                rows = [
                    p for p in rows
                    if needle.lower() in _rich_text((p.get("properties") or {}).get(prop)).lower()
                ]
        rows.sort(key=lambda p: p.get("created") or 0)
        return Response(
            200,
            {
                **self.pager.apply(req, [_page_out(self, req, p) for p in rows]),
                "object": "list",
                "type": "page",
                "page": {},
            },
        )

    # ---- admin ----

    def handle_admin(self, req: Request) -> Response:
        if req.path == "/admin/capability":
            state = req.run.state.setdefault("notion", self.initial_state()["notion"])
            if req.method.upper() == "GET":
                return Response(200, {"capability": state.get("capability"), "options": list(CAPABILITIES)})
            want = req.json().get("capability")
            if want not in CAPABILITIES:
                return Response(400, {"error": f"capability must be one of {list(CAPABILITIES)}"})
            state["capability"] = want
            return Response(200, {"ok": True, "capability": want})
        if req.path == "/admin/seed":
            # 造数据：database / page / block，让 scenario seed 不必直接改 Python 对象
            state = req.run.state.setdefault("notion", self.initial_state()["notion"])
            body = req.json()
            for d in body.get("databases") or []:
                did = d.get("id") or f"db-{uuid.uuid4().hex[:10]}"
                state["databases"][did] = {
                    "object": "database",
                    "id": did,
                    "title": _rich(d.get("title") or ""),
                    "properties": d.get("properties") or {},
                    "created": now(),
                }
            for pg in body.get("pages") or []:
                pid = pg.get("id") or f"page-{uuid.uuid4().hex[:10]}"
                state["pages"][pid] = {
                    "id": pid,
                    "parent": pg.get("parent") or {},
                    "properties": pg.get("properties") or {},
                    "archived": bool(pg.get("archived")),
                    "created": now(),
                    "updated": now(),
                }
            for u in body.get("users") or []:
                uid = u.get("id") or f"usr-{uuid.uuid4().hex[:10]}"
                state["users"][uid] = {
                    "object": "user",
                    "id": uid,
                    "name": u.get("name"),
                    "type": u.get("type") or "person",
                    "person": {"email": u.get("email")} if u.get("email") else {},
                }
            if body.get("capability") in CAPABILITIES:
                state["capability"] = body["capability"]
            return Response(
                200,
                {
                    "ok": True,
                    "databases": len(state["databases"]),
                    "pages": len(state["pages"]),
                    "users": len(state["users"]),
                },
            )
        return super().handle_admin(req)



def _page_out(twin, req: Request, e: dict) -> dict:
    return {
        "object": "page",
        "id": e["id"],
        "created_time": _iso(e.get("created")),
        "last_edited_time": _iso(e.get("updated") or e.get("created")),
        "archived": bool(e.get("archived")),
        "parent": e.get("parent") or {},
        "properties": e.get("properties") or {},
        "url": e.get("url") or f"https://twin.notion.local/{e['id']}",
    }

# ---- blocks ----

def _block_out(b: dict) -> dict:
    out = {
        "object": "block",
        "id": b["id"],
        "type": b["type"],
        "has_children": b.get("has_children", False),
        "archived": b.get("archived", False),
        "created_time": _iso(b.get("created")),
        "last_edited_time": _iso(b.get("updated") or b.get("created")),
        b["type"]: b.get(b["type"]) or {"rich_text": []},
    }
    return out


def _iso(ts: Any) -> str:
    import datetime

    if not ts:
        ts = now()
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
