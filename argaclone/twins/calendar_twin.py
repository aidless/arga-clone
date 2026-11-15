"""Google Calendar twin：事件、参会人、**通知语义**。

为什么日历单独做而不是并进 Gmail：日历类任务的核心危险是**通知**。
"把会议改到明天下午"在模型看来是改一个字段，在真实世界里是给 8 个人发了邮件。
所以 `sendUpdates` 必须保真 —— 改了时间却没通知，是这个域最典型的静默事故。

保真清单（实现）：
  GET  /calendar/v3/users/me/calendarList
  GET  /calendar/v3/calendars/{cal}/events          timeMin/timeMax/singleEvents/q/maxResults
  POST /calendar/v3/calendars/{cal}/events          sendUpdates=all|externalOnly|none
  GET  /calendar/v3/calendars/{cal}/events/{eid}
  PATCH /calendar/v3/calendars/{cal}/events/{eid}   同样受 sendUpdates 管
  DELETE /calendar/v3/calendars/{cal}/events/{eid}  取消 ≠ 删除（status=cancelled）
  POST /calendar/v3/calendars/{cal}/events/{eid}/move
  POST /calendar/v3/freeBusy                        判断时段冲突的正规入口

刻意不实现：recurring events（RRULE）、ACL、watch/channels、conferenceData。
`events.list` 遇到不支持的时间格式会 400，不静默当作"没有事件"。
"""

from __future__ import annotations

import datetime as dt
import time

from .base import Request, Response, Twin, bearer
from .gmail_twin import GOOGLE_ERRORS

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _rfc3339_to_ts(value: str) -> float | None:
    """解析 RFC3339。解析不了返回 None，由调用方决定报错还是忽略。"""
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


def _ts_to_rfc3339(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CalendarTwin(Twin):
    name = "calendar"

    def _build_routes(self) -> None:
        self.route("GET", "/calendar/v3/users/me/calendarList", self.list_calendars)
        self.route("GET", "/calendar/v3/calendars/{cal}/events", self.list_events)
        self.route("POST", "/calendar/v3/calendars/{cal}/events", self.insert_event)
        self.route("GET", "/calendar/v3/calendars/{cal}/events/{eid}", self.get_event)
        self.route("PATCH", "/calendar/v3/calendars/{cal}/events/{eid}", self.patch_event)
        self.route("DELETE", "/calendar/v3/calendars/{cal}/events/{eid}", self.cancel_event)
        self.route("POST", "/calendar/v3/calendars/{cal}/events/{eid}/move", self.move_event)
        self.route("POST", "/calendar/v3/freeBusy", self.free_busy)

    def initial_state(self) -> dict:
        return {
            "calendars": {
                "primary": {"id": "primary", "summary": "ops@acme.example", "timeZone": "UTC"},
                "team": {"id": "team", "summary": "Platform Team", "timeZone": "UTC"},
            },
            "events": {},
            "notifications": [],   # 谁被通知了、为什么 —— 通知是真实副作用，必须可观测
            "granted_scopes": list(DEFAULT_SCOPES),
            "next_id": 1,
        }

    # ---------- 鉴权 ----------

    def _auth(self, req: Request) -> Response | None:
        token = bearer(req)
        if not token or not token.startswith("ya29.") or len(token) <= len("ya29."):
            return Response(401, GOOGLE_ERRORS.unauthorized("missing or malformed token"))
        granted = req.run.state[self.name]["granted_scopes"]
        if "https://www.googleapis.com/auth/calendar" not in granted and "*" not in granted:
            return Response(403, GOOGLE_ERRORS.forbidden("calendar scope"))
        return None

    def authenticate(self, req: Request) -> Response | None:
        return self._auth(req)

    def dispatch(self, req: Request) -> Response:
        if not req.path.startswith("/admin"):
            bad = self._auth(req)
            if bad is not None:
                return bad
        return super().dispatch(req)

    # ---------- 通知 ----------

    def _notify(self, req: Request, event: dict, send_updates: str, reason: str) -> None:
        """按 sendUpdates 决定通知谁。这一步是**副作用**，不是渲染。

        默认值取 `none` 还是 `all` 是个真实的工程选择：真 Google 默认 none（不通知），
        所以"改完就以为对方知道了"是 agent 最常见的错误。这里取真值。
        """
        if send_updates not in ("all", "externalOnly"):
            return
        state = req.run.state[self.name]
        for a in event.get("attendees") or []:
            if send_updates == "externalOnly" and not a.get("external"):
                continue
            state["notifications"].append({
                "eventId": event["id"],
                "email": a.get("email"),
                "reason": reason,
                "sentAt": _ts_to_rfc3339(time.time()),
            })

    # ---------- 视图 ----------

    @staticmethod
    def _view(e: dict) -> dict:
        return {
            "kind": "calendar#event",
            "id": e["id"],
            "status": e["status"],
            "summary": e["summary"],
            "description": e.get("description") or "",
            "location": e.get("location") or "",
            "start": dict(e["start"]),
            "end": dict(e["end"]),
            "attendees": [dict(a) for a in e.get("attendees") or []],
            "organizer": {"email": e.get("organizer") or "ops@acme.example"},
            "htmlLink": f"https://calendar.google.com/event?eid={e['id']}",
            "updated": e["updated"],
        }

    @staticmethod
    def _times(body: dict) -> tuple[float | None, float | None, dict, dict]:
        start = body.get("start") or {}
        end = body.get("end") or {}
        si = start.get("dateTime") or start.get("date")
        ei = end.get("dateTime") or end.get("date")
        return _rfc3339_to_ts(si or ""), _rfc3339_to_ts(ei or ""), start, end

    # ---------- handlers ----------

    def list_calendars(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        return Response(200, {"kind": "calendar#calendarList",
                              "items": list(state["calendars"].values())})

    def list_events(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        cal = params["cal"]
        if cal not in state["calendars"]:
            return Response(404, GOOGLE_ERRORS.not_found(f"calendar {cal}"))

        rows = [e for e in state["events"].values() if e["calendarId"] == cal]
        if not req.query_one("showDeleted"):
            rows = [e for e in rows if e["status"] != "cancelled"]

        time_min, time_max = req.query_one("timeMin"), req.query_one("timeMax")
        if time_min:
            ts = _rfc3339_to_ts(time_min)
            if ts is None:
                return Response(400, GOOGLE_ERRORS.bad_request(f"timeMin is not RFC3339: {time_min}"))
            rows = [e for e in rows if e["startTs"] >= ts]
        if time_max:
            ts = _rfc3339_to_ts(time_max)
            if ts is None:
                return Response(400, GOOGLE_ERRORS.bad_request(f"timeMax is not RFC3339: {time_max}"))
            rows = [e for e in rows if e["startTs"] < ts]

        q = req.query_one("q")
        if q:
            rows = [e for e in rows if q.lower() in (e["summary"] + " " + (e.get("description") or "")).lower()]

        rows.sort(key=lambda e: e["startTs"])
        limit = int(req.query_one("maxResults") or 250)
        return Response(200, {"kind": "calendar#events", "items": [self._view(e) for e in rows[:limit]]})

    def insert_event(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        cal = params["cal"]
        if cal not in state["calendars"]:
            return Response(404, GOOGLE_ERRORS.not_found(f"calendar {cal}"))
        body = req.json() or {}
        if not body.get("summary"):
            return Response(400, GOOGLE_ERRORS.bad_request("summary is required"))
        start_ts, end_ts, start, end = self._times(body)
        if start_ts is None or end_ts is None:
            return Response(400, GOOGLE_ERRORS.bad_request(
                "start/end must provide dateTime (RFC3339) or date"))
        if end_ts <= start_ts:
            return Response(400, GOOGLE_ERRORS.bad_request("end must be after start"))

        eid = f"evt_{state['next_id']:04d}"
        state["next_id"] += 1
        event = {
            "id": eid,
            "calendarId": cal,
            "status": "confirmed",
            "summary": body["summary"],
            "description": body.get("description"),
            "location": body.get("location"),
            "start": start,
            "end": end,
            "startTs": start_ts,
            "endTs": end_ts,
            "attendees": body.get("attendees") or [],
            "organizer": "ops@acme.example",
            "updated": _ts_to_rfc3339(time.time()),
        }
        state["events"][eid] = event
        self._notify(req, event, req.query_one("sendUpdates") or "none", "created")
        return Response(200, self._view(event))

    def _event_or_404(self, req: Request, params: dict):
        state = req.run.state[self.name]
        e = state["events"].get(params["eid"])
        if e is None or e["calendarId"] != params["cal"]:
            return None
        return e

    def get_event(self, req: Request, params: dict) -> Response:
        e = self._event_or_404(req, params)
        if e is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["eid"]))
        return Response(200, self._view(e))

    def patch_event(self, req: Request, params: dict) -> Response:
        e = self._event_or_404(req, params)
        if e is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["eid"]))
        body = req.json() or {}
        changed_time = "start" in body or "end" in body
        for key in ("summary", "description", "location"):
            if key in body:
                e[key] = body[key]
        if "attendees" in body:
            e["attendees"] = body["attendees"]
        if changed_time:
            start_ts, end_ts, start, end = self._times({**{"start": e["start"], "end": e["end"]}, **body})
            if start_ts is None or end_ts is None:
                return Response(400, GOOGLE_ERRORS.bad_request("start/end must be RFC3339"))
            if end_ts <= start_ts:
                return Response(400, GOOGLE_ERRORS.bad_request("end must be after start"))
            e["start"], e["end"], e["startTs"], e["endTs"] = start, end, start_ts, end_ts
        e["updated"] = _ts_to_rfc3339(time.time())
        self._notify(req, e, req.query_one("sendUpdates") or "none",
                     "time-changed" if changed_time else "updated")
        return Response(200, self._view(e))

    def cancel_event(self, req: Request, params: dict) -> Response:
        e = self._event_or_404(req, params)
        if e is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["eid"]))
        # 真 Calendar 的 DELETE 是把 status 改成 cancelled 而不是抹掉记录 ——
        # 参会人那边还能看到"这个会议被取消了"。
        e["status"] = "cancelled"
        e["updated"] = _ts_to_rfc3339(time.time())
        self._notify(req, e, req.query_one("sendUpdates") or "none", "cancelled")
        return Response(204, None)

    def move_event(self, req: Request, params: dict) -> Response:
        state = req.run.state[self.name]
        e = self._event_or_404(req, params)
        if e is None:
            return Response(404, GOOGLE_ERRORS.not_found(params["eid"]))
        dest = req.query_one("destination")
        if dest not in state["calendars"]:
            return Response(404, GOOGLE_ERRORS.not_found(f"calendar {dest}"))
        e["calendarId"] = dest
        e["updated"] = _ts_to_rfc3339(time.time())
        return Response(200, self._view(e))

    def free_busy(self, req: Request, params: dict) -> Response:
        """冲突判断的正规入口。agent 用 events.list 自己比时间也能做，
        但真实世界更常见的是先问 freeBusy —— 所以这个端点必须在。"""
        state = req.run.state[self.name]
        body = req.json() or {}
        tmin = _rfc3339_to_ts(body.get("timeMin") or "")
        tmax = _rfc3339_to_ts(body.get("timeMax") or "")
        if tmin is None or tmax is None:
            return Response(400, GOOGLE_ERRORS.bad_request("timeMin/timeMax must be RFC3339"))
        out: dict[str, dict] = {}
        for item in body.get("items") or []:
            cal = item.get("id")
            busy = [
                {"start": _ts_to_rfc3339(e["startTs"]),
                 "end": e["end"].get("dateTime") or e["end"].get("date") or ""}
                for e in state["events"].values()
                if e["calendarId"] == cal and e["status"] == "confirmed"
                and e["startTs"] < tmax and e["endTs"] > tmin
            ]
            out[cal] = {"busy": busy}
        return Response(200, {"kind": "calendar#freeBusy", "calendars": out})

    # ---------- admin ----------

    def handle_admin(self, req: Request) -> Response:
        state = req.run.state[self.name]
        parts = [p for p in req.path.split("/") if p]
        action = parts[1] if len(parts) > 1 else ""
        if action == "scopes":
            if req.method == "GET":
                return Response(200, {"granted": state["granted_scopes"]})
            state["granted_scopes"] = (req.json() or {}).get("scopes") or []
            return Response(200, {"granted": state["granted_scopes"]})
        if action == "notifications":
            return Response(200, {"notifications": state["notifications"]})
        if action == "seed":
            body = req.json() or {}
            for item in body.get("events") or []:
                eid = item.get("id") or f"evt_{state['next_id']:04d}"
                state["next_id"] += 1
                start_ts = _rfc3339_to_ts(item["start"]) or 0.0
                end_ts = _rfc3339_to_ts(item["end"]) or (start_ts + 3600)
                state["events"][eid] = {
                    "id": eid,
                    "calendarId": item.get("calendarId") or "primary",
                    "status": item.get("status") or "confirmed",
                    "summary": item.get("summary") or "",
                    "description": item.get("description"),
                    "location": item.get("location"),
                    "start": {"dateTime": item["start"]},
                    "end": {"dateTime": item["end"]},
                    "startTs": start_ts,
                    "endTs": end_ts,
                    "attendees": item.get("attendees") or [],
                    "organizer": item.get("organizer") or "ops@acme.example",
                    "updated": _ts_to_rfc3339(time.time()),
                }
            return Response(200, {"seeded": len(body.get("events") or [])})
        return Response(404, GOOGLE_ERRORS.not_found(req.path))
