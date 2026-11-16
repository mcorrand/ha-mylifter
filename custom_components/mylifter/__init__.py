"""The MyLifter integration."""

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ENTRY_TYPE, CONF_MEMBERS, DOMAIN, ENTRY_TYPE_GROUP
from .coordinator import MyLifterCoordinator
from .group import MyLifterGroup

MOTOR_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.SENSOR,
]
GROUP_PLATFORMS: list[Platform] = [Platform.COVER]

type MyLifterConfigEntry = ConfigEntry[MyLifterCoordinator | MyLifterGroup]


def is_group_entry(entry: ConfigEntry) -> bool:
    """Return True for lifter group entries."""
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP


async def async_setup_entry(hass: HomeAssistant, entry: MyLifterConfigEntry) -> bool:
    """Set up a motor or a lifter group from a config entry."""
    if is_group_entry(entry):
        group = MyLifterGroup(hass, entry)
        if group.try_members() is None:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="group_members_not_ready"
            )
        entry.runtime_data = group
        await hass.config_entries.async_forward_entry_setups(entry, GROUP_PLATFORMS)
        return True

    address: str = entry.data[CONF_ADDRESS]
    if not bluetooth.async_ble_device_from_address(hass, address, connectable=True):
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"address": address},
        )
    coordinator = MyLifterCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, MOTOR_PLATFORMS)

    # Groups hold references to member coordinators: once this motor is LOADED,
    # reload groups that use it (or that are waiting for it).
    def _reload_groups() -> None:
        if entry.state is not ConfigEntryState.LOADED:
            return
        unsub()
        for other in hass.config_entries.async_entries(DOMAIN):
            if (
                is_group_entry(other)
                and entry.entry_id in other.data[CONF_MEMBERS]
                and other.state
                in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_RETRY)
            ):
                hass.config_entries.async_schedule_reload(other.entry_id)

    unsub = entry.async_on_state_change(_reload_groups)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: MyLifterConfigEntry
) -> None:
    """Options only hold calibration: re-render entities, no reload needed."""
    entry.runtime_data.async_update_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: MyLifterConfigEntry) -> bool:
    """Unload a config entry."""
    platforms = GROUP_PLATFORMS if is_group_entry(entry) else MOTOR_PLATFORMS
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok and is_group_entry(entry):
        group = entry.runtime_data
        assert isinstance(group, MyLifterGroup)
        await group.async_shutdown()
    return unload_ok
