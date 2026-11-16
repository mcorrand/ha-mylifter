"""Cover commands and the safety rules (STOP priority, no move retries)."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mylifter.const import CONF_BOTTOM_POSITION, CONF_TOP_POSITION
from custom_components.mylifter.protocol import CommandCode, MoveCode
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    DOMAIN as COVER_DOMAIN,
    CoverEntityFeature,
    CoverState,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_STOP_COVER,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import FAST_TIMINGS, motor_entry
from .fake_device import FakeMyLifter

COVER = "cover.mylifter_d348"
LAST_ERROR = "sensor.mylifter_d348_last_error"


async def _wait(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def _call(hass: HomeAssistant, service: str, entity_id: str = COVER) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


def _moving(hass: HomeAssistant) -> bool:
    return hass.states.get(COVER).state in (CoverState.OPENING, CoverState.CLOSING)


async def test_open_close_learn_positions(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """Open/close run smart moves, finish at the smart points and learn them."""
    entry = init_integration
    assert hass.states.get(COVER).state == "unknown"  # nothing learned yet

    await _call(hass, SERVICE_OPEN_COVER)
    # A sync error caused by the initial echo is cleared before moving.
    assert bytes([CommandCode.CLEAR_ERROR, 0]) in device.frames
    first = device.smart_moves()[0]
    assert first.code is MoveCode.SMART_DOWN
    assert first.echo == 1000  # echoes the device position
    await _wait(lambda: not _moving(hass))
    assert device.position == 0
    assert entry.options[CONF_BOTTOM_POSITION] == 0

    await _call(hass, SERVICE_CLOSE_COVER)
    assert hass.states.get(COVER).state == CoverState.CLOSING
    await _wait(lambda: not _moving(hass))
    assert device.position == 2000
    assert entry.options[CONF_TOP_POSITION] == 2000
    state = hass.states.get(COVER)
    assert state.state == CoverState.CLOSED
    assert state.attributes[ATTR_CURRENT_POSITION] == 0
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    await _call(hass, SERVICE_OPEN_COVER)
    assert hass.states.get(COVER).state == CoverState.OPENING
    await _wait(lambda: not _moving(hass))
    state = hass.states.get(COVER)
    assert state.state == CoverState.OPEN
    assert state.attributes[ATTR_CURRENT_POSITION] == 100
    # After a completed move the stream is back to STOP.
    await asyncio.sleep(0.05)
    assert device.moves[-1].code is MoveCode.STOP


async def test_stop_has_priority(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """STOP mid-move: next frames are STOP, the motor stops, nothing is learned."""
    device.step = 5
    await _call(hass, SERVICE_OPEN_COVER)
    assert _moving(hass)
    await _call(hass, SERVICE_STOP_COVER)
    frames_at_stop = len(device.moves)
    await asyncio.sleep(0.1)
    after = device.moves[frames_at_stop:]
    assert after
    assert all(m.code is MoveCode.STOP for m in after)
    stopped_at = device.position
    await asyncio.sleep(0.1)
    assert device.position == stopped_at
    assert not _moving(hass)
    assert CONF_BOTTOM_POSITION not in init_integration.options
    assert hass.states.get(LAST_ERROR).state == "none"


async def test_no_stale_move_frame_after_stop(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """With slow writes, no move frame queued before STOP may be sent after it."""
    device.step = 2
    device.write_delay = 0.02  # keep-alive frames queue up behind each other
    await _call(hass, SERVICE_OPEN_COVER)
    for _ in range(5):
        await _call(hass, SERVICE_STOP_COVER)
        first_stop = len(device.moves)
        await asyncio.sleep(0.15)
        assert all(m.code is MoveCode.STOP for m in device.moves[first_stop:])
        stopped_at = device.position
        await asyncio.sleep(0.05)
        assert device.position == stopped_at
        await _call(hass, SERVICE_OPEN_COVER)
    await _call(hass, SERVICE_STOP_COVER)


async def test_stop_cancels_move_that_is_still_connecting(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """STOP while a move is still connecting: the move never gets sent."""
    await _wait(lambda: device.client is None)  # poll session has closed
    device.connect_delay = 0.2
    open_task = hass.async_create_task(_call(hass, SERVICE_OPEN_COVER))
    await asyncio.sleep(0.05)
    await _call(hass, SERVICE_STOP_COVER)
    await open_task
    await asyncio.sleep(0.1)
    assert device.smart_moves() == []
    assert device.moves[-1].code is MoveCode.STOP


async def test_unconfirmed_move_is_not_retried(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """A move that cannot be confirmed is stopped and reported, never re-sent."""
    device.stuck = True
    with pytest.raises(HomeAssistantError, match="move_not_confirmed"):
        await _call(hass, SERVICE_OPEN_COVER)
    assert hass.states.get(LAST_ERROR).state == "move_not_confirmed"
    smart = device.smart_moves()
    assert smart
    assert {m.session for m in smart} == {smart[0].session}
    frames_at_failure = len(device.moves)
    await asyncio.sleep(0.5)  # well past idle disconnect and any "retry" window
    assert device.smart_moves() == smart
    assert all(m.code is MoveCode.STOP for m in device.moves[frames_at_failure:])


async def test_failed_move_write_is_not_resent(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """If the move frame cannot be written, report an error and do not resend."""
    device.fail_write_codes = {MoveCode.SMART_DOWN}
    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_OPEN_COVER)
    await asyncio.sleep(0.3)
    assert device.smart_moves() == []
    assert hass.states.get(LAST_ERROR).state in (
        "move_write_failed",
        "connection_lost_while_moving",
    )
    assert not _moving(hass)


async def test_connection_lost_mid_move(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """Link loss while moving: error, safety STOP on a new session, no resume."""
    device.step = 5
    await _call(hass, SERVICE_OPEN_COVER)
    move_session = device.smart_moves()[0].session
    device.drop_connection()
    await hass.async_block_till_done()
    assert hass.states.get(LAST_ERROR).state == "connection_lost_while_moving"
    assert not _moving(hass)
    # Safety STOP reconnects and only ever sends STOP.
    await _wait(lambda: device.sessions > move_session)
    await _wait(lambda: len(device.moves_in_session(move_session + 1)) > 0)
    await asyncio.sleep(0.05)
    assert all(
        m.code is MoveCode.STOP for m in device.moves if m.session > move_session
    )


async def test_smart_point_not_set(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """0x83 is reported and the move is stopped."""
    device.smart_points_set = False
    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_OPEN_COVER)
    assert hass.states.get(LAST_ERROR).state == "smart_point_not_set"


async def test_unknown_device_error_blocks_moves(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """Unknown device errors are not cleared automatically and block moves."""
    device.sticky_error = 0x42
    await init_integration.runtime_data.async_refresh()
    await asyncio.sleep(0.05)
    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_OPEN_COVER)
    assert device.smart_moves() == []
    state = hass.states.get(LAST_ERROR)
    assert state.state == "device_error"
    assert state.attributes["device_error_code"] == 0x42


async def test_already_at_limit(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """Opening when already at the bottom is a no-op, not an error."""
    device.position = 0
    await init_integration.runtime_data.async_refresh()
    await _call(hass, SERVICE_OPEN_COVER)
    assert hass.states.get(LAST_ERROR).state == "none"
    assert init_integration.options[CONF_BOTTOM_POSITION] == 0


async def test_moves_are_not_queued(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """A second move while moving is refused."""
    device.step = 5
    await _call(hass, SERVICE_OPEN_COVER)
    with pytest.raises(HomeAssistantError, match="already in progress"):
        await _call(hass, SERVICE_CLOSE_COVER)
    await _call(hass, SERVICE_STOP_COVER)


async def test_move_timeout(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device: FakeMyLifter
) -> None:
    """A move exceeding the maximum duration is stopped with an error."""
    device.step = 5
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.mylifter.coordinator.TIMINGS",
        replace(FAST_TIMINGS, max_move_duration=0.3),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    await _call(hass, SERVICE_OPEN_COVER)
    await _wait(lambda: not _moving(hass))
    assert hass.states.get(LAST_ERROR).state == "move_timeout"
    stopped_at = device.position
    await asyncio.sleep(0.1)
    assert device.position == stopped_at
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_stop_without_session_connects(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """STOP works even when no session is open (e.g. moved by the phone app)."""
    await _wait(lambda: device.client is None)
    sessions = device.sessions
    await _call(hass, SERVICE_STOP_COVER)
    assert device.sessions == sessions + 1
    assert device.moves[-1].code is MoveCode.STOP


async def test_stop_failure_reported(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """If STOP cannot be delivered after retries, an error is raised."""
    await _wait(lambda: device.client is None)
    device.connect_error = TimeoutError()
    with pytest.raises(HomeAssistantError, match="STOP could not be delivered"):
        await _call(hass, SERVICE_STOP_COVER)
    assert hass.states.get(LAST_ERROR).state == "stop_failed"


async def test_multi_motor_member_is_stop_only(
    hass: HomeAssistant, fake_bluetooth, device: FakeMyLifter
) -> None:
    """A motor of a multi-motor lifter cannot move on its own."""
    entry = motor_entry(multi_motor=True)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(COVER)
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == CoverEntityFeature.STOP
    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_OPEN_COVER)
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.async_move(MoveCode.SMART_DOWN)
    await _call(hass, SERVICE_STOP_COVER)
    assert device.smart_moves() == []
    await hass.config_entries.async_unload(entry.entry_id)
