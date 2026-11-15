"""Slack twin。

Slack 的保真点跟 Stripe 完全相反，这是最容易被做错的地方：

  1. **HTTP 永远 200**，成功失败看 body 里的 `ok`。用状态码判错的客户端在这里会
     把全部失败当成功。
  2. **鉴权是 scope 而不是 key 有效性**：token 无效是 `invalid_auth`，token 有效但
     scope 不够是 `missing_scope` 且要带上 needed/provided——这两种错 agent 的应对
     方式完全不同（一个要重新登录，一个要降级）。
  3. **商业档位真的改行为**：free 档的历史消息有 90 天窗口，超过就查不到。
"""

from __future__ import annotations

import time
from typing import Any

from ..store import new_id
from .base import Request, Response, Twin


# method → 需要的 scope。抄自 Slack 官方文档，不是拍脑袋。
METHOD_SCOPES: dict[str, str] = {
    "chat.postMessage": "chat:write",
    "chat.update": "chat:write",
    "chat.delete": "chat:write",
    "conversations.list": "channels:read",
    "conversations.history": "channels:history",
    "channels.history": "channels:history",
    "pins.list": "pins:read",
    "pins.add": "pins:write",
    "pins.remove": "pins:write",
    "conversations.open": "im:write",
    "conversations.members": "channels:read",
    "users.list": "users:read",
    "users.info": "users:read",
    "users.lookupByEmail": "users:read.email",
    "conversations.replies": "channels:history",
    "conversations.info": "channels:read",
    "conversations.create": "channels:manage",
    "conversations.join": "channels:join",
    "usergroups.list": "usergroups:read",
    "search.messages": "search:read",
    "files.list": "files:read",
    "im.list": "im:read",
    "files.upload": "files:write",
    "auth.test": "",
}

TIERS = ("free", "pro", "business_plus", "enterprise", "paid")
HISTORY_WINDOW = {"free": 90 * 86400}  # 其余档位无限
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 GiB，跟 Arga 文档里的数字一致


def _now() -> int:
    return int(time.time())


class SlackTwin(Twin):
    name = "slack"

    # 真 Slack 的 Web API 方法**一律 POST**（`POST /api/chat.postMessage`）。
    # 所以 `GET /api/...` 不存在 —— 120 episodes 里这类请求是把 GitHub 的
    # `GET /api/repos/...` 或路径猜测打到了 Slack 的 Host 上。
    # `/v1/...` 是 Stripe 的形状，同理。
    absent_paths = (
        ("GET", "/api/**", "真 Slack 的 Web API 方法一律 POST；GET /api/... 不存在。"),
        ("*", "/v1/*", "Slack 没有 /v1 REST 前缀（那是 Stripe 的形状），多半是 Host 用错。"),
        ("*", "/repos/*", "GitHub 的仓库路径打到了 Slack 的 Host 上。"),
    )

    # 声明的边界：外部上传流（files.getUploadURLExternal / completeUploadExternal）
    # 不仿真。它把字节交给第三方 URL，孪生给不出那个 URL 的语义。
    declared_stubs = (
        ("POST", "/api/files.getUploadURLExternal", "不打算实现：外部上传流需要一个真实的对象存储 URL。"),
        ("POST", "/api/files.completeUploadExternal", "不打算实现：外部上传流的收尾步骤（同上）。"),
    )

    def initial_state(self) -> dict[str, Any]:
        return {
            "users": {},
            "channels": {},
            "messages": [],      # 全局按时间排序，带 channel 字段
            "files": {},
            "tier": "pro",
            "granted_scopes": ["*"],   # 默认全给；scenario 可以收窄
            "next_ts": _now() * 1000,  # Slack 的 ts 是 "秒.微秒" 字符串，这里用整数微秒
        }

    # ---------- 路由 ----------

    def _build_routes(self) -> None:
        self.route("POST", "/api/chat.postMessage", self.chat_post_message)
        self.route("POST", "/api/conversations.list", self.conversations_list)
        self.route("GET", "/api/conversations.list", self.conversations_list)
        self.route("POST", "/api/conversations.history", self.conversations_history)
        self.route("GET", "/api/conversations.history", self.conversations_history)
        # 老方法名仍被一半客户端在用（Slack 自己也没停用），少它就把 "翻历史" 判成 stub
        self.route("POST", "/api/channels.history", self.conversations_history)
        self.route("GET", "/api/channels.history", self.conversations_history)
        self.route("POST", "/api/pins.list", self.pins_list)
        self.route("POST", "/api/pins.add", self.pins_add)
        self.route("POST", "/api/pins.remove", self.pins_remove)
        self.route("POST", "/api/conversations.open", self.conversations_open)
        self.route("POST", "/api/users.list", self.users_list)
        self.route("GET", "/api/users.list", self.users_list)
        self.route("POST", "/api/auth.test", self.auth_test)
        self.route("POST", "/api/files.upload", self.files_upload)
        # 读会话的其余入口。480 episodes 里 conversations.replies 被撞了 16 次 ——
        # 看到一条消息想知道"后面还说了什么"是必然动作，缺了它 agent 只能猜。
        self.route("POST", "/api/conversations.replies", self.conversations_replies)
        self.route("POST", "/api/conversations.members", self.conversations_members)
        self.route("POST", "/api/conversations.info", self.conversations_info)
        self.route("POST", "/api/conversations.create", self.conversations_create)
        self.route("POST", "/api/conversations.join", self.conversations_join)
        self.route("POST", "/api/users.info", self.users_info)
        self.route("POST", "/api/users.lookupByEmail", self.users_lookup_by_email)
        self.route("POST", "/api/usergroups.list", self.usergroups_list)
        self.route("POST", "/api/search.messages", self.search_messages)
        self.route("POST", "/api/files.list", self.files_list)
        self.route("POST", "/api/im.list", self.im_list)
        self.route("GET", "/api/conversations.list", self.conversations_list)

    # ---------- 通用回应 ----------

    def _ok(self, **payload: Any) -> Response:
        return Response(200, {"ok": True, **payload})

    def _fail(self, error: str, **extra: Any) -> Response:
        """Slack 的错误一律 200。"""
        return Response(200, {"ok": False, "error": error, **extra})

    # ---------- 鉴权 + scope ----------

    def _check(self, req: Request, method: str) -> Response | None:
        auth = req.header("authorization")
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        if not token:
            token = req.query_one("token") or str(req.json().get("token") or "")
        if not token.startswith(("xoxb-", "xoxp-", "xoxe.", "xoxa-")):
            return self._fail("invalid_auth", **{"_twin_hint": "token must look like xoxb-…"})

        state = req.run.state[self.name]
        granted: list[str] = list(state["granted_scopes"])
        needed = METHOD_SCOPES.get(method, "")
        if not needed or "*" in granted or needed in granted:
            return None
        return self._fail(
            "missing_scope",
            needed=needed,
            provided=",".join(granted),
        )

    def _next_ts(self, state: dict[str, Any]) -> str:
        state["next_ts"] += 1
        micro = state["next_ts"]
        return f"{micro // 1_000_000}.{micro % 1_000_000:06d}"

    # ---------- 方法 ----------

    def auth_test(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "auth.test")) is not None:
            return bad
        return self._ok(
            url="https://argaclone.slack.local/",
            team="Arga Clone",
            user="twin-bot",
            team_id="T0TWIN0001",
            user_id="U0TWINBOT",
            bot_id="B0TWINBOT",
        )

    def chat_post_message(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "chat.postMessage")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()

        channel = body.get("channel")
        if not channel:
            return self._fail("channel_not_found", **{"_twin_hint": "channel is required"})
        resolved = self._resolve_channel(state, channel)
        if resolved is None:
            return self._fail(
                "channel_not_found",
                **{"_twin_hint": "known channels: " + ", ".join(sorted(c["name"] for c in state["channels"].values()))},
            )
        channel = resolved

        text = body.get("text") or ""
        blocks = body.get("blocks")
        if not text and not blocks:
            return self._fail("no_text")

        ts = self._next_ts(state)
        msg = {
            "type": "message",
            "ts": ts,
            "text": text,
            "user": "U0TWINBOT",
            "bot_id": "B0TWINBOT",
            "channel": channel,
            "thread_ts": body.get("thread_ts"),
            "blocks": blocks,
            "created": _now(),
        }
        state["messages"].append(msg)
        return self._ok(channel=channel, ts=ts, message=msg)

    def _resolve_channel(self, state: dict, channel: str) -> str | None:
        """id 或名字都能用。

        真 Slack 的 chat.postMessage 接受 `#name` 与 channel id 两种写法，
        twin 只认 id 会让所有照文档写的 agent 在这里撞一次 404 —— 那不是
        agent 的错，是 twin 的保真缺口。这里补齐，并接受带 `#` 前缀的形式。
        """
        if channel in state["channels"]:
            return channel
        name = channel[1:] if channel.startswith("#") else channel
        for cid, ch in state["channels"].items():
            if ch.get("name") == name:
                return cid
        return None

    def conversations_list(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "conversations.list")) is not None:
            return bad
        state = req.run.state[self.name]
        chans = list(state["channels"].values())
        return self._ok(channels=chans, response_metadata={"next_cursor": ""})

    def pins_list(self, req: Request, _: dict[str, str]) -> Response:
        """置顶消息是"这条必须被看到"的取证面。返回里要带消息正文，
        只回 ts 的话调用方还得再查一次，而再查那一步常常就是它放弃的地方。"""
        if (bad := self._check(req, "pins.list")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        channel = body.get("channel")
        items = []
        for pin in state.get("pins", []):
            if channel and pin["channel"] != channel:
                continue
            msg = next((m for m in state["messages"]
                        if m["channel"] == pin["channel"] and m["ts"] == pin["ts"]), None)
            if msg is None:
                continue
            items.append({"channel": pin["channel"], "created": pin.get("created", 0),
                          "created_by": pin.get("created_by", "U_SYSTEM"),
                          "message": msg})
        return self._ok(items=items)

    def pins_add(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "pins.add")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        channel, ts = body.get("channel"), body.get("timestamp") or body.get("ts")
        if channel not in state["channels"]:
            return self._fail("channel_not_found")
        if not any(m["channel"] == channel and m["ts"] == ts for m in state["messages"]):
            return self._fail("message_not_found")
        pins = state.setdefault("pins", [])
        if not any(p["channel"] == channel and p["ts"] == ts for p in pins):
            pins.append({"channel": channel, "ts": ts, "created": _now(),
                         "created_by": "U_SYSTEM"})
        return self._ok()

    def pins_remove(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "pins.remove")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        channel, ts = body.get("channel"), body.get("timestamp") or body.get("ts")
        state["pins"] = [p for p in state.get("pins", [])
                         if not (p["channel"] == channel and p["ts"] == ts)]
        return self._ok()

    def conversations_history(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "conversations.history")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        channel = body.get("channel") or req.query_one("channel")
        if channel not in state["channels"]:
            return self._fail("channel_not_found")

        msgs = [m for m in state["messages"] if m["channel"] == channel]

        # 档位真的改行为：free 档只能看 90 天内
        window = HISTORY_WINDOW.get(state["tier"])
        if window is not None:
            cutoff = _now() - window
            msgs = [m for m in msgs if m["created"] >= cutoff]

        msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
        limit = int(req.query_one("limit", "100") or body.get("limit") or 100)
        trimmed = [
            {
                "type": "message",
                "ts": m["ts"],
                "text": m["text"],
                "user": m["user"],
                "bot_id": m.get("bot_id"),
                "blocks": m.get("blocks"),
            }
            for m in msgs[:limit]
        ]
        return self._ok(messages=trimmed, has_more=len(msgs) > limit)

    def conversations_open(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "conversations.open")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        users = body.get("users") or ""
        if not users:
            return self._fail("users_list_not_specified")
        first = users.split(",")[0].strip()
        cid = f"D{first[-8:].upper()}" if first else "D0TWIN000"
        if cid not in state["channels"]:
            state["channels"][cid] = {
                "id": cid,
                "name": "directmessage",
                "is_channel": False,
                "is_im": True,
                "is_private": True,
                "created": _now(),
                "num_members": 2,
            }
        return self._ok(channel={"id": cid})

    def users_list(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "users.list")) is not None:
            return bad
        state = req.run.state[self.name]
        return self._ok(
            members=list(state["users"].values()),
            response_metadata={"next_cursor": ""},
        )

    # ---------- 读会话 ----------

    def conversations_replies(self, req: Request, _: dict[str, str]) -> Response:
        """读一个 thread。

        Slack 的 thread 语义容易抄错：父消息自己的 `thread_ts` 是空的，
        但按 thread_ts 查询时必须**把它自己也算进去**（Slack 就是这么回的）。
        漏掉父消息，agent 会以为"这条消息没内容"，然后去别处找上下文。
        """
        if (bad := self._check(req, "conversations.replies")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        channel = body.get("channel") or req.query_one("channel")
        ts = body.get("ts") or req.query_one("ts")
        resolved = self._resolve_channel(state, channel or "")
        if resolved is None:
            return self._fail("channel_not_found")
        if not ts:
            return self._fail("thread_not_found")
        msgs = [
            m for m in state["messages"]
            if m["channel"] == resolved and m.get("ts") == ts or
               (m["channel"] == resolved and m.get("thread_ts") == ts)
        ]
        msgs.sort(key=lambda m: float(m["ts"]))
        return self._ok(
            messages=[{"type": "message", "ts": m["ts"], "text": m["text"],
                       "user": m["user"], "bot_id": m.get("bot_id"),
                       "thread_ts": m.get("thread_ts")} for m in msgs],
            has_more=False,
        )

    def conversations_members(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "conversations.members")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        resolved = self._resolve_channel(state, body.get("channel") or req.query_one("channel") or "")
        if resolved is None:
            return self._fail("channel_not_found")
        ch = state["channels"][resolved]
        members = ch.get("members")
        if members is None:
            # 没有显式成员表时，按"发过言的人"推断 —— 空列表会让 agent
            # 以为频道里没人，进而判断"这个提醒没人看到"。
            members = sorted({m["user"] for m in state["messages"] if m["channel"] == resolved})
        return self._ok(members=members, response_metadata={"next_cursor": ""})

    def conversations_info(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "conversations.info")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        resolved = self._resolve_channel(state, body.get("channel") or req.query_one("channel") or "")
        if resolved is None:
            return self._fail("channel_not_found")
        return self._ok(channel=state["channels"][resolved])

    def conversations_create(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "conversations.create")) is not None:
            return bad
        state = req.run.state[self.name]
        name = (req.json() or {}).get("name")
        if not name:
            return self._fail("invalid_name_required")
        for ch in state["channels"].values():
            if ch.get("name") == name:
                return self._fail("name_taken")
        cid = new_id("C").upper()[:11]
        state["channels"][cid] = {
            "id": cid, "name": name, "is_channel": True, "is_private": False,
            "created": _now(), "num_members": 1,
        }
        return self._ok(channel=state["channels"][cid])

    def conversations_join(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "conversations.join")) is not None:
            return bad
        state = req.run.state[self.name]
        resolved = self._resolve_channel(state, (req.json() or {}).get("channel") or "")
        if resolved is None:
            return self._fail("channel_not_found")
        return self._ok(channel=state["channels"][resolved])

    def users_info(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "users.info")) is not None:
            return bad
        state = req.run.state[self.name]
        uid = (req.json() or {}).get("user") or req.query_one("user")
        user = state["users"].get(uid or "")
        if not user:
            return self._fail("user_not_found")
        return self._ok(user=user)

    def users_lookup_by_email(self, req: Request, _: dict[str, str]) -> Response:
        """按 email 找人。跨系统对账里"这个人是谁"的最高频入口。"""
        if (bad := self._check(req, "users.lookupByEmail")) is not None:
            return bad
        state = req.run.state[self.name]
        email = ((req.json() or {}).get("email") or req.query_one("email") or "").lower()
        if not email:
            return self._fail("users_not_found")
        for user in state["users"].values():
            if ((user.get("profile") or {}).get("email") or "").lower() == email:
                return self._ok(user=user)
        return self._fail("users_not_found")

    def usergroups_list(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "usergroups.list")) is not None:
            return bad
        return self._ok(usergroups=req.run.state[self.name].get("usergroups", []))

    def search_messages(self, req: Request, _: dict[str, str]) -> Response:
        """只做子串匹配。

        真 Slack 的 search 有修饰符（`in:#chan`、`from:@user`、`before:`），
        复刻解析器不值得；但"搜一下有没有人提过这件事"是分诊的第一步。
        不认的修饰符**原样当子串**处理，而不是静默返回全量 —— 后者会让
        agent 以为"搜到了很多"，比搜不到更危险。
        """
        if (bad := self._check(req, "search.messages")) is not None:
            return bad
        state = req.run.state[self.name]
        q = ((req.json() or {}).get("query") or req.query_one("query") or "").strip()
        hits = [
            {"type": "message", "ts": m["ts"], "text": m["text"], "user": m["user"],
             "channel": {"id": m["channel"],
                         "name": (state["channels"].get(m["channel"]) or {}).get("name")}}
            for m in state["messages"]
            if q.lower() in (m["text"] or "").lower()
        ]
        return self._ok(
            messages={"total": len(hits), "matches": hits, "paging": {"count": 20, "total": len(hits)}},
            query=q,
        )

    def files_list(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "files.list")) is not None:
            return bad
        return self._ok(files=list(req.run.state[self.name]["files"].values()),
                        response_metadata={"next_cursor": ""})

    def im_list(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "im.list")) is not None:
            return bad
        state = req.run.state[self.name]
        dms = [ch for ch in state["channels"].values() if ch.get("is_im")]
        return self._ok(channels=dms, response_metadata={"next_cursor": ""})

    def files_upload(self, req: Request, _: dict[str, str]) -> Response:
        if (bad := self._check(req, "files.upload")) is not None:
            return bad
        state = req.run.state[self.name]
        body = req.json()
        size = int(body.get("length") or 0)
        if size > MAX_FILE_BYTES:
            return self._fail(
                "file_too_large",
                tier=state["tier"],
                max_file_bytes=MAX_FILE_BYTES,
                attempted_bytes=size,
            )
        fid = new_id("F", 10).upper()
        state["files"][fid] = {
            "id": fid,
            "name": body.get("filename"),
            "size": size,
            "created": _now(),
        }
        return self._ok(file=state["files"][fid])

    # ---------- admin ----------

    def authenticate(self, req: Request) -> Response | None:
        return self._check(req, "")

    def handle_admin(self, req: Request) -> Response:
        state = req.run.state[self.name]

        if req.path == "/admin/tier" and req.method in ("GET", "POST"):
            if req.method == "POST":
                body = req.json()
                tier = body.get("tier")
                if tier not in TIERS:
                    return Response(400, {"error": f"tier must be one of {TIERS}"})
                state["tier"] = tier
            return Response(200, {"tier": state["tier"], "available": list(TIERS)})

        if req.path == "/admin/scopes" and req.method in ("GET", "POST"):
            if req.method == "POST":
                body = req.json()
                scopes = body.get("granted_scopes")
                if not isinstance(scopes, list):
                    return Response(400, {"error": "granted_scopes must be a list"})
                state["granted_scopes"] = scopes
            return Response(200, {"granted_scopes": state["granted_scopes"]})

        if req.path == "/admin/seed-user" and req.method == "POST":
            body = req.json()
            email = body.get("email")
            if not email:
                return Response(400, {"error": "email required"})
            uid = new_id("U", 10).upper()
            state["users"][uid] = {
                "id": uid,
                "team_id": "T0TWIN0001",
                "name": body.get("name") or email.split("@")[0],
                "real_name": body.get("real_name") or email.split("@")[0],
                "profile": {"email": email, "display_name": email.split("@")[0]},
                "deleted": False,
                "is_bot": False,
            }
            req.store.link_person(req.run_id, email, "slack", uid)
            return Response(200, state["users"][uid])

        if req.path == "/admin/seed-channel" and req.method == "POST":
            body = req.json()
            cid = body.get("id") or new_id("C", 10).upper()
            state["channels"][cid] = {
                "id": cid,
                "name": body.get("name") or "general",
                "is_channel": True,
                "is_im": False,
                "is_private": bool(body.get("is_private")),
                "created": _now(),
                "num_members": body.get("num_members", 2),
                "topic": {"value": body.get("topic") or ""},
            }
            return Response(200, state["channels"][cid])

        return Response(404, {"error": "no such admin route"})
