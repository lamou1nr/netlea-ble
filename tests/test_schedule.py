"""Tests for saved settings, schedules and the scheduler. No Bluetooth is used."""
import asyncio
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

os.environ.setdefault("NETLEA_MAC", "AA:BB:CC:DD:EE:FF")
os.environ.setdefault("STATE_FILE", os.path.join(tempfile.mkdtemp(), "state.json"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ble_bridge as b  # noqa: E402
import netlea_schedule as ns  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from test_api import FakeClient  # noqa: E402

logging.disable(logging.CRITICAL)

DAY = {"id": "day", "name": "Day", "R": 10, "W": 16, "G": 5, "B": 2, "F": 1}
EVE = {"id": "eve", "name": "Evening", "R": 20, "W": 30, "G": 10, "B": 6, "F": 0}


def sched(points, smooth=False):
    return {"id": "s", "name": "Test", "smooth": smooth, "points": points}


def at(h, m=0, day=16):  # 2026-11-16 is a Monday
    return datetime(2026, 11, day, h, m)


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.pts = [
            {"time": "12:30", "days": 127, "action": "setting", "setting": "day", "brightness": 100},
            {"time": "17:00", "days": 127, "action": "setting", "setting": "eve", "brightness": 50},
            {"time": "20:30", "days": 127, "action": "off"},
        ]

    def test_step_schedule(self):
        s = sched(self.pts)
        r = ns.evaluate(s, [DAY, EVE], at(13))
        self.assertEqual(r["target"], {"lampState": 1, "R": 10, "W": 16, "G": 5, "B": 2, "F": 1})
        self.assertEqual(r["next_change"], at(17))
        r = ns.evaluate(s, [DAY, EVE], at(18))
        self.assertEqual(r["target"], {"lampState": 1, "R": 10, "W": 15, "G": 5, "B": 3, "F": 0})

    def test_overnight_uses_last_point_of_yesterday(self):
        r = ns.evaluate(sched(self.pts), [DAY, EVE], at(3))
        self.assertEqual(r["target"], ns.OFF)
        self.assertEqual(r["since"], at(20, 30, day=15))
        self.assertEqual(r["next_change"], at(12, 30))

    def test_days_mask(self):
        pts = [{"time": "08:00", "days": 1, "action": "values",  # Mondays only
                "values": {"R": 0, "W": 9, "G": 0, "B": 0, "F": 0}, "brightness": 100},
               {"time": "08:00", "days": 126, "action": "off"}]
        self.assertEqual(ns.evaluate(sched(pts), [], at(9))["target"]["W"], 9)
        self.assertEqual(ns.evaluate(sched(pts), [], at(9, day=17))["target"], ns.OFF)

    def test_smooth_fades_between_points(self):
        pts = [{"time": "12:00", "days": 127, "action": "off"},
               {"time": "13:00", "days": 127, "action": "values",
                "values": {"R": 20, "W": 40, "G": 0, "B": 0, "F": 10}, "brightness": 100}]
        r = ns.evaluate(sched(pts, smooth=True), [], at(12, 30))
        self.assertTrue(r["fading"])
        self.assertEqual(r["target"], {"lampState": 1, "R": 10, "W": 20, "G": 0, "B": 0, "F": 5})
        r = ns.evaluate(sched(pts, smooth=False), [], at(12, 30))
        self.assertEqual(r["target"], ns.OFF)

    def test_empty_schedule(self):
        self.assertIsNone(ns.evaluate(sched([]), [], at(12)))

    def test_preview_covers_the_day(self):
        p = ns.day_preview(sched(self.pts), [DAY, EVE], at(0), step_minutes=30)
        self.assertEqual(len(p), 49)
        self.assertEqual(p[26]["W"], 16)  # 13:00


class Validation(unittest.TestCase):
    def test_setting(self):
        self.assertEqual(ns.clean_setting({"name": " Day ", "R": 5})["name"], "Day")
        for bad in ({"R": 5}, {"name": ""}, {"name": "x", "R": 256}, {"name": "x", "F": 11},
                    {"name": "x" * 41}, {"name": "x", "R": 1.5}):
            with self.assertRaises(ns.Invalid):
                ns.clean_setting(bad)

    def test_schedule_points(self):
        ok = {"time": "07:30", "action": "off"}
        s = ns.clean_schedule({"name": "S", "points": [
            {"time": "19:00", "action": "setting", "setting": "day"}, ok]}, {"day"})
        self.assertEqual([p["time"] for p in s["points"]], ["07:30", "19:00"])
        self.assertEqual(s["points"][0]["days"], 127)
        for bad in ({"time": "7:30", "action": "off"}, {"time": "24:00", "action": "off"},
                    {"time": "07:30", "action": "setting", "setting": "nope"},
                    {"time": "07:30", "action": "dance"},
                    {"time": "07:30", "action": "off", "days": 0},
                    {"time": "07:30", "action": "values", "values": {"R": 300}},
                    {"time": "07:30", "action": "values", "values": {}, "brightness": 101}):
            with self.assertRaises(ns.Invalid, msg=bad):
                ns.clean_schedule({"name": "S", "points": [bad]}, {"day"})


class SchedulerApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b.store.path = Path(self.tmp.name) / "state.json"
        b.store.data = b.store._empty()
        b.scheduler.__init__()
        FakeClient.made = []
        FakeClient.fail_writes = False
        b.BleakClient = FakeClient
        b.client = None
        b.ble_lock = asyncio.Lock()
        b.response_event = asyncio.Event()
        b.RESPONSE_TIMEOUT = 0.01
        self.http = TestClient(TestServer(b.create_app()))
        await self.http.start_server()
        # The tests drive the scheduler with tick() themselves.
        self.http.server.app["scheduler_task"].cancel()

    async def asyncTearDown(self):
        await self.http.close()
        self.tmp.cleanup()

    async def call(self, method, path, body=None):
        r = await self.http.request(method, path, json=body)
        return r.status, await r.json()

    async def make(self):
        _, day = await self.call("POST", "/api/settings", {"name": "Day", "R": 10, "W": 16})
        st, sc = await self.call("POST", "/api/schedules", {"name": "Main", "points": [
            {"time": "00:00", "action": "setting", "setting": day["id"]}]})
        self.assertEqual(st, 201)
        return day, sc

    async def test_settings_crud_and_persistence(self):
        st, item = await self.call("POST", "/api/settings", {"name": "Blue", "B": 9})
        self.assertEqual(st, 201)
        st, _ = await self.call("PUT", f"/api/settings/{item['id']}", {"name": "Blue 2", "B": 8})
        self.assertEqual(st, 200)
        saved = b.store.path.read_text()
        self.assertIn("Blue 2", saved)
        st, _ = await self.call("POST", "/api/settings", {"name": "Bad", "B": 300})
        self.assertEqual(st, 400)
        st, _ = await self.call("DELETE", f"/api/settings/{item['id']}")
        self.assertEqual(st, 200)
        self.assertEqual((await self.call("GET", "/api/settings"))[1], [])

    async def test_setting_in_use_cannot_be_deleted(self):
        day, _ = await self.make()
        st, js = await self.call("DELETE", f"/api/settings/{day['id']}")
        self.assertEqual(st, 409)
        self.assertIn("Main", js["error"])

    async def test_scheduler_is_off_until_enabled(self):
        _, sc = await self.make()
        await b.scheduler.tick()
        self.assertEqual(FakeClient.made, [], "nothing may be sent while disabled")
        st, _ = await self.call("POST", "/api/scheduler", {"enabled": True})
        self.assertEqual(st, 400, "needs a schedule first")
        st, js = await self.call("POST", "/api/scheduler",
                                 {"active_schedule": sc["id"], "enabled": True})
        self.assertEqual((st, js["running"]), (200, True))
        await b.scheduler.tick()
        self.assertEqual(FakeClient.made[-1].writes[-1][1].hex(),
                         "5a1b01100000010000000a100000000000000000000000000000a1")
        n = len(FakeClient.made[-1].writes)
        await b.scheduler.tick()
        self.assertEqual(len(FakeClient.made[-1].writes), n, "unchanged target is not resent")

    async def test_manual_change_pauses_until_next_point(self):
        _, sc = await self.make()
        await self.call("POST", "/api/scheduler", {"active_schedule": sc["id"], "enabled": True})
        await b.scheduler.tick()
        st, _ = await self.call("POST", "/direct", {"R": 3})
        self.assertEqual(st, 200)
        self.assertIsNotNone(b.scheduler.override_until)
        n = len(FakeClient.made[-1].writes)
        await b.scheduler.tick()
        self.assertEqual(len(FakeClient.made[-1].writes), n, "paused")
        st, js = await self.call("POST", "/api/scheduler/resume")
        self.assertIsNone(js["paused_until"])
        await b.scheduler.tick()
        self.assertEqual(len(FakeClient.made[-1].writes), n + 1, "resumed")

    async def test_failed_send_is_retried(self):
        _, sc = await self.make()
        await self.call("POST", "/api/scheduler", {"active_schedule": sc["id"], "enabled": True})
        FakeClient.fail_writes = True
        await b.scheduler.tick()
        self.assertIsNotNone(b.scheduler.last_error)
        FakeClient.fail_writes = False
        await b.scheduler.tick()
        self.assertIsNone(b.scheduler.last_error)
        self.assertEqual(len(FakeClient.made[-1].writes), 1)

    async def test_state_endpoint(self):
        st, js = await self.call("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertIn("scheduler", js)
        self.assertFalse(js["scheduler"]["enabled"])

    async def test_page_and_assets_are_served(self):
        r = await self.http.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.headers["Content-Type"])
        r = await self.http.get("/assets/logo.png")
        self.assertEqual(r.status, 200)
        r = await self.http.get("/assets/manifest.json")
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json(content_type=None))["name"], "Aquarium Light")
        for bad in ("/assets/nope.png", "/assets/..%2Findex.html", "/assets/.hidden"):
            r = await self.http.get(bad)
            self.assertEqual(r.status, 404, bad)

    async def test_report_is_decoded(self):
        raw = bytes.fromhex("5a26011400000100000100010a0f0504000000010000000001000000010000010001000000bf")
        rep = b.parse_response(bytearray(raw))["report"]
        self.assertEqual(rep, {"on": 1, "mode": 0, "R": 10, "W": 15, "G": 5, "B": 4, "F": 0})


if __name__ == "__main__":
    unittest.main()
