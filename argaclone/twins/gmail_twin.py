"""Gmail twin：一个能读、能发、能打标签的收件箱。

为什么先做 Gmail 而不是先做更"结构化"的 CRM：agent 失败最集中的动作是
**发邮件**（对外的、不可撤回的），而这一类动作在别的 twin 里都没有对等物。
Slack 消息可以编辑，邮件发出去就没了。

保真清单（实现）：
  GET  /gmail/v1/users/{u}/profile
  GET  /gmail/v1/users/{u}/messages            列表只回 id + threadId（真 Gmail 如此）
  GET  /gmail/v1/users/{u}/messages/{id}       支持 format=minimal|metadata|full
  POST /gmail/v1/users/{u}/messages/send       raw = base64url(MIME)，解析 To/Subject/正文
  POST /gmail/v1/users/{u}/messages/{id}/modify  加/删 label
  POST /gmail/v1/users/{u}/messages/{id}/trash
  GET  /gmail/v1/users/{u}/threads
  GET  /gmail/v1/users/{u}/threads/{id}
  GET  /gmail/v1/users/{u}/labels
  GET  /gmail/v1/users/{u}/drafts
  POST /gmail/v1/users/{u}/drafts              draft 里的 raw 同样解析

刻意不实现（落 stub，会显式标记）：
  附件上传/下载、history 增量同步、push 通知（watch）、发信给外部 SMTP。

鉴权：Bearer ya29.*（Google OAuth access token 形状）。scope 用
`gmail.readonly` / `gmail.send` / `gmail.modify` 三档，父 scope 遗传。
"""

from __future__ import annotations

import base64
import re
import time

from ..twin_spec import ErrorShape
from .base import Request, Response, Twin, bearer

# Google 的错误信封：错误在 body 里，HTTP 状态码也设，但客户端大多只读 body
GOOGLE_ERRORS = ErrorShape(
    not_found=lambda what: {"error": {"code": 404, "message": f"Requested entity was not found: {what}",
                                      "errors": [{"reason": "notFound", "message": "not found"}],
                                      "status": "NOT_FOUND"}},
    bad_request=lambda what: {"error": {"code": 400, "message": str(what),
                                        "errors": [{"reason": "invalidArgument"}],
                                        "status": "INVALID_ARGUMENT"}},
    forbidden=lambda what: {"error": {"code": 403, "message": "Insufficient Permission",
                                      "errors": [{"reason": "insufficientPermissions"}],
                                      "status": "PERMISSION_DENIED"}},
    unauthorized=lambda what: {"error": {"code": 401, "message": "Request had invalid authentication credentials.",
                                         "errors": [{"reason": "authError"}],
                                         "status": "UNAUTHENTICATED"}},
)

# 父 scope 授子 scope（与真实 Google 的依赖关系一致）
_GMAIL_PARENTS = {
    "https://mail.google.com/": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ],
    "https://www.googleapis.com/auth/gmail.modify": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ],
}

SYSTEM_LABELS = ["INBOX", "SENT", "DRAFT", "UNREAD", "STARRED", "TRASH", "SPAM", "IMPORTANT"]


def _b64url_decode(data: str) -> bytes:
    """Gmail 的 raw 是 base64url 且**去掉了 padding**。用标准 b64decode 直接解会炸。"""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_mime(raw: bytes) -> dict:
    """从 MIME 里抠出我们能保守承诺的那几个字段。

    不用 email 模块的完整解析：我们只需要 To/From/Subject/正文，
    而完整解析会把 multipart 的怪形状也当成"支持的一部分"。
    """
    text = raw.decode("utf-8", "replace")
    headers, _, body = text.partition("\r\n\r\n")
    if not _:
        headers, _, body = text.partition("\n\n")

    def header(name: str) -> str:
        m = re.search(rf"^{name}:\s*(.+)$", headers, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else ""

    return {
        "to": header("To"),
        "from": header("From"),
        "subject": header("Subject"),
        "body": body.strip(),
    }


def _addr_list(value: str) -> list[str]:
    return [a.strip() for a in re.split(r",", value) if a.strip()]


class GmailTwin(Twin):
    name = "gmail"
    DEFAULT_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

    def __init__(self) -> None:
        super().__init__()

    # ---------- 状态 ----------

    def initial_state(self) -> dict:
        return {
            "profile": {"emailAddress": "ops@acme.example", "messagesTotal": 0, "threadsTotal": 0},
            "messages": {},
            "drafts": {},
            "labels": {name: {"id": name, "name": name.title(), "type": "system"} for name in SYSTEM_LABELS},
            "granted_scopes": list(self.DEFAULT_SCOPES),
            "next_id": 1,
        }

    # ---------- 鉴权 ----------

    def _expand(self, granted: list[str]) -> set[str]:
        out = set(granted)
        changed = True
        while changed:
            changed = False
            for parent, children in _GMAIL_PARENTS.items():
                if parent in out and not set(children) <= out:
                    out |= set(children)
                    changed = True
        return out

    def _auth(self, req: Request, need: str = "https://www.googleapis.com/auth/gmail.readonly") -> Response | None:
        token = bearer(req)
        if not token:
            return Response(401, GOOGLE_ERRORS.unauthorized("missing token"))
        # 只查前缀会让 `ya29.` 这种"前缀齐全、值为空"的令牌通过 —— 与 jira/linear/notion
        # 的空令牌是同一类缺陷：所有正常测试都带真令牌，没人试空串。
        if not token.startswith("ya29.") or len(token) <= len("ya29."):
            return Response(401, GOOGLE_ERRORS.unauthorized("malformed token"))
        granted = self._expand(req.run.state[self.name]["granted_scopes"])
        if need not in granted and "*" not in granted:
            return Response(403, GOOGLE_ERRORS.forbidden(need))
        return None

    def authenticate(self, req: Request) -> Response | None:
        return self._auth(req)

    def dispatch(self, req: Request) -> Response:
        """**所有非 admin 请求先鉴权**，再进路由。

        为什么要在 dispatch 层兜一次而不是每个 handler 自己判：
        handler 里忘一行，那个端点就变成匿名可读。gmail 的 get_profile 起初就是
        这样漏的 —— 测试里"缺 token 应 401"直接拿到 200。兜一层之后，
        单个 handler 漏判最多泄露它自己的数据形状，不会让整个收件箱可读。
        """
        if not req.path.startswith("/admin"):
            bad = self._auth(req)
            if bad is not None:
                return bad
        return super().dispatch(req)

    # ---------- 路由 ----------

    def _build_routes(self) -> None:
        p = r"/gmail/v1/users/{user}"
        self.route("GET", p + "/profile", self.get_profile)
        self.route("GET", p + "/messages", self.list_messages)
        self.route("GET", p + "/messages/{mid}", self.get_message)
        self.route("POST", p + "/messages/send", self.send_message)
        self.route("POST", p + "/messages/{mid}/modify", self.modify_message)
        self.route("POST", p + "/messages/{mid}/trash", self.trash_message)
        self.route("GET", p + "/threads", self.list_threads)
        self.route("GET", p + "/threads/{tid}", self.get_thread)
        self.route("GET", p + "/labels", self.list_labels)
        self.route("GET", p + "/drafts", self.list_drafts)
        self.route("POST", p + "/drafts", self.create_draft)

    # ---------- 视图 ----------

    def _summary(self, msg: dict) -> dict:
        """列表视图：**只有 id 和 threadId**。

        真 Gmail 就是这样。很多客户端以为 list 会带 subject，然后在真环境里
        发现要再打一次 get —— 这个"多一跳"必须复刻，否则 twin 比真实世界更宽容。
        """
        return {"id": msg["id"], "threadId": msg["threadId"]}

    def _full(self, msg: dict, fmt: str) -> dict:
        base = {
            "id": msg["id"],
            "threadId": msg["threadId"],
            "labelIds": list(msg["labelIds"]),
            "internalDate": str(int(msg["internalDate"] * 1000)),
            "sizeEstimate": len(msg["raw"]),
        }
        if fmt == "minimal":
            return base
        base["snippet"] = msg["body"][:120]
        if fmt == "metadata":
            base["payload"] = {"headers": [{"name": "Subject", "value": msg["subject"]},
                                           {"name": "From", "value": msg["from"]},
                                           {"name": "To", "value": ", ".join(msg["to"])}]}
            return base
        base["payload"] = {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": msg["subject"]},
                        {"name": "From", "value": msg["from"]},
                        {"name": "To", "value": ", ".join(msg["to"])}],
            "body": {"size": len(msg["body"]), "data": _b64url_encode(msg["body"].encode())},
        }
        base["raw"] = msg["raw"]
        return base

    def _store_message(self, state: dict, parsed: dict, raw: str, labels: list[str]) -> dict:
        mid = state["next_id"]
        state["next_id"] += 1
        msg = {
            "id": f"msg_{mid:04d}",
            "threadId": f"thr_{mid:04d}",
            "labelIds": labels,
            "internalDate": time.time(),
            "subject": parsed["subject"],
            "from": parsed["from"] or state["profile"]["emailAddress"],
            "to": _addr_list(parsed["to"]),
            "body": parsed["body"],
            "raw": raw,
        }
        state["messages"][msg["id"]] = msg
        state["profile"]["messagesTotal"] = len(state["messages"])
        state["profile"]["threadsTotal"] = len({m["threadId"] for m in state["messages"].values()})
        return msg

    # ---------- handlers ----------

    def get_profile(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        return Response(200, dict(state["profile"]))

    def list_messages(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        q = (req.query_one("q") or "").strip()
        rows = sorted(state["messages"].values(), key=lambda m: m["internalDate"], reverse=True)
        if q:
            # 只支持最常用的三种：label:X、is:unread、from:x —— 其余的明确回 400，
            # 而不是"假装搜到了空结果"（后者会让 agent 以为邮箱是空的）。
            m = re.match(r"^label:(\S+)", q)
            if m:
                want = m.group(1)
                rows = [r for r in rows if want in r["labelIds"]]
            elif q == "is:unread":
                rows = [r for r in rows if "UNREAD" in r["labelIds"]]
            elif q.startswith("from:"):
                who = q[5:].strip()
                rows = [r for r in rows if who.lower() in r["from"].lower()]
            else:
                return Response(400, GOOGLE_ERRORS.bad_request(
                    f"unsupported q: {q!r} (only label:, is:unread, from:)"), )
        limit = int(req.query_one("maxResults") or 100)
        return Response(200, {
            "messages": [self._summary(r) for r in rows[:limit]],
            "resultSizeEstimate": len(rows),
        })

    def _message_or_404(self, req: Request, params: dict) -> tuple[dict, dict] | Response:
        state = req.run.state[self.name]
        mid = params["mid"]
        msg = state["messages"].get(mid)
        if msg is None:
            return Response(404, GOOGLE_ERRORS.not_found(mid))
        return msg, state

    def get_message(self, req: Request, params: dict) -> Response:
        got = self._message_or_404(req, params)
        if isinstance(got, Response):
            return got
        msg, _ = got
        fmt = req.query_one("format") or "full"
        if fmt not in ("minimal", "metadata", "full"):
            return Response(400, GOOGLE_ERRORS.bad_request(f"format={fmt}"))
        return Response(200, self._full(msg, fmt))

    def send_message(self, req: Request, params: dict) -> Response:
        bad = self._auth(req, "https://www.googleapis.com/auth/gmail.send")
        if bad:
            return bad
        state = req.run.state[self.name]
        raw = (req.json() or {}).get("raw")
        if not raw:
            return Response(400, GOOGLE_ERRORS.bad_request("raw is required"))
        try:
            decoded = _b64url_decode(raw)
        except Exception:
            return Response(400, GOOGLE_ERRORS.bad_request("raw is not valid base64url"))
        parsed = parse_mime(decoded)
        if not parsed["to"]:
            return Response(400, GOOGLE_ERRORS.bad_request("message has no To header"))
        msg = self._store_message(state, parsed, raw, ["SENT"])
        # 跨 twin 身份：收件人如果是已知的人，把这次发信记到同一个"人"名下
        for addr in _addr_list(parsed["to"]):
            req.store.link_person(req.run_id, addr, "gmail", msg["id"])
        return Response(200, self._full(msg, "full"))

    def modify_message(self, req: Request, params: dict) -> Response:
        bad = self._auth(req, "https://www.googleapis.com/auth/gmail.modify")
        if bad:
            return bad
        got = self._message_or_404(req, params)
        if isinstance(got, Response):
            return got
        msg, state = got
        body = req.json() or {}
        for label in body.get("addLabelIds") or []:
            if label not in state["labels"]:
                return Response(400, GOOGLE_ERRORS.bad_request(f"unknown label {label}"))
            if label not in msg["labelIds"]:
                msg["labelIds"].append(label)
        for label in body.get("removeLabelIds") or []:
            if label in msg["labelIds"]:
                msg["labelIds"].remove(label)
        return Response(200, self._full(msg, "full"))

    def trash_message(self, req: Request, params: dict) -> Response:
        bad = self._auth(req, "https://www.googleapis.com/auth/gmail.modify")
        if bad:
            return bad
        got = self._message_or_404(req, params)
        if isinstance(got, Response):
            return got
        msg, _ = got
        if "TRASH" not in msg["labelIds"]:
            msg["labelIds"].append("TRASH")
        if "INBOX" in msg["labelIds"]:
            msg["labelIds"].remove("INBOX")
        return Response(200, self._full(msg, "minimal"))

    def list_threads(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        seen: dict[str, list[str]] = {}
        for m in state["messages"].values():
            seen.setdefault(m["threadId"], []).append(m["id"])
        return Response(200, {
            "threads": [{"id": tid, "snippet": state["messages"][ids[0]]["body"][:60]}
                        for tid, ids in seen.items()],
            "resultSizeEstimate": len(seen),
        })

    def get_thread(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        tid = params["tid"]
        msgs = [m for m in state["messages"].values() if m["threadId"] == tid]
        if not msgs:
            return Response(404, GOOGLE_ERRORS.not_found(tid))
        return Response(200, {"id": tid, "messages": [self._full(m, "full") for m in msgs]})

    def list_labels(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        return Response(200, {"labels": list(state["labels"].values())})

    def list_drafts(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        return Response(200, {"drafts": [{"id": d["id"], "message": {"id": d["messageId"]}}
                                         for d in state["drafts"].values()]})

    def create_draft(self, req: Request, params: dict) -> Response:
        bad = self._auth(req, "https://www.googleapis.com/auth/gmail.modify")
        if bad:
            return bad
        state = req.run.state[self.name]
        raw = (req.json() or {}).get("message", {}).get("raw")
        if not raw:
            return Response(400, GOOGLE_ERRORS.bad_request("message.raw is required"))
        parsed = parse_mime(_b64url_decode(raw))
        msg = self._store_message(state, parsed, raw, ["DRAFT"])
        did = f"draft_{len(state['drafts']) + 1:04d}"
        state["drafts"][did] = {"id": did, "messageId": msg["id"]}
        # 草稿是"没发出去的邮件"：把它从 SENT 里摘掉，否则判据会把草稿当成已发送
        msg["labelIds"] = ["DRAFT"]
        return Response(200, {"id": did, "message": self._full(msg, "full")})

    def handle_admin(self, req: Request) -> Response:
        state = req.run.state[self.name]
        parts = [p for p in req.path.split("/") if p]
        action = parts[1] if len(parts) > 1 else ""
        if action == "scopes":
            if req.method == "GET":
                return Response(200, {
                    "granted": state["granted_scopes"],
                    "effective": sorted(self._expand(state["granted_scopes"])),
                })
            state["granted_scopes"] = (req.json() or {}).get("scopes") or []
            return Response(200, {"granted": state["granted_scopes"]})
        if action == "seed":
            body = req.json() or {}
            for m in body.get("messages") or []:
                raw = _b64url_encode(
                    f"To: {m.get('to', '')}\r\nFrom: {m.get('from', '')}\r\n"
                    f"Subject: {m.get('subject', '')}\r\n\r\n{m.get('body', '')}".encode()
                )
                parsed = {"to": m.get("to", ""), "from": m.get("from", ""),
                          "subject": m.get("subject", ""), "body": m.get("body", "")}
                msg = self._store_message(state, parsed, raw, m.get("labels") or ["INBOX"])
                msg["internalDate"] = time.time() - float(m.get("age_seconds") or 0)
                if m.get("to"):
                    req.store.link_person(req.run_id, m["to"], "gmail", msg["id"])
            if body.get("profile"):
                state["profile"].update(body["profile"])
            return Response(200, {"seeded": len(body.get("messages") or [])})
        return Response(404, GOOGLE_ERRORS.not_found(req.path))
