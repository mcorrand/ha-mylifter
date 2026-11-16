"""Coordinator for one MyLifter motor."""

import logging

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CLOSED_TOLERANCE,
    CONF_BOTTOM_POSITION,
    CONF_MULTI_MOTOR,
    CONF_PASSKEY,
    CONF_TOP_POSITION,
    DOMAIN,
    POLL_INTERVAL,
)
from .lifter import (
    DEFAULT_TIMINGS,
    LastError,
    LifterState,
    MotionOutcome,
    MoveResult,
    MyLifterAuthError,
    MyLifterBusyError,
    MyLifterClient,
    MyLifterError,
)
from .protocol import MoveCode

_LOGGER = logging.getLogger(__name__)

TIMINGS = DEFAULT_TIMINGS


class MyLifterCoordinator(DataUpdateCoordinator[LifterState]):
    """Polls the motor while idle and receives pushed status while connected."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=POLL_INTERVAL,
            always_update=False,
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.multi_motor: bool = entry.data.get(CONF_MULTI_MOTOR, False)
        self.client = MyLifterClient(
            self.address,
            bytes.fromhex(entry.data[CONF_PASSKEY]),
            self._async_ble_device,
            name=entry.title,
            timings=TIMINGS,
            on_update=self._async_client_updated,
            on_motion_end=self._async_motion_ended,
        )
        self._sw_version: str | None = None

    @callback
    def _async_ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

    async def _async_update_data(self) -> LifterState:
        try:
            await self.client.async_refresh()
        except MyLifterAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except MyLifterError as err:
            self._record_connect_error(err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        return self.client.snapshot()

    @callback
    def _async_client_updated(self) -> None:
        self.async_set_updated_data(self.client.snapshot())
        firmware = self.client.state.firmware
        if firmware is not None and firmware != self._sw_version:
            self._sw_version = firmware
            device_registry = dr.async_get(self.hass)
            if device := device_registry.async_get_device_by_identifier(
                (DOMAIN, self.address), self.config_entry.entry_id
            ):
                device_registry.async_update_device(device.id, sw_version=firmware)

    @callback
    def _async_motion_ended(
        self, code: MoveCode, outcome: MotionOutcome, position: int | None
    ) -> None:
        """Learn the smart points from moves that ended on their own."""
        if position is None or outcome not in (
            MotionOutcome.COMPLETED,
            MotionOutcome.AT_LIMIT,
        ):
            return
        key = CONF_TOP_POSITION if code is MoveCode.SMART_UP else CONF_BOTTOM_POSITION
        if self.config_entry.options.get(key) == position:
            return
        _LOGGER.debug("%s: learned %s = %s", self.config_entry.title, key, position)
        self.hass.config_entries.async_update_entry(
            self.config_entry, options={**self.config_entry.options, key: position}
        )

    # ---------------------------------------------------------------- derived

    @property
    def top_position(self) -> int | None:
        """Raw position of the top smart point (learned or configured)."""
        return self.config_entry.options.get(CONF_TOP_POSITION)

    @property
    def bottom_position(self) -> int | None:
        """Raw position of the bottom smart point (learned or configured)."""
        return self.config_entry.options.get(CONF_BOTTOM_POSITION)

    def cover_position(self, state: LifterState) -> int | None:
        """0 = stowed at the top, 100 = at the bottom smart point."""
        top, bottom = self.top_position, self.bottom_position
        if state.position is None or top is None or bottom is None or top == bottom:
            return None
        fraction = (top - state.position) / (top - bottom)
        return round(min(max(fraction, 0.0), 1.0) * 100)

    def is_closed(self, state: LifterState) -> bool | None:
        """Closed = stowed at the top smart point."""
        if state.position is None or self.top_position is None:
            return None
        return abs(state.position - self.top_position) <= CLOSED_TOLERANCE

    # --------------------------------------------------------------- commands

    async def async_move(
        self, code: MoveCode, *, from_group: bool = False
    ) -> MoveResult:
        """Start a smart move, translating errors for the UI."""
        if self.multi_motor and not from_group:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="multi_motor_solo_move"
            )
        try:
            return await self.client.async_move(code)
        except MyLifterBusyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="busy"
            ) from err
        except MyLifterError as err:
            self._record_connect_error(err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="move_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_stop(self) -> None:
        """Stop the motor."""
        try:
            await self.client.async_stop()
        except MyLifterError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="stop_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    def _record_connect_error(self, err: MyLifterError) -> None:
        """Record connection problems without marking the coordinator as updated."""
        if err.reason in (LastError.CONNECTION_FAILED, LastError.NO_CONNECTION_SLOT):
            self.client.record_error(err.reason, err.detail, notify=False)
            self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Stop polling and close the BLE session (STOP first if moving)."""
        await super().async_shutdown()
        await self.client.async_shutdown()
