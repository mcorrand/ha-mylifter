"""Binary sensor platform for MyLifter (connection state)."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLifterConfigEntry
from .coordinator import MyLifterCoordinator
from .entity import MyLifterEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLifterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connection binary sensor."""
    coordinator = entry.runtime_data
    assert isinstance(coordinator, MyLifterCoordinator)
    async_add_entities([MyLifterConnectionSensor(coordinator)])


class MyLifterConnectionSensor(MyLifterEntity, BinarySensorEntity):
    """On while a BLE session to the motor is open (sessions are on demand)."""

    _attr_translation_key = "connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MyLifterCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        """Always available: it reports the connection itself."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while connected."""
        return self.coordinator.client.connected
