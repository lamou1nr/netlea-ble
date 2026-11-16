#!/usr/bin/env python3
"""
Netlea AT5S Gen 3 BLE Bridge Service
Connects to the light over Bluetooth LE and exposes a local REST API.
Runs on the host (not in Docker) since it needs raw BLE access.

API Endpoints:
  GET  /health          - Service health check
  GET  /scan            - Scan for Netlea BLE devices
  GET  /status          - Last values this bridge sent successfully
  POST /connect         - Connect to the light
  POST /disconnect      - Disconnect from the light
  POST /direct          - Send direct control command (cmdId 16)
  POST /power           - Power on/off
  POST /schedule        - Set schedule (cmdId 18)
  POST /control         - Send manual control command (cmdId 26, unreliable)
  POST /read            - Send cmdId 5 (purpose not confirmed)
  POST /raw             - Send a raw command packet
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from aiohttp import web
from bleak import BleakClient, BleakScanner

import netlea_schedule as ns

# ── Config ──────────────────────────────────────────────────────────────────
# Set NETLEA_MAC in the environment (see config.example). There is no default.
NETLEA_MAC = os.environ.get("NETLEA_MAC", "").strip()
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "3003"))
# Address to listen on. The default only accepts connections from this machine.
# Set BRIDGE_HOST=0.0.0.0 to accept connections from other devices on the network.
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
# Value for Access-Control-Allow-Origin. Empty = send no CORS headers.
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*").strip()
# Seconds to wait for the light to answer a command.
RESPONSE_TIMEOUT = float(os.environ.get("RESPONSE_TIMEOUT", "3.0"))
APP_DIR = Path(__file__).resolve().parent
# Saved settings and schedules.
STATE_FILE = Path(os.environ.get("STATE_FILE", "").strip() or APP_DIR / "data" / "state.json")
# The web page served at "/".
WEB_DIR = APP_DIR / "web"
# While a schedule runs, resend the current target this often even if it has
# not changed, so a missed command or a light reset is corrected.
RESEND_MINUTES = float(os.environ.get("RESEND_MINUTES", "15"))
SCHEDULER_TICK = 20  # seconds

# BLE GATT UUIDs.
# Service FFFA on the light has two write characteristics (FF02 and FFFC) and
# two notify characteristics (FF01 and FFFD).
# IMPORTANT: the light only acts on writes to FF02 and is silent on FFFC.
# Replies are expected on FF01. Do not swap these for the FFFC/FFFD pair.
SERVICE_UUID = "0000fffa-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
# FFFD is subscribed as well, so that nothing the light sends is missed.
SECOND_NOTIFY_UUID = "0000fffd-0000-1000-8000-00805f9b34fb"

PACKET_START = 0x5A
HEADER_BYTE = 1  # third byte of every packet

MAX_U8 = 0xFF
MAX_U32 = 0xFFFFFFFF
MAX_F = 10
MAX_TRIPS = 10
CHANNELS = ("R", "W", "G", "B", "F")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("netlea-ble")


class _SkipHealthChecks(logging.Filter):
    """Keep once-a-minute monitoring requests out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return '"GET /health ' not in msg and '"GET /api/state ' not in msg


logging.getLogger("aiohttp.access").addFilter(_SkipHealthChecks())

# ── Global State ────────────────────────────────────────────────────────────
client: Optional[BleakClient] = None
ble_lock = asyncio.Lock()  # one BLE operation at a time
light_state = {
    "connected": False,
    "mac": NETLEA_MAC,
    "power": None,
    "R": 0, "W": 0, "G": 0, "B": 0, "F": 0,
    "grade": 0,
    "mode": None,
    "last_response": None,
    "last_update": None,
    "last_ok": None,
}
response_event = asyncio.Event()
_services_logged = False


class BadRequest(Exception):
    """The request body is not acceptable. Reported as HTTP 400."""


# ── BLE Protocol ────────────────────────────────────────────────────────────
def checksum(data) -> int:
    """Checksum: sum of all bytes mod 256."""
    return sum(data) & 0xFF


def build_packet(cmd_id: int, data, flag_byte4: int = 0) -> bytearray:
    """
    Build a command packet.
    Format: [0x5A, totalLen, 0x01, cmdId, byte4, 0x00, ...data, checksum]
    """
    header = [PACKET_START, 0, HEADER_BYTE, cmd_id, flag_byte4, 0]
    total_len = len(header) + len(data) + 1  # +1 for checksum
    if total_len > MAX_U8:
        raise ValueError("packet too long")
    header[1] = total_len
    packet = header + list(data)
    packet.append(checksum(packet))
    return bytearray(packet)


def int_to_bytes_le(value: int, size: int = 4):
    """Convert integer to little-endian byte list."""
    return [(value >> (i * 8)) & 0xFF for i in range(size)]


def build_manual_control(on: int, off: int, r: int, w: int, g: int, b: int, f: int,
                         mode_id: int = 0, grade: int = 0) -> bytearray:
    """
    cmdId 26 - Manual/temporary control.
    Fields: on(4 bytes LE), off(4 bytes LE), R, W, G, B, F, modeId, grade(4 bytes LE)
    """
    data = []
    data.extend(int_to_bytes_le(on, 4))
    data.extend(int_to_bytes_le(off, 4))
    data.extend([r & 0xFF, w & 0xFF, g & 0xFF, b & 0xFF, f & 0xFF])
    data.append(mode_id & 0xFF)
    data.extend(int_to_bytes_le(grade, 4))
    return build_packet(26, data)


def build_direct_control(lamp_state: int, lamp_mode: int, r: int, w: int, g: int,
                         b: int, f: int, restore_time: int = 0, mode_id: int = 0,
                         grade: int = 0, reserve1: int = 0, reserve2: int = 0,
                         reserve3: int = 0, reserve4: int = 0) -> bytearray:
    """
    cmdId 16 - Direct light control. Primary working command.
    Fields: lampState, lampMode, reserve1, reserve2, R, W, G, B, F,
            restoreActTime(4 bytes LE), modeId, grade(4 bytes LE), reserve3, reserve4
    """
    data = [lamp_state & 0xFF, lamp_mode & 0xFF, reserve1 & 0xFF, reserve2 & 0xFF,
            r & 0xFF, w & 0xFF, g & 0xFF, b & 0xFF, f & 0xFF]
    data.extend(int_to_bytes_le(restore_time, 4))
    data.append(mode_id & 0xFF)
    data.extend(int_to_bytes_le(grade, 4))
    data.extend([reserve3 & 0xFF, reserve4 & 0xFF])
    return build_packet(16, data)


def build_power_command(power_on: bool) -> bytearray:
    """Power on/off using direct control (cmdId 16).

    Off: lampState 0 with every channel 0. Tested on an AT5S Gen 3: lampState 0
    alone, with the color channels still set, leaves the light on.
    On: lampState 1 with the last color values sent by this process.
    """
    if not power_on:
        return build_direct_control(lamp_state=0, lamp_mode=0, r=0, w=0, g=0, b=0, f=0, grade=0)
    return build_direct_control(
        lamp_state=1,
        lamp_mode=0,
        r=light_state["R"],
        w=light_state["W"],
        g=light_state["G"],
        b=light_state["B"],
        f=light_state["F"],
        grade=light_state["grade"],
    )


def build_read_state() -> bytearray:
    """cmdId 5. Meant as "read device state"; purpose not confirmed."""
    return build_packet(5, [0])


def build_schedule(sun_moon_disable: int, sun_time: int, moon_time: int,
                   trips) -> bytearray:
    """
    cmdId 18 - Schedule/trip control.
    Each trip: tripDisable, weekCycle, startH, startM, endH, endM,
               R, W, G, B, F, modeId, grade(4 bytes LE).
    Always sends 10 trip slots; unused slots are zeros.
    """
    trips = list(trips)[:MAX_TRIPS]
    data = [sun_moon_disable & 0xFF]
    data.extend(int_to_bytes_le(sun_time, 4))
    data.extend(int_to_bytes_le(moon_time, 4))

    for trip in trips:
        for key in ("tripDisable", "weekCycle", "startH", "startM", "endH", "endM",
                    "R", "W", "G", "B", "F", "modeId"):
            data.append(trip.get(key, 0) & 0xFF)
        data.extend(int_to_bytes_le(trip.get("grade", 0), 4))

    empty_trip_size = 16  # 12 single bytes + 4 bytes for grade
    data.extend([0] * (empty_trip_size * (MAX_TRIPS - len(trips))))
    return build_packet(18, data)


def parse_response(data: bytearray) -> dict:
    """Split a packet received from the light into its parts. No interpretation."""
    if len(data) < 7:
        return {"raw": data.hex(), "error": "Response too short"}
    return {
        "raw": data.hex(),
        "start": data[0],
        "length": data[1],
        "protocol": data[2],
        "cmdId": data[3],
        "byte4": data[4],
        "byte5": data[5],
        "payload": data[6:-1].hex(),
        "checksum_ok": data[-1] == checksum(data[:-1]),
        **({"report": decode_report(data[6:-1])} if data[3] == 20 else {}),
    }


def decode_report(payload) -> Optional[dict]:
    """cmdId 20 status report: lamp on flag and R, W, G, B, F as the light has them."""
    if len(payload) < 11:
        return None
    return {"on": payload[3], "mode": payload[4],
            "R": payload[6], "W": payload[7], "G": payload[8], "B": payload[9], "F": payload[10]}


# ── Request validation ──────────────────────────────────────────────────────
async def read_json(request) -> dict:
    """Return the JSON object in the request body ({} when there is no body)."""
    if not request.can_read_body:
        return {}
    if request.content_type != "application/json":
        raise BadRequest("Content-Type must be application/json")
    try:
        body = await request.json()
    except Exception:
        raise BadRequest("Body is not valid JSON")
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    return body


def int_field(obj: dict, name: str, default, low: int, high: int) -> int:
    """Read a whole number from obj and make sure it is within low..high."""
    if name not in obj:
        if default is None:
            raise BadRequest(f"{name} is required")
        return default
    value = obj[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"{name} must be a whole number")
    if not low <= value <= high:
        raise BadRequest(f"{name} must be between {low} and {high}")
    return value


def channel_fields(obj: dict) -> dict:
    """Read R, W, G, B (0-255) and F (0-10). Missing channels are 0."""
    values = {c: int_field(obj, c, 0, 0, MAX_U8) for c in ("R", "W", "G", "B")}
    values["F"] = int_field(obj, "F", 0, 0, MAX_F)
    return values


def json_result(result: dict) -> web.Response:
    """200 when the BLE step worked, 502 when it did not."""
    ok = result.get("status") in ("ok", "connected", "already_connected", "disconnected")
    return web.json_response(result, status=200 if ok else 502)


# ── BLE Notification Handler ────────────────────────────────────────────────
def notification_handler(sender, data: bytearray):
    """Handle incoming BLE notifications from the light."""
    source = getattr(sender, "uuid", sender)
    log.info(f"BLE notification from {source}: {bytes(data).hex()}")
    light_state["last_response"] = parse_response(data)
    light_state["last_update"] = time.time()
    response_event.set()


def _on_disconnect(closed_client):
    log.info("BLE link closed")
    if client is None or closed_client is client:
        light_state["connected"] = False


# ── BLE Connection Management ──────────────────────────────────────────────
async def _connect(mac: Optional[str] = None) -> dict:
    """Connect to the light. Caller must hold ble_lock."""
    global client, _services_logged
    target = mac or NETLEA_MAC

    if client and client.is_connected:
        return {"status": "already_connected", "mac": light_state["mac"]}

    new_client = None
    try:
        log.info(f"Connecting to {target}...")
        new_client = BleakClient(target, disconnected_callback=_on_disconnect, timeout=30.0)
        await new_client.connect()
        if not new_client.is_connected:
            return {"status": "failed", "error": "Connection failed"}

        if not _services_logged:
            for service in new_client.services:
                log.info(f"Service: {service.uuid}")
                for char in service.characteristics:
                    log.info(f"  Char: {char.uuid} | Props: {char.properties}")
            _services_logged = True

        subscribed = []
        for uuid in (NOTIFY_UUID, SECOND_NOTIFY_UUID):
            try:
                await new_client.start_notify(uuid, notification_handler)
                subscribed.append(uuid[4:8])
            except Exception as e:
                log.warning(f"Could not subscribe to {uuid}: {e}")
        log.info(f"Subscribed to notifications on: {', '.join(subscribed) or 'nothing'}")

        client = new_client
        light_state["connected"] = True
        light_state["mac"] = target
        return {"status": "connected", "mac": target}

    except Exception as e:
        log.error(f"Connection error: {e}")
        if new_client is not None:
            try:
                await new_client.disconnect()
            except Exception:
                pass
        light_state["connected"] = False
        client = None
        return {"status": "error", "error": str(e)}


async def connect_to_light(mac: Optional[str] = None) -> dict:
    """Connect to the light."""
    async with ble_lock:
        return await _connect(mac)


async def disconnect_from_light() -> dict:
    """Disconnect from the light."""
    global client
    async with ble_lock:
        try:
            if client and client.is_connected:
                await client.disconnect()
        except Exception as e:
            log.warning(f"Disconnect error: {e}")
        client = None
        light_state["connected"] = False
        return {"status": "disconnected"}


async def send_command(packet: bytearray, write_uuid: str = WRITE_UUID) -> dict:
    """Write one command packet, then wait briefly for the light to answer."""
    global client
    async with ble_lock:
        if not client or not client.is_connected:
            result = await _connect()
            if result["status"] not in ("connected", "already_connected"):
                return result

        try:
            response_event.clear()
            log.info(f"Sending: {packet.hex()}")
            await client.write_gatt_char(write_uuid, packet, response=False)
        except Exception as e:
            log.error(f"Send error: {e}")
            light_state["connected"] = False
            client = None
            return {"status": "error", "error": str(e)}

        light_state["last_ok"] = time.time()
        try:
            await asyncio.wait_for(response_event.wait(), timeout=RESPONSE_TIMEOUT)
            return {
                "status": "ok",
                "sent": packet.hex(),
                "response": light_state.get("last_response")
            }
        except asyncio.TimeoutError:
            return {
                "status": "ok",
                "sent": packet.hex(),
                "response": None,
                "note": "Command sent, no response received (may be normal)"
            }


# ── HTTP API Routes ─────────────────────────────────────────────────────────
routes = web.RouteTableDef()


@routes.get("/health")
async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "netlea-ble-bridge",
        "light_mac": NETLEA_MAC,
        "connected": light_state["connected"]
    })


@routes.get("/scan")
async def scan(request):
    """Scan for Netlea BLE devices."""
    log.info("Scanning for BLE devices...")
    found = await BleakScanner.discover(timeout=15, return_adv=True)
    netlea_devices = []
    for device, adv in found.values():
        name = device.name or adv.local_name or ""
        if ("NL-" in name or "netlea" in name.lower()
                or device.address.lower() == NETLEA_MAC.lower()):
            netlea_devices.append({
                "name": name or None,
                "address": device.address,
                "rssi": adv.rssi
            })
    return web.json_response({
        "devices": netlea_devices,
        "total_scanned": len(found)
    })


@routes.get("/status")
async def status(request):
    """Last values this bridge sent successfully (not read back from the light)."""
    return web.json_response(light_state)


@routes.post("/connect")
async def connect(request):
    """Connect to the light."""
    body = await read_json(request)
    mac = body.get("mac", NETLEA_MAC)
    if not isinstance(mac, str) or not mac:
        raise BadRequest("mac must be a text value")
    return json_result(await connect_to_light(mac))


@routes.post("/disconnect")
async def disconnect(request):
    """Disconnect from the light."""
    return json_result(await disconnect_from_light())


@routes.post("/control")
async def manual_control(request):
    """
    Manual control (cmdId 26). Unreliable on the tested light; use /direct.
    Body: { "R": 50, "W": 80, "G": 30, "B": 60, "F": 5, "grade": 0, "on": 1, "off": 0 }
    """
    body = await read_json(request)
    ch = channel_fields(body)
    packet = build_manual_control(
        on=int_field(body, "on", 1, 0, MAX_U32),
        off=int_field(body, "off", 0, 0, MAX_U32),
        r=ch["R"], w=ch["W"], g=ch["G"], b=ch["B"], f=ch["F"],
        mode_id=int_field(body, "modeId", 0, 0, MAX_U8),
        grade=int_field(body, "grade", 0, 0, MAX_U32),
    )
    return json_result(await send_command(packet))


@routes.post("/direct")
async def direct_control(request):
    """
    Direct control (cmdId 16). Primary control endpoint.
    Body: { "lampState": 1, "lampMode": 0, "R": 50, "W": 80, "G": 30, "B": 60, "F": 5,
            "grade": 0, "modeId": 0 }
    """
    body = await read_json(request)
    ch = channel_fields(body)
    lamp_state = int_field(body, "lampState", 1, 0, 1)
    mode_id = int_field(body, "modeId", 0, 0, MAX_U8)
    grade = int_field(body, "grade", 0, 0, MAX_U32)
    packet = build_direct_control(
        lamp_state=lamp_state,
        lamp_mode=int_field(body, "lampMode", 0, 0, MAX_U8),
        r=ch["R"], w=ch["W"], g=ch["G"], b=ch["B"], f=ch["F"],
        restore_time=int_field(body, "restoreActTime", 0, 0, MAX_U32),
        mode_id=mode_id,
        grade=grade,
    )

    result = await send_command(packet)
    if result.get("status") == "ok":
        light_state.update(ch)
        light_state.update({"grade": grade, "power": lamp_state > 0, "mode": mode_id})
        scheduler.manual_change()
    return json_result(result)


@routes.post("/power")
async def power(request):
    """
    Power on/off.
    Body: { "on": true } or { "on": false }
    """
    body = await read_json(request)
    power_on = body.get("on", True)
    if power_on in (0, 1):
        power_on = bool(power_on)
    if not isinstance(power_on, bool):
        raise BadRequest("on must be true or false")
    # Power-on resends the last color values this process sent. If those are
    # all 0 (for example right after a restart), "on" would leave the light
    # dark. Refuse instead of guessing.
    if power_on and not any(light_state[c] for c in CHANNELS):
        return web.json_response({
            "status": "error",
            "error": "The last color values this bridge sent are all 0. "
                     "Send /direct with the levels you want instead."
        }, status=409)
    result = await send_command(build_power_command(power_on))
    if result.get("status") == "ok":
        light_state["power"] = power_on
        scheduler.manual_change()
    return json_result(result)


@routes.post("/read")
async def read_state(request):
    """Send cmdId 5. Meant as "read device state"; purpose not confirmed."""
    return json_result(await send_command(build_read_state()))


@routes.post("/schedule")
async def schedule(request):
    """
    Set schedule (cmdId 18).
    Body: {
        "sunMoonDisable": 0,
        "sunTime": 0,
        "moonTime": 0,
        "trips": [
            { "tripDisable": 0, "weekCycle": 127, "startH": 8, "startM": 0,
              "endH": 20, "endM": 0, "R": 50, "W": 80, "G": 30, "B": 60, "F": 5,
              "modeId": 0, "grade": 0 }
        ]
    }
    """
    body = await read_json(request)
    raw_trips = body.get("trips", [])
    if not isinstance(raw_trips, list):
        raise BadRequest("trips must be a list")
    if len(raw_trips) > MAX_TRIPS:
        raise BadRequest(f"at most {MAX_TRIPS} trips")

    trips = []
    for n, raw in enumerate(raw_trips, start=1):
        if not isinstance(raw, dict):
            raise BadRequest(f"trip {n} must be an object")
        try:
            trip = {
                "tripDisable": int_field(raw, "tripDisable", 0, 0, 1),
                "weekCycle": int_field(raw, "weekCycle", 0, 0, 127),
                "startH": int_field(raw, "startH", 0, 0, 23),
                "startM": int_field(raw, "startM", 0, 0, 59),
                "endH": int_field(raw, "endH", 0, 0, 23),
                "endM": int_field(raw, "endM", 0, 0, 59),
                "modeId": int_field(raw, "modeId", 0, 0, MAX_U8),
                "grade": int_field(raw, "grade", 0, 0, MAX_U32),
            }
            trip.update(channel_fields(raw))
        except BadRequest as e:
            raise BadRequest(f"trip {n}: {e}")
        trips.append(trip)

    packet = build_schedule(
        sun_moon_disable=int_field(body, "sunMoonDisable", 0, 0, 1),
        sun_time=int_field(body, "sunTime", 0, 0, MAX_U32),
        moon_time=int_field(body, "moonTime", 0, 0, MAX_U32),
        trips=trips,
    )
    return json_result(await send_command(packet))


@routes.post("/raw")
async def raw_command(request):
    """
    Send a raw command packet. The bridge adds the header and checksum.
    Body: { "cmdId": 16, "data": [1, 0, 0, 0], "flag": 0 }
    """
    body = await read_json(request)
    cmd_id = int_field(body, "cmdId", None, 0, MAX_U8)
    flag = int_field(body, "flag", 0, 0, MAX_U8)
    data = body.get("data")
    if not isinstance(data, list):
        raise BadRequest("data must be a list of byte values")
    if len(data) > MAX_U8 - 7:
        raise BadRequest(f"data can hold at most {MAX_U8 - 7} bytes")
    for value in data:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_U8:
            raise BadRequest("data must be a list of whole numbers between 0 and 255")
    return json_result(await send_command(build_packet(cmd_id, data, flag)))


# ── Saved settings and schedules ────────────────────────────────────────────
class Store:
    """Settings, schedules and scheduler switches, kept in one JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.data = self._empty()

    @staticmethod
    def _empty() -> dict:
        return {"version": 1, "settings": [], "schedules": [],
                "active_schedule": None, "scheduler_enabled": False}

    def load(self):
        try:
            raw = json.loads(self.path.read_text())
            data = self._empty()
            data.update({k: raw[k] for k in data if k in raw})
            self.data = data
            log.info(f"Loaded {len(data['settings'])} settings and "
                     f"{len(data['schedules'])} schedules from {self.path}")
        except FileNotFoundError:
            self.data = self._empty()
        except Exception as e:
            log.error(f"Could not read {self.path}: {e}. Starting with nothing saved; "
                      f"the file is left untouched until something is saved.")
            self.data = self._empty()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)

    # helpers
    def settings(self):
        return self.data["settings"]

    def schedules(self):
        return self.data["schedules"]

    def find(self, kind: str, item_id: str):
        for item in self.data[kind]:
            if item["id"] == item_id:
                return item
        return None

    def active(self):
        sid = self.data["active_schedule"]
        return self.find("schedules", sid) if sid else None


store = Store(STATE_FILE)


class Scheduler:
    """Keeps the light on what the active schedule asks for.

    Every SCHEDULER_TICK seconds it works out the target. It sends it when the
    target changed, when the last send failed, or every RESEND_MINUTES, so a
    missed command or a light reset is corrected without waiting for the next
    schedule point. A manual change (/direct, /power) pauses it until the next
    schedule point.
    """

    def __init__(self):
        self.last_target = None
        self.last_sent_at = 0.0
        self.override_until: Optional[datetime] = None
        self.last_error = None
        self.wake = asyncio.Event()

    def enabled(self) -> bool:
        return bool(store.data["scheduler_enabled"] and store.active())

    def current(self, now: Optional[datetime] = None):
        sched = store.active()
        if not sched:
            return None
        return ns.evaluate(sched, store.settings(), now or datetime.now())

    def manual_change(self):
        """Called after a successful manual command."""
        if not self.enabled():
            return
        r = self.current()
        self.override_until = r["next_change"] if r else None
        self.last_target = None
        log.info(f"Manual change: schedule paused until {self.override_until}")

    def resume(self):
        self.override_until = None
        self.last_target = None
        self.wake.set()

    def refresh(self):
        """A setting or the running schedule was edited: recheck soon, keep any pause."""
        self.last_target = None
        self.wake.set()

    def reset(self):
        """Schedule or switches changed: apply the new target soon."""
        self.override_until = None
        self.last_target = None
        self.wake.set()

    async def tick(self, now: Optional[datetime] = None):
        now = now or datetime.now()
        if not self.enabled():
            return None
        if self.override_until is not None:
            if now < self.override_until:
                return None
            self.override_until = None
        r = self.current(now)
        if r is None:
            return None
        target = r["target"]
        due = (target != self.last_target
               or time.time() - self.last_sent_at >= RESEND_MINUTES * 60)
        if not due:
            return None
        packet = build_direct_control(
            lamp_state=target["lampState"], lamp_mode=0,
            r=target["R"], w=target["W"], g=target["G"], b=target["B"], f=target["F"],
            grade=0)
        result = await send_command(packet)
        if result.get("status") != "ok":
            self.last_error = result.get("error", "send failed")
            log.warning(f"Scheduler: send failed: {self.last_error}")
            return result
        self.last_error = None
        self.last_target = target
        self.last_sent_at = time.time()
        light_state.update({c: target[c] for c in CHANNELS})
        light_state.update({"grade": 0, "power": target["lampState"] > 0, "mode": 0})
        report = (result.get("response") or {}).get("report")
        if report and any(report[c] != target[c] for c in CHANNELS):
            log.warning(f"Scheduler: light reports {report}, expected {target}")
        log.info(f"Scheduler: applied {target}")
        return result

    async def run(self):
        while True:
            try:
                result = await self.tick()
                pause = 60 if (result and result.get("status") != "ok") else SCHEDULER_TICK
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(f"Scheduler error: {e}")
                pause = 60
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=pause)
            except asyncio.TimeoutError:
                pass


scheduler = Scheduler()


def _iso(dt: Optional[datetime]):
    return dt.isoformat(timespec="minutes") if dt else None


def scheduler_status() -> dict:
    r = scheduler.current() if store.active() else None
    return {
        "enabled": store.data["scheduler_enabled"],
        "active_schedule": store.data["active_schedule"],
        "running": scheduler.enabled(),
        "target": r["target"] if r else None,
        "since": _iso(r["since"]) if r else None,
        "next_change": _iso(r["next_change"]) if r else None,
        "fading": r["fading"] if r else False,
        "paused_until": _iso(scheduler.override_until),
        "last_error": scheduler.last_error,
    }


def _invalid(e: ns.Invalid):
    raise BadRequest(str(e))


async def _body_or_400(request) -> dict:
    return await read_json(request)


@routes.get("/")
async def index(request):
    page = WEB_DIR / "index.html"
    if not page.is_file():
        raise web.HTTPNotFound(text="Web page not installed")
    return web.FileResponse(page, headers={"Cache-Control": "no-cache"})


@routes.get("/assets/{name}")
async def asset(request):
    """Images and other files used by the web page (web/assets/)."""
    name = request.match_info["name"]
    path = WEB_DIR / "assets" / name
    if Path(name).name != name or name.startswith(".") or not path.is_file():
        raise web.HTTPNotFound(text="Not found")
    return web.FileResponse(path, headers={"Cache-Control": "max-age=3600"})


@routes.get("/api/state")
async def api_state(request):
    last = light_state.get("last_response") or {}
    return web.json_response({
        "now": datetime.now().isoformat(timespec="seconds"),
        "connected": light_state["connected"],
        "sent": {c: light_state[c] for c in CHANNELS} | {"power": light_state["power"]},
        "light": last.get("report"),
        "light_at": light_state.get("last_update"),
        "last_ok": light_state.get("last_ok"),
        "scheduler": scheduler_status(),
    })


@routes.get("/api/settings")
async def api_settings(request):
    return web.json_response(store.settings())


@routes.post("/api/settings")
async def api_setting_add(request):
    try:
        item = ns.clean_setting(await _body_or_400(request))
    except ns.Invalid as e:
        _invalid(e)
    store.settings().append(item)
    store.save()
    return web.json_response(item, status=201)


@routes.put("/api/settings/{id}")
async def api_setting_put(request):
    old = store.find("settings", request.match_info["id"])
    if old is None:
        raise web.HTTPNotFound(text="Setting not found")
    try:
        item = ns.clean_setting(await _body_or_400(request), old["id"])
    except ns.Invalid as e:
        _invalid(e)
    old.clear()
    old.update(item)
    store.save()
    scheduler.refresh()
    return web.json_response(item)


@routes.delete("/api/settings/{id}")
async def api_setting_delete(request):
    sid = request.match_info["id"]
    if store.find("settings", sid) is None:
        raise web.HTTPNotFound(text="Setting not found")
    users = [s["name"] for s in store.schedules()
             if any(p.get("setting") == sid for p in s["points"])]
    if users:
        return web.json_response(
            {"status": "error", "error": "Used by schedule: " + ", ".join(users)}, status=409)
    store.data["settings"] = [s for s in store.settings() if s["id"] != sid]
    store.save()
    return web.json_response({"status": "deleted"})


@routes.get("/api/schedules")
async def api_schedules(request):
    return web.json_response(store.schedules())


@routes.post("/api/schedules")
async def api_schedule_add(request):
    ids = {s["id"] for s in store.settings()}
    try:
        item = ns.clean_schedule(await _body_or_400(request), ids)
    except ns.Invalid as e:
        _invalid(e)
    store.schedules().append(item)
    store.save()
    return web.json_response(item, status=201)


@routes.put("/api/schedules/{id}")
async def api_schedule_put(request):
    old = store.find("schedules", request.match_info["id"])
    if old is None:
        raise web.HTTPNotFound(text="Schedule not found")
    ids = {s["id"] for s in store.settings()}
    try:
        item = ns.clean_schedule(await _body_or_400(request), ids, old["id"])
    except ns.Invalid as e:
        _invalid(e)
    old.clear()
    old.update(item)
    store.save()
    if store.data["active_schedule"] == item["id"]:
        scheduler.refresh()
    return web.json_response(item)


@routes.delete("/api/schedules/{id}")
async def api_schedule_delete(request):
    sid = request.match_info["id"]
    if store.find("schedules", sid) is None:
        raise web.HTTPNotFound(text="Schedule not found")
    store.data["schedules"] = [s for s in store.schedules() if s["id"] != sid]
    if store.data["active_schedule"] == sid:
        store.data["active_schedule"] = None
        store.data["scheduler_enabled"] = False
    store.save()
    scheduler.reset()
    return web.json_response({"status": "deleted"})


@routes.get("/api/schedules/{id}/preview")
async def api_schedule_preview(request):
    sched = store.find("schedules", request.match_info["id"])
    if sched is None:
        raise web.HTTPNotFound(text="Schedule not found")
    return web.json_response(ns.day_preview(sched, store.settings(), datetime.now()))


@routes.post("/api/scheduler")
async def api_scheduler(request):
    """Body: {"enabled": true|false, "active_schedule": "<id>"|null} (both optional)."""
    body = await read_json(request)
    if "active_schedule" in body:
        sid = body["active_schedule"]
        if sid is not None and store.find("schedules", sid) is None:
            raise BadRequest("schedule not found")
        store.data["active_schedule"] = sid
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise BadRequest("enabled must be true or false")
        if body["enabled"] and not store.data["active_schedule"]:
            raise BadRequest("choose a schedule first")
        store.data["scheduler_enabled"] = body["enabled"]
    if store.data["active_schedule"] is None:
        store.data["scheduler_enabled"] = False
    store.save()
    scheduler.reset()
    log.info(f"Scheduler: enabled={store.data['scheduler_enabled']} "
             f"schedule={store.data['active_schedule']}")
    return web.json_response(scheduler_status())


@routes.post("/api/scheduler/resume")
async def api_scheduler_resume(request):
    scheduler.resume()
    return web.json_response(scheduler_status())


# ── Middleware ───────────────────────────────────────────────────────────────
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
    if CORS_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except BadRequest as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


# ── Main ────────────────────────────────────────────────────────────────────
async def on_startup(app):
    log.info(f"Netlea BLE Bridge starting on {BRIDGE_HOST}:{BRIDGE_PORT}")
    log.info(f"Target light MAC: {NETLEA_MAC}")
    log.info("Auto-connecting on first command...")
    store.load()
    app["scheduler_task"] = asyncio.create_task(scheduler.run())


async def on_shutdown(app):
    log.info("Shutting down...")
    task = app.get("scheduler_task")
    if task:
        task.cancel()
    await disconnect_from_light()


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware, error_middleware])
    app.add_routes(routes)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


def main():
    if not NETLEA_MAC:
        sys.exit("NETLEA_MAC is not set. Copy config.example to config and set your light's MAC address.")
    web.run_app(create_app(), host=BRIDGE_HOST, port=BRIDGE_PORT)


if __name__ == "__main__":
    main()
