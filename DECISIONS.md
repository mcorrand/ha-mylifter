# DECISIONS

Decisions made while building v0.1.0 autonomously. Each entry: decision, why, alternatives.

## Motor count (unknown at build time)
The task template left `<ONE | TWO>` unfilled. **Decision:** support both. A single motor is a
normal cover. Multi-motor lifters use the "multi-motor" model below. The pairing form asks
explicitly. It has no default, so the user has to make a conscious choice.

## Multi-motor
- A lifter with several motors must never move one motor on its own: the platform would
  tilt and the load could slide or fall.
- **Decision:** every motor is still its own config entry (one BLE device, one passkey). At
  pairing the user chooses *single motor* or *one of several motors on the same lifter*.
  That choice is stored in `entry.data["multi_motor"]` and can only be changed by removing
  and re-adding the motor.
- A multi-motor motor's cover exposes **STOP only**, so open/close cannot be called on
  it at all. That holds even before a group exists.
- The user then creates a **lifter group** entry ("Add integration → MyLifter → Lifter
  group") that selects two or more multi-motor motors. The group's cover:
  - starts all members concurrently with the same smart move;
  - requires every member to confirm. If any member fails to confirm, STOP is sent to all
    members and an error is raised;
  - during the move, STOPs all members if any member disconnects, reports an error or
    finishes early while others still move, or if the members' travel since the start
    differs by more than 300 ticks;
  - STOP on the group STOPs every member in parallel.
- **Limitation (documented in README):** the members are synchronised by HA, not by the
  motors. They are started within one event-loop pass, but over BLE through a proxy their
  start can differ by up to a few hundred ms. Each motor stops at its own smart points.
  The smart points must be set consistently in the MyLifter app. The firmware "link"
  commands (0x40–0x45) are undocumented and not used.
- Alternatives considered: (a) refusing multi-motor setups entirely, which is too
  limiting; (b) using firmware linking, which is undocumented and untestable without
  hardware.

## Test harness version
`pytest-homeassistant-custom-component` has no release for HA 2026.11.x. The latest,
0.13.375 (2026-10-25), pins `homeassistant==2026.10.4`. **Decision:** pin 0.13.375. The
bluetooth requirements (bleak 3.0.2, bleak-retry-connector 4.7.1, habluetooth 7.1.2, …)
are identical in 2026.10.4 and 2026.11.1, so the BLE stack under test matches production.
Bump when a 2026.11 release appears. `hacs.json` declares a minimum of `2026.11.0`, the
version targeted for runtime.

## Coordinator type
**Decision:** a plain `DataUpdateCoordinator` with push updates and a fixed 5-minute poll,
not `ActiveBluetoothDataUpdateCoordinator`. The device only reports status while we stream
keep-alive frames over an open connection, and status arrives as notifications.
`iot_class` is `local_polling`: at rest, state comes from periodic polls. During motion the
updates are pushed.

## Connection lifecycle
**Decision:** on-demand sessions.
- A poll connects, reads status and version, lingers 2 s and disconnects.
- A command connects, moves, stays connected for 30 s after the motion ends, then
  disconnects.
- The protocol does not require a permanent connection: the passkey is permanent and
  status can be read on any connection.
- Holding a connection permanently would keep one of the ESPHome proxy's ~3 connection
  slots busy forever and stream 4 frames per second indefinitely.
- Cost: open/close/stop latency includes a connection setup, typically 1–4 s through a
  proxy.
- Status between polls can be up to 5 minutes stale, for example when the user moved the
  hoist with the phone app.

## STOP while no session is open
STOP connects (or joins an in-flight connect) and sends STOP. The attempt is retried up to 3
times, because STOP is safe to retry. Even if HA believes nothing is moving, the motor may
have been started by the phone app.

## Keep-alive frames vs "never re-send a move"
- The protocol expresses "keep moving" by repeating `MOVE <code>` frames that echo the
  position. The first frame of a move is the command. The following frames are the
  keep-alive the protocol requires. We do not treat them as retries.
- The rule is implemented as:
  - a move command is only ever started by a user/automation call;
  - it is never queued;
  - if the first write fails or the move is not confirmed, the intent becomes STOP and an
    error is raised;
  - any connection loss resets the intent to STOP and nothing reconnects to resume it;
  - every new session starts with STOP.

## Connection drop mid-move
- Facts: the GarageSmart manual says an internal brake locks the spool when the cable stops
  or power is lost. PyLifter does not document whether the motor stops on its own when the
  keep-alive stream stops (a watchdog). Its `harness.py` starts a move with one frame and
  stops it with a separate STOP frame, which hints that a single frame is enough to keep
  moving for at least ~2 s.
- **Assumption:** there may be no watchdog. After a drop, the motor may continue its current
  smart move until it reaches the smart point stored in the motor (or a limit). Then it
  stops and the brake holds.
- Mitigations:
  1. Only smart moves are exposed, so travel is always bounded by the motor's own smart
     points. Open-ended `UP`/`DOWN` (and therefore `SET_POSITION`) is not offered.
  2. On an unexpected disconnect during a move, HA immediately starts a background safety
     STOP: reconnect and send STOP, up to 3 attempts.
  3. The move is reported as failed (`connection_lost_while_moving`) and never resumed.
- **To verify on hardware** (see the README test checklist): what the motor does when the
  proxy is powered off mid-move.

## No SET_POSITION in v0.1
See above. Moving to an intermediate position requires open-ended `UP`/`DOWN` that only
stops when HA sends STOP. Revisit once watchdog behaviour is verified.

## Error clearing
PyLifter always sends `CLEAR_ERROR` (0x06) before every move. **Decision:** clear only the
known benign codes before a move:
- `0x09` sync error, which is caused by our own echo before the first status arrives;
- `0x81` soft limit;
- `0x83` smart point not set (re-checked by the move itself);
- `0x86` end of travel.
Any other non-zero device error code blocks moves, with "device error, check the MyLifter
app". Auto-clearing unknown faults could bypass a protection.

## Position percentage
- The protocol cannot read the smart points. **Decision:** learn them.
- When a `SMART_UP` move ends naturally (soft-limit code or stall, not a user STOP), the
  current raw position is saved as `top_position` in the entry options. `SMART_DOWN` saves
  `bottom_position` the same way.
- The user can view, edit or clear these in the options flow.
- Direction: **open = raise (`SMART_UP`), closed = lowered to the bottom.** This matches
  the cover card, where you drag up to open, and garage doors. (v0.1.0 had it the other
  way round, which felt backwards in use.)
- `current_cover_position` = 100 at top, 0 at bottom, clamped. It is `None` until both
  points are known.
- `is_closed` only needs `bottom_position`: within 50 ticks of it.
- Upstream's cm calibration (slope/intercept) is not used.

## Load sensor units
The MOVE response carries a 16-bit "weight". Its unit and scale are undocumented.
**Decision:** expose the raw value with no unit and no device class, and name it "Load".
Upstream's `parse_move_response` unpacks it as signed (`h`) while the client uses unsigned
(`H`). We use unsigned, as the code path upstream actually uses.

## Authentication confirmation
- Upstream sends `SET_PASSKEY` and does not wait for anything. PROTOCOL.md calls the ACK
  "optional/implied".
- **Decision:** after `SET_PASSKEY`, wait up to 5 s for either an ACK for 0x03 or the
  first MOVE status. Either one counts as authenticated.
- A NACK for 0x03 raises an auth error, which becomes `ConfigEntryAuthFailed` and starts
  reauth.
- A timeout is treated as a connection error, not as an auth failure, so flaky links do
  not trigger reauth.

## Pairing UX
The task asks for "press the button on the lifter, then Submit". The device only sends the
passkey after `GET_PASSKEY` has been received **and** the button has been pressed. The order
is not documented. **Decision:** the form says "press the button, then Submit". The
following progress step waits up to 30 s and tells the user to press the button again if
nothing happens, which covers both orders.

## Speed
Smart moves are sent with speed 100, as upstream does. The protocol allows 0–100 and the
upstream CLI uses 25–100. Speed is not configurable in v0.1.

## Timings
Taken from upstream:
- keep-alive interval 250 ms idle, 200 ms moving;
- 20 ms pause after every write;
- stall detection after 2 s.

Added here:
- move confirmation 3 s;
- maximum move duration 180 s;
- auth timeout 5 s;
- pairing timeout 30 s;
- idle disconnect 30 s.

None of these are user-configurable. HA guidance says polling intervals must not be
configurable.

## Vendoring instead of a PyPI library
HA guidance prefers protocol code in a separate PyPI library. The task explicitly asks to
vendor PyLifter's protocol code, so it lives in `protocol.py` with its GPL notice. The BLE
session logic lives in `lifter.py`, which has no HA imports, so it can be split into a
library later.

## Manifest
- `dependencies: ["bluetooth_adapters"]`, matching current core BLE integrations such as
  `led_ble`.
- `integration_type: "device"`.
- `iot_class: "local_polling"`.
- Bluetooth matchers: the service UUID (PyLifter's `harness.py` scans for it, so it is
  advertised) and `local_name: "MyLifter*"` (the upstream CLI filters on "mylifter" in the
  name). If the motor uses a different name, the service UUID still matches.
- `requirements: []`. bleak and bleak-retry-connector come with HA's bluetooth integration.

## Brand images
- The Marketplace reads icons from `custom_components/mylifter/brand/`.
- **Decision:** no brand images in v0.1. We must not use GarageSmart/MyLifter trademarks,
  and the integration is unaffiliated. Add a neutral original icon later.

## Upstream clone
The sandbox blocked creating a nested `.git` inside the repo. The working tree was copied to
`.scratch/pylifter` (git-ignored) and the commit hash recorded in
`.scratch/PYLIFTER_COMMIT` and here: `029abf219ff47dc0a836f3adef492b44b4846e3e`.

## GitHub owner
The template placeholder `<GITHUB_USERNAME>` was filled from the git remote: `mcorrand`.

## Frames are built inside the write lock
Found during implementation. If a keep-alive MOVE frame is built before waiting for the
GATT write lock, a frame built just before STOP could be written *after* the STOP frame. It
would restart the motor for one keep-alive period, and it would also carry a stale position
echo (sync error). **Decision:** `_write()` accepts a frame factory that is evaluated while
the lock is held. A move's first frame is skipped if STOP ended the move in the meantime.
There is a regression test (`test_no_stale_move_frame_after_stop`, with slow writes).

## Idle disconnect vs. operations in flight
An idle-disconnect timer that fires while a move is still preparing must not close the
session. **Decision:** an in-use counter covers polls, moves and STOP. The idle disconnect
re-checks it synchronously right before closing.

## Translations
Custom integrations load `translations/en.json` directly. **Decision:** keep only that file
(no `strings.json` copy that could drift).

## Validation workflows
- `home-assistant/actions/hassfest@master` and `hacs/action@main` (category
  `integration`), as the Marketplace contributor docs recommend.
- The HACS `brands` check is ignored until an original icon exists.
- hassfest was not run locally. Running downloaded core code was not permitted in the
  build sandbox. The manifest was checked by hand against the 2026.11.1 hassfest schema.
- The HACS action also checks GitHub repository metadata (description, topics, issues
  enabled). Set these on GitHub before the first release.
