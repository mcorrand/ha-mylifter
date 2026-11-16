"""Base entity for MyLifter motors."""

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import MyLifterCoordinator


class MyLifterEntity(CoordinatorEntity[MyLifterCoordinator]):
    """Entity bound to one motor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MyLifterCoordinator, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        address = coordinator.address
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=coordinator.data.firmware if coordinator.data else None,
        )

    @property
    def available(self) -> bool:
        """Available while polls succeed or a session is open."""
        return super().available or self.coordinator.client.connected
