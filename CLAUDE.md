# CLAUDE.md — ha-mylifter

Home Assistant custom integration (domain `mylifter`) for GarageSmart MyLifter BLE ceiling
hoists. One config entry per motor, plus optional "lifter group" entries for multi-motor
lifters. The protocol was reverse engineered by PyLifter (GPL-3.0, vendored in
`protocol.py`). The whole repo is GPL-3.0.

Read `PLAN.md` (architecture) and `DECISIONS.md` (why things are the way they are) before
changing behaviour.

## Commands
```bash
uv sync                      # Python 3.14 venv with HA test harness
uv run pytest                # all tests (simulated device, no Bluetooth)
uv run ruff check            # lint (HA core rule set)
uv run ruff format           # format
```

## Layout
- `custom_components/mylifter/protocol.py`: vendored PyLifter protocol (keep the licence
  header and the MODIFIED list up to date).
- `custom_components/mylifter/lifter.py`: BLE session and safety logic. No HA imports.
- `custom_components/mylifter/coordinator.py`, `group.py`, `config_flow.py`, platforms.
- `custom_components/mylifter/translations/en.json`: the only strings file. Custom
  integrations load this directly.
- `tests/fake_device.py`: protocol simulator. Extend it instead of mocking internals.

## Hard rules
1. **Never send commands to the real hoist unless the user explicitly asks for it in that
   conversation.** Never run PyLifter scripts or any code against real hardware. Never use
   this Mac's Bluetooth. All tests use mocks/fakes.
2. **Bluetooth (HA rules):**
   - No scanning of our own.
   - Get the device from `homeassistant.components.bluetooth.async_ble_device_from_address(hass,
     address, connectable=True)`.
   - Connect with `bleak_retry_connector.establish_connection(BleakClientWithServiceCache, …)`
     so it works through ESPHome proxies.
   - Fully async: no blocking I/O, no `time.sleep`.
   - Respect the proxy's limited connection slots. Sessions are on demand and closed when
     idle.
3. **Safety (non-negotiable):**
   - STOP has priority over everything and is safe to retry. It sets the intent to STOP
     synchronously before any await.
   - Never automatically retry, queue or resume a move. A move is written once. If it is
     not confirmed, send STOP and report an error. Every new session starts with STOP.
   - Keep-alive frames are built **inside the write lock**, so a frame built before STOP
     can never be sent after it.
   - Only smart moves (`SMART_UP`/`SMART_DOWN`). No `UP`/`DOWN`/override/factory commands
     and no set-position until watchdog behaviour on link loss has been verified on
     hardware (DECISIONS.md "Connection drop mid-move").
   - Motors flagged `multi_motor` must never move on their own. Only a group may move
     them, and the group stops all members if any one fails.
   - Unknown device error codes block moves. Do not auto-clear them.
4. Any change to the move/stop logic needs a test in `tests/test_cover.py` or
   `tests/test_group.py` that would fail without the change.

## Conventions
- HA 2026.11 style: `probatio` (not voluptuous), `typing.override`, `runtime_data`,
  `has_entity_name`, translation keys for entities and exceptions.
- Poll interval and timings are constants, never user options.
- The test harness is `pytest-homeassistant-custom-component` 0.13.375 (HA 2026.10.4),
  because no 2026.11 release exists yet. Bump it when one does.
- Do not use `async_fire_time_changed` in tests: it also fires the protocol's asyncio
  timers early. Call `entry.runtime_data.async_refresh()` instead.
- Commit in logical steps. Do not push, tag or release. The maintainer does that.
