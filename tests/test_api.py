"""HTTP API tests. No Bluetooth is used: the BLE client is replaced by a fake.

Run from the repository root:  python -m unittest discover -s tests
"""
import asyncio
import logging
import os
import sys
import unittest

os.environ.setdefault("NETLEA_MAC", "AA:BB:CC:DD:EE:FF")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

os.environ["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "state.json")
import ble_bridge as b  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

logging.disable(logging.CRITICAL)


class FakeClient:
    """Stands in for bleak.BleakClient."""
    made = []
    fail_writes = False

    def __init__(self, target, disconnected_callback=None, timeout=30.0):
        self.target = target
        self.is_connected = False
        self.services = []
        self.subscribed = []
        self.writes = []
        FakeClient.made.append(self)

    async def connect(self):
        await asyncio.sleep(0.05)
        self.is_connected = True

    async def start_notify(self, uuid, handler):
        self.subscribed.append(uuid)

    async def write_gatt_char(self, uuid, packet, response=False):
        if FakeClient.fail_writes:
            raise RuntimeError("simulated BLE failure")
        self.writes.append((uuid, bytes(packet)))

    async def disconnect(self):
        self.is_connected = False


class ApiCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.made = []
        FakeClient.fail_writes = False
        b.BleakClient = FakeClient
        b.client = None
        b.ble_lock = asyncio.Lock()
        b.response_event = asyncio.Event()
        b.RESPONSE_TIMEOUT = 0.01
        b.light_state.update({"connected": False, "power": None, "R": 0, "W": 0, "G": 0,
                              "B": 0, "F": 0, "grade": 0, "mode": None})
        self.http = TestClient(TestServer(b.create_app()))
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.http.close()

    async def post(self, path, body=None, **kw):
        if body is None and not kw:
            r = await self.http.post(path)
        elif kw:
            r = await self.http.post(path, **kw)
        else:
            r = await self.http.post(path, json=body)
        return r.status, await r.json()

    def last_packet(self):
        return FakeClient.made[-1].writes[-1][1].hex()

    # -- what the cron scripts and the portal send must not change ----------
    async def test_cron_sequence_sends_the_known_packet(self):
        self.assertEqual((await self.post("/disconnect"))[0], 200)
        st, js = await self.post("/connect")
        self.assertEqual((st, js["status"]), (200, "connected"))
        st, js = await self.post("/direct", {"lampState": 1, "lampMode": 0, "R": 16, "W": 22,
                                             "G": 8, "B": 5, "F": 0, "grade": 0, "modeId": 0})
        self.assertEqual((st, js["status"]), (200, "ok"))
        self.assertEqual(js["sent"], "5a1b011000000100000010160805000000000000000000000000ba")
        self.assertEqual(FakeClient.made[-1].writes[-1][0], b.WRITE_UUID)

    async def test_cron_off_packet(self):
        st, js = await self.post("/direct", {"lampState": 0, "lampMode": 0, "R": 0, "W": 0,
                                             "G": 0, "B": 0, "F": 0, "grade": 0, "modeId": 0})
        self.assertEqual(js["sent"], "5a1b01100000000000000000000000000000000000000000000086")

    async def test_listens_on_ff01(self):
        await self.post("/connect")
        self.assertEqual(FakeClient.made[-1].subscribed[0], b.NOTIFY_UUID)

    # -- bad input is refused, never wrapped ---------------------------------
    async def test_out_of_range_values_are_refused(self):
        for body in ({"R": -1}, {"R": 256}, {"W": 1000}, {"F": 11}, {"R": "50"},
                     {"R": 50.5}, {"R": True}, {"lampState": 2}, {"grade": -1}):
            st, js = await self.post("/direct", body)
            self.assertEqual(st, 400, body)
            self.assertEqual(js["status"], "error")
        self.assertEqual(FakeClient.made, [], "nothing may reach Bluetooth")

    async def test_malformed_json_is_400(self):
        st, _ = await self.post("/direct", data=b"{not json",
                                headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)

    async def test_body_must_be_declared_as_json(self):
        st, _ = await self.post("/direct", data=b'{"R": 200}',
                                headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 400)
        self.assertEqual(FakeClient.made, [])

    async def test_grade_defaults_to_0(self):
        _, js = await self.post("/direct", {"R": 5})
        self.assertEqual(js["sent"][40:48], "00000000")

    async def test_schedule_validation(self):
        good = {"weekCycle": 127, "startH": 8, "startM": 0, "endH": 20, "endM": 0, "W": 10}
        for trips in ([dict(good, startH=24)], [dict(good, endM=60)], [dict(good, weekCycle=128)],
                      [dict(good, R=-1)], [good] * 11, {"a": 1}, ["x"]):
            st, _ = await self.post("/schedule", {"trips": trips})
            self.assertEqual(st, 400, trips)
        st, js = await self.post("/schedule", {"trips": [good]})
        self.assertEqual((st, len(js["sent"]) // 2), (200, 176))

    async def test_raw_needs_explicit_command_and_valid_bytes(self):
        for body in ({}, {"cmdId": 16}, {"cmdId": 16, "data": [256]},
                     {"cmdId": 16, "data": [0] * 300}, {"cmdId": 300, "data": [0]}):
            st, _ = await self.post("/raw", body)
            self.assertEqual(st, 400, body)
        st, js = await self.post("/raw", {"cmdId": 16, "data": [1, 0, 0, 0, 0, 0, 10] + [0] * 13})
        self.assertEqual(js["sent"], "5a1b011000000100000000000a0000000000000000000000000091")

    # -- remembered state is only what really went out ----------------------
    async def test_failed_send_is_502_and_not_remembered(self):
        await self.post("/direct", {"W": 10, "grade": 0})
        FakeClient.fail_writes = True
        st, js = await self.post("/direct", {"R": 77, "grade": 0})
        self.assertEqual((st, js["status"]), (502, "error"))
        state = await (await self.http.get("/status")).json()
        self.assertEqual((state["R"], state["W"]), (0, 10))

    async def test_power(self):
        st, _ = await self.post("/power", {"on": True})
        self.assertEqual(st, 409, "nothing remembered yet")
        await self.post("/control", {"R": 99, "on": 1})
        st, _ = await self.post("/power", {"on": True})
        self.assertEqual(st, 409, "/control must not count as a remembered color")
        await self.post("/direct", {"W": 10, "grade": 0})
        st, js = await self.post("/power", {"on": False})
        self.assertEqual(js["sent"], "5a1b01100000000000000000000000000000000000000000000086")
        st, js = await self.post("/power", {"on": True})
        self.assertEqual(js["sent"], "5a1b0110000001000000000a000000000000000000000000000091")

    # -- one Bluetooth operation at a time ----------------------------------
    async def test_two_commands_at_once_share_one_connection(self):
        p1 = b.build_direct_control(1, 0, 1, 0, 0, 0, 0)
        p2 = b.build_direct_control(1, 0, 2, 0, 0, 0, 0)
        r1, r2 = await asyncio.gather(b.send_command(p1), b.send_command(p2))
        self.assertEqual((r1["status"], r2["status"]), ("ok", "ok"))
        self.assertEqual(len(FakeClient.made), 1)
        self.assertEqual(len(FakeClient.made[0].writes), 2)

    async def test_default_listen_address_is_this_machine_only(self):
        if "BRIDGE_HOST" not in os.environ:
            self.assertEqual(b.BRIDGE_HOST, "127.0.0.1")

    async def test_cors_can_be_switched_off(self):
        r = await self.http.get("/health")
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")
        b.CORS_ORIGIN = ""
        try:
            r = await self.http.get("/health")
            self.assertNotIn("Access-Control-Allow-Origin", r.headers)
        finally:
            b.CORS_ORIGIN = "*"


if __name__ == "__main__":
    unittest.main()
