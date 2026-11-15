"""Jira twin：用工厂建的第三个 provider，也是工厂的第一次试产。

选 Jira 试产不是因为它简单，是因为它同时踩中工厂要解决的三个难点：
  1. **ADF**：comment/description 是 Atlassian Document Format，不是字符串。
     真 Jira 对纯字符串宽容（自动包一层），照文档写纯字符串的客户端必须能用。
  2. **scope 遗传**：`write:jira-work` 授 `read:jira-work`、`manage:jira-configuration`
     授 `manage:jira-project` + `read:jira-work`。手写 if-else 加第三个 scope 就错。
  3. **错误信封是定制的**：`{"errorMessages":[...],"errors":{...}}`，400 与 401 都是这个壳。

保真清单（照 Arga 文档实测行为对齐）：
  ✅ 必须 Bearer，否则 401 带 "Client must be authenticated to access this resource."
  ✅ scope 父授子（闭包）
  ✅ comment/description 用 ADF 存，纯字符串自动包成最小 doc
  ✅ issue 用 `PROJ-123` 的 key 寻址，不是数字 id
  ✅ create 的 body 是 `{"fields": {...}}`，读出来也包回 `fields`
  ❌ 不做：JQL 搜索、workflow transitions、webhook 投递（判据用不到，先不造假）
"""

from __future__ import annotations

from typing import Any

from ..twin_spec import (
    Field,
    Pager,
    Resource,
    ScopeFamily,
    SpecTwin,
    adf,
    jira_errors,
)
from .base import Request, Response, bearer

# Jira 的父→子 scope 关系（实测自 Arga 文档）
JIRA_SCOPES = ScopeFamily(
    parents={
        "write:jira-work": ["read:jira-work"],
        "manage:jira-project": ["read:jira-work"],
        "manage:jira-configuration": ["read:jira-work", "manage:jira-project"],
    }
)

DEFAULT_SCOPES = [
    "read:jira-work",
    "write:jira-work",
    "read:jira-user",
    "manage:jira-project",
    "manage:jira-webhook",
    "manage:jira-configuration",
]


def _issue_fields_in(twin, req: Request, body: dict) -> dict:
    """把 Jira 的 `{"fields": {...}}` 拍平成 entity 字段。"""
    f = body.get("fields") if isinstance(body.get("fields"), dict) else body
    status = f.get("status")
    if isinstance(status, dict):
        status = status.get("name") or "To Do"
    assignee = f.get("assignee")
    if isinstance(assignee, dict):
        assignee = assignee.get("accountId") or assignee.get("emailAddress")
    project = f.get("project")
    if isinstance(project, dict):
        project = project.get("key") or project.get("id")
    desc = f.get("description")
    return {
        "summary": f.get("summary"),
        "description": adf(desc) if desc else None,
        "status": status or "To Do",
        "labels": f.get("labels") or [],
        "project": project or "GEN",
        "assignee": assignee,
    }


def _issue_out(twin, req: Request, e: dict) -> dict:
    """读出来包回 Jira 的 `fields` 结构。agent 照文档读 `.fields.summary` 必须能用。"""
    return {
        "id": e.get("id"),
        "key": e.get("key"),
        "self": f"https://twin.atlassian.local/rest/api/3/issue/{e.get('key')}",
        "fields": {
            "summary": e.get("summary"),
            "description": e.get("description"),
            "status": {"name": e.get("status")},
            "labels": list(e.get("labels") or []),
            "project": {"key": e.get("project")},
            "assignee": {"accountId": e.get("assignee")} if e.get("assignee") else None,
            "created": e.get("created"),
            "updated": e.get("updated"),
        },
    }


def _comment_in(twin, req: Request, body: dict) -> dict:
    text = body.get("body")
    return {"body": adf(text) if text else None}


def _comment_out(twin, req: Request, e: dict) -> dict:
    return {
        "id": e.get("id"),
        "body": e.get("body"),
        "issue": e.get("issue"),
        "author": {"accountId": e.get("author") or "twin-bot"},
        "created": e.get("created"),
    }


class JiraTwin(SpecTwin):
    name = "jira"

    pager = Pager("offset")
    errors = jira_errors()

    # ---- 生命周期 ----

    def initial_state(self) -> dict[str, Any]:
        return {
            "jira": {
                "projects": {},
                "issues": {},
                "comments": {},
                "granted_scopes": list(DEFAULT_SCOPES),
                "next_comment_id": 10000,
            }
        }

    def _build_routes(self) -> None:
        # 资源声明与路由注册放在同一处：拆成两个方法时，加资源忘调 mount 的 bug
        # 会表现成"端点静默落进 stub"，非常难查。
        self.declare(
            Resource(
                name="projects",
                collection="/rest/api/3/project",
                item="/rest/api/3/project/{id}",
                id_field="key",
                id_prefix="PROJ",
                fields=[
                    Field("key", required=True),
                    Field("name", required=True),
                    Field("projectTypeKey", default="software"),
                    Field("lead"),
                ],
                pager_style="none",
            )
        )
        self.declare(
            Resource(
                name="issues",
                collection="/rest/api/3/issue",
                item="/rest/api/3/issue/{id}",
                id_field="key",
                id_style="jira_key",
                fields=[
                    Field("summary", required=True),
                    Field("description"),
                    Field("status", default="To Do"),
                    Field("labels", default=[]),
                    Field("project", default="GEN"),
                    Field("assignee"),
                ],
                pager_style="offset",
                hooks={"normalize_in": _issue_fields_in, "decorate": _issue_out},
            )
        )
        self.declare(
            Resource(
                name="comments",
                collection="/rest/api/3/issue/{project}/comment",
                item="/rest/api/3/issue/{project}/comment/{id}",
                id_field="id",
                id_style="increment",
                parent_keys=("project",),
                fields=[
                    Field("body", required=True),
                    Field("author"),
                ],
                pager_style="none",
                hooks={"normalize_in": _comment_in, "decorate": _comment_out},
            )
        )

        self.mount()
        self.route("GET", "/rest/api/3/myself", self._myself)
        self.route("GET", "/rest/api/3/search", self._search)

    # ---- 鉴权 ----

    def granted(self, req: Request) -> list[str]:
        return req.run.state.get("jira", {}).get("granted_scopes", list(DEFAULT_SCOPES))

    def _auth(self, req: Request, needed: str | None = None) -> Response | None:
        """返回 None 表示通过；否则返回该直接发出的错误响应。"""
        token = bearer(req)
        if not token:
            # 真 Jira 对"没带 Bearer"回的是这句话，不是 "unauthorized"。
            # 空 Bearer（`Bearer ` 后面什么都没有）也走这里 —— 只查前缀会放行空令牌。
            return self.errors.unauthorized(
                "Client must be authenticated to access this resource."
            )
        if needed and not JIRA_SCOPES.allows(self.granted(req), needed):
            return self.errors.forbidden(
                f"scope '{needed}' is required but not granted"
            )
        return None

    def dispatch(self, req: Request) -> Response:
        # 每个非 admin 请求都要先过鉴权，否则"忘加检查"的端点会静默放行
        if not req.path.startswith("/admin"):
            bad = self._auth(req, self._needed_scope(req))
            if bad is not None:
                return bad
        return super().dispatch(req)

    @staticmethod
    def _needed_scope(req: Request) -> str | None:
        if req.method.upper() in ("GET", "HEAD"):
            return "read:jira-work"
        return "write:jira-work"

    # ---- 额外端点 ----

    def _myself(self, req: Request, params: dict[str, str]) -> Response:
        return Response(
            200,
            {
                "accountId": "twin-account-0001",
                "displayName": "Twin Bot",
                "emailAddress": "twin@example.com",
                "active": True,
            },
        )

    def _search(self, req: Request, params: dict[str, str]) -> Response:
        """只支持 `project = KEY` 这一种子集，明确写成子集而不是假装支持 JQL。

        假支持比不支持更糟：agent 会写出 `project = X AND status = Done`，
        拿到全量结果之后以为过滤生效了。
        """
        jql = req.query_one("jql", "").upper()
        issues = self.bucket(req, self._by_name["issues"])
        rows = list(issues.values())
        if "PROJECT = " in jql:
            key = jql.split("PROJECT = ", 1)[1].split()[0].strip('"\'')
            rows = [e for e in rows if str(e.get("project", "")).upper() == key]
        rows.sort(key=lambda e: e.get("created") or 0)
        # 分页形状交给 Pager（offset 风格），twin 不自己拼 startAt/maxResults
        body = self.pager.apply(req, [_issue_out(self, req, e) for e in rows], key="issues")
        body["_twin_scope"] = "只支持 project = KEY 的 JQL 子集"
        return Response(200, body)

    # ---- admin ----

    def handle_admin(self, req: Request) -> Response:
        if req.path == "/admin/scopes":
            if req.method.upper() == "GET":
                g = self.granted(req)
                return Response(200, {"granted": g, "effective": sorted(JIRA_SCOPES.expand(g))})
            body = req.json()
            req.run.state.setdefault("jira", {})["granted_scopes"] = list(
                body.get("granted") or body.get("scopes") or DEFAULT_SCOPES
            )
            return Response(200, {"ok": True, "granted": self.granted(req)})
        return super().handle_admin(req)
