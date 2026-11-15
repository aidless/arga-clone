"""Google Drive twin：文件、目录、共享权限。

为什么 Drive 值得单独做：**误公开**是 agent 最典型的高危副作用之一 ——
"把报告发给合作方"和"把报告设成 anyone 可读"在模型看来是同一件事，
而在真实世界里后者会留下不可撤回的公开链接。所以 permissions 这一面必须保真。

与 Gmail 共享 run-scoped 状态：同一个 run 里 Drive 的文件 id 可以被邮件正文、
Slack 消息引用；`link_person` 让"这个文件被分享给了谁"落到同一个人身上。

保真清单（实现）：
  GET  /drive/v3/files                  q 子集（name contains / in parents / trashed / mimeType）
  GET  /drive/v3/files/{id}             **不带内容**，必须 alt=media 才拿字节
  GET  /drive/v3/files/{id}?alt=media
  POST /upload/drive/v3/files?uploadType=media    body 即文件内容
  POST /drive/v3/files                 只建 metadata（配合 media 上传后的元信息更新）
  PATCH /drive/v3/files/{id}
  DELETE /drive/v3/files/{id}           真 Drive 是进回收站，不是删除
  POST /drive/v3/files/{id}/copy
  GET  /drive/v3/files/{id}/permissions
  POST /drive/v3/files/{id}/permissions  role=reader|writer，type=user|anyone|domain
  GET  /drive/v3/about

刻意不实现：revisions、changes 增量、team drives、export 到 Office 格式。

⚠️ 一条刻意的"不宽容"：`POST permissions` 创建 `type=anyone` 时，
响应里会带 `_twin_public_link: true` 与 `webViewLink`。真实 Google 不会标这个 ——
但 twin 的目标是让"公开了"这件事在 trace 里可见，而不是让 agent 好受。
"""

from __future__ import annotations

import re
import time
import uuid

from .base import Request, Response, Twin, bearer
from .gmail_twin import GOOGLE_ERRORS  # 共用 Google 的错误信封

DRIVE_SCOPES = {
    "https://www.googleapis.com/auth/drive": [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
    "https://www.googleapis.com/auth/drive.file": [],
    "https://www.googleapis.com/auth/drive.readonly": [],
}

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


class DriveTwin(Twin):
    name = "drive"

    def _build_routes(self) -> None:
        self.route("GET", "/drive/v3/about", self.about)
        self.route("GET", "/drive/v3/files", self.list_files)
        self.route("POST", "/drive/v3/files", self.create_metadata_only)
        self.route("GET", "/drive/v3/files/{fid}", self.get_file)
        self.route("PATCH", "/drive/v3/files/{fid}", self.patch_file)
        self.route("DELETE", "/drive/v3/files/{fid}", self.trash_file)
        self.route("POST", "/drive/v3/files/{fid}/copy", self.copy_file)
        self.route("GET", "/drive/v3/files/{fid}/permissions", self.list_permissions)
        self.route("POST", "/drive/v3/files/{fid}/permissions", self.create_permission)
        self.route("POST", "/upload/drive/v3/files", self.upload)

    def initial_state(self) -> dict:
        return {
            "files": {},
            "contents": {},
            "permissions": {},
            "granted_scopes": list(DEFAULT_SCOPES),
            "next_id": 1,
        }

    # ---------- 鉴权 ----------

    def _expand(self, granted: list[str]) -> set[str]:
        out = set(granted)
        for parent, children in DRIVE_SCOPES.items():
            if parent in out:
                out |= set(children)
        return out

    def _auth(self, req: Request, write: bool = False) -> Response | None:
        token = bearer(req)
        if not token:
            return Response(401, GOOGLE_ERRORS.unauthorized("missing token"))
        # 只查前缀会让 `ya29.` 这种"前缀齐全、值为空"的令牌通过 —— 与 jira/linear/notion
        # 的空令牌是同一类缺陷：所有正常测试都带真令牌，没人试空串。
        if not token.startswith("ya29.") or len(token) <= len("ya29."):
            return Response(401, GOOGLE_ERRORS.unauthorized("malformed token"))
        granted = self._expand(req.run.state[self.name]["granted_scopes"])
        need = ("https://www.googleapis.com/auth/drive"
                if write else "https://www.googleapis.com/auth/drive.readonly")
        if need not in granted and "*" not in granted:
            return Response(403, GOOGLE_ERRORS.forbidden(need))
        return None

    def authenticate(self, req: Request) -> Response | None:
        return self._auth(req)

    def dispatch(self, req: Request) -> Response:
        if not req.path.startswith("/admin"):
            bad = self._auth(req, write=req.method.upper() not in ("GET", "HEAD"))
            if bad is not None:
                return bad
        return super().dispatch(req)

    # ---------- 视图 ----------

    @staticmethod
    def _view(f: dict) -> dict:
        return {
            "kind": "drive#file",
            "id": f["id"],
            "name": f["name"],
            "mimeType": f["mimeType"],
            "parents": list(f["parents"]),
            "modifiedTime": f["modifiedTime"],
            "createdTime": f["createdTime"],
            "trashed": f["trashed"],
            "size": str(f["size"]),
            "webViewLink": f"https://drive.google.com/file/d/{f['id']}/view",
            "owners": [{"emailAddress": f["owner"]}],
        }

    def _new_file(self, state: dict, name: str, mime: str, parents: list[str],
                  content: bytes, owner: str) -> dict:
        fid = f"file_{state['next_id']:04d}"
        state["next_id"] += 1
        f = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": list(parents),
            "modifiedTime": _now_iso(),
            "createdTime": _now_iso(),
            "trashed": False,
            "size": len(content),
            "owner": owner,
        }
        state["files"][fid] = f
        state["contents"][fid] = content
        state["permissions"][fid] = [
            {"id": "perm_owner", "type": "user", "role": "owner", "emailAddress": owner}
        ]
        return f

    # ---------- q 解析 ----------

    def _match_q(self, f: dict, q: str) -> bool:
        """只支持四种子句，用 and 连接。不认的**必须报错**。

        真 Drive 的 q 是一门查询语言（contains / = / and / or / not / 字段组合）。
        假装支持一个子集是最糟的选择：agent 写了一个我们不认的子句，
        我们默默忽略它，返回**全量**列表，agent 会以为"筛选生效了、只有这些文件"。
        """
        for clause in re.split(r"\s+and\s+", q.strip()):
            clause = clause.strip()
            if not clause:
                continue
            m = re.match(r"name\s+contains\s+'([^']*)'", clause)
            if m:
                if m.group(1).lower() not in f["name"].lower():
                    return False
                continue
            m = re.match(r"'([^']+)'\s+in\s+parents", clause)
            if m:
                if m.group(1) not in f["parents"]:
                    return False
                continue
            m = re.match(r"mimeType\s*=\s*'([^']+)'", clause)
            if m:
                if f["mimeType"] != m.group(1):
                    return False
                continue
            m = re.match(r"trashed\s*=\s*(true|false)", clause)
            if m:
                if f["trashed"] != (m.group(1) == "true"):
                    return False
                continue
            raise ValueError(clause)
        return True

    # ---------- handlers ----------

    def about(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        return Response(200, {
            "kind": "drive#about",
            "user": {"emailAddress": "ops@acme.example", "displayName": "Ops"},
            "storageQuota": {"limit": "16106127360", "usage": str(sum(f["size"] for f in state["files"].values()))},
        })

    def list_files(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        q = (req.query_one("q") or "").strip()
        rows = list(state["files"].values())
        if q:
            try:
                rows = [f for f in rows if self._match_q(f, q)]
            except ValueError as exc:
                return Response(400, GOOGLE_ERRORS.bad_request(
                    f"unsupported q clause: {exc}  (supported: name contains, '<id>' in parents, "
                    f"mimeType =, trashed =, joined by 'and')"))
        if not req.query_one("includeTrashed"):
            rows = [f for f in rows if not f["trashed"]]
        rows.sort(key=lambda f: f["name"])
        return Response(200, {"kind": "drive#fileList", "files": [self._view(f) for f in rows]})

    def get_file(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        f = state["files"].get(params["fid"])
        if f is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["fid"]))
        if req.query_one("alt") == "media":
            return Response(200, state["contents"][f["id"]], {"Content-Type": f["mimeType"]})
        # 不带 alt=media 时**不返回内容** —— 真 Drive 就是这样，
        # 客户端以为 files.get 能拿到正文是常见的实现错误。
        return Response(200, self._view(f))

    def upload(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        name = req.query_one("name") or "Untitled"
        mime = req.query_one("mimeType") or "application/octet-stream"
        parents = [p for p in (req.query_one("parents") or "").split(",") if p]
        f = self._new_file(state, name, mime, parents, req.body or b"", "ops@acme.example")
        return Response(200, self._view(f))

    def create_metadata_only(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        body = req.json() or {}
        if not body.get("name"):
            return Response(400, GOOGLE_ERRORS.bad_request("name is required"))
        f = self._new_file(state, body["name"], body.get("mimeType") or "application/octet-stream",
                           body.get("parents") or [], b"", "ops@acme.example")
        return Response(200, self._view(f))

    def patch_file(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        f = state["files"].get(params["fid"])
        if f is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["fid"]))
        body = req.json() or {}
        for key in ("name", "mimeType", "parents", "trashed"):
            if key in body:
                f[key] = body[key]
        f["modifiedTime"] = _now_iso()
        return Response(200, self._view(f))

    def trash_file(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        f = state["files"].get(params["fid"])
        if f is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["fid"]))
        # 真 Drive 的 DELETE 是进回收站，不是抹掉 —— 审计场景下这个区别很关键
        f["trashed"] = True
        f["modifiedTime"] = _now_iso()
        return Response(204, None)

    def copy_file(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        src = state["files"].get(params["fid"])
        if src is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["fid"]))
        body = req.json() or {}
        new = self._new_file(state, body.get("name") or f"Copy of {src['name']}",
                             src["mimeType"], body.get("parents") or src["parents"],
                             state["contents"][src["id"]], src["owner"])
        return Response(200, self._view(new))

    def list_permissions(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        if params["fid"] not in state["files"]:
            return Response(404, GOOGLE_ERRORS.not_found(params["fid"]))
        return Response(200, {"kind": "drive#permissionList",
                              "permissions": state["permissions"][params["fid"]]})

    def create_permission(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        fid = params["fid"]
        if fid not in state["files"]:
            return Response(404, GOOGLE_ERRORS.not_found(fid))
        body = req.json() or {}
        ptype = body.get("type")
        role = body.get("role")
        if ptype not in ("user", "group", "domain", "anyone"):
            return Response(400, GOOGLE_ERRORS.bad_request(f"type={ptype}"))
        if role not in ("reader", "commenter", "writer"):
            return Response(400, GOOGLE_ERRORS.bad_request(f"role={role}"))
        perm = {
            "id": f"perm_{uuid.uuid4().hex[:8]}",
            "type": ptype,
            "role": role,
        }
        if ptype == "user":
            if not body.get("emailAddress"):
                return Response(400, GOOGLE_ERRORS.bad_request("emailAddress required for type=user"))
            perm["emailAddress"] = body["emailAddress"]
            req.store.link_person(req.run_id, body["emailAddress"], "drive", fid)
        state["permissions"][fid].append(perm)
        view = self._view(state["files"][fid])
        if ptype == "anyone":
            # 让"公开了"这件事在 trace 里显形。真实 Google 不标这个字段，
            # 但 twin 的读者是审计与 agent，不是用户体验。
            view["_twin_public_link"] = True
        return Response(200, {"kind": "drive#permission", **perm, "_file": view["webViewLink"],
                              **({"_twin_public_link": True} if ptype == "anyone" else {})})

    # ---------- admin ----------

    def handle_admin(self, req: Request) -> Response:
        state = req.run.state[self.name]
        parts = [p for p in req.path.split("/") if p]
        action = parts[1] if len(parts) > 1 else ""
        if action == "scopes":
            if req.method == "GET":
                return Response(200, {"granted": state["granted_scopes"],
                                      "effective": sorted(self._expand(state["granted_scopes"]))})
            state["granted_scopes"] = (req.json() or {}).get("scopes") or []
            return Response(200, {"granted": state["granted_scopes"]})
        if action == "seed":
            body = req.json() or {}
            for item in body.get("files") or []:
                self._new_file(state, item["name"], item.get("mimeType") or "text/plain",
                               item.get("parents") or [],
                               (item.get("content") or "").encode(),
                               item.get("owner") or "ops@acme.example")
            return Response(200, {"seeded": len(body.get("files") or [])})
        return Response(404, GOOGLE_ERRORS.not_found(req.path))
