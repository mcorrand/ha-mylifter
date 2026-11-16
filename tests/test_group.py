"""Multi-motor lifter groups: motors only move together and stop together."""

import asyncio
from collections.abc import Callable

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mylifter.const import (
    CONF_BOTTOM_POSITION,
    CONF_ENTRY_TYPE,
    CONF_MEMBERS,
    DOMAIN,
    ENTRY_TYPE_GROUP,
)
from custom_components.mylifter.protocol import MoveCode
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN, CoverState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_STOP_COVER,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .conftest import ADDRESS, ADDRESS_2, motor_entry
from .fake_device import FakeBluetooth, FakeMyLifter


async def _wait(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
async def group_setup(hass: HomeAssistant, fake_bluetooth: FakeBluetooth):
    """Two multi-motor motors and their group."""
    dev1 = fake_bluetooth.add(ADDRESS)
    dev2 = fake_bluetooth.add(ADDRESS_2)
    m1 = motor_entry(ADDRESS, multi_motor=True, title="Motor A")
    m2 = motor_entry(ADDRESS_2, multi_motor=True, title="Motor B")
    for entry in (m1, m2):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    group = MockConfigEntry(
        domain=DOMAIN,
        title="Lifter (Motor A + Motor B)",
        unique_id=f"group_{ADDRESS}_{ADDRESS_2}",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
            CONF_MEMBERS: sorted([m1.entry_id, m2.entry_id]),
        },
    )
    group.add_to_hass(hass)
    assert await hass.config_entries.async_setup(group.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        COVER_DOMAIN, DOMAIN, f"{group.entry_id}_cover"
    )
    yield entity_id, group, (m1, m2), (dev1, dev2)
    for entry in (group, m1, m2):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        COVER_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


def _any_moving(devices: tuple[FakeMyLifter, ...], entries) -> bool:
    return any(e.runtime_data.client.is_moving for e in entries)


async def test_group_moves_all_motors(hass: HomeAssistant, group_setup) -> None:
    """Open/close drive all members together."""
    entity_id, group, members, devices = group_setup
    assert hass.states.get(entity_id) is not None
    await _call(hass, SERVICE_OPEN_COVER, entity_id)
    assert {m.session for d in devices for m in d.smart_moves()}
    assert all(d.smart_moves() for d in devices)
    await _wait(lambda: not _any_moving(devices, members))
    await _wait(lambda: group.runtime_data.moving is None)
    assert all(d.position == 0 for d in devices)
    assert all(m.options[CONF_BOTTOM_POSITION] == 0 for m in members)

    await _call(hass, SERVICE_CLOSE_COVER, entity_id)
    await _wait(lambda: not _any_moving(devices, members))
    await _wait(lambda: group.runtime_data.moving is None)
    assert hass.states.get(entity_id).state == CoverState.CLOSED
    assert group.runtime_data.last_error is None


async def test_group_aborts_when_one_member_fails(
    hass: HomeAssistant, group_setup
) -> None:
    """One member cannot confirm: every member is stopped, error raised."""
    entity_id, _group, members, (dev1, dev2) = group_setup
    dev1.step = 5
    dev2.stuck = True
    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_OPEN_COVER, entity_id)
    assert not _any_moving((dev1, dev2), members)
    stopped_at = dev1.position
    frames = len(dev1.moves)
    await asyncio.sleep(0.1)
    assert dev1.position == stopped_at
    assert all(m.code is MoveCode.STOP for m in dev1.moves[frames:])
    assert members[0].runtime_data.client.state.last_error == "group_aborted"


async def test_group_stops_on_divergence(hass: HomeAssistant, group_setup) -> None:
    """Members drifting apart are stopped."""
    entity_id, group, members, (dev1, dev2) = group_setup
    dev1.step = 100
    dev2.step = 6
    dev1.bottom = -10000
    await _call(hass, SERVICE_OPEN_COVER, entity_id)
    await _wait(lambda: group.runtime_data.moving is None)
    await _wait(lambda: not _any_moving((dev1, dev2), members))
    assert "diverged" in group.runtime_data.last_error


async def test_group_out_of_sync(hass: HomeAssistant, group_setup) -> None:
    """One member already at the smart point, the other not: stop and report."""
    entity_id, _group, members, (dev1, dev2) = group_setup
    dev1.position = 0
    await members[0].runtime_data.async_refresh()
    dev2.step = 5
    with pytest.raises(HomeAssistantError, match="not at the same smart point"):
        await _call(hass, SERVICE_OPEN_COVER, entity_id)
    assert not _any_moving((dev1, dev2), members)


async def test_group_member_link_loss_stops_all(
    hass: HomeAssistant, group_setup
) -> None:
    """A member disconnecting mid-move stops the others."""
    entity_id, group, members, (dev1, dev2) = group_setup
    dev1.step = dev2.step = 5
    await _call(hass, SERVICE_OPEN_COVER, entity_id)
    dev2.drop_connection()
    await _wait(lambda: not _any_moving((dev1, dev2), members))
    await _wait(lambda: group.runtime_data.moving is None)
    assert group.runtime_data.last_error


async def test_group_stop(hass: HomeAssistant, group_setup) -> None:
    """STOP on the group stops every member."""
    entity_id, group, members, devices = group_setup
    for d in devices:
        d.step = 5
    await _call(hass, SERVICE_OPEN_COVER, entity_id)
    assert hass.states.get(entity_id).state == CoverState.OPENING
    await _call(hass, SERVICE_STOP_COVER, entity_id)
    assert not _any_moving(devices, members)
    await asyncio.sleep(0.05)
    assert all(d.moves[-1].code is MoveCode.STOP for d in devices)
    assert group.runtime_data.last_error is None


async def test_group_reloads_with_member(hass: HomeAssistant, group_setup) -> None:
    """Reloading a member reloads the group so it tracks the new coordinator."""
    _entity_id, group, members, _devices = group_setup
    await hass.config_entries.async_reload(members[0].entry_id)
    await hass.async_block_till_done()
    assert group.state is ConfigEntryState.LOADED
    assert any(m is members[0].runtime_data for m in group.runtime_data.members())
