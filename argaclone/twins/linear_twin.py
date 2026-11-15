"""Linear twin：GraphQL 面的 provider。

Linear 是这几个里唯一不走 REST 的，所以它没法用工厂的 RestResource。但工厂里
不用重写的那三件（scope、错误形状、admin 面）照样复用 —— 这正是"工厂"与
"框架"的区别：工厂给你可组合的件，框架逼你用它的形状。

保真清单（照 Arga 文档实测行为对齐）：
  ✅ POST /graphql，单一入口
  ✅ 每个实体带 `__typename`
  ✅ 关系默认只回 `{id}`，要多字段必须显式写子选择（真实 Linear 就是这样）
  ✅ 错误 `{"errors":[{"message":..., "extensions":{"type":"..."}}]}`，
     type 是小写串：`authentication error` / `invalid input` / `ratelimited`
     / `feature not accessible`
  ✅ webhook 头：`Linear-Signature`（小写 hex HMAC-SHA256）、`Linear-Delivery`
     `Linear-Event`、`Linear-Timestamp`
  ✅ `/admin/webhook-events` 可手动 flush / replay
  ✅ `viewer { id name email }`
  ❌ 不做：完整 GraphQL 语法（fragment / alias / variables 里的嵌套 input 只支持一层）。
     **不假装的判据**：遇到不支持的形状必须回 errors，不能回空 data。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from typing import Any

from ..twin_spec import ScopeFamily, SpecTwin, now
from .base import Request, Response, bearer

LINEAR_SCOPES = ScopeFamily(
    parents={
        "write": ["read"],
    }
)

DEFAULT_SCOPES = ["read", "write"]

TYPENAMES = {
    "issues": "Issue",
    "issue": "Issue",
    "teams": "Team",
    "team": "Team",
    "users": "User",
    "user": "User",
    "viewer": "User",
    "comments": "Comment",
    "comment": "Comment",
    "projects": "Project",
}


# ---------------------------------------------------------------- 极简 GraphQL


def _tokenize_selection(text: str) -> dict[str, Any]:
    """把 `issues { nodes { id title } }` 解析成嵌套 dict。

    不写完整 parser 的理由：完整 GraphQL 有 fragment、alias、directive、变量，
    写一个"差不多能用"的解析器比不写更危险 —— 它会静默丢掉它不认识的东西，
    于是 agent 拿到少字段的响应还以为字段不存在。这里只认一种形状，
    不认的当场报 UNSUPPORTED。
    """
    # 外层花括号是"整个操作"的容器，解析从它里面开始。
    # 早期版本忘了剥，parse_block 看到第一个字符是 `{` 就报"无法解析的字符"，
    # 于是每一句合法 GraphQL 都被判 invalid input —— 而错误信封是 200，
    # 客户端只看到 data 缺失，很容易被误读成"字段不存在"。
    t = text.strip()
    if t.startswith("{"):
        t = t[1:-1] if t.endswith("}") else t[1:]

    i = 0
    n = len(t)

    def skip_ws() -> None:
        nonlocal i
        while i < n and t[i] in " \t\n\r,":
            i += 1

    def parse_block(depth: int) -> dict[str, Any]:
        nonlocal i
        out: dict[str, Any] = {}
        while True:
            skip_ws()
            if i >= n or t[i] == "}":
                break
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", t[i:])
            if not m:
                raise ValueError(f"无法解析的字符: {t[i:i + 12]!r}")
            name = m.group(0)
            i += len(name)
            skip_ws()
            sub: dict[str, Any] = {}
            if i < n and t[i] == "(":
                # 参数：只用于筛选，不当字段解析
                depth_p = 1
                i += 1
                while i < n and depth_p:
                    if t[i] == "(":
                        depth_p += 1
                    elif t[i] == ")":
                        depth_p -= 1
                    i += 1
                skip_ws()
            if i < n and t[i] == "{":
                i += 1
                sub = parse_block(depth + 1)
                skip_ws()
                if i < n and t[i] == "}":
                    i += 1
            out[name] = sub
        return out

    return parse_block(0)


def _parse_operation(query: str) -> tuple[str, str | None, dict[str, Any]]:
    """返回 (kind, field_name, selection)。kind ∈ query | mutation | unsupported。"""
    q = query.strip()
    kind = "query"
    m = re.match(r"^(query|mutation)\b", q)
    if m:
        kind = m.group(1)
        q = q[m.end():].strip()
        # operationName 与变量声明
        q = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*", "", q).strip()
        if q.startswith("("):
            depth = 0
            for idx, ch in enumerate(q):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        q = q[idx + 1:].strip()
                        break
    if q.startswith("{"):
        q = q[1:].strip()
        if q.endswith("}"):
            q = q[:-1].strip()
    sel = _tokenize_selection(q)
    if len(sel) != 1:
        return "unsupported", None, {}
    root = next(iter(sel))
    return kind, root, sel[root]


def _project(obj: Any, sel: dict[str, Any], typename: str | None) -> Any:
    """按选择集投影。关系没写子选择时只回 {id}，这是 Linear 的真实行为。"""
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_project(x, sel, typename) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out: dict[str, Any] = {}
    for key, sub in sel.items():
        if key == "__typename":
            out["__typename"] = obj.get("__typename") or typename
            continue
        val = obj.get(key)
        if isinstance(val, dict) and "id" in val:
            out[key] = _project(val, sub, val.get("__typename")) if sub else {"id": val["id"]}
        elif isinstance(val, list):
            out[key] = _project(val, sub, None) if sub else [{"id": v["id"]} for v in val if "id" in v]
        else:
            out[key] = val
    return out


def _err(message: str, etype: str) -> Response:
    """Linear 的错误形状。type 必须是小写串 —— 它决定客户端会不会重试。"""
    return Response(200, {"errors": [{"message": message, "extensions": {"type": etype}}]})


class LinearTwin(SpecTwin):
    name = "linear"
    errors = None  # Linear 不用 REST 错误信封，见 _err

    def _build_routes(self) -> None:
        self.route("POST", "/graphql", self._graphql)
        self.route("GET", "/graphql", self._graphql_get)
        # 几个 Linear 客户端会先探的 REST 端点
        self.route("GET", "/v1/viewer", self._viewer_rest)

    def initial_state(self) -> dict[str, Any]:
        return {
            "linear": {
                "teams": {},
                "issues": {},
                "comments": {},
                "users": {},
                "webhook_events": [],
                "granted_scopes": list(DEFAULT_SCOPES),
                "secret": "twin-webhook-secret",
                "next_number": 1,
            }
        }

    # ---- 鉴权 ----

    def _auth(self, req: Request, needed: str | None = None) -> Response | None:
        token = bearer(req)
        if not token:
            # 只看"头存在"会放行 `Authorization: Bearer ` 这种空令牌
            return _err("Authentication required", "authentication error")
        if needed and not LINEAR_SCOPES.allows(self._granted(req), needed):
            return _err(f"Missing scope: {needed}", "feature not accessible")
        return None

    def _granted(self, req: Request) -> list[str]:
        return req.run.state.get("linear", {}).get("granted_scopes", list(DEFAULT_SCOPES))

    def dispatch(self, req: Request) -> Response:
        if not req.path.startswith("/admin"):
            bad = self._auth(
                req, "read" if req.method.upper() == "GET" else "write"
            )
            if bad is not None:
                return bad
        return super().dispatch(req)

    # ---- GraphQL ----

    def _graphql_get(self, req: Request, params: dict[str, str]) -> Response:
        return _err("Use POST for GraphQL", "invalid input")

    def _graphql(self, req: Request, params: dict[str, str]) -> Response:
        body = req.json()
        query = body.get("query") or ""
        if not query:
            return _err("query is required", "invalid input")
        try:
            kind, root, sel = _parse_operation(query)
        except ValueError as exc:
            return _err(f"Parse error: {exc}", "invalid input")
        if kind == "unsupported" or root is None:
            return _err(
                "This twin supports exactly one root field per operation", "invalid input"
            )

        state = req.run.state.setdefault("linear", self.initial_state()["linear"])
        handler = {
            "viewer": self._q_viewer,
            "issues": self._q_issues,
            "issue": self._q_issue,
            "teams": self._q_teams,
            "team": self._q_team,
            "users": self._q_users,
            "comments": self._q_comments,
            "issueCreate": self._m_issue_create,
            "issueUpdate": self._m_issue_update,
            "commentCreate": self._m_comment_create,
        }.get(root)
        if handler is None:
            return _err(f"Unknown field: {root}", "invalid input")
        data, err = handler(req, sel, query, state)
        if err:
            return err
        return Response(200, {"data": {root: _project(data, sel, TYPENAMES.get(root))}})

    # ---- query resolvers ----

    def _q_viewer(self, req, sel, query, state):
        return {
            "id": "usr_twin_me",
            "name": "Twin Bot",
            "email": "twin@example.com",
            "displayName": "Twin Bot",
            "__typename": "User",
        }, None

    def _q_teams(self, req, sel, query, state):
        rows = list(state["teams"].values())
        return {"nodes": rows}, None

    def _q_team(self, req, sel, query, state):
        tid = self._arg(query, "id")
        team = state["teams"].get(tid)
        if team is None:
            return None, _err(f"Team not found: {tid}", "invalid input")
        return team, None

    def _q_users(self, req, sel, query, state):
        return {"nodes": list(state["users"].values())}, None

    def _q_issues(self, req, sel, query, state):
        rows = list(state["issues"].values())
        team = self._arg(query, "teamId")
        if team:
            rows = [e for e in rows if e.get("team", {}).get("id") == team]
        rows.sort(key=lambda e: e.get("createdAt") or 0)
        return {"nodes": rows}, None

    def _q_issue(self, req, sel, query, state):
        iid = self._arg(query, "id")
        issue = state["issues"].get(iid)
        if issue is None:
            return None, _err(f"Issue not found: {iid}", "invalid input")
        return issue, None

    def _q_comments(self, req, sel, query, state):
        rows = list(state["comments"].values())
        return {"nodes": rows}, None

    # ---- mutations ----

    def _m_issue_create(self, req, sel, query, state):
        body = req.json().get("variables") or {}
        inp = body.get("input") or self._input_from_query(query)
        title = inp.get("title")
        if not title:
            return None, _err("Argument Validation Error: title is required", "invalid input")
        tid = f"iss_{uuid.uuid4().hex[:10]}"
        team_id = inp.get("teamId") or (next(iter(state["teams"]), ""))
        issue = {
            "id": tid,
            "identifier": f"TWIN-{state['next_number']}",
            "title": title,
            "description": inp.get("description"),
            "priority": int(inp.get("priority") or 0),
            "state": {"id": "state_todo", "name": "Todo", "__typename": "WorkflowState"},
            "team": {"id": team_id, "__typename": "Team"},
            "assignee": {"id": inp["assigneeId"]} if inp.get("assigneeId") else None,
            "labels": [],
            "createdAt": now(),
            "updatedAt": now(),
            "__typename": "Issue",
        }
        state["next_number"] += 1
        state["issues"][tid] = issue
        return issue, None

    def _m_issue_update(self, req, sel, query, state):
        inp = (req.json().get("variables") or {}).get("input") or self._input_from_query(query)
        iid = inp.get("id")
        issue = state["issues"].get(iid)
        if issue is None:
            return None, _err(f"Issue not found: {iid}", "invalid input")
        for k in ("title", "description", "priority"):
            if k in inp:
                issue[k] = inp[k]
        if "stateId" in inp:
            issue["state"] = {"id": inp["stateId"], "name": "Done", "__typename": "WorkflowState"}
        issue["updatedAt"] = now()
        return issue, None

    def _m_comment_create(self, req, sel, query, state):
        inp = (req.json().get("variables") or {}).get("input") or self._input_from_query(query)
        iid = inp.get("issueId")
        if not inp.get("body"):
            return None, _err("Argument Validation Error: body is required", "invalid input")
        cid = f"cmt_{uuid.uuid4().hex[:10]}"
        comment = {
            "id": cid,
            "body": inp["body"],
            "issue": {"id": iid, "__typename": "Issue"},
            "user": {"id": "usr_twin_me", "__typename": "User"},
            "createdAt": now(),
            "__typename": "Comment",
        }
        state["comments"][cid] = comment
        return comment, None

    # ---- 参数抽取 ----

    @staticmethod
    def _arg(query: str, name: str) -> str:
        m = re.search(rf'{name}\s*:\s*"([^"]+)"', query)
        return m.group(1) if m else ""

    @staticmethod
    def _input_from_query(query: str) -> dict:
        """从 `input: {title: "x"}` 里抽字段（只支持一层、值只能是字符串/数字）。"""
        m = re.search(r"input\s*:\s*\{([^{}]*)\}", query, re.S)
        if not m:
            return {}
        out: dict[str, Any] = {}
        for k, sval, nval in re.findall(r'(\w+)\s*:\s*(?:"([^"]*)"|(-?\d+))', m.group(1)):
            # 注意正则必须用非捕获组：`("([^"]*)"|-?\d+)` 是两层捕获，
            # findall 会返回三元组，解包成两个变量直接 ValueError。
            out[k] = sval if sval != "" or nval == "" else int(nval)
        return out

    def _viewer_rest(self, req: Request, params: dict[str, str]) -> Response:
        return Response(200, {"data": {"viewer": {"id": "usr_twin_me", "name": "Twin Bot"}}})

    # ---- webhook ----

    def sign(self, payload: bytes, state: dict) -> str:
        return hmac.new(
            state.get("secret", "").encode(), payload, hashlib.sha256
        ).hexdigest()

    def deliver(self, req: Request, event: str, payload: dict) -> dict:
        """把一次投递记进队列（并算出真实签名头），供 /admin/webhook-events 取用。"""
        state = req.run.state.setdefault("linear", self.initial_state()["linear"])
        body = json.dumps(payload, sort_keys=True).encode()
        record = {
            "delivery": uuid.uuid4().hex,
            "event": event,
            "payload": payload,
            "at": now(),
            "headers": {
                "Linear-Signature": self.sign(body, state),
                "Linear-Delivery": "",
                "Linear-Event": event,
                "Linear-Timestamp": str(now()),
            },
        }
        record["headers"]["Linear-Delivery"] = record["delivery"]
        state["webhook_events"].append(record)
        return record

    # ---- admin ----

    def handle_admin(self, req: Request) -> Response:
        state = req.run.state.setdefault("linear", self.initial_state()["linear"])
        if req.path == "/admin/webhook-events":
            if req.method.upper() == "POST":
                body = req.json()
                if body.get("clear"):
                    state["webhook_events"].clear()
                    return Response(200, {"ok": True, "count": 0})
                # replay：重放同一条，签名与 delivery 都重新算（真实重放也会换 delivery）
                want = body.get("delivery")
                for rec in list(state["webhook_events"]):
                    if want and rec["delivery"] != want:
                        continue
                    self.deliver(req, rec["event"], rec["payload"])
                return Response(200, {"ok": True, "count": len(state["webhook_events"])})
            return Response(200, {"events": list(reversed(state["webhook_events"]))})
        if req.path == "/admin/scopes":
            if req.method.upper() == "GET":
                g = self._granted(req)
                return Response(200, {"granted": g, "effective": sorted(LINEAR_SCOPES.expand(g))})
            req.run.state.setdefault("linear", {})["granted_scopes"] = list(
                req.json().get("granted") or DEFAULT_SCOPES
            )
            return Response(200, {"ok": True})
        if req.path == "/admin/seed":
            # 造数据：团队 / 用户 / issue，让 scenario seed 不必直接改 Python 对象
            body = req.json()
            for t in body.get("teams") or []:
                state["teams"][t["id"]] = {
                    "id": t["id"],
                    "key": t.get("key") or t["id"].upper()[:3],
                    "name": t.get("name") or t["id"],
                    "__typename": "Team",
                }
            for u in body.get("users") or []:
                state["users"][u["id"]] = {
                    "id": u["id"],
                    "name": u.get("name"),
                    "email": u.get("email"),
                    "__typename": "User",
                }
            for i in body.get("issues") or []:
                iid = i.get("id") or f"iss_{uuid.uuid4().hex[:10]}"
                state["issues"][iid] = {
                    "id": iid,
                    "identifier": i.get("identifier") or f"TWIN-{state['next_number']}",
                    "title": i.get("title"),
                    "description": i.get("description"),
                    "priority": int(i.get("priority") or 0),
                    "state": {"id": "state_todo", "name": i.get("state") or "Todo",
                              "__typename": "WorkflowState"},
                    "team": {"id": i.get("teamId") or "", "__typename": "Team"},
                    "assignee": {"id": i["assigneeId"]} if i.get("assigneeId") else None,
                    "labels": list(i.get("labels") or []),
                    "createdAt": i.get("createdAt") or now(),
                    "updatedAt": now(),
                    "__typename": "Issue",
                }
                state["next_number"] += 1
            return Response(200, {"ok": True, "issues": len(state["issues"])})
        return super().handle_admin(req)
