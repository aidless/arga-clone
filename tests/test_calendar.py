"""Calendar twin 测试：核心是 `sendUpdates` 的通知语义。

一组断言值得单独说：**改时间不通知**必须能被观测到。
如果没有这条，"agent 把会议挪了但没说"在 trace 里看不出任何痕迹，
而这正是日历域最常见的静默事故。
"""

from __future__ import annotations

import json

import pytest

from argaclone.store import Store
from argaclone.twins.base import Request
from argaclone.twins.calendar_twin import CalendarTwin

TOKEN = "ya29.calendar-token"
T1 = "2026-11-20T10:00:00Z"
T2 = "2026-11-20T11:00:00Z"
T3 = "2026-11-20T14:00:00Z"


@pytest.fixture()
def ctx():
    store = Store()
    run = store.create(["calendar"], 600)
    run.state["calendar"] = CalendarTwin().initial_state()
    return run, store


def call(twin, ctx, method, path, body=None, query=None, token=TOKEN):
    run, store = ctx
    headers = {"authorization": f"Bearer {token}"} if token else {}
    req = Request(method=method, path=path, query=query or {}, headers=headers,
                  body=b"" if body is None else json.dumps(body).encode(),
                  run=run, run_id=run.run_id, store=store)
    return twin.dispatch(req)


def mk(twin, ctx, summary="Sync", attendees=None, query=None):
    body = {"summary": summary, "start": {"dateTime": T1}, "end": {"dateTime": T2}}
    if attendees:
        body["attendees"] = attendees
    return call(twin, ctx, "POST", "/calendar/v3/calendars/primary/events", body, query=query)


def test_create_requires_summary_and_valid_times(ctx):
    twin = CalendarTwin()
    assert call(twin, ctx, "POST", "/calendar/v3/calendars/primary/events",
                {"start": {"dateTime": T1}, "end": {"dateTime": T2}}).status == 400
    bad = call(twin, ctx, "POST", "/calendar/v3/calendars/primary/events",
               {"summary": "x", "start": {"dateTime": "明天"}, "end": {"dateTime": T2}})
    assert bad.status == 400
    assert "RFC3339" in bad.body["error"]["message"]
    backwards = call(twin, ctx, "POST", "/calendar/v3/calendars/primary/events",
                     {"summary": "x", "start": {"dateTime": T2}, "end": {"dateTime": T1}})
    assert backwards.status == 400


def test_no_send_updates_means_no_notification(ctx):
    """默认 notify=none。这是真 Calendar 的行为，也是最容易踩的坑。"""
    twin = CalendarTwin()
    att = [{"email": "morgan@retail.example"}]
    mk(twin, ctx, attendees=att)
    notes = call(twin, ctx, "GET", "/admin/notifications").body["notifications"]
    assert notes == []


def test_send_updates_all_notifies_every_attendee(ctx):
    twin = CalendarTwin()
    att = [{"email": "a@x.example"}, {"email": "b@y.example"}]
    mk(twin, ctx, attendees=att, query={"sendUpdates": ["all"]})
    notes = call(twin, ctx, "GET", "/admin/notifications").body["notifications"]
    assert {n["email"] for n in notes} == {"a@x.example", "b@y.example"}


def test_external_only_skips_internal_attendees(ctx):
    twin = CalendarTwin()
    att = [{"email": "a@acme.example"}, {"email": "p@partner.example", "external": True}]
    mk(twin, ctx, attendees=att, query={"sendUpdates": ["externalOnly"]})
    notes = call(twin, ctx, "GET", "/admin/notifications").body["notifications"]
    assert [n["email"] for n in notes] == ["p@partner.example"]


def test_reschedule_without_send_updates_is_silent(ctx):
    """把会议挪走却不通知 —— 必须能在 trace 里看出来"没发"。

    这条是这道题域的核心：如果 twin 在改时间时自动补一条通知，
    "忘了通知"这个错误就永远测不出来，而且会被判成做对了。
    """
    twin = CalendarTwin()
    eid = mk(twin, ctx, attendees=[{"email": "morgan@retail.example"}]).body["id"]
    call(twin, ctx, "PATCH", f"/calendar/v3/calendars/primary/events/{eid}",
         {"start": {"dateTime": T3}, "end": {"dateTime": "2026-11-20T15:00:00Z"}})
    notes = call(twin, ctx, "GET", "/admin/notifications").body["notifications"]
    assert notes == []

    call(twin, ctx, "PATCH", f"/calendar/v3/calendars/primary/events/{eid}",
         {"start": {"dateTime": T3}, "end": {"dateTime": "2026-11-20T15:00:00Z"}},
         query={"sendUpdates": ["all"]})
    notes = call(twin, ctx, "GET", "/admin/notifications").body["notifications"]
    assert [n["reason"] for n in notes] == ["time-changed"]


def test_cancel_keeps_record_with_cancelled_status(ctx):
    twin = CalendarTwin()
    eid = mk(twin, ctx).body["id"]
    assert call(twin, ctx, "DELETE", f"/calendar/v3/calendars/primary/events/{eid}").status == 204

    listed = call(twin, ctx, "GET", "/calendar/v3/calendars/primary/events")
    assert listed.body["items"] == []          # 默认不显示已取消
    shown = call(twin, ctx, "GET", "/calendar/v3/calendars/primary/events",
                 query={"showDeleted": ["true"]})
    assert shown.body["items"][0]["status"] == "cancelled"


def test_time_window_filters_and_rejects_bad_format(ctx):
    twin = CalendarTwin()
    mk(twin, ctx, "morning")
    call(twin, ctx, "POST", "/calendar/v3/calendars/primary/events",
         {"summary": "afternoon", "start": {"dateTime": "2026-11-20T16:00:00Z"},
          "end": {"dateTime": "2026-11-20T17:00:00Z"}})
    window = call(twin, ctx, "GET", "/calendar/v3/calendars/primary/events",
                  query={"timeMin": ["2026-11-20T12:00:00Z"], "timeMax": ["2026-11-21T00:00:00Z"]})
    assert [e["summary"] for e in window.body["items"]] == ["afternoon"]
    bad = call(twin, ctx, "GET", "/calendar/v3/calendars/primary/events",
               query={"timeMin": ["next monday"]})
    assert bad.status == 400


def test_free_busy_detects_overlap(ctx):
    twin = CalendarTwin()
    mk(twin, ctx, "existing")
    fb = call(twin, ctx, "POST", "/calendar/v3/freeBusy", {
        "timeMin": T1, "timeMax": T2, "items": [{"id": "primary"}],
    })
    assert len(fb.body["calendars"]["primary"]["busy"]) == 1
    far = call(twin, ctx, "POST", "/calendar/v3/freeBusy", {
        "timeMin": "2026-12-01T00:00:00Z", "timeMax": "2026-12-02T00:00:00Z",
        "items": [{"id": "primary"}],
    })
    assert far.body["calendars"]["primary"]["busy"] == []


def test_move_between_calendars(ctx):
    twin = CalendarTwin()
    eid = mk(twin, ctx).body["id"]
    moved = call(twin, ctx, "POST",
                 f"/calendar/v3/calendars/primary/events/{eid}/move",
                 query={"destination": ["team"]})
    assert moved.status == 200
    assert call(twin, ctx, "GET", "/calendar/v3/calendars/primary/events").body["items"] == []
    assert len(call(twin, ctx, "GET", "/calendar/v3/calendars/team/events").body["items"]) == 1


def test_missing_scope_is_403_and_missing_token_is_401(ctx):
    twin = CalendarTwin()
    run, _ = ctx
    assert call(twin, ctx, "GET", "/calendar/v3/calendars/primary/events", token=None).status == 401
    run.state["calendar"]["granted_scopes"] = ["https://www.googleapis.com/auth/calendar.readonly"]
    assert call(twin, ctx, "GET", "/calendar/v3/calendars/primary/events").status == 403
