"""Cover platform: the hoist (motor) and multi-motor lifter groups."""

from typing import Any, override

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MyLifterConfigEntry
from .const import DOMAIN, MANUFACTURER
from .coordinator import MyLifterCoordinator
from .entity import MyLifterEntity
from .group import MyLifterGroup
from .protocol import MoveCode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyLifterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the cover."""
    runtime = entry.runtime_data
    if isinstance(runtime, MyLifterGroup):
        async_add_entities([MyLifterGroupCover(runtime)])
    else:
        async_add_entities([MyLifterCover(runtime)])


class MyLifterCover(MyLifterEntity, CoverEntity):
    """One motor. Open = lower to the bottom smart point, closed = stowed at the top.

    Motors that belong to a multi-motor lifter only support STOP; they move only
    through their group (see DECISIONS.md "Multi-motor").
    """

    _attr_name = None
    _attr_translation_key = "hoist"

    def __init__(self, coordinator: MyLifterCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "cover")
        self._attr_supported_features = (
            CoverEntityFeature.STOP
            if coordinator.multi_motor
            else CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
        )

    @property
    @override
    def current_cover_position(self) -> int | None:
        return self.coordinator.cover_position(self.coordinator.data)

    @property
    @override
    def is_closed(self) -> bool | None:
        return self.coordinator.is_closed(self.coordinator.data)

    @property
    @override
    def is_opening(self) -> bool:
        return self.coordinator.data.moving is MoveCode.SMART_DOWN

    @property
    @override
    def is_closing(self) -> bool:
        return self.coordinator.data.moving is MoveCode.SMART_UP

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_move(MoveCode.SMART_DOWN)

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_move(MoveCode.SMART_UP)

    @override
    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.coordinator.async_stop()


class MyLifterGroupCover(CoverEntity):
    """All motors of one lifter, moved together."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "lifter_group"
    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, group: MyLifterGroup) -> None:
        """Initialize."""
        self._group = group
        self._attr_unique_id = f"{group.entry.entry_id}_cover"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, group.entry.entry_id)},
            name=group.entry.title,
            manufacturer=MANUFACTURER,
            model="Multi-motor lifter group",
        )

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._group.async_add_listener(self._async_update))
        for member in self._group.members():
            self.async_on_remove(member.async_add_listener(self._async_update))

    @callback
    def _async_update(self) -> None:
        self.async_write_ha_state()

    def _members(self) -> list[MyLifterCoordinator]:
        return self._group.try_members() or []

    @property
    @override
    def available(self) -> bool:
        members = self._group.try_members()
        return bool(members) and all(
            m.last_update_success or m.client.connected for m in members
        )

    @property
    @override
    def current_cover_position(self) -> int | None:
        positions = [m.cover_position(m.data) for m in self._members()]
        if not positions or any(p is None for p in positions):
            return None
        return round(sum(p for p in positions if p is not None) / len(positions))

    @property
    @override
    def is_closed(self) -> bool | None:
        closed = [m.is_closed(m.data) for m in self._members()]
        if not closed or any(c is None for c in closed):
            return None
        return all(closed)

    @property
    @override
    def is_opening(self) -> bool:
        return any(m.data.moving is MoveCode.SMART_DOWN for m in self._members())

    @property
    @override
    def is_closing(self) -> bool:
        return any(m.data.moving is MoveCode.SMART_UP for m in self._members())

    @override
    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._group.async_move(MoveCode.SMART_DOWN)

    @override
    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._group.async_move(MoveCode.SMART_UP)

    @override
    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._group.async_stop()
