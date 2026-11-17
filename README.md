# MyLifter for Home Assistant

A Home Assistant custom integration for **GarageSmart Universal lifters driven by MyLifter
motors**, the Bluetooth (BLE) ceiling hoists normally controlled with the MyLifter phone
app.

> [!WARNING]
> **Early development (v0.1.0). Not affiliated with, endorsed by or supported by GarageSmart
> or MyLifter.** The BLE protocol is reverse engineered (see Credits). It has been tested
> only against a simulator, not against real hardware.
>
> A ceiling hoist carries heavy loads over people and cars. Incorrect operation can cause
> damage or serious injury. Use this integration **only while you can see the lifter and
> reach its physical controls.** Do not automate unattended moves. Keep the area below the
> load clear. Use at your own risk.

## What it does

Each motor becomes a device with these entities:

| Entity | Description |
|---|---|
| **Hoist** (cover) | *Open* raises the lifter to the top smart point. *Close* lowers it to the bottom smart point ("closed" = lowered, like a garage door). *Stop* stops it. Position is 100 % at the top and 0 % at the bottom (see Calibration). There is no "set position" in v0.1. |
| **Load** | Raw load value reported by the motor (unit unknown). |
| **Firmware** (diagnostic) | Motor firmware version. |
| **Last error** (diagnostic) | Last integration error, plus the raw device error code. |
| **Connection** (diagnostic) | Whether a Bluetooth session is currently open. |
| **Raw position** (diagnostic, disabled by default) | Motor position in raw ticks. |

### Multi-motor lifters
If your lifter has **more than one motor**, pair each motor as *"One of several motors on
the same lifter"*.
- Such motors **cannot move on their own**: their covers only offer STOP.
- Add a **Lifter group** (Settings → Devices & services → Add integration → MyLifter →
  *Create a lifter group*) and select all motors of that lifter.
- The group's cover starts all motors together, requires every motor to confirm that it is
  moving, and **stops all motors** if any one fails, disconnects, stops, or drifts more
  than 300 ticks from the others.
- Limitation: the motors are synchronised by Home Assistant over Bluetooth, not by the
  motors themselves. Their start can differ by a few hundred milliseconds. Each motor
  stops at its *own* smart points, so set the smart points consistently in the MyLifter
  app.

## Requirements
- Home Assistant **2026.11** or newer.
- A Bluetooth adapter or, typically, an **ESPHome Bluetooth proxy with active connections
  enabled** within range of the motor(s):
  ```yaml
  bluetooth_proxy:
    active: true
  ```
- Each motor uses one proxy connection slot while a session is open. Proxies have about
  3 slots by default. Sessions are opened on demand and closed again (see below).
- The top and bottom **smart points set in the MyLifter app**. Open/close move between
  them.

## Installation (Marketplace)
1. In Home Assistant open **Settings → Marketplace**.
2. Choose **⋮ → Add from link** and enter `mcorrand/ha-mylifter` (or the full GitHub URL),
   category *Integration*.
   Or use this link:
   [Open in Home Assistant](https://my.home-assistant.io/redirect/marketplace_repository/?owner=mcorrand&repository=ha-mylifter&category=integration)
3. Install **MyLifter** and restart Home Assistant.

## Pairing
1. Power the motor and make sure the proxy is in range. A discovered motor shows up under
   **Settings → Devices & services**. Otherwise choose **Add integration → MyLifter** and
   pick the motor or type its Bluetooth address.
2. Choose whether this is a single-motor lifter or one of several motors.
3. **Press the button on the lifter motor, then click Submit.** Home Assistant waits up
   to 30 seconds for the motor to send its passkey. If nothing happens, press the button
   again while it waits.
4. The passkey is stored in the config entry. It is permanent, so you only pair once. If
   the motor ever rejects it, Home Assistant asks you to pair again.

## How it behaves
- **On-demand connection:** every 5 minutes Home Assistant connects briefly, reads
  position, load and errors, and disconnects. A command connects, runs, and disconnects
  30 s after the motion ends. Commands therefore take a few seconds to start. Status
  between polls can be up to 5 minutes old, for example after using the phone app.
- **STOP always wins:** it is sent immediately, retried up to 3 times (reconnecting if
  needed) and works even when Home Assistant thinks nothing is moving.
- **Moves are never retried or queued:** a move is sent once. If the motor does not report
  movement within 3 s, Home Assistant sends STOP and shows an error. A second move while
  one is running is refused.
- **Connection lost during a move:**
  - The move is marked failed (*Connection lost while moving*) and is **never resumed**.
  - Home Assistant immediately reconnects to send STOP.
  - Whether the motor stops by itself when the stream stops is not yet verified. Only
    smart moves are used, so the travel is always limited by the smart points stored in
    the motor.
  - Per the GarageSmart manual, an internal brake locks the spool when the cable stops or
    power is lost.
- **Errors:** sync, soft-limit, end-of-travel and smart-point-not-set errors are cleared
  automatically before a move. Any other device error blocks moves until it is resolved
  (for example in the MyLifter app).
- A move that takes longer than 180 s is stopped.

### Calibration
Position percent needs the raw positions of the top and bottom smart points. They are
learned automatically when an open (top) and a close (bottom) complete on their own. You
can view, edit or reset them under the integration's **Configure** options. Enable the
*Raw position* sensor to see live values.

## First hardware test (suggested)
Do this supervised, with the lifter empty and the physical controls within reach:
1. Pair one motor through the proxy and check that Load, Firmware and Raw position show
   values.
2. Press **Stop** while nothing moves. Nothing should happen, and *Last error* stays
   "None".
3. Short supervised move: press **Open** (raise) and press **Stop** after about 1 s. The
   motor should stop within a fraction of a second.
4. Let a full **Close** and **Open** complete, then check that the position shows 0 % and
   100 %.
5. During a move, disconnect the proxy (unplug it) and note whether the motor stops on
   its own. Please report the result in an issue: it decides whether "set position" can
   be added safely.

## Removal
Settings → Devices & services → MyLifter → ⋮ → Delete (remove a lifter group before its
motors). Then uninstall it from the Marketplace. The passkey is deleted with the entry.
The motor keeps working with the phone app.

## Development
See [CLAUDE.md](CLAUDE.md), [PLAN.md](PLAN.md) and [DECISIONS.md](DECISIONS.md).
```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
```
The tests use a simulated motor. No real Bluetooth is used.

## Credits and licence
- The BLE protocol and parts of the protocol code come from
  **[PyLifter](https://github.com/sblu/PyLifter)** by sblu and contributors (GPL-3.0),
  reverse engineered from the MyLifter Android app. `custom_components/mylifter/protocol.py`
  is a modified copy. The connection handling was rewritten for Home Assistant.
- This project is licensed under the **GNU General Public License v3.0**. See
  [LICENSE](LICENSE).
- GarageSmart and MyLifter are trademarks of their respective owners. They are used here
  only to describe compatibility.
