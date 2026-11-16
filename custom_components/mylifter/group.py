"""Synchronised control of a lifter driven by several MyLifter motors.

See DECISIONS.md "Multi-motor": member motors never move on their own; the group starts
them together, requires every member to confirm and stops all members as soon as one
of them fails, disconnects, stops or diverges.
"""

import asyncio
from collections.abc import Callable
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_MEMBERS, DOMAIN, GROUP_FINISH_GRACE, GROUP_MAX_DIVERGENCE
from .coordinator import MyLifterCoordinator
from .lifter import LastError, MotionOutcome, MoveResult
from .protocol import MoveCode

_LOGGER = logging.getLogger(__name__)


class MyLifterGroup:
    """Drives all member motors of one lifter together."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.member_entry_ids: list[str] = list(entry.data[CONF_MEMBERS])
        self.moving: MoveCode | None = None
        self.last_error: str | None = None
        self._start_positions: dict[str, int] = {}
        self._member_unsubs: list[CALLBACK_TYPE] = []
        self._grace_handle: asyncio.TimerHandle | None = None
        self._listeners: list[Callable[[], None]] = []
        self._aborting = False
        self._tasks: set[asyncio.Task[None]] = set()

    # -------------------------------------------------------------- members

    def try_members(self) -> list[MyLifterCoordinator] | None:
        """Return the member coordinators, or None if any member is not loaded."""
        members: list[MyLifterCoordinator] = []
        for entry_id in self.member_entry_ids:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.state is not ConfigEntryState.LOADED:
                return None
            members.append(entry.runtime_data)
        return members

    def members(self) -> list[MyLifterCoordinator]:
        """Return the member coordinators or raise if any is unavailable."""
        if (members := self.try_members()) is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="group_member_unavailable"
            )
        return members

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> CALLBACK_TYPE:
        """Listen for group state changes."""
        self._listeners.append(update_callback)

        @callback
        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove

    @callback
    def _notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ------------------------------------------------------------- commands

    async def async_move(self, code: MoveCode) -> None:
        """Start the same smart move on all members; abort all if any fails."""
        members = self.members()
        if self.moving is not None or any(m.client.is_moving for m in members):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="busy")
        self.moving = code
        self.last_error = None
        self._aborting = False
        self._notify()
        try:
            results = await asyncio.gather(
                *(m.async_move(code, from_group=True) for m in members),
                return_exceptions=True,
            )
        except BaseException:
            await self._async_abort(members, "group move interrupted")
            raise

        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            await self._async_abort(members, str(errors[0]))
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="group_move_failed",
                translation_placeholders={"error": str(errors[0])},
            ) from errors[0]
        if self.moving is None or MoveResult.CANCELLED in results:
            # STOP arrived while starting: _async_stop_all already handled it.
            await self._async_abort(members, None)
            return
        if all(r is MoveResult.AT_LIMIT for r in results):
            self._finish()
            return
        if MoveResult.AT_LIMIT in results:
            await self._async_abort(members, "members are not at the same smart point")
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="group_out_of_sync"
            )

        # All members confirmed motion: supervise until they all finish.
        self._start_positions = {
            m.address: m.client.motion_start_position or 0 for m in members
        }
        for member in members:
            self._member_unsubs.append(
                member.async_add_listener(self._async_check_members)
            )
        self._async_check_members()

    async def async_stop(self) -> None:
        """STOP all members (in parallel). Raises if any member could not be stopped."""
        self.moving = None
        self._cleanup_supervision()
        self._notify()
        members = self.try_members()
        if members is None:
            # Stop whatever is loaded; never skip STOP because one member is missing.
            members = [
                entry.runtime_data
                for entry_id in self.member_entry_ids
                if (entry := self.hass.config_entries.async_get_entry(entry_id))
                and entry.state is ConfigEntryState.LOADED
            ]
        results = await asyncio.gather(
            *(m.async_stop() for m in members), return_exceptions=True
        )
        if errors := [r for r in results if isinstance(r, BaseException)]:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="stop_failed",
                translation_placeholders={"error": str(errors[0])},
            ) from errors[0]

    # ----------------------------------------------------------- supervision

    @callback
    def _async_check_members(self) -> None:
        if self.moving is None or self._aborting:
            return
        members = self.try_members()
        if members is None:
            self._schedule_abort(None, "a member was unloaded while moving")
            return
        still_moving = [m for m in members if m.client.is_moving]
        for member in members:
            if member.client.is_moving:
                continue
            outcome = member.client.state.last_motion_outcome
            if outcome not in (MotionOutcome.COMPLETED, MotionOutcome.AT_LIMIT):
                self._schedule_abort(
                    members, f"{member.config_entry.title} stopped ({outcome})"
                )
                return
        travels = [
            abs(
                (m.client.state.position or 0) - self._start_positions.get(m.address, 0)
            )
            for m in members
        ]
        if max(travels) - min(travels) > GROUP_MAX_DIVERGENCE:
            self._schedule_abort(
                members, f"members diverged by {max(travels) - min(travels)} ticks"
            )
            return
        if not still_moving:
            self._finish()
            return
        if len(still_moving) < len(members) and self._grace_handle is None:
            self._grace_handle = self.hass.loop.call_later(
                GROUP_FINISH_GRACE, self._grace_expired
            )
        self._notify()

    @callback
    def _grace_expired(self) -> None:
        self._grace_handle = None
        members = self.try_members()
        if self.moving is not None and (
            members is None or any(m.client.is_moving for m in members)
        ):
            self._schedule_abort(members, "members did not finish together")

    @callback
    def _schedule_abort(
        self, members: list[MyLifterCoordinator] | None, reason: str
    ) -> None:
        if self._aborting:
            return
        self._aborting = True
        task = self.entry.async_create_background_task(
            self.hass,
            self._async_abort(members, reason),
            f"{DOMAIN} group abort {self.entry.entry_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _async_abort(
        self, members: list[MyLifterCoordinator] | None, reason: str | None
    ) -> None:
        """Stop every member; record why (None = user STOP, no error)."""
        if reason is not None:
            _LOGGER.error("%s: stopping all motors: %s", self.entry.title, reason)
            self.last_error = reason
        self.moving = None
        self._cleanup_supervision()
        if members is None:
            members = self.try_members() or []
        results = await asyncio.gather(
            *(m.client.async_stop() for m in members), return_exceptions=True
        )
        for member, result in zip(members, results, strict=True):
            if isinstance(result, BaseException):
                _LOGGER.error(
                    "%s: STOP failed for %s: %s",
                    self.entry.title,
                    member.config_entry.title,
                    result,
                )
            if reason is not None:
                member.client.record_error(LastError.GROUP_ABORTED, reason)
        self._aborting = False
        self._notify()

    @callback
    def _finish(self) -> None:
        self.moving = None
        self._cleanup_supervision()
        self._notify()

    @callback
    def _cleanup_supervision(self) -> None:
        for unsub in self._member_unsubs:
            unsub()
        self._member_unsubs.clear()
        if self._grace_handle is not None:
            self._grace_handle.cancel()
            self._grace_handle = None

    async def async_shutdown(self) -> None:
        """Unload: stop a running group move."""
        if self.moving is not None:
            members = self.try_members() or []
            await asyncio.gather(
                *(m.client.async_stop() for m in members), return_exceptions=True
            )
        self.moving = None
        self._cleanup_supervision()
        for task in list(self._tasks):
            task.cancel()
