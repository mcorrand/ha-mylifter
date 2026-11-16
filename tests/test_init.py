"""Setup, unload and polling behaviour."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from bleak.exc import BleakError
from bleak_retry_connector import BleakOutOfConnectionSlotsError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mylifter.const import (
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    DOMAIN,
    ENTRY_TYPE_GROUP,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .conftest import ADDRESS, FAST_TIMINGS, motor_entry
from .fake_device import FakeBluetooth, FakeMyLifter


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run the scheduled poll now.

    ``async_fire_time_changed`` is not used: it also fires the protocol's own
    timers (asyncio sleeps/timeouts) early.
    """
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


LOAD = "sensor.mylifter_d348_load"
CONNECTION = "binary_sensor.mylifter_d348_connection"
FIRMWARE = "sensor.mylifter_d348_firmware"
LAST_ERROR = "sensor.mylifter_d348_last_error"


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """First refresh connects, authenticates, reads status and version."""
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(LOAD).state == "42"
    assert hass.states.get(FIRMWARE).state == "3.1"
    assert hass.states.get(LAST_ERROR).state == "none"
    device_entry = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), entry.entry_id
    )
    assert device_entry.sw_version == "3.1"
    # The first frame after SET_PASSKEY is a STOP keep-alive.
    assert device.frames[0] == b"\x03\x06" + device.passkey
    assert all(m.code == 0 for m in device.moves)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert device.client is None


async def test_poll_session_lingers_then_disconnects(
    hass: HomeAssistant, init_integration: MockConfigEntry, device: FakeMyLifter
) -> None:
    """A poll does not hold the connection (frees the proxy slot)."""
    await asyncio.sleep(0.2)
    await hass.async_block_till_done()
    assert device.client is None
    assert hass.states.get(CONNECTION).state == STATE_OFF
    # The last known values stay available while disconnected.
    assert hass.states.get(LOAD).state == "42"


async def test_poll_failure_and_recovery(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device: FakeMyLifter,
) -> None:
    """Unreachable motor -> unavailable; next successful poll -> available."""
    await asyncio.sleep(0.2)
    device.connect_error = BleakError("out of range")
    await _poll(hass, init_integration)
    await hass.async_block_till_done()
    assert hass.states.get(LOAD).state == STATE_UNAVAILABLE
    assert hass.states.get(LAST_ERROR).state == "connection_failed"
    assert hass.states.get(CONNECTION).state == STATE_OFF

    device.connect_error = None
    device.weight = 77
    await _poll(hass, init_integration)
    await hass.async_block_till_done()
    assert hass.states.get(LOAD).state == "77"


async def test_no_connection_slot_reported(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device: FakeMyLifter,
) -> None:
    """Proxy out of connection slots is reported specifically."""
    await asyncio.sleep(0.2)
    device.connect_error = BleakOutOfConnectionSlotsError("no slots")
    await _poll(hass, init_integration)
    await hass.async_block_till_done()
    assert hass.states.get(LAST_ERROR).state == "no_connection_slot"


async def test_unexpected_disconnect_while_idle(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device: FakeMyLifter
) -> None:
    """A link drop while idle just closes the session; nothing is resent."""
    mock_config_entry.add_to_hass(hass)
    with patch(
        "custom_components.mylifter.coordinator.TIMINGS",
        replace(FAST_TIMINGS, poll_linger=10),
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(CONNECTION).state == STATE_ON
    sessions = device.sessions
    device.drop_connection()
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTION).state == STATE_OFF
    assert hass.states.get(LAST_ERROR).state == "none"
    await asyncio.sleep(0.1)
    assert device.sessions == sessions  # no reconnect when nothing was moving
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_setup_retry_when_not_found(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth
) -> None:
    """No connectable proxy has seen the motor -> retry later."""
    entry = motor_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retry_when_connect_fails(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device: FakeMyLifter
) -> None:
    """Connection failure on first refresh -> retry later."""
    device.connect_error = BleakError("boom")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failed_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device: FakeMyLifter
) -> None:
    """NACK on SET_PASSKEY -> setup error and a reauth flow."""
    device.reject_passkey = True
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_auth_without_ack(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device: FakeMyLifter
) -> None:
    """Firmware that does not ACK SET_PASSKEY is accepted on the first status."""
    device.ack_passkey = False
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_no_response_after_auth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device: FakeMyLifter
) -> None:
    """Silence after SET_PASSKEY is a connection error, not an auth failure."""
    device.ack_passkey = False
    device.respond = False
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_group_waits_for_members(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth
) -> None:
    """A group whose members are not loaded is retried."""
    group = MockConfigEntry(
        domain=DOMAIN,
        title="Lifter",
        unique_id="group_x",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
            CONF_MEMBERS: ["missing1", "missing2"],
        },
    )
    group.add_to_hass(hass)
    await hass.config_entries.async_setup(group.entry_id)
    assert group.state is ConfigEntryState.SETUP_RETRY
