# Changelog

## v0.1.1 (unreleased)

- The default listen address is now `127.0.0.1` (this machine only). It was `0.0.0.0`.
  If other devices need to reach the bridge, set `BRIDGE_HOST=0.0.0.0` in `config`.

## v0.1.0 (2026-11-15)

First public release.

- HTTP bridge to a Netlea AT5S Gen 3 (Model 580S, 92W) aquarium light over Bluetooth LE.
- Endpoints: `/health`, `/status`, `/scan`, `/connect`, `/disconnect`, `/direct`, `/power`,
  `/schedule`, `/control`, `/read`, `/raw`.
- `/direct` (cmdId 16) is the working control command. The light answers it with a status
  packet (cmdId 20) on characteristic FF01, which the bridge returns in `response`.
- Web page at `/` to control the light, save named settings, and create, edit and run
  daily schedules.
- Schedules are run by the bridge with cmdId 16, with optional smooth fades, a pause after
  manual changes, and periodic resends. Saved in a JSON file.
- Settings and schedules API under `/api/`.
- Request values are checked and refused with HTTP 400 when out of range. Nothing is wrapped.
- A Bluetooth failure returns HTTP 502.
- `/power` off sends `lampState` 0 with every channel 0. `/power` on resends the last channel
  values and returns HTTP 409 when those are all 0.
- `/schedule` (cmdId 18) uploads up to 10 trips. Tested with a single trip only.
- Configuration through environment variables: `NETLEA_MAC` (required, no built-in default),
  `BRIDGE_PORT`, `BRIDGE_HOST`, `CORS_ORIGIN`, `RESPONSE_TIMEOUT`, `STATE_FILE`, `RESEND_MINUTES`.
- Tests that need no light: the packets seen working on the real light, and the HTTP API
  against a fake Bluetooth client.
- Known issues: `/control` (cmdId 26) is not reliable on the tested light, `/read` (cmdId 5)
  should not be used, schedule timing is unverified and the bridge does not set the light's
  clock.
