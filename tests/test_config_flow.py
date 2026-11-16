"""Config flow: discovery, manual, pairing, groups, reauth and options."""

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from bleak.exc import BleakError
from bleak_retry_connector import BleakOutOfConnectionSlotsError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mylifter.const import (
    CONF_BOTTOM_POSITION,
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    CONF_MOTOR_ARRANGEMENT,
    CONF_MULTI_MOTOR,
    CONF_PASSKEY,
    CONF_RESET_CALIBRATION,
    CONF_TOP_POSITION,
    DOMAIN,
    ENTRY_TYPE_GROUP,
    ENTRY_TYPE_MOTOR,
)
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import ADDRESS, ADDRESS_2, motor_entry, service_info
from .fake_device import PASSKEY, FakeBluetooth, FakeMyLifter


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Do not set up the entry after the flow."""
    with patch(
        "custom_components.mylifter.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def _pair(
    hass: HomeAssistant, flow_id: str, arrangement: str = "single"
) -> dict[str, Any]:
    """Submit the pair form and let the progress step finish."""
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MOTOR_ARRANGEMENT: arrangement}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "wait_for_button"
    await hass.async_block_till_done()
    await asyncio.sleep(0)
    return await hass.config_entries.flow.async_configure(flow_id)


async def _wait_progress_done(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    for _ in range(100):
        await asyncio.sleep(0.01)
        await hass.async_block_till_done()
        progress = hass.config_entries.flow.async_get(flow_id)
        if progress["step_id"] != "pair_wait":
            break
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_bluetooth_discovery_pairs(
    hass: HomeAssistant, device: FakeMyLifter, mock_setup_entry: AsyncMock
) -> None:
    """Discovery -> confirm -> press button -> entry with passkey."""
    device.button_pressed = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"

    result = await _pair(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "MyLifter (D348)"
    assert result["data"] == {
        CONF_ENTRY_TYPE: ENTRY_TYPE_MOTOR,
        CONF_ADDRESS: ADDRESS,
        CONF_PASSKEY: PASSKEY.hex(),
        CONF_MULTI_MOTOR: False,
    }
    assert result["result"].unique_id == ADDRESS
    # Pairing never sends move frames.
    assert device.moves == []
    assert device.client is None


async def test_button_pressed_during_wait(
    hass: HomeAssistant, device: FakeMyLifter, mock_setup_entry: AsyncMock
) -> None:
    """The passkey also arrives if the button is pressed while HA is waiting."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MOTOR_ARRANGEMENT: "multi"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await asyncio.sleep(0.05)
    device.press_button()
    result = await _wait_progress_done(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MULTI_MOTOR] is True


async def test_discovery_already_configured(
    hass: HomeAssistant, device: FakeMyLifter
) -> None:
    """A configured motor is not offered again."""
    motor_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_flow(
    hass: HomeAssistant, device: FakeMyLifter, mock_setup_entry: AsyncMock
) -> None:
    """User flow: pick the discovered motor, pair."""
    device.button_pressed = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "motor"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS.lower()}
    )
    assert result["step_id"] == "pair"
    result = await _pair(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == ADDRESS


async def test_manual_flow_errors(
    hass: HomeAssistant, fake_bluetooth: FakeBluetooth
) -> None:
    """Invalid address, unknown address, already configured."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: "nope"}
    )
    assert result["errors"] == {CONF_ADDRESS: "invalid_address"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    )
    assert result["errors"] == {"base": "not_found"}

    motor_entry(ADDRESS_2).add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS_2}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("setup_device", "error"),
    [
        (lambda d: None, "pairing_timeout"),
        (lambda d: setattr(d, "connect_error", BleakError("x")), "cannot_connect"),
        (
            lambda d: setattr(d, "connect_error", BleakOutOfConnectionSlotsError("x")),
            "no_connection_slot",
        ),
        (lambda d: setattr(d, "connect_error", RuntimeError("x")), "unknown"),
    ],
)
async def test_pairing_errors_and_recovery(
    hass: HomeAssistant,
    device: FakeMyLifter,
    mock_setup_entry: AsyncMock,
    setup_device: Any,
    error: str,
) -> None:
    """Pairing failures return to the pair form; a retry can then succeed."""
    setup_device(device)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    flow_id = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow_id, {})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_MOTOR_ARRANGEMENT: "single"}
    )
    result = await _wait_progress_done(hass, flow_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert result["errors"] == {"base": error}

    device.connect_error = None
    device.button_pressed = True
    result = await _pair(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_pairing_rejected(
    hass: HomeAssistant, device: FakeMyLifter, mock_setup_entry: AsyncMock
) -> None:
    """NACK on SET_PASSKEY during pairing -> invalid_auth."""
    device.button_pressed = True
    device.reject_passkey = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _pair(hass, result["flow_id"])
    assert result["errors"] == {"base": "invalid_auth"}


async def test_group_flow(hass: HomeAssistant, fake_bluetooth: FakeBluetooth) -> None:
    """Two multi-motor motors can be grouped; groups need two motors."""
    fake_bluetooth.add(ADDRESS)
    fake_bluetooth.add(ADDRESS_2)
    m1 = motor_entry(ADDRESS, multi_motor=True, title="Motor A")
    m2 = motor_entry(ADDRESS_2, multi_motor=True, title="Motor B")
    for entry in (m1, m2):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "group"}
    )
    assert result["step_id"] == "group"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MEMBERS: [m1.entry_id]}
    )
    assert result["errors"] == {CONF_MEMBERS: "group_needs_two"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MEMBERS: [m2.entry_id, m1.entry_id]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Lifter (Motor A + Motor B)"
    assert result["data"] == {
        CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
        CONF_MEMBERS: sorted([m1.entry_id, m2.entry_id]),
    }
    await hass.async_block_till_done()
    group = result["result"]

    # Grouped motors are no longer offered; with none left the menu is skipped.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "motor"
    for entry in (group, m1, m2):
        await hass.config_entries.async_unload(entry.entry_id)


async def test_reauth(
    hass: HomeAssistant, device: FakeMyLifter, mock_setup_entry: AsyncMock
) -> None:
    """Reauth repeats the button pairing and stores the new passkey."""
    entry = motor_entry()
    entry.add_to_hass(hass)
    device.passkey = bytes.fromhex("010203040506")
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    result = await _wait_progress_done(hass, flow_id)
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "pairing_timeout"}

    device.button_pressed = True
    await hass.config_entries.flow.async_configure(flow_id, {})
    result = await _wait_progress_done(hass, flow_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSKEY] == "010203040506"


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Calibration values can be set and reset."""
    entry = init_integration
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TOP_POSITION: 2000, CONF_BOTTOM_POSITION: 0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_TOP_POSITION: 2000, CONF_BOTTOM_POSITION: 0}
    await hass.async_block_till_done()
    assert hass.states.get("cover.mylifter_d348").attributes["current_position"] == 50

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RESET_CALIBRATION: True}
    )
    assert entry.options == {}
