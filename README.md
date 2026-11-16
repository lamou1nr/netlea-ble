<p align="center">
  <img src="web/assets/icon-180.png" width="96" height="96" alt="Good Boy Rick Labs logo, a line drawing of a dog">
</p>

# netlea-ble

A Good Boy Rick Labs project.

A small Python service that talks to a Netlea aquarium light over Bluetooth Low Energy (BLE).
It has a web page for controlling the light, saving your own settings and running daily
schedules, and a local HTTP API that anything else (a cron job, a script, a home automation
system) can use.

This is an unofficial hobby project. It is not affiliated with or endorsed by Netlea. The
protocol details below are the author's own working notes for one light and may be incomplete
or wrong for yours.

## Tested hardware

Only one light has been tested:

- **Netlea AT5S Gen 3 (Model 580S, 92W)**. It advertises over BLE with a name that starts
  with `NL-5S-580`.

Nothing is known about other Netlea models or other AT5S generations.

Tested host: one Linux machine running Debian 13 with BlueZ 5.82, Python 3.13.5, bleak 3.0.1
and aiohttp 3.13.5. The service runs directly on the host (not in a container) because it
needs the host's Bluetooth adapter.

## What works and what does not

Results from the author's light. "Tested" means the author watched the light while the
command was sent on 2026-11-15, unless noted.

| Endpoint | Command | Status |
|---|---|---|
| `POST /direct` | cmdId 16 | Works. This is the command the author uses for all day-to-day control, driven from cron. |
| `POST /raw` | any | Works. Tested by sending a cmdId 16 payload. |
| `POST /power` | cmdId 16 | Works. Off and on both tested. See the notes on `/power` below. |
| `POST /schedule` | cmdId 18 | Partly tested. A single-trip upload worked. See "Schedule upload" for what is still untested. |
| `POST /control` | cmdId 26 | Does not work reliably. Do not use it. In the 2026-11-15 test it had no visible effect. In the author's earlier notes, sending `on: 1` turned the light off. |
| `POST /read` | cmdId 5 | Do not use. The bridge has never received a reply to it, and it may not be a read at all: in the one watched test the light's output appeared to change after it was sent. |
| `GET /health`, `GET /status`, `GET /scan`, `POST /connect`, `POST /disconnect` | none | In regular use. |
| Web page (`/`) | cmdId 16 | Works. Used from a phone browser on 2026-11-15: sliders, brightness, on and off, and saved settings. |
| Bridge schedules | cmdId 16 | Partly tested. On 2026-11-15 a schedule was started from the page, applied its current point, paused after manual changes, and applied the next point on time. Smooth fades, day selection and the periodic resend have only been checked by the automated tests, not on the light. |

Important limitations:

- The light answers every `/direct` command with a status packet (see "cmdId 20" below). The
  bridge returns it in the `response` field, usually well under a second after the command.
  `"response": null` means nothing came back within the wait time. Replies to the other
  commands have not been observed yet.
- `GET /status` reports the values the bridge last sent successfully. It is not read back
  from the light.
- There is no authentication. See "Security".

## Requirements

- Linux with BlueZ and a Bluetooth LE adapter in range of the light.
- Python 3 (tested with 3.13).
- Python packages `bleak` and `aiohttp` (see `requirements.txt`).

## Install

```bash
# clone this repository to /opt/netlea-ble (or copy the files there), then:
cd /opt/netlea-ble
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example config
chmod 600 config
```

Edit `config` and set your light's MAC address. `config` is listed in `.gitignore`, so your
MAC address stays out of git.

To find the MAC address, close the vendor phone app, then run:

```bash
bluetoothctl scan on
```

and look for a device whose name starts with `NL-`.

### Which devices can reach it

Out of the box the bridge only answers on the machine it runs on (`127.0.0.1`). That is
enough for scripts and cron jobs on the same machine.

To use the web page from a phone, tablet or another computer on your network, remove the
`#` in front of this line in `config`:

```
BRIDGE_HOST=0.0.0.0
```

Then restart the service and open `http://<address of this machine>:3003/`. There is no
login, so read "Security" before you do this.

## Configuration

`config` is a plain `KEY=VALUE` file in systemd `EnvironmentFile` format. The service reads
these environment variables:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `NETLEA_MAC` | yes | none | Bluetooth MAC address of the light. The service exits at startup if it is not set. |
| `BRIDGE_PORT` | no | `3003` | TCP port for the HTTP API. |
| `BRIDGE_HOST` | no | `127.0.0.1` | Address to listen on. The default accepts connections from the same machine only. Set it to `0.0.0.0` to listen on every network interface, which is needed to open the web page from a phone or another computer. Read "Security" first. |
| `CORS_ORIGIN` | no | `*` | Value of the `Access-Control-Allow-Origin` header. Set it to empty to send no CORS headers. |
| `RESPONSE_TIMEOUT` | no | `3.0` | Seconds to wait for the light to answer a command. |
| `STATE_FILE` | no | `data/state.json` next to `ble_bridge.py` | Where saved settings and schedules are kept. |
| `RESEND_MINUTES` | no | `15` | While a schedule runs, how often the current values are sent again even if nothing changed. |

## Run

By hand:

```bash
cd /opt/netlea-ble
set -a; . ./config; set +a
.venv/bin/python3 ble_bridge.py
```

With systemd:

```bash
cp netlea-ble.service.example /etc/systemd/system/netlea-ble.service
# edit the paths in the unit if you installed somewhere other than /opt/netlea-ble
systemctl daemon-reload
systemctl enable --now netlea-ble
curl http://localhost:3003/health
```

The bridge does not connect to the light at startup. It connects on the first command that
needs the light, or when you call `POST /connect`.

Using the bridge while the vendor phone app is connected to the light has not been tested.
Close the app first.

## Web page

Open `http://localhost:3003/` in a browser on the machine that runs the bridge. To open it
from a phone or another computer, set `BRIDGE_HOST=0.0.0.0` in `config`, restart the
service, and use `http://<host>:3003/`. The page has three parts:

- **Control**: a large brightness slider that scales the color mix, an on and off switch,
  sliders for R, W, G, B and F, and a box to save the current values under a name. The
  top of the page shows what the light itself reports (see "cmdId 20").
- **Saved settings**: named sets of values. You can send one to the light, change it or
  delete it. A setting that a schedule uses cannot be deleted until the schedule stops
  using it.
- **Schedules**: a schedule is a list of points. Each point has a time, the days it applies
  to, and what to do: turn off, use a saved setting, or use custom values, with a brightness
  percentage. "Fade smoothly between points" moves the values gradually from one point to
  the next instead of switching at the point. A chart shows the day.

The round switch at the top right changes between light and dark mode. To use your own
logo in the header, replace `web/assets/logo.png` with your image (dark artwork on a transparent
background; the page shows it in white). The `icon-*.png` files are the browser tab and home
screen icons, and `manifest.json` holds the name shown when the page is added to a phone's
home screen.

The page works behind a reverse proxy under a sub-path (for example `/light/`), because it
uses relative addresses.

### How schedules run

Schedules are run by the bridge, not stored in the light. The bridge works out every 20
seconds what the active schedule asks for and sends it with cmdId 16 when it changes. It
also resends it every `RESEND_MINUTES`, so a missed command or a light that lost its
setting is corrected without waiting for the next point. If sending fails, it retries.

- Only one schedule runs at a time, and the bridge schedule is off until you start it.
- A change made by hand while a schedule runs (from the page, `/direct` or `/power`) pauses
  the schedule until its next point. "Resume schedule now" ends the pause.
- Times use the clock and time zone of the computer running the bridge.
- If something else also schedules the light (for example cron jobs calling `/direct`),
  turn that off before starting a bridge schedule. Each of its commands would count as a
  manual change and pause the schedule.
- The bridge has to keep running for a schedule to work. If the computer is off, the light
  keeps whatever it was last told.

The light's own schedule memory (`POST /schedule`, cmdId 18) is not used by the web page,
because the light's clock has not been verified (see "Schedule upload: tested and
untested").

## HTTP API

All request and response bodies are JSON. A request that has a body must send
`Content-Type: application/json`. The examples assume the default port.

Status codes:

| Code | Meaning |
|---|---|
| 200 | The command was written to the light (or the request needed no Bluetooth). |
| 400 | The request was refused: not JSON, a value missing, not a whole number, or out of range. Nothing was sent to the light. |
| 409 | `/power` on was refused because there are no color values to restore. |
| 502 | Bluetooth failed: the light could not be reached or the write failed. |

Values are checked, never wrapped. R, W, G and B must be 0 to 255, F 0 to 10, hours 0 to 23,
minutes 0 to 59.

### GET /health

Returns `{"status": "ok", "service": "netlea-ble-bridge", "light_mac": "...", "connected": false}`.
Does not touch Bluetooth.

### GET /status

Returns the bridge's own record of what it last sent successfully (`R`, `W`, `G`, `B`, `F`,
`grade`, `power`, `mode`), the configured MAC, the connection flag, the last packet received
from the light (`last_response`) and the time of the last successful write (`last_ok`).

### GET /scan

Runs a 15 second BLE scan and returns devices whose name contains `NL-` or `netlea`, or
whose address equals the configured MAC.

### POST /connect and POST /disconnect

Connect to, or disconnect from, the light. `/connect` accepts an optional `{"mac": "..."}` to
override the configured address for that connection.

The author's cron scripts call `/disconnect`, then `/connect`, then `/direct` for every
change, so each command goes out over a fresh connection.

### POST /direct

Direct control, cmdId 16. This is the endpoint to use.

```bash
curl -X POST http://localhost:3003/direct -H "Content-Type: application/json" \
  -d '{"lampState":1,"lampMode":0,"R":0,"W":10,"G":0,"B":0,"F":0,"grade":0,"modeId":0}'
```

| Field | Default | Meaning |
|---|---|---|
| `lampState` | 1 | 1 = on, 0 = off. To turn the light off, send 0 here and 0 for every channel. `lampState` 0 with channel values still set leaves the light on (tested). The tested light fades out over a few seconds when turned off, so it does not go dark at once. |
| `lampMode` | 0 | Always sent as 0 by the author. Other values untested. |
| `R`, `W`, `G`, `B` | 0 | Channel levels. Each is one raw byte, 0 to 255, sent to the light as is. The 2026-11-15 tests used values of 22 or lower. |
| `F` | 0 | Fifth value, 0 to 10. What it controls has not been verified (see below). |
| `grade` | 0 | Sent as a 4-byte value. The author dims by scaling the channel values and always sends `grade` 0, and with `grade` 0 the light follows the channel values. Whether the light also dims in response to other `grade` values has not been tested. |
| `modeId` | 0 | Always sent as 0 by the author. Other values untested. |
| `restoreActTime` | 0 | Always sent as 0 by the author. Other values untested. |

Each channel is driven independently, and the mix of the five values sets the color. From
the author's notes: R is red (635 and 655 nm), W is phosphor white, G is green (520 nm), B is
blue (437 and 450 nm). Dim single-channel tests of R, W, G and B on 2026-11-15 produced red,
white, green and blue light.

F has not been tested on its own. The author's notes describe it as warm red (about 615 nm)
and say it cannot light the fixture by itself. It is also possible that F is not a color
at all but the cooling fan level. This has not been tested.

A `/direct` command sent while an uploaded schedule trip was active took over from the trip
(observed once).

### POST /power

```bash
curl -X POST http://localhost:3003/power -H "Content-Type: application/json" -d '{"on": false}'
curl -X POST http://localhost:3003/power -H "Content-Type: application/json" -d '{"on": true}'
```

- Off sends cmdId 16 with `lampState` 0 and every channel 0.
- On resends the last channel values this bridge process sent, with `lampState` 1.
- On returns HTTP 409 if the last values the bridge knows are all 0 (for example right after
  the service starts), because resending zeros would leave the light dark. Send `/direct`
  with the levels you want instead.

### POST /schedule

Uploads a schedule of up to 10 time periods (called trips) to the light, cmdId 18. How the
light times them has not been verified. See "Schedule upload: tested and untested" below.

```bash
curl -X POST http://localhost:3003/schedule -H "Content-Type: application/json" -d '{
  "sunMoonDisable": 0, "sunTime": 0, "moonTime": 0,
  "trips": [
    {"tripDisable": 0, "weekCycle": 127, "startH": 8, "startM": 0, "endH": 20, "endM": 0,
     "R": 0, "W": 10, "G": 0, "B": 0, "F": 0, "modeId": 0, "grade": 0}
  ]
}'
```

Every upload sends all 10 trip slots. Slots you do not fill are sent as zeros, so an upload
replaces the whole stored schedule. More than 10 trips is refused. See "Schedule upload"
below for the tested and untested parts.

### POST /control

Manual control, cmdId 26. Body: `{"R":0,"W":0,"G":0,"B":0,"F":0,"grade":0,"on":1,"off":0,"modeId":0}`.
Not reliable on the tested light (see the table above). Use `/direct`. Values sent with
`/control` are not remembered for `/power` or `/status`.

### POST /read

Sends cmdId 5. It was meant to read the device state, but no reply has ever been received
and the command may do something else (see the table above). Do not use it on a light you
care about until it has been understood.

### Settings and schedules API

All of these use JSON. Field rules are the same as in the web page: names up to 40
characters, R, W, G, B 0 to 255, F 0 to 10, times as `HH:MM` (24-hour clock), `days` as a
bit mask (bit 0 Monday to bit 6 Sunday, 127 every day), brightness 0 to 100, at most 48
points per schedule.

| Method and path | What it does |
|---|---|
| `GET /api/state` | What the light last reported, what the bridge last sent, and the schedule status (running, paused until, next point, last error). |
| `GET /api/settings` | List saved settings. |
| `POST /api/settings` | Add one: `{"name": "Day", "R": 10, "W": 16, "G": 5, "B": 2, "F": 1}`. |
| `PUT /api/settings/{id}` | Replace one. |
| `DELETE /api/settings/{id}` | Delete one. HTTP 409 if a schedule uses it. |
| `GET /api/schedules` | List schedules. |
| `POST /api/schedules` | Add one (example below). |
| `PUT /api/schedules/{id}` | Replace one. |
| `DELETE /api/schedules/{id}` | Delete one. If it was running, the bridge schedule stops. |
| `GET /api/schedules/{id}/preview` | The values the schedule asks for over today, every 10 minutes. |
| `POST /api/scheduler` | `{"active_schedule": "<id>", "enabled": true}` starts a schedule; `{"enabled": false}` stops it. |
| `POST /api/scheduler/resume` | End a pause caused by a manual change. |

Example schedule:

```json
{"name": "Weekdays", "smooth": true, "points": [
  {"time": "08:00", "days": 31, "action": "setting", "setting": "<setting id>", "brightness": 50},
  {"time": "12:00", "days": 31, "action": "values", "values": {"R": 0, "W": 20, "G": 0, "B": 5, "F": 1}},
  {"time": "20:00", "days": 127, "action": "off"}]}
```

### POST /raw

Sends any command. Body: `{"cmdId": 16, "data": [1, 0, 0, 0, ...], "flag": 0}` where `data` is
the list of payload bytes (each 0 to 255). The bridge adds the header and checksum. `cmdId`
and `data` are required. `flag` is optional and sets the fifth header byte.

## Protocol notes

These describe what the code sends. They are not an official specification.

### GATT

| Role | UUID | Notes |
|---|---|---|
| Service | `0000fffa-0000-1000-8000-00805f9b34fb` | |
| Write | `0000ff02-0000-1000-8000-00805f9b34fb` | Write without response. This is the characteristic the bridge writes to. The light acts on it. |
| Write (unused) | `0000fffc-0000-1000-8000-00805f9b34fb` | Present on the light. According to the author's notes the light is silent on it. The bridge does not use it. |
| Notify | `0000ff01-0000-1000-8000-00805f9b34fb` | The light answers here (tested 2026-11-15). The bridge subscribes to it. |
| Notify (second) | `0000fffd-0000-1000-8000-00805f9b34fb` | The bridge subscribes to it as well. Nothing has ever been received on it. |

### Packet format

```
[0x5A, totalLength, 0x01, cmdId, flag, 0x00, payload..., checksum]
```

- `totalLength` counts every byte including the checksum.
- The third byte is always 1 (protocol version).
- `flag` is 0 for every command the bridge builds itself.
- `checksum` is the sum of all preceding bytes modulo 256.
- The bridge sends multi-byte fields as 4 bytes, little-endian. That is what worked for the
  author, but no test so far could tell a 4-byte reading from a 2-byte one: the channel
  values come before these fields, and only a single schedule trip has been tried. If the
  light reads them as 2 bytes, values such as `grade` and schedule trips after the first
  would be misread.

### cmdId 16: direct control

Payload, 20 bytes:

```
lampState, lampMode, reserve1, reserve2, R, W, G, B, F,
restoreActTime (4 bytes LE), modeId, grade (4 bytes LE), reserve3, reserve4
```

Example, R=16 W=22 G=8 B=5 F=0, grade 0 (27 bytes in total):

```
5a 1b 01 10 00 00  01 00 00 00  10 16 08 05 00  00 00 00 00  00  00 00 00 00  00 00  ba
```

### cmdId 18: schedule upload

Payload, 169 bytes (the whole packet is 176 bytes):

```
sunMoonDisable, sunTime (4 bytes LE), moonTime (4 bytes LE), then 10 trips of 16 bytes each:
  tripDisable, weekCycle, startH, startM, endH, endM, R, W, G, B, F, modeId, grade (4 bytes LE)
```

`weekCycle` is a bit mask of weekdays. 127 means every day, and it is the only value that has
been tested. According to the author's notes, bit 0 is Monday and bit 6 is Sunday,
`tripDisable` 1 switches a trip off, `sunMoonDisable` 1 switches the fades on (despite its
name), and
`sunTime` and `moonTime` are sunrise and sunset fade durations in minutes (0 to 300), not
color presets.

### Schedule upload: tested and untested

Tested on 2026-11-15 with one trip, `weekCycle` 127, `grade` 0, `sunMoonDisable` 1,
`sunTime` 0 and `moonTime` 0:

- Uploading a trip whose time window included the current time switched the light to that
  trip's color at once. It is not known whether the light compared the window with its own
  clock or simply showed the trip it had just received.
- Uploading the same kind of trip with `tripDisable` 1 did not change the light.

Not tested: a trip that starts later than the upload, more than one trip, what the light
does when a trip ends, sunrise and sunset fades (non-zero `sunTime` or `moonTime`),
`weekCycle` values other than 127, `grade` values other than 0, and whether a stored
schedule survives a power cut.

The bridge never sets the light's clock. According to the author's notes the protocol has a
time sync command (cmdId 4), which this bridge does not implement. Until that exists, do
not rely on schedule times.

### cmdId 20: status report from the light

The light sends this on FF01 after every cmdId 16 command, about 0.1 seconds later. It is
38 bytes with a 31-byte payload. Observed on 2026-11-15 after sending R=10 W=15 G=5 B=4 F=0:

```
5a 26 01 14 00 00  01 00 00 01 00 01  0a 0f 05 04 00  00 00  (9 bytes) (9 bytes)  bf
```

Counting the payload from 0, bytes 6 to 10 are R, W, G, B and F as the light now has them.
In both tests they equalled the values just sent. Byte 3 was 1 while the light was on. The
names used for the
other bytes in other public descriptions of this protocol family are, in order: stateType,
startH, startM, lampState, lampMode, pattern, then the five channels and two reserved bytes,
followed by two 9-byte blocks for other lamp types. Those names have not been verified here.

### cmdId 26: manual control

Payload: `on (4 bytes LE), off (4 bytes LE), R, W, G, B, F, modeId, grade (4 bytes LE)`.
Not reliable on the tested light.

### cmdId 5

Payload: one byte, 0. Purpose not confirmed. See `POST /read`.

## Security

- The API has **no authentication**. By default it listens on `127.0.0.1`, so only
  programs on the same machine can reach it. If you set `BRIDGE_HOST=0.0.0.0`, anyone who
  can reach the port can control the light.
- By default it sends `Access-Control-Allow-Origin: *`. This can be changed with
  `CORS_ORIGIN`.
- If no web page on another origin needs the API, set `CORS_ORIGIN` to empty.
- `GET /health` and `GET /status` return the light's MAC address.
- The web page and the settings and schedules API have no login either. Anyone who can open
  the page can change the light and edit or delete schedules.
- If you open it to the network, do so only on a network you trust, and restrict the port
  with a firewall to the machines that need it. Do not expose it to the internet.

To report a security problem, see [SECURITY.md](SECURITY.md).

## Troubleshooting

- `GET /scan` or `POST /connect` cannot find the light: close the vendor phone app and check
  that the light is powered and in Bluetooth range of the host.
- Commands return `ok` but nothing happens: call `/disconnect` and then `/connect` and send
  the command again. In the author's notes, after many service restarts the light sometimes
  needed to be unplugged and plugged back in.

## Tests

```bash
cd /opt/netlea-ble
.venv/bin/python3 -m unittest discover -s tests
```

The tests use a fake Bluetooth client, so they need no light and no adapter. They include
the exact packets that were seen working on the real light. If one of those fails, do not
change the expected bytes without testing on a real light.

## Credits

netlea-ble is written by Neil Lamoureux and published as a Good Boy Rick Labs project. The
logo is a drawing of Ricky, the author's dog.

## License

The code is under the MIT license. See [LICENSE](LICENSE).

### Logo and name

The Ricky logo (the image files in `web/assets/`) and the name Good Boy Rick Labs are not
covered by the MIT license. You may keep and show them when you run, copy or share this app.
You may not use them for any other project or product. If you build something different
from this code, replace the logo and the name with your own.
