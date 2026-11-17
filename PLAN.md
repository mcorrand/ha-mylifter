# PLAN — `mylifter` Home Assistant integration

Status: implemented as described below (v0.1.0). Decisions with alternatives are in
[DECISIONS.md](DECISIONS.md).

Protocol source: [sblu/PyLifter](https://github.com/sblu/PyLifter) at commit
`029abf219ff47dc0a836f3adef492b44b4846e3e` (2026-01-01), GPL-3.0.

## 1. What the protocol forces on us

From `PROTOCOL.md`, `protocol.py` and `client.py`:

| Fact | Consequence |
|---|---|
| One GATT service `2d88fb13-…`, write char `A886C7EC-…`, notify char `00EFF2B2-…` | Simple single-characteristic request/notify protocol. |
| Frames are `[cmd][len][payload]`, little endian | Pure encode/decode, easy to unit test. |
| App-level pairing: `GET_PASSKEY` (0x03, empty) is only answered after the **physical button** is pressed; the 6-byte passkey is permanent. Every session must send `SET_PASSKEY` (0x03 + 6 bytes) first. | Config flow pairing step; passkey stored in the config entry. No BLE bonding. |
| Status is **only** reported as the response to a `MOVE` (0x23) frame. A client keeps sending `MOVE` frames ("keep-alive") at ~4 Hz idle / ~5 Hz moving. | There is no passive status read. Reading the load or position means connecting and streaming `MOVE STOP` frames. |
| Every `MOVE` frame must echo the device's last reported position, otherwise the device reports **Sync error 0x09**. | Before the first status arrives the echo is `0` (same as upstream). Sync errors are cleared before a move. |
| Motion is carried by the keep-alive stream itself: the move code in each frame is the current *intent*. | "Moving" = keep-alive frames carry `SMART_UP`/`SMART_DOWN`. "Stopped" = frames carry `STOP`. |
| `SMART_UP`/`SMART_DOWN` drive to the top/bottom smart points stored **in the motor**; error `0x83` if not set, `0x81` at soft limit, `0x86` at end of travel. | Cover open/close map to smart moves, which are bounded by the device even if HA disappears. |
| No command reads back the smart points or a travel range. | The cover's 0–100 % has to come from learned or user-entered raw positions. |
| `GET_VERSION` (0x0A) gives firmware major/minor in payload bytes 7/6. | Firmware is shown in device info and a diagnostic sensor. |
| Upstream "transparent reconnect" resumes the previous move intent after a link error. | **Not reused.** It is an automatic move retry, which the safety rules forbid. |

## 2. Architecture

```
custom_components/mylifter/
  protocol.py      vendored + modified PyLifter protocol layer (pure, no I/O)
  lifter.py        MyLifterClient: one BLE session to one motor (rewritten)
  coordinator.py   MyLifterCoordinator (DataUpdateCoordinator) per motor entry
  group.py         MyLifterGroup: synchronised control of a multi-motor lifter
  config_flow.py   bluetooth discovery, manual, pairing, group, reauth, options
  cover.py / sensor.py / binary_sensor.py / entity.py
  diagnostics.py, translations/en.json, icons.json, manifest.json, quality_scale.yaml
```

### Layers
1. **protocol.py** (pure): enums, frame builders, `parse_notification()` that returns typed
   dataclasses (`MoveStatus`, `Ack`, `Nack`, `Passkey`, `Version`, `ProtocolVersion`, `Stats`,
   `Unknown`).
2. **lifter.py** (`MyLifterClient`, no HA imports): BLE session state machine. It connects with
   `bleak_retry_connector.establish_connection(BleakClientWithServiceCache, …,
   ble_device_callback=…)`, so the device always comes from HA's bluetooth stack and works
   through ESPHome proxies. It also handles authentication, the keep-alive task, move
   supervision, STOP handling, disconnect handling, the idle-disconnect timer and the
   one-shot pairing helper `async_pair()`.
3. **coordinator.py**: a `DataUpdateCoordinator[LifterState]` with a fixed 5-minute poll.
   It resolves the `BLEDevice` via `bluetooth.async_ble_device_from_address(hass, address,
   connectable=True)`, receives push updates from the client while a session is open, maps
   errors to `UpdateFailed` / `ConfigEntryAuthFailed`, persists learned smart points and keeps
   the device registry's `sw_version` current.
4. **Entities**: thin; they read `coordinator.data` and call coordinator methods.

Why not `ActiveBluetoothDataUpdateCoordinator`: it polls when an advertisement arrives and
is built for request/response devices. This device must stream frames for the whole time
it is connected, and status arrives as pushed notifications. A plain
`DataUpdateCoordinator` with push (`async_set_updated_data`) plus a slow poll models that
directly. See DECISIONS.md.

## 3. Entity model (per motor config entry)

| Entity | Platform | Notes |
|---|---|---|
| Hoist (device name) | `cover` | `OPEN` → `SMART_UP`, `CLOSE` → `SMART_DOWN`, `STOP`. Closed = lowered to the bottom smart point. `current_cover_position` 0 (bottom) … 100 (top) from learned/configured raw positions. No `SET_POSITION` (see Safety). For motors marked as part of a multi-motor lifter: **STOP only**. |
| Load | `sensor` | Raw load value from the MOVE response (unit unknown, see DECISIONS.md). `state_class: measurement`. |
| Raw position | `sensor` (diagnostic, disabled by default) | Motor ticks; useful for calibration. |
| Firmware | `sensor` (diagnostic) | Also set as device `sw_version`. |
| Last error | `sensor` (diagnostic, `enum`) | Integration-level last error plus raw device error code attribute. |
| Connection | `binary_sensor` (diagnostic, `connectivity`) | Whether a BLE session is currently open. |

Group config entry (multi-motor lifter): one `cover` that drives all member motors together,
with an aggregated position.

## 4. Pairing and passkeys
1. Discovery (manifest `bluetooth` matcher: service UUID, plus `MyLifter*` local name) or a
   manual flow (pick from discovered devices or type a MAC address seen by a connectable
   proxy).
2. Pair form: the user picks "single motor" or "one of several motors on the same lifter",
   presses the button on the motor, then clicks Submit.
3. A progress step connects, sends `GET_PASSKEY` and waits up to 30 s for the passkey
   notification. Pressing the button again during this window also works. It then sends
   `SET_PASSKEY`, waits briefly for the ACK and disconnects.
4. The passkey (hex) is stored in `entry.data["passkey"]`. It is redacted from
   diagnostics.
5. If authentication is later rejected (NACK on `SET_PASSKEY`), the integration raises
   `ConfigEntryAuthFailed` and a reauth flow repeats the button pairing.

## 5. Connection lifecycle (on demand, not permanent)
- **Idle**: disconnected. Every 5 minutes a poll opens a session, streams STOP frames until
  the first status and the firmware version are in, then disconnects after a short linger
  (2 s). That uses one proxy slot for a few seconds.
- **Command**: open/close/stop connects on demand (or joins an in-flight connect),
  authenticates and streams the intent.
- **After motion**: the session stays up for 30 s idle (cheap follow-up commands) and then
  disconnects to free the ESPHome proxy slot.
- **Unexpected disconnect**: the session is dropped. If a move was in progress, the move
  is marked failed (`connection_lost_while_moving`), the intent becomes STOP, and a
  background *safety STOP* task reconnects and sends STOP (STOP is safe to retry). The
  move itself is **never** resumed.
- `BleakOutOfConnectionSlotsError` from a proxy surfaces as a clear error.
- No scanning of our own, no `BleakClient(address)`, no blocking calls, no `time.sleep`.
  Pacing uses `asyncio.sleep` inside tasks the client owns and cancels on unload.

## 6. Safety model
- **STOP priority**: `async_stop()` first sets intent=STOP synchronously, so the very next
  keep-alive frame is STOP. It bumps a *stop generation* that cancels any move still
  connecting or confirming, then writes an immediate STOP frame. If the write fails it
  reconnects and retries, up to 3 attempts. STOP works even when no session is open.
- **No retries or queues for moves**: a move is written exactly once as a fresh command. The
  following keep-alive frames carry the same intent, because that is how the protocol
  expresses "keep moving" (it is not a resend). Any connection loss resets the intent to
  STOP. A new session always starts with STOP. Moves are refused (not queued) while another
  move is active.
- **Confirmation**: after the move write, the client waits 3 s for a status frame that shows
  the position has changed (≥ 5 ticks). If it sees `0x81`/`0x86` (already at the limit), the
  move finishes as a no-op. On `0x83` or any other error, or on timeout, the client sends
  STOP, records `last_error` and raises. It never re-sends the move.
- **Supervision while moving**: the move ends normally on a limit code or a 2 s stall. It is
  stopped with an error on an unexpected error code, on exceeding 180 s, or on disconnect.
- **No SET_POSITION**: intermediate positions need open-ended `UP`/`DOWN` frames that
  depend on HA staying connected. Smart moves are bounded by the motor's own smart
  points.
- **Unknown device error codes** block moves. They are not auto-cleared. Only the sync
  error (0x09) and the limit/smart-point warnings (0x81, 0x83, 0x86) are cleared before a
  move, as upstream does.
- **Multi-motor**: motors flagged as part of a multi-motor lifter only expose STOP. Motion
  only comes from a group entity. The group starts all members, requires every member to
  confirm and STOPs all members if any one fails, disconnects, errors or diverges.

## 7. Reuse vs rewrite
| PyLifter | Here |
|---|---|
| `protocol.py` UUIDs, enums, `build_packet`, `build_move_packet`, smart-point builders | **Vendored** (modified, headers kept). |
| `client.py` notification parsing (passkey, ACK, MOVE, version, protocol version, stats) | **Moved into `protocol.py`** as pure parsers (modified). |
| `client.py` connection, keep-alive, reconnect, pairing | **Rewritten** in `lifter.py` (HA bluetooth rules, safety rules). |
| Override moves (`GO_OVERRIDE`), factory calibrate, manual `UP`/`DOWN`, cm calibration, link commands, firmware update | **Not used** (unsafe or out of scope). The enum values stay for reference. |
| Demos, CLI, cable robot | Not used. |

## 8. Risks
- Firmware v3.1 has limited upstream testing. The auth ACK and frame timing may differ.
- Unknown device behaviour when the keep-alive stops mid-move (watchdog or not). This is
  why only smart moves are exposed (see DECISIONS.md "Connection drop mid-move").
- Load unit and position scale are unknown, so the sensors are raw.
- Smart-point learning depends on detecting a natural end of travel.
- The ESPHome proxy needs a free connection slot (default 3). A two-motor group needs two.
- The test harness is HA 2026.10.4 because no 2026.11 test package exists.

## 9. Test plan
A fake MyLifter device simulator (`tests/fake_device.py`) implements the BLE client
surface (`start_notify`, `write_gatt_char`, `disconnect`). It answers frames as the protocol
describes: passkey after a simulated button press, MOVE responses, position progression,
smart points, error codes, dropped links. Tests cover protocol encode/decode, the config
flow (discovery, manual, pairing success/failure/timeout, already configured, group,
reauth, options), setup/unload, coordinator poll and disconnect, cover commands (STOP
priority, STOP while connecting, no-retry on unconfirmed move, disconnect mid-move,
multi-motor STOP-only), the group, and diagnostics.
