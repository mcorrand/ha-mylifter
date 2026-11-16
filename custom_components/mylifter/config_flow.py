"""Config flow for MyLifter."""

import asyncio
from collections.abc import Mapping
import logging
import re
from typing import Any, override

from bluetooth_data_tools import human_readable_name
import probatio

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    ARRANGEMENT_MULTI,
    ARRANGEMENT_SINGLE,
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
from .lifter import (
    DEFAULT_TIMINGS,
    LastError,
    MyLifterAuthError,
    MyLifterConnectionError,
    MyLifterPairingTimeout,
    async_pair,
)
from .protocol import MYLIFTER_SERVICE_UUID

_LOGGER = logging.getLogger(__name__)

PAIRING_TIMINGS = DEFAULT_TIMINGS
LOCAL_NAME_PREFIX = "MyLifter"
_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def _is_mylifter(info: BluetoothServiceInfoBleak) -> bool:
    return MYLIFTER_SERVICE_UUID in [u.lower() for u in info.service_uuids] or (
        info.name or ""
    ).startswith(LOCAL_NAME_PREFIX)


class MyLifterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MyLifter."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._address: str | None = None
        self._name: str | None = None
        self._multi_motor = False
        self._passkey: str | None = None
        self._pair_task: asyncio.Task[bytes] | None = None
        self._pair_errors: dict[str, str] = {}
        self._discovered: dict[str, str] = {}

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MyLifterOptionsFlow()

    @classmethod
    @callback
    @override
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        return config_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_GROUP

    # ------------------------------------------------------------ discovery

    @override
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a discovered motor."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._name = human_readable_name(
            None, discovery_info.name, discovery_info.address
        )
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered motor."""
        if user_input is not None:
            return await self.async_step_pair()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._name or ""},
        )

    # --------------------------------------------------------------- manual

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a motor, or a lifter group when multi-motor motors exist."""
        if self._groupable_motors():
            return self.async_show_menu(step_id="user", menu_options=["motor", "group"])
        return await self.async_step_motor()

    async def async_step_motor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a discovered motor or enter its Bluetooth address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if address in self._discovered:
                name = self._discovered[address]
            else:
                name = human_readable_name(None, LOCAL_NAME_PREFIX, address)
            if not _MAC_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                if async_ble_device_from_address(self.hass, address, connectable=True):
                    self._address = address
                    self._name = name
                    return await self.async_step_pair()
                errors["base"] = "not_found"

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or not _is_mylifter(info):
                continue
            self._discovered[info.address] = human_readable_name(
                None, info.name, info.address
            )
        options = [
            SelectOptionDict(value=address, label=f"{name} ({address})")
            for address, name in self._discovered.items()
        ]
        return self.async_show_form(
            step_id="motor",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            custom_value=True,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    # -------------------------------------------------------------- pairing

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the motor arrangement and the button press."""
        if user_input is not None:
            self._multi_motor = user_input[CONF_MOTOR_ARRANGEMENT] == ARRANGEMENT_MULTI
            return await self.async_step_pair_wait()
        errors, self._pair_errors = self._pair_errors, {}
        return self.async_show_form(
            step_id="pair",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_MOTOR_ARRANGEMENT): SelectSelector(
                        SelectSelectorConfig(
                            options=[ARRANGEMENT_SINGLE, ARRANGEMENT_MULTI],
                            translation_key=CONF_MOTOR_ARRANGEMENT,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"name": self._name or ""},
            errors=errors,
        )

    async def async_step_pair_wait(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait (up to 30 s) for the passkey the motor sends after the button press."""
        if self._pair_task is None:
            # Not eager: the step must return SHOW_PROGRESS first even if pairing
            # fails instantly, otherwise the flow manager re-enters this form step.
            self._pair_task = self.hass.async_create_task(
                self._async_pair(), eager_start=False
            )
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair_wait",
                progress_action="wait_for_button",
                progress_task=self._pair_task,
                description_placeholders={"name": self._name or ""},
            )
        task, self._pair_task = self._pair_task, None
        try:
            self._passkey = task.result().hex()
        except MyLifterPairingTimeout:
            self._pair_errors = {"base": "pairing_timeout"}
        except MyLifterAuthError:
            self._pair_errors = {"base": "invalid_auth"}
        except MyLifterConnectionError as err:
            self._pair_errors = {
                "base": "no_connection_slot"
                if err.reason is LastError.NO_CONNECTION_SLOT
                else "cannot_connect"
            }
        except Exception:
            _LOGGER.exception("Unexpected error while pairing")
            self._pair_errors = {"base": "unknown"}
        if self._pair_errors:
            next_step = "reauth_confirm" if self.source == SOURCE_REAUTH else "pair"
            return self.async_show_progress_done(next_step_id=next_step)
        return self.async_show_progress_done(next_step_id="finish")

    async def _async_pair(self) -> bytes:
        assert self._address is not None
        ble_device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            raise MyLifterConnectionError(LastError.CONNECTION_FAILED, "not found")
        return await async_pair(
            ble_device, name=self._name or self._address, timings=PAIRING_TIMINGS
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry (or update it after reauth)."""
        assert self._passkey is not None
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={CONF_PASSKEY: self._passkey},
            )
        assert self._address is not None
        return self.async_create_entry(
            title=self._name or self._address,
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_MOTOR,
                CONF_ADDRESS: self._address,
                CONF_PASSKEY: self._passkey,
                CONF_MULTI_MOTOR: self._multi_motor,
            },
        )

    # --------------------------------------------------------------- reauth

    @override
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The motor rejected the stored passkey."""
        self._address = entry_data[CONF_ADDRESS]
        self._name = self._get_reauth_entry().title
        self._multi_motor = entry_data.get(CONF_MULTI_MOTOR, False)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to press the button again."""
        if user_input is not None:
            return await self.async_step_pair_wait()
        errors, self._pair_errors = self._pair_errors, {}
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"name": self._name or ""},
            errors=errors,
        )

    # ---------------------------------------------------------------- group

    def _groupable_motors(self) -> dict[str, ConfigEntry]:
        """Loaded multi-motor motors that are not in a group yet."""
        grouped: set[str] = set()
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
                grouped.update(entry.data[CONF_MEMBERS])
        return {
            entry.entry_id: entry
            for entry in self._async_current_entries(include_ignore=False)
            if entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_MOTOR) == ENTRY_TYPE_MOTOR
            and entry.data.get(CONF_MULTI_MOTOR)
            and entry.entry_id not in grouped
            and entry.state is ConfigEntryState.LOADED
        }

    async def async_step_group(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Combine the motors of one lifter so they only move together."""
        motors = self._groupable_motors()
        errors: dict[str, str] = {}
        if user_input is not None:
            members = sorted(user_input[CONF_MEMBERS])
            if len(members) < 2 or not set(members) <= motors.keys():
                errors[CONF_MEMBERS] = "group_needs_two"
            else:
                await self.async_set_unique_id(
                    "group_"
                    + "_".join(sorted(str(motors[m].unique_id) for m in members))
                )
                self._abort_if_unique_id_configured()
                titles = " + ".join(sorted(motors[m].title for m in members))
                return self.async_create_entry(
                    title=f"Lifter ({titles})",
                    data={CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP, CONF_MEMBERS: members},
                )
        if len(motors) < 2:
            return self.async_abort(reason="not_enough_motors")
        return self.async_show_form(
            step_id="group",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_MEMBERS): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=entry_id, label=entry.title)
                                for entry_id, entry in motors.items()
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
        )


class MyLifterOptionsFlow(OptionsFlow):
    """Calibration: raw positions of the top/bottom smart points."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit or reset the learned smart points."""
        if user_input is not None:
            if user_input.get(CONF_RESET_CALIBRATION):
                return self.async_create_entry(data={})
            options: dict[str, int] = {}
            for key in (CONF_TOP_POSITION, CONF_BOTTOM_POSITION):
                if user_input.get(key) is not None:
                    options[key] = int(user_input[key])
            return self.async_create_entry(data=options)

        position_selector = NumberSelector(
            NumberSelectorConfig(
                min=-(2**31), max=2**31 - 1, step=1, mode=NumberSelectorMode.BOX
            )
        )
        schema = probatio.Schema(
            {
                probatio.Optional(CONF_TOP_POSITION): position_selector,
                probatio.Optional(CONF_BOTTOM_POSITION): position_selector,
                probatio.Optional(CONF_RESET_CALIBRATION, default=False): (
                    BooleanSelector()
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, dict(self.config_entry.options)
            ),
        )
