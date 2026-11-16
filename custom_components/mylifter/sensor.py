"""Sensor platform for MyLifter."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLifterConfigEntry
from .coordinator import MyLifterCoordinator
from .entity import MyLifterEntity
from .lifter import LastError, LifterState
from .protocol import describe_device_error

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MyLifterSensorEntityDescription(SensorEntityDescription):
    """Describes a MyLifter sensor."""

    value_fn: Callable[[LifterState], str | int | None]
    # Read the live client state instead of the last successful poll, and stay
    # available when the motor is unreachable (diagnostic error reporting).
    live: bool = False


SENSORS: tuple[MyLifterSensorEntityDescription, ...] = (
    MyLifterSensorEntityDescription(
        key="load",
        translation_key="load",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.weight,
    ),
    MyLifterSensorEntityDescription(
        key="raw_position",
        translation_key="raw_position",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.position,
    ),
    MyLifterSensorEntityDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.firmware,
    ),
    MyLifterSensorEntityDescription(
        key="last_error",
        translation_key="last_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=[error.value for error in LastError],
        value_fn=lambda state: state.last_error.value,
        live=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLifterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    assert isinstance(coordinator, MyLifterCoordinator)
    async_add_entities(MyLifterSensor(coordinator, desc) for desc in SENSORS)


class MyLifterSensor(MyLifterEntity, SensorEntity):
    """A MyLifter sensor."""

    entity_description: MyLifterSensorEntityDescription

    def __init__(
        self,
        coordinator: MyLifterCoordinator,
        description: MyLifterSensorEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    def _state(self) -> LifterState:
        if self.entity_description.live:
            return self.coordinator.client.state
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Live diagnostic sensors are always available."""
        return self.entity_description.live or super().available

    @property
    def native_value(self) -> str | int | None:
        """Return the value."""
        return self.entity_description.value_fn(self._state())

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None] | None:
        """Expose the raw device error code next to the last error."""
        if self.entity_description.key != "last_error":
            return None
        state = self._state()
        return {
            "detail": state.last_error_detail,
            "device_error_code": state.device_error,
            "device_error": describe_device_error(state.device_error),
        }
