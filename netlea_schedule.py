"""Saved settings, schedules and the "what should the light show now" logic.

Pure functions only (no Bluetooth, no HTTP), so they are easy to test.

Data model (stored as JSON by the bridge):

    setting  = {"id", "name", "R", "W", "G", "B", "F"}
    schedule = {"id", "name", "smooth": bool, "points": [point, ...]}
    point    = {"time": "HH:MM", "days": 0..127,
                "action": "off" | "setting" | "values",
                "setting": <setting id>          (action "setting")
                "values": {"R", "W", "G", "B", "F"} (action "values")
                "brightness": 0..100}            (scales the values, default 100)

days is a bit mask: bit 0 = Monday ... bit 6 = Sunday, 127 = every day.
"""

import re
import secrets
from datetime import datetime, timedelta

CHANNEL_MAX = {"R": 255, "W": 255, "G": 255, "B": 255, "F": 10}
CHANNELS = tuple(CHANNEL_MAX)
ALL_DAYS = 127
MAX_POINTS = 48
MAX_NAME = 40
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class Invalid(Exception):
    """A setting or schedule is not acceptable."""


def new_id() -> str:
    return secrets.token_hex(4)


# ── Validation ──────────────────────────────────────────────────────────────
def _int(obj, name, low, high, default=None):
    if name not in obj:
        if default is None:
            raise Invalid(f"{name} is required")
        return default
    value = obj[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"{name} must be a whole number")
    if not low <= value <= high:
        raise Invalid(f"{name} must be between {low} and {high}")
    return value


def _name(obj) -> str:
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        raise Invalid("name is required")
    name = name.strip()
    if len(name) > MAX_NAME:
        raise Invalid(f"name can be at most {MAX_NAME} characters")
    return name


def channels(obj) -> dict:
    """R, W, G, B 0-255 and F 0-10. Missing channels are 0."""
    if not isinstance(obj, dict):
        raise Invalid("values must be an object")
    return {c: _int(obj, c, 0, CHANNEL_MAX[c], 0) for c in CHANNELS}


def clean_setting(obj, setting_id=None) -> dict:
    if not isinstance(obj, dict):
        raise Invalid("setting must be an object")
    out = {"id": setting_id or new_id(), "name": _name(obj)}
    out.update(channels(obj))
    return out


def clean_schedule(obj, setting_ids, schedule_id=None) -> dict:
    if not isinstance(obj, dict):
        raise Invalid("schedule must be an object")
    smooth = obj.get("smooth", False)
    if not isinstance(smooth, bool):
        raise Invalid("smooth must be true or false")
    points = obj.get("points", [])
    if not isinstance(points, list):
        raise Invalid("points must be a list")
    if len(points) > MAX_POINTS:
        raise Invalid(f"a schedule can have at most {MAX_POINTS} points")

    cleaned = []
    for n, p in enumerate(points, start=1):
        try:
            cleaned.append(_clean_point(p, setting_ids))
        except Invalid as e:
            raise Invalid(f"point {n}: {e}")
    cleaned.sort(key=lambda p: p["time"])
    return {"id": schedule_id or new_id(), "name": _name(obj), "smooth": smooth,
            "points": cleaned}


def _clean_point(p, setting_ids) -> dict:
    if not isinstance(p, dict):
        raise Invalid("must be an object")
    t = p.get("time")
    if not isinstance(t, str) or not _TIME.match(t):
        raise Invalid("time must look like 07:30 (24-hour clock)")
    out = {"time": t, "days": _int(p, "days", 1, ALL_DAYS, ALL_DAYS)}
    action = p.get("action")
    if action == "off":
        out["action"] = "off"
        return out
    out["brightness"] = _int(p, "brightness", 0, 100, 100)
    if action == "setting":
        sid = p.get("setting")
        if sid not in setting_ids:
            raise Invalid("setting not found")
        out.update(action="setting", setting=sid)
    elif action == "values":
        out.update(action="values", values=channels(p.get("values", {})))
    else:
        raise Invalid('action must be "off", "setting" or "values"')
    return out


# ── Evaluation ──────────────────────────────────────────────────────────────
OFF = {"lampState": 0, "R": 0, "W": 0, "G": 0, "B": 0, "F": 0}


def point_values(point, settings_by_id) -> dict:
    """Channel values and lampState a point asks for."""
    if point["action"] == "off":
        return dict(OFF)
    if point["action"] == "setting":
        base = settings_by_id.get(point["setting"])
        if base is None:
            return dict(OFF)
    else:
        base = point["values"]
    scale = point.get("brightness", 100) / 100
    vals = {c: min(CHANNEL_MAX[c], int(base[c] * scale + 0.5)) for c in CHANNELS}
    vals["lampState"] = 1 if any(vals[c] for c in CHANNELS) else 0
    return vals


def _events(schedule, around: datetime, span_days: int = 8):
    """(datetime, point) for every point occurrence within +/- span_days."""
    out = []
    start = around.date() - timedelta(days=span_days)
    for d in range(2 * span_days + 1):
        day = start + timedelta(days=d)
        bit = 1 << day.weekday()
        for p in schedule["points"]:
            if p["days"] & bit:
                h, m = map(int, p["time"].split(":"))
                out.append((datetime(day.year, day.month, day.day, h, m), p))
    out.sort(key=lambda e: e[0])
    return out


def evaluate(schedule, settings, now: datetime):
    """What the light should show at `now` under this schedule.

    Returns None when the schedule has no points, otherwise a dict:
      target       {lampState, R, W, G, B, F}
      since        start of the current point
      next_change  start of the next point (None if there is none)
      fading       True while moving smoothly towards the next point
    """
    events = _events(schedule, now)
    if not events:
        return None
    by_id = {s["id"]: s for s in settings}
    prev = nxt = None
    for when, p in events:
        if when <= now:
            prev = (when, p)
        else:
            nxt = (when, p)
            break
    if prev is None:
        return None

    target = point_values(prev[1], by_id)
    fading = False
    if schedule.get("smooth") and nxt is not None:
        a, b = target, point_values(nxt[1], by_id)
        span = (nxt[0] - prev[0]).total_seconds()
        f = (now - prev[0]).total_seconds() / span if span else 0
        if a != b:
            fading = True
            target = {c: int(a[c] + (b[c] - a[c]) * f + 0.5) for c in CHANNELS}
            target["lampState"] = 1 if any(target[c] for c in CHANNELS) else 0
    return {"target": target, "since": prev[0],
            "next_change": nxt[0] if nxt else None, "fading": fading}


def day_preview(schedule, settings, day: datetime, step_minutes: int = 10):
    """Samples of the target over one day, for drawing a chart."""
    start = datetime(day.year, day.month, day.day)
    samples = []
    for i in range(0, 24 * 60 + 1, step_minutes):
        t = start + timedelta(minutes=i)
        r = evaluate(schedule, settings, t)
        vals = r["target"] if r else dict(OFF)
        samples.append({"minute": i, **{c: vals[c] for c in CHANNELS}})
    return samples
