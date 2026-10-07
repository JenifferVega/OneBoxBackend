#!/usr/bin/env python3
"""Per-user timezone and language (table onebox-users).

    python tests/test_user_settings.py

Before this, the backend used three different clocks, none of them the
user's: the planner's "today" was the server's date (UTC), scheduled sends
used a fixed America/Tegucigalpa, and recurring sends used the UTC weekday --
"every Monday" went out on Sunday evening for a user in Bogota.

No AWS: the table is faked.
"""
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.services.users as users  # noqa: E402

failures = []
print()


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class FakeTable:
    def __init__(self, missing=False):
        self.items, self.reads, self.missing = {}, 0, missing

    def get_item(self, Key):
        self.reads += 1
        if self.missing:
            raise RuntimeError("ResourceNotFoundException: table does not exist")
        item = self.items.get(Key["userId"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues):
        item = self.items.setdefault(Key["userId"], {"userId": Key["userId"]})
        for alias, field in ExpressionAttributeNames.items():
            item[field] = ExpressionAttributeValues[":v" + alias[2:]]


def fresh(table):
    users.users_table = table
    users._cache.clear()


print("─ validation ─────────────────────────────────────────────────")
check(users.is_valid_timezone("America/Bogota"), "a real IANA zone is valid")
check(not users.is_valid_timezone("GMT-5 Bogota"), "free text is rejected")
check(not users.is_valid_timezone(""), "empty is rejected")

print("\n─ nothing saved yet ──────────────────────────────────────────")
fresh(FakeTable())
s = users.get_settings("u1")
check(s["timezone"] is None and s["language"] is None,
      "unsaved fields come back as None, so the frontend can auto-detect")
check(users.user_timezone("u1") == users.DEFAULT_TIMEZONE,
      "the agent falls back to the default zone")

print("\n─ save and read back ─────────────────────────────────────────")
t = FakeTable()
fresh(t)
users.get_settings("u1")                      # warms the cache with {}
out = users.update_settings("u1", "America/Bogota", "ES")
check(out["timezone"] == "America/Bogota", "timezone saved")
check(out["language"] == "es", "language normalised to lower case")
check(users.user_timezone("u1") == "America/Bogota",
      "a save invalidates the cache: the agent sees it immediately")
users.update_settings("u1", language="en")
check(t.items["u1"]["timezone"] == "America/Bogota",
      "a partial update leaves the other field alone")

for bad, label in (({"timezone_name": "Mars/Base"}, "an invalid zone"),
                   ({"language": "fr"}, "an unsupported language")):
    try:
        users.update_settings("u1", **bad)
        check(False, f"{label} is refused")
    except ValueError:
        check(True, f"{label} is refused")

print("\n─ cache ──────────────────────────────────────────────────────")
t = FakeTable()
t.items["u2"] = {"userId": "u2", "timezone": "Europe/Madrid"}
fresh(t)
for _ in range(5):
    users.user_timezone("u2")
check(t.reads == 1, "five agent turns, one database read")

print("\n─ table missing: nothing breaks ──────────────────────────────")
fresh(FakeTable(missing=True))
check(users.user_timezone("u3") == users.DEFAULT_TIMEZONE,
      "the agent still gets a zone when the table is unreachable")
check(users.get_settings("u3")["timezone"] is None,
      "the settings page still loads")

print("\n─ times shown to the user are local ──────────────────────────")
check(users.local_time_label("2026-10-06T20:00:00Z", "America/Bogota")
      == "2026-10-06 15:00 (America/Bogota)",
      "20:00 UTC is told as 15:00 in Bogota")

print("\n─ 'at 1pm' means 1pm where the user is ───────────────────────")
from agent.schedule import apply_schedule  # noqa: E402

now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)      # 07:00 Bogota
params = {"recipient": "+57300", "message": "x", "channel": "whatsapp",
          "schedule": {"type": "fixed_time", "hour": 13, "minute": 0}}
bog, err = apply_schedule(dict(params), tz="America/Bogota", now=now)
mad, _ = apply_schedule(dict(params), tz="Europe/Madrid", now=now.replace(hour=8))
check(not err and bog["scheduled_at"] == "2026-10-06T18:00:00Z",
      "13:00 in Bogota is 18:00 UTC")
check(mad["scheduled_at"] == "2026-10-06T11:00:00Z",
      "13:00 in Madrid is 11:00 UTC: same words, different instant")

print("\n─ the planner's 'today' ──────────────────────────────────────")
from agent.tools.context import set_current_user, clear_current_user  # noqa: E402
from agent.graph.nodes.planner.node import _today_for_user  # noqa: E402

t = FakeTable()
t.items["u4"] = {"userId": "u4", "timezone": "Pacific/Kiritimati"}   # UTC+14
fresh(t)
set_current_user("u4", "")
today = _today_for_user()
clear_current_user()
expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
check(today.startswith(expected), "today is the date in the user's zone")
check("Pacific/Kiritimati" in today, "and the prompt says which zone it is")

print("\n─ recurring sends follow the owner's local day ──────────────")
import agent.tools as agent_tools  # noqa: E402
import api.services.notifications as notif_service  # noqa: E402

local = datetime.now(ZoneInfo("Asia/Tokyo"))
day = local.strftime("%A").lower()
t = FakeTable()
t.items["u5"] = {"userId": "u5", "timezone": "Asia/Tokyo"}
fresh(t)


class FakeNotifs:
    def scan(self, **_):
        return {"Items": [{"userId": "u5", "notificationId": "n1",
                           "status": "pending", "channel": "whatsapp",
                           "recipient": "+81", "message": "hi",
                           "isRecurring": True, "recurringDays": [day]}]}

    def update_item(self, **_):
        pass


sent = []
real = (agent_tools.notifications_table, agent_tools.send_notification)
agent_tools.notifications_table = FakeNotifs()
agent_tools.send_notification = lambda **kw: sent.append(kw) or {"success": True}
try:
    notif_service.dispatch_pending_notifications()
finally:
    agent_tools.notifications_table, agent_tools.send_notification = real
should = local.hour >= notif_service.RECURRING_SEND_HOUR
check(bool(sent) == should,
      f"a recurring send on today's LOCAL day ({day}, {local:%H:%M} Tokyo) "
      f"{'goes out' if should else 'waits for the morning'}")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("user settings: all clear\n")
