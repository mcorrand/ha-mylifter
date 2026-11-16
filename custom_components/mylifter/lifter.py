"""BLE session handling for one MyLifter motor.

Rewritten for Home Assistant (not derived from PyLifter's client.py). The protocol
facts it relies on come from PyLifter's PROTOCOL.md; see protocol.py for the
vendored encoder/decoder.

This module deliberately has no Home Assistant imports. The caller supplies a callback
that returns the current ``BLEDevice`` (from HA's bluetooth integration), so connections
always go through HA's Bluetooth stack (local adapter or ESPHome proxy).

Safety rules implemented here (see DECISIONS.md):
- STOP has priority: it updates the streamed intent synchronously, cancels pending
  moves and is retried (reconnecting if needed).
- A move is written exactly once. It is never queued, retried or resumed after a
  connection loss; every new session starts with STOP.
- A move that is not confirmed (position change) within a short window is stopped and
  reported as an error.
"""

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
import logging
import time
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from .protocol import (
    COMMAND_CHAR_UUID,
    MAX_SPEED,
    RESPONSE_CHAR_UUID,
    Ack,
    CommandCode,
    DeviceError,
    MoveCode,
    MoveStatus,
    Nack,
    Notification,
    Passkey,
    ProtocolError,
    ProtocolVersion,
    Version,
    build_clear_error_packet,
    build_get_passkey_packet,
    build_get_protocol_version_packet,
    build_get_version_packet,
    build_move_packet,
    build_set_passkey_packet,
    describe_device_error,
    parse_notification,
)

_LOGGER = logging.getLogger(__name__)

SMART_MOVES = (MoveCode.SMART_UP, MoveCode.SMART_DOWN)
LIMIT_CODES = frozenset({DeviceError.SOFT_LIMIT, DeviceError.END_OF_TRAVEL})
# Device errors that are cleared automatically before a move (as upstream does).
CLEARABLE_ERRORS = frozenset(
    {
        DeviceError.SYNC,
        DeviceError.SOFT_LIMIT,
        DeviceError.SMART_POINT_NOT_SET,
        DeviceError.END_OF_TRAVEL,
    }
)
CONNECT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Timings:
    """Protocol timings in seconds (tests use faster values)."""

    keepalive_idle: float = 0.25
    keepalive_moving: float = 0.2
    write_pause: float = 0.02
    write_timeout: float = 3.0
    auth_timeout: float = 5.0
    status_timeout: float = 3.0
    query_timeout: float = 3.0
    clear_error_timeout: float = 1.0
    move_confirm_timeout: float = 3.0
    move_confirm_ticks: int = 5
    stall_time: float = 2.0
    max_move_duration: float = 180.0
    idle_disconnect: float = 30.0
    poll_linger: float = 2.0
    stop_attempts: int = 3
    pairing_timeout: float = 30.0
    ack_timeout: float = 5.0


DEFAULT_TIMINGS = Timings()


class LastError(StrEnum):
    """Integration-level error, exposed by the "last error" sensor."""

    NONE = "none"
    CONNECTION_FAILED = "connection_failed"
    NO_CONNECTION_SLOT = "no_connection_slot"
    AUTH_FAILED = "auth_failed"
    MOVE_NOT_CONFIRMED = "move_not_confirmed"
    MOVE_WRITE_FAILED = "move_write_failed"
    CONNECTION_LOST_WHILE_MOVING = "connection_lost_while_moving"
    MOVE_TIMEOUT = "move_timeout"
    SMART_POINT_NOT_SET = "smart_point_not_set"
    DEVICE_ERROR = "device_error"
    STOP_FAILED = "stop_failed"
    GROUP_ABORTED = "group_aborted"


class MoveResult(StrEnum):
    """Outcome of a move request once it returns without raising."""

    CONFIRMED = "confirmed"  # the motor is moving
    AT_LIMIT = "at_limit"  # already at the requested smart point, nothing moved
    CANCELLED = "cancelled"  # STOP arrived before/while the move started


class MotionOutcome(StrEnum):
    """How a confirmed move ended."""

    COMPLETED = "completed"  # natural end (smart point / limit / stall)
    AT_LIMIT = "at_limit"
    STOPPED = "stopped"  # STOP requested
    FAILED = "failed"


class MyLifterError(Exception):
    """Base error."""

    def __init__(self, reason: LastError, detail: str | None = None) -> None:
        """Initialize."""
        super().__init__(detail or reason.value)
        self.reason = reason
        self.detail = detail


class MyLifterConnectionError(MyLifterError):
    """The BLE link could not be established or broke."""


class MyLifterNotFoundError(MyLifterConnectionError):
    """No connectable adapter/proxy currently sees the device."""

    def __init__(self, detail: str | None = None) -> None:
        """Initialize."""
        super().__init__(LastError.CONNECTION_FAILED, detail or "device not found")


class MyLifterAuthError(MyLifterError):
    """The device rejected the passkey."""

    def __init__(self, detail: str | None = None) -> None:
        """Initialize."""
        super().__init__(LastError.AUTH_FAILED, detail)


class MyLifterMoveError(MyLifterError):
    """A move was refused, not confirmed or aborted."""


class MyLifterBusyError(MyLifterError):
    """A move is already in progress; moves are never queued."""

    def __init__(self) -> None:
        """Initialize."""
        super().__init__(LastError.NONE, "a move is already in progress")


class MyLifterPairingTimeout(MyLifterError):
    """No passkey was received (button not pressed)."""

    def __init__(self) -> None:
        """Initialize."""
        super().__init__(LastError.AUTH_FAILED, "no passkey received")


@dataclass(slots=True)
class LifterState:
    """Snapshot of what is known about the motor."""

    connected: bool = False
    position: int | None = None
    weight: int | None = None
    device_error: int = 0
    move_status: int | None = None
    moving: MoveCode | None = None
    last_motion_outcome: MotionOutcome | None = None
    last_error: LastError = LastError.NONE
    last_error_detail: str | None = None
    firmware: str | None = None
    hardware: str | None = None
    protocol_version: str | None = None


@dataclass(slots=True, eq=False)
class _Motion:
    code: MoveCode
    start_position: int
    generation: int
    confirmed: asyncio.Future[MoveResult]
    started: float = 0.0
    written: bool = False
    is_confirmed: bool = False
    last_position: int = 0
    last_change: float = 0.0
    last_error_code: int = 0


@dataclass(slots=True, eq=False)
class _Waiter:
    predicate: Callable[[Notification], Any]
    future: asyncio.Future[Any] = field(repr=False)


class _Cancelled(Exception):
    """Internal: STOP superseded a move that was still starting."""


def _now() -> float:
    return time.monotonic()


class MyLifterClient:
    """One motor, one (on-demand) BLE session."""

    def __init__(
        self,
        address: str,
        passkey: bytes,
        ble_device_callback: Callable[[], BLEDevice | None],
        *,
        name: str = "MyLifter",
        timings: Timings = DEFAULT_TIMINGS,
        on_update: Callable[[], None] | None = None,
        on_motion_end: Callable[[MoveCode, MotionOutcome, int | None], None]
        | None = None,
    ) -> None:
        """Initialize the client."""
        self.address = address
        self._passkey = passkey
        self._ble_device_callback = ble_device_callback
        self._name = name
        self.timings = timings
        self._on_update = on_update
        self._on_motion_end = on_motion_end

        self.state = LifterState()
        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._intent: MoveCode = MoveCode.STOP
        self._motion: _Motion | None = None
        self._stop_generation = 0
        self._status_count = 0
        self._waiters: list[_Waiter] = []
        self._keepalive_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_deadline = 0.0
        self._closed = False
        self._in_use = 0  # operations currently relying on the session

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        """Whether an authenticated session is open."""
        return self._client is not None and self.state.connected

    @property
    def intent(self) -> MoveCode:
        """Move code currently streamed in keep-alive frames."""
        return self._intent

    @property
    def is_moving(self) -> bool:
        """Whether a move is starting or in progress."""
        return self._motion is not None

    @property
    def motion_start_position(self) -> int | None:
        """Raw position at which the current move started."""
        return self._motion.start_position if self._motion is not None else None

    def snapshot(self) -> LifterState:
        """Return a copy of the state."""
        return replace(self.state)

    def diagnostics(self) -> dict[str, Any]:
        """Return session internals for diagnostics."""
        return {
            "connected": self.connected,
            "intent": self._intent.name,
            "moving": self._motion.code.name if self._motion else None,
            "stop_generation": self._stop_generation,
            "status_frames_this_session": self._status_count,
            "timings": asdict(self.timings),
        }

    async def async_refresh(self) -> None:
        """Poll: make sure we have a fresh status, then let the session linger briefly."""
        self._in_use += 1
        try:
            await self._ensure_connected()
            await self._wait_for_status()
            if self.state.firmware is None:
                await self._query_versions()
        finally:
            self._in_use -= 1
        if self._motion is None:
            self._schedule_idle_disconnect(self.timings.poll_linger)

    async def async_move(self, code: MoveCode) -> MoveResult:
        """Start a smart move. Written once; never retried or queued."""
        if code not in SMART_MOVES:
            raise ValueError(f"unsupported move code {code!r}")
        if self._motion is not None:
            raise MyLifterBusyError
        generation = self._stop_generation
        self._cancel_idle_disconnect()
        self._in_use += 1
        try:
            return await self._async_move(code, generation)
        finally:
            self._in_use -= 1

    async def _async_move(self, code: MoveCode, generation: int) -> MoveResult:
        try:
            await self._ensure_connected()
            self._raise_if_cancelled(generation)
            await self._wait_for_status()
            self._raise_if_cancelled(generation)
            await self._clear_benign_error(generation)
            self._raise_if_cancelled(generation)
        except _Cancelled:
            self._schedule_idle_disconnect(self.timings.idle_disconnect)
            return MoveResult.CANCELLED
        if self._motion is not None:
            raise MyLifterBusyError
        return await self._start_motion(code, generation)

    async def async_stop(self) -> None:
        """Stop the motor. Highest priority, safe to retry, works without a session."""
        self._stop_generation += 1
        self._intent = MoveCode.STOP
        if (motion := self._motion) is not None:
            self._end_motion(motion, MotionOutcome.STOPPED)
        self._in_use += 1
        try:
            await self._async_stop_attempts()
        finally:
            self._in_use -= 1

    async def _async_stop_attempts(self) -> None:
        last_err: MyLifterError | None = None
        for attempt in range(1, self.timings.stop_attempts + 1):
            try:
                await self._ensure_connected()
                self._intent = MoveCode.STOP
                await self._write(self._stop_frame)
            except MyLifterAuthError:
                raise
            except MyLifterConnectionError as err:
                _LOGGER.warning(
                    "%s: STOP attempt %s/%s failed: %s",
                    self._name,
                    attempt,
                    self.timings.stop_attempts,
                    err,
                )
                last_err = err
                await self._drop_session()
                continue
            self._schedule_idle_disconnect(self.timings.idle_disconnect)
            return
        self._set_error(LastError.STOP_FAILED, str(last_err))
        self._notify()
        raise MyLifterConnectionError(
            LastError.STOP_FAILED, f"STOP could not be delivered: {last_err}"
        )

    async def async_disconnect(self) -> None:
        """Close the session (sends STOP first if a move is active)."""
        self._cancel_idle_disconnect()
        if self._motion is not None:
            await self.async_stop()
            return
        await self._drop_session()
        self._notify()

    async def async_shutdown(self) -> None:
        """Stop everything for unload."""
        if self._motion is not None:
            try:
                await self.async_stop()
            except MyLifterError as err:
                _LOGGER.warning("%s: STOP during shutdown failed: %s", self._name, err)
        self._closed = True
        self._cancel_idle_disconnect()
        for task in list(self._tasks):
            task.cancel()
        await self._drop_session()

    # --------------------------------------------------------------- session

    def _session_ready(self) -> bool:
        return (
            self._client is not None
            and self._client.is_connected
            and self.state.connected
        )

    async def _ensure_connected(self) -> None:
        if self._session_ready():
            return
        async with self._connect_lock:
            if self._session_ready():
                return
            if self._client is not None:
                await self._drop_session()
            await self._connect()

    async def _connect(self) -> None:
        if self._closed:
            raise MyLifterConnectionError(LastError.CONNECTION_FAILED, "client closed")
        ble_device = self._ble_device_callback()
        if ble_device is None:
            raise MyLifterNotFoundError(
                "not seen by any connectable Bluetooth adapter or proxy"
            )
        # Every new session starts with STOP; a previous move is never resumed.
        self._intent = MoveCode.STOP
        self._status_count = 0
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self._name,
                disconnected_callback=self._on_disconnected,
                ble_device_callback=self._current_ble_device(ble_device),
                max_attempts=CONNECT_ATTEMPTS,
            )
        except BleakOutOfConnectionSlotsError as err:
            raise MyLifterConnectionError(
                LastError.NO_CONNECTION_SLOT,
                "no free connection slot on the Bluetooth adapter/proxy",
            ) from err
        except (BleakError, TimeoutError) as err:
            raise MyLifterConnectionError(
                LastError.CONNECTION_FAILED, str(err)
            ) from err

        self._client = client
        auth: asyncio.Future[Any] | None = None
        try:
            await client.start_notify(RESPONSE_CHAR_UUID, self._on_notification)
            auth = self._wait_for(_auth_result, self.timings.auth_timeout)
            await self._write(build_set_passkey_packet(self._passkey), client)
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(client), name=f"mylifter keepalive {self.address}"
            )
            result = await auth
        except BaseException as err:
            if auth is not None:
                _discard(auth)
            await self._drop_session()
            if isinstance(err, TimeoutError):
                raise MyLifterConnectionError(
                    LastError.CONNECTION_FAILED, "no response after authentication"
                ) from err
            if isinstance(err, BleakError):
                raise MyLifterConnectionError(
                    LastError.CONNECTION_FAILED, str(err)
                ) from err
            raise
        if result == "nack":
            await self._drop_session()
            raise MyLifterAuthError("passkey rejected")
        self.state.connected = True
        _LOGGER.debug("%s: session open", self._name)
        self._notify()

    def _current_ble_device(self, fallback: BLEDevice) -> Callable[[], BLEDevice]:
        def _get() -> BLEDevice:
            return self._ble_device_callback() or fallback

        return _get

    async def _drop_session(self) -> None:
        """Close the session we own (expected disconnect)."""
        client = self._client
        self._client = None
        self.state.connected = False
        self._intent = MoveCode.STOP
        if (task := self._keepalive_task) is not None:
            self._keepalive_task = None
            if task is not asyncio.current_task():
                task.cancel()
        self._fail_waiters(
            MyLifterConnectionError(LastError.CONNECTION_FAILED, "closed")
        )
        if client is not None:
            try:
                await client.disconnect()
            except BleakError as err:
                _LOGGER.debug("%s: error while disconnecting: %s", self._name, err)

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        if client is not self._client:
            return  # stale client or our own disconnect
        _LOGGER.debug("%s: disconnected unexpectedly", self._name)
        self._handle_link_lost()

    def _handle_link_lost(self) -> None:
        self._client = None
        self.state.connected = False
        self._intent = MoveCode.STOP
        if (task := self._keepalive_task) is not None:
            self._keepalive_task = None
            if task is not asyncio.current_task():
                task.cancel()
        self._fail_waiters(
            MyLifterConnectionError(LastError.CONNECTION_FAILED, "connection lost")
        )
        if (motion := self._motion) is not None:
            _LOGGER.warning(
                "%s: connection lost while moving; the move is NOT resumed, "
                "sending a safety STOP",
                self._name,
            )
            self._fail_motion(motion, LastError.CONNECTION_LOST_WHILE_MOVING)
            self._create_task(self._safety_stop(), "safety stop")
        self._notify()

    async def _safety_stop(self) -> None:
        try:
            await self.async_stop()
        except MyLifterError as err:
            _LOGGER.error(
                "%s: safety STOP after connection loss failed: %s", self._name, err
            )

    # ------------------------------------------------------------- keepalive

    async def _keepalive_loop(self, client: BleakClientWithServiceCache) -> None:
        while client is self._client:
            try:
                await self._write(self._keepalive_frame, client)
            except MyLifterConnectionError as err:
                if client is self._client:
                    _LOGGER.debug("%s: keep-alive write failed: %s", self._name, err)
                    self._handle_link_lost()
                    self._create_task(self._close_client(client), "close client")
                return
            self._supervise_tick()
            await asyncio.sleep(
                self.timings.keepalive_idle
                if self._intent is MoveCode.STOP
                else self.timings.keepalive_moving
            )

    async def _close_client(self, client: BleakClientWithServiceCache) -> None:
        with contextlib.suppress(BleakError):
            await client.disconnect()

    async def _write(
        self,
        frame: bytes | Callable[[], bytes | None],
        client: BleakClientWithServiceCache | None = None,
    ) -> None:
        """Write one frame.

        ``frame`` may be a factory that is evaluated only once the write lock is
        held, so MOVE frames always carry the *current* intent and position echo.
        A frame built before STOP can therefore never be sent after it. A factory
        returning None skips the write.
        """
        client = client or self._client
        if client is None:
            raise MyLifterConnectionError(LastError.CONNECTION_FAILED, "not connected")
        async with self._write_lock:
            if callable(frame) and (frame := frame()) is None:
                return
            try:
                async with asyncio.timeout(self.timings.write_timeout):
                    await client.write_gatt_char(
                        COMMAND_CHAR_UUID, frame, response=False
                    )
            except (BleakError, TimeoutError) as err:
                raise MyLifterConnectionError(
                    LastError.CONNECTION_FAILED, f"write failed: {err}"
                ) from err
            await asyncio.sleep(self.timings.write_pause)

    def _keepalive_frame(self) -> bytes:
        return build_move_packet(
            self._intent,
            0 if self._intent is MoveCode.STOP else MAX_SPEED,
            self._echo(),
        )

    def _stop_frame(self) -> bytes:
        return build_move_packet(MoveCode.STOP, 0, self._echo())

    def _echo(self) -> int:
        return self.state.position if self.state.position is not None else 0

    # ---------------------------------------------------------- notifications

    def _on_notification(self, _sender: Any, data: bytearray) -> None:
        try:
            notification = parse_notification(bytes(data))
        except ProtocolError as err:
            _LOGGER.debug(
                "%s: ignoring frame %s: %s", self._name, bytes(data).hex(), err
            )
            return
        if isinstance(notification, MoveStatus):
            self._handle_status(notification)
        elif isinstance(notification, Version):
            self.state.firmware = notification.firmware
            self.state.hardware = notification.hardware
            self._notify()
        elif isinstance(notification, ProtocolVersion):
            self.state.protocol_version = notification.version
        for waiter in list(self._waiters):
            if waiter.future.done():
                continue
            if (result := waiter.predicate(notification)) is not None:
                waiter.future.set_result(result)

    def _handle_status(self, status: MoveStatus) -> None:
        self._status_count += 1
        changed = (
            status.position != self.state.position
            or status.weight != self.state.weight
            or status.error_code != self.state.device_error
        )
        self.state.position = status.position
        self.state.weight = status.weight
        self.state.device_error = status.error_code
        self.state.move_status = status.move_status
        if (motion := self._motion) is not None and motion.written:
            self._supervise_status(motion, status)
        if changed:
            self._notify()

    def _wait_for(
        self, predicate: Callable[[Notification], Any], timeout: float
    ) -> asyncio.Future[Any]:
        """Register a waiter before writing the request; await the returned future.

        The future fails with ``TimeoutError`` after ``timeout`` seconds and with a
        connection error if the session closes first.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        waiter = _Waiter(predicate, future)
        self._waiters.append(waiter)
        handle = loop.call_later(timeout, _fail_future, future, TimeoutError())

        def _done(_future: asyncio.Future[Any]) -> None:
            handle.cancel()
            if waiter in self._waiters:
                self._waiters.remove(waiter)

        future.add_done_callback(_done)
        return future

    def _fail_waiters(self, err: Exception) -> None:
        for waiter in list(self._waiters):
            _fail_future(waiter.future, err)

    async def _wait_for_status(self) -> None:
        if self._status_count:
            return
        try:
            await self._wait_for(_is_status, self.timings.status_timeout)
        except TimeoutError as err:
            raise MyLifterConnectionError(
                LastError.CONNECTION_FAILED, "no status received"
            ) from err

    async def _query_versions(self) -> None:
        for builder, predicate in (
            (build_get_version_packet, _is_version),
            (build_get_protocol_version_packet, _is_protocol_version),
        ):
            waiter = self._wait_for(predicate, self.timings.query_timeout)
            try:
                await self._write(builder())
                await waiter
            except (TimeoutError, MyLifterConnectionError) as err:
                _discard(waiter)
                _LOGGER.debug("%s: version query failed: %s", self._name, err)

    # ----------------------------------------------------------------- motion

    def _raise_if_cancelled(self, generation: int) -> None:
        if generation != self._stop_generation:
            raise _Cancelled

    async def _clear_benign_error(self, generation: int) -> None:
        code = self.state.device_error
        if code == DeviceError.NONE:
            return
        if code not in CLEARABLE_ERRORS:
            self._set_error(LastError.DEVICE_ERROR, describe_device_error(code))
            self._notify()
            raise MyLifterMoveError(LastError.DEVICE_ERROR, describe_device_error(code))
        _LOGGER.debug(
            "%s: clearing %s before move", self._name, describe_device_error(code)
        )
        waiter = self._wait_for(
            _is_status_without_error, self.timings.clear_error_timeout
        )
        try:
            await self._write(build_clear_error_packet())
            await waiter
        except TimeoutError:
            pass  # the move confirmation decides what happens next
        finally:
            _discard(waiter)

    async def _start_motion(self, code: MoveCode, generation: int) -> MoveResult:
        loop = asyncio.get_running_loop()
        start = self._echo()
        motion = _Motion(
            code=code,
            start_position=start,
            generation=generation,
            confirmed=loop.create_future(),
            last_position=start,
        )
        self._motion = motion
        self._intent = code
        self.state.moving = code
        self.state.last_motion_outcome = None
        self._notify()

        def _move_frame() -> bytes | None:
            # Skipped if STOP (or a disconnect) ended this move while waiting.
            if self._motion is not motion:
                return None
            return build_move_packet(code, MAX_SPEED, self._echo())

        try:
            await self._write(_move_frame)
        except MyLifterConnectionError as err:
            # The first and only write of this move failed: stop, report, do not resend.
            self._fail_motion(motion, LastError.MOVE_WRITE_FAILED)
            raise MyLifterMoveError(LastError.MOVE_WRITE_FAILED, str(err)) from err
        if self._motion is not motion:
            # STOP or a disconnect happened during the write.
            return await motion.confirmed
        motion.written = True
        motion.started = motion.last_change = _now()
        try:
            async with asyncio.timeout(self.timings.move_confirm_timeout + 1.0):
                return await asyncio.shield(motion.confirmed)
        except TimeoutError:
            # Backstop only; normally the keep-alive tick fails the move first.
            self._fail_motion(motion, LastError.MOVE_NOT_CONFIRMED)
            return await motion.confirmed

    def _supervise_status(self, motion: _Motion, status: MoveStatus) -> None:
        motion.last_error_code = status.error_code
        code = status.error_code
        if not motion.is_confirmed:
            if code == DeviceError.SMART_POINT_NOT_SET:
                self._fail_motion(
                    motion, LastError.SMART_POINT_NOT_SET, describe_device_error(code)
                )
                return
            if code not in LIMIT_CODES and code != DeviceError.NONE:
                self._fail_motion(
                    motion, LastError.DEVICE_ERROR, describe_device_error(code)
                )
                return
            if (
                abs(status.position - motion.start_position)
                >= self.timings.move_confirm_ticks
            ):
                motion.is_confirmed = True
                motion.last_position = status.position
                motion.last_change = _now()
                if not motion.confirmed.done():
                    motion.confirmed.set_result(MoveResult.CONFIRMED)
            return
        if code in LIMIT_CODES:
            self._end_motion(motion, MotionOutcome.COMPLETED)
            return
        if code != DeviceError.NONE:
            self._fail_motion(
                motion, LastError.DEVICE_ERROR, describe_device_error(code)
            )
            return
        if status.position != motion.last_position:
            motion.last_position = status.position
            motion.last_change = _now()

    def _supervise_tick(self) -> None:
        motion = self._motion
        if motion is None or not motion.written:
            return
        now = _now()
        if not motion.is_confirmed:
            if now - motion.started >= self.timings.move_confirm_timeout:
                if motion.last_error_code in LIMIT_CODES:
                    self._end_motion(motion, MotionOutcome.AT_LIMIT)
                else:
                    self._fail_motion(motion, LastError.MOVE_NOT_CONFIRMED)
            return
        if now - motion.started >= self.timings.max_move_duration:
            self._fail_motion(motion, LastError.MOVE_TIMEOUT)
        elif now - motion.last_change >= self.timings.stall_time:
            self._end_motion(motion, MotionOutcome.COMPLETED)

    def _end_motion(self, motion: _Motion, outcome: MotionOutcome) -> None:
        """Finish a move without error (natural end, at limit, or STOP)."""
        if self._motion is not motion:
            return
        self._motion = None
        self._intent = MoveCode.STOP
        self.state.moving = None
        self.state.last_motion_outcome = outcome
        if outcome in (MotionOutcome.COMPLETED, MotionOutcome.AT_LIMIT):
            self.state.last_error = LastError.NONE
            self.state.last_error_detail = None
        if not motion.confirmed.done():
            motion.confirmed.set_result(
                MoveResult.AT_LIMIT
                if outcome is MotionOutcome.AT_LIMIT
                else MoveResult.CANCELLED
            )
        if outcome is not MotionOutcome.STOPPED:
            self._send_stop_soon()
            self._schedule_idle_disconnect(self.timings.idle_disconnect)
        _LOGGER.debug("%s: move %s ended: %s", self._name, motion.code.name, outcome)
        self._notify()
        if self._on_motion_end is not None and motion.written:
            self._on_motion_end(motion.code, outcome, self.state.position)

    def _fail_motion(
        self, motion: _Motion, reason: LastError, detail: str | None = None
    ) -> None:
        """Abort a move: intent STOP, record the error, never resend."""
        if self._motion is not motion:
            return
        self._motion = None
        self._intent = MoveCode.STOP
        self.state.moving = None
        self.state.last_motion_outcome = MotionOutcome.FAILED
        self._set_error(reason, detail)
        _LOGGER.warning(
            "%s: move %s aborted (%s)%s",
            self._name,
            motion.code.name,
            reason.value,
            f": {detail}" if detail else "",
        )
        if not motion.confirmed.done():
            motion.confirmed.set_exception(MyLifterMoveError(reason, detail))
            # Mark retrieved: the caller may already have stopped waiting.
            motion.confirmed.exception()
        self._send_stop_soon()
        self._schedule_idle_disconnect(self.timings.idle_disconnect)
        self._notify()
        if self._on_motion_end is not None and motion.written:
            self._on_motion_end(motion.code, MotionOutcome.FAILED, self.state.position)

    def _send_stop_soon(self) -> None:
        """Send an immediate STOP frame in addition to the STOP keep-alive."""
        if self._client is None:
            return

        async def _stop_frame() -> None:
            try:
                await self._write(self._stop_frame)
            except MyLifterConnectionError as err:
                _LOGGER.debug("%s: immediate STOP frame failed: %s", self._name, err)

        self._create_task(_stop_frame(), "stop frame")

    def _set_error(self, reason: LastError, detail: str | None = None) -> None:
        self.state.last_error = reason
        self.state.last_error_detail = detail

    def record_error(
        self, reason: LastError, detail: str | None = None, *, notify: bool = True
    ) -> None:
        """Record an error from a higher layer (e.g. connection failure, group abort)."""
        self._set_error(reason, detail)
        if notify:
            self._notify()

    # ------------------------------------------------------------ utilities

    def _schedule_idle_disconnect(self, delay: float) -> None:
        if self._closed:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        if self._idle_handle is not None and self._idle_deadline >= deadline:
            return
        self._cancel_idle_disconnect()
        self._idle_deadline = deadline
        self._idle_handle = loop.call_at(deadline, self._idle_timeout)

    def _cancel_idle_disconnect(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
            self._idle_deadline = 0.0

    def _idle_timeout(self) -> None:
        self._idle_handle = None
        self._idle_deadline = 0.0
        if self._idle_allowed():
            self._create_task(self._idle_disconnect(), "idle disconnect")

    def _idle_allowed(self) -> bool:
        return self._motion is None and self._in_use == 0 and self._client is not None

    async def _idle_disconnect(self) -> None:
        # Re-checked synchronously right before closing: a move/poll/STOP that
        # started meanwhile keeps the session.
        if not self._idle_allowed():
            return
        await self._drop_session()
        self._notify()

    def _create_task(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=f"mylifter {name} {self.address}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _notify(self) -> None:
        if self._on_update is not None:
            self._on_update()


def _fail_future(future: asyncio.Future[Any], err: BaseException) -> None:
    """Fail a waiter future; mark the exception retrieved (callers may be gone)."""
    if not future.done():
        future.set_exception(err)
        future.exception()


def _discard(future: asyncio.Future[Any]) -> None:
    """Cancel a waiter we no longer need and swallow its outcome."""
    if future.done():
        if not future.cancelled():
            future.exception()
    else:
        future.cancel()


def _auth_result(notification: Notification) -> str | None:
    if isinstance(notification, Ack) and notification.command == CommandCode.PASSKEY:
        return "ack"
    if isinstance(notification, Nack) and notification.command == CommandCode.PASSKEY:
        return "nack"
    if isinstance(notification, MoveStatus):
        return "status"
    return None


def _is_status(notification: Notification) -> MoveStatus | None:
    return notification if isinstance(notification, MoveStatus) else None


def _is_status_without_error(notification: Notification) -> MoveStatus | None:
    if isinstance(notification, MoveStatus) and notification.error_code == 0:
        return notification
    return None


def _is_version(notification: Notification) -> Version | None:
    return notification if isinstance(notification, Version) else None


def _is_protocol_version(notification: Notification) -> ProtocolVersion | None:
    return notification if isinstance(notification, ProtocolVersion) else None


async def async_pair(
    ble_device: BLEDevice,
    *,
    name: str = "MyLifter",
    timings: Timings = DEFAULT_TIMINGS,
) -> bytes:
    """Retrieve the passkey. The user must press the button on the motor.

    Connects, sends GET_PASSKEY, waits for the passkey notification, confirms it with
    SET_PASSKEY and disconnects. No move frames are sent.
    """
    loop = asyncio.get_running_loop()
    passkey_future: asyncio.Future[bytes] = loop.create_future()
    ack_future: asyncio.Future[bool] = loop.create_future()

    def _handler(_sender: Any, data: bytearray) -> None:
        try:
            notification = parse_notification(bytes(data))
        except ProtocolError:
            return
        if isinstance(notification, Passkey) and not passkey_future.done():
            passkey_future.set_result(notification.passkey)
        elif (
            isinstance(notification, (Ack, Nack))
            and notification.command == CommandCode.PASSKEY
            and passkey_future.done()
            and not ack_future.done()
        ):
            ack_future.set_result(isinstance(notification, Ack))

    try:
        client = await establish_connection(
            BleakClientWithServiceCache, ble_device, name, max_attempts=CONNECT_ATTEMPTS
        )
    except BleakOutOfConnectionSlotsError as err:
        raise MyLifterConnectionError(LastError.NO_CONNECTION_SLOT, str(err)) from err
    except (BleakError, TimeoutError) as err:
        raise MyLifterConnectionError(LastError.CONNECTION_FAILED, str(err)) from err
    try:
        await client.start_notify(RESPONSE_CHAR_UUID, _handler)
        await client.write_gatt_char(
            COMMAND_CHAR_UUID, build_get_passkey_packet(), response=False
        )
        try:
            async with asyncio.timeout(timings.pairing_timeout):
                passkey = await passkey_future
        except TimeoutError as err:
            raise MyLifterPairingTimeout from err
        await client.write_gatt_char(
            COMMAND_CHAR_UUID, build_set_passkey_packet(passkey), response=False
        )
        try:
            async with asyncio.timeout(timings.ack_timeout):
                accepted = await ack_future
        except TimeoutError:
            accepted = True  # ACK is optional per PROTOCOL.md
            _LOGGER.debug("No ACK for SET_PASSKEY during pairing (treated as optional)")
        if not accepted:
            raise MyLifterAuthError("device rejected its own passkey")
    except BleakError as err:
        raise MyLifterConnectionError(LastError.CONNECTION_FAILED, str(err)) from err
    finally:
        with contextlib.suppress(BleakError):
            await client.disconnect()
    return passkey
