"""Fixtures for MyLifter tests. No real Bluetooth is ever used."""

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BluetoothServiceInfoBleak
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mylifter.const import (
    CONF_ENTRY_TYPE,
    CONF_MULTI_MOTOR,
    CONF_PASSKEY,
    DOMAIN,
    ENTRY_TYPE_MOTOR,
)
from custom_components.mylifter.lifter import Timings
from custom_components.mylifter.protocol import MYLIFTER_SERVICE_UUID
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .fake_device import PASSKEY, FakeBluetooth

ADDRESS = "CC:CC:CC:FD:D3:48"
ADDRESS_2 = "CC:CC:CC:FD:D3:A7"

FAST_TIMINGS = Timings(
    keepalive_idle=0.01,
    keepalive_moving=0.01,
    write_pause=0.0,
    write_timeout=0.5,
    auth_timeout=0.3,
    status_timeout=0.3,
    query_timeout=0.2,
    clear_error_timeout=0.2,
    move_confirm_timeout=0.2,
    move_confirm_ticks=5,
    stall_time=0.1,
    max_move_duration=3.0,
    idle_disconnect=0.3,
    poll_linger=0.05,
    stop_attempts=3,
    pairing_timeout=0.3,
    ack_timeout=0.1,
)


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations: None, enable_bluetooth: None) -> None:
    """Enable custom integrations and the (mocked) bluetooth stack."""


def service_info(
    address: str = ADDRESS, name: str = "MyLifter"
) -> BluetoothServiceInfoBleak:
    """Build a discovery service info."""
    device = BLEDevice(address, name, {})
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[MYLIFTER_SERVICE_UUID],
        source="esphome-proxy",
        device=device,
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data={},
            service_data={},
            service_uuids=[MYLIFTER_SERVICE_UUID],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        ),
        connectable=True,
        time=0,
        tx_power=None,
    )


@pytest.fixture
def fake_bluetooth() -> Generator[FakeBluetooth]:
    """Fake motors reachable through a fake connectable proxy."""
    fake = FakeBluetooth()

    def _ble_device(hass: HomeAssistant, address: str, connectable: bool = True) -> Any:
        if address in fake.devices:
            return BLEDevice(address, "MyLifter", {})
        return None

    def _discovered(hass: HomeAssistant, connectable: bool = True) -> list[Any]:
        return [service_info(address) for address in fake.devices]

    with (
        patch(
            "custom_components.mylifter.lifter.establish_connection",
            fake.establish_connection,
        ),
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            _ble_device,
        ),
        patch(
            "custom_components.mylifter.config_flow.async_ble_device_from_address",
            _ble_device,
        ),
        patch(
            "custom_components.mylifter.config_flow.async_discovered_service_info",
            _discovered,
        ),
        patch("custom_components.mylifter.coordinator.TIMINGS", FAST_TIMINGS),
        patch("custom_components.mylifter.config_flow.PAIRING_TIMINGS", FAST_TIMINGS),
    ):
        yield fake


@pytest.fixture
def device(fake_bluetooth: FakeBluetooth):
    """A single fake motor."""
    return fake_bluetooth.add(ADDRESS)


def motor_entry(
    address: str = ADDRESS, multi_motor: bool = False, title: str = "MyLifter D348"
) -> MockConfigEntry:
    """Build a motor config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=address,
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_MOTOR,
            CONF_ADDRESS: address,
            CONF_PASSKEY: PASSKEY.hex(),
            CONF_MULTI_MOTOR: multi_motor,
        },
    )


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A single-motor config entry."""
    return motor_entry()


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, device
) -> Any:
    """Set up the integration with one motor; unload afterwards."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    yield mock_config_entry
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
