"""A simulated MyLifter motor and BLE client, used instead of any real Bluetooth.

The simulator follows PyLifter's PROTOCOL.md:
- GET_PASSKEY (0x03, empty) is answered with the passkey only once the button is pressed.
- SET_PASSKEY (0x03 + 6 bytes) is answered with ACK (0x01 [0x03]) or NACK.
- Every MOVE frame (0x23) is answered with a status frame
  ``[0x23][8][status][error][position int32][weight uint16]``.
- The echoed position must match the device position, otherwise error 0x09 (sync).
- SMART_UP/SMART_DOWN move towards the top/bottom smart point (position increases
  upwards); 0x81 is reported on arrival, 0x83 if the smart points are not set.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import struct
from typing import Any

from bleak.exc import BleakError

from custom_components.mylifter.protocol import (
    COMMAND_CHAR_UUID,
    CommandCode,
    DeviceError,
    MoveCode,
)

PASSKEY = bytes.fromhex("85f14a7953b3")


@dataclass
class SentMove:
    """A MOVE frame received by the fake device."""

    session: int
    code: MoveCode
    speed: int
    echo: int


@dataclass
class FakeMyLifter:
    """State and behaviour of one simulated motor."""

    address: str
    passkey: bytes = PASSKEY
    position: int = 1000
    weight: int = 42
    error: int = 0
    top: int = 2000
    bottom: int = 0
    smart_points_set: bool = True
    step: int = 100  # ticks per MOVE frame while moving
    stuck: bool = False  # motor does not move (move cannot be confirmed)
    button_pressed: bool = False
    reject_passkey: bool = False
    ack_passkey: bool = True
    respond: bool = True
    sticky_error: int | None = None  # error that CLEAR_ERROR cannot clear
    firmware: tuple[int, int] = (3, 1)
    connect_error: Exception | None = None
    connect_delay: float = 0.0
    fail_write_codes: set[MoveCode] = field(default_factory=set)
    write_delay: float = 0.0  # slow GATT writes (e.g. a busy proxy)

    sessions: int = 0
    frames: list[bytes] = field(default_factory=list)
    moves: list[SentMove] = field(default_factory=list)
    client: FakeBleakClient | None = None
    passkey_requested: bool = False
    authenticated: bool = False

    # ------------------------------------------------------------ helpers

    def press_button(self) -> None:
        """Simulate pressing the physical button."""
        self.button_pressed = True
        if self.passkey_requested and self.client:
            self.passkey_requested = False
            self.client.notify(bytes([CommandCode.PASSKEY, 6]) + self.passkey)

    def drop_connection(self) -> None:
        """Simulate the BLE link breaking (proxy out of range, power loss…)."""
        if self.client:
            self.client.drop()

    def moves_in_session(self, session: int) -> list[SentMove]:
        """MOVE frames received in a given session."""
        return [m for m in self.moves if m.session == session]

    def smart_moves(self) -> list[SentMove]:
        """MOVE frames that carried a smart move code."""
        return [
            m for m in self.moves if m.code in (MoveCode.SMART_UP, MoveCode.SMART_DOWN)
        ]

    def status_frame(self) -> bytes:
        """Encode the current status."""
        return bytes([CommandCode.MOVE, 8]) + struct.pack(
            "<BBiH", 0, self.error, self.position, self.weight
        )

    # ------------------------------------------------------------ protocol

    def receive(self, client: FakeBleakClient, frame: bytes) -> list[bytes]:
        """Process a written frame and return the notifications to send."""
        self.frames.append(frame)
        command, payload = frame[0], frame[2:]
        if command == CommandCode.PASSKEY and not payload:
            if self.button_pressed:
                return [bytes([CommandCode.PASSKEY, 6]) + self.passkey]
            self.passkey_requested = True
            return []
        if command == CommandCode.PASSKEY:
            if payload != self.passkey or self.reject_passkey:
                return [bytes([CommandCode.NACK, 2, CommandCode.PASSKEY, 1])]
            self.authenticated = True
            return (
                [bytes([CommandCode.ACK, 1, CommandCode.PASSKEY])]
                if self.ack_passkey
                else []
            )
        if not self.authenticated:
            return []
        if command == CommandCode.CLEAR_ERROR:
            self.error = self.sticky_error or 0
            return []
        if command == CommandCode.GET_VERSION:
            fw_major, fw_minor = self.firmware
            return [
                bytes(
                    [CommandCode.GET_VERSION, 8, 1, 2, 3, 0, 0, 0, fw_minor, fw_major]
                )
            ]
        if command == CommandCode.GET_PROTOCOL_VERSION:
            return [bytes([CommandCode.GET_PROTOCOL_VERSION, 1, 0x41])]
        if command == CommandCode.MOVE:
            code_raw, speed, echo = struct.unpack("<BBi", payload)
            code = MoveCode(code_raw)
            self.moves.append(SentMove(client.session, code, speed, echo))
            self._apply_move(code, echo)
            return [self.status_frame()] if self.respond else []
        return []

    def _apply_move(self, code: MoveCode, echo: int) -> None:
        if echo != self.position:
            self.error = DeviceError.SYNC
            return
        if self.sticky_error:
            self.error = self.sticky_error
            return
        if code not in (MoveCode.SMART_UP, MoveCode.SMART_DOWN):
            return
        if not self.smart_points_set:
            self.error = DeviceError.SMART_POINT_NOT_SET
            return
        if self.stuck:
            return
        target = self.top if code is MoveCode.SMART_UP else self.bottom
        if self.position == target:
            self.error = DeviceError.SOFT_LIMIT
            return
        self.error = 0
        if code is MoveCode.SMART_UP:
            self.position = min(self.position + self.step, target)
        else:
            self.position = max(self.position - self.step, target)
        if self.position == target:
            self.error = DeviceError.SOFT_LIMIT


class FakeBleakClient:
    """Implements the subset of BleakClient used by the integration."""

    def __init__(
        self,
        device: FakeMyLifter,
        disconnected_callback: Callable[[Any], None] | None,
    ) -> None:
        """Initialize."""
        self.device = device
        self._disconnected_callback = disconnected_callback
        self._notify_callback: Callable[[Any, bytearray], None] | None = None
        self.is_connected = True
        device.sessions += 1
        self.session = device.sessions
        device.client = self
        device.authenticated = False
        device.passkey_requested = False

    async def start_notify(
        self, uuid: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        """Subscribe to notifications."""
        self._notify_callback = callback

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = False
    ) -> None:
        """Write a frame."""
        assert uuid == COMMAND_CHAR_UUID
        if not self.is_connected:
            raise BleakError("Not connected")
        frame = bytes(data)
        if (
            frame[0] == CommandCode.MOVE
            and MoveCode(frame[2]) in self.device.fail_write_codes
        ):
            raise BleakError("write failed")
        await asyncio.sleep(self.device.write_delay)
        for notification in self.device.receive(self, frame):
            self.notify(notification)

    def notify(self, data: bytes) -> None:
        """Deliver a notification asynchronously."""
        if self._notify_callback is None or not self.is_connected:
            return
        callback = self._notify_callback
        asyncio.get_running_loop().call_soon(
            lambda: self.is_connected and callback(None, bytearray(data))
        )

    async def disconnect(self) -> None:
        """Disconnect (bleak also calls the disconnected callback)."""
        if not self.is_connected:
            return
        self.is_connected = False
        if self.device.client is self:
            self.device.client = None
        if self._disconnected_callback:
            self._disconnected_callback(self)

    def drop(self) -> None:
        """Link loss."""
        self.is_connected = False
        if self.device.client is self:
            self.device.client = None
        if self._disconnected_callback:
            self._disconnected_callback(self)


class FakeBluetooth:
    """Registry of fake motors plus a fake ``establish_connection``."""

    def __init__(self) -> None:
        """Initialize."""
        self.devices: dict[str, FakeMyLifter] = {}

    def add(self, address: str, **kwargs: Any) -> FakeMyLifter:
        """Add a fake motor."""
        device = FakeMyLifter(address=address, **kwargs)
        self.devices[address] = device
        return device

    async def establish_connection(
        self,
        client_class: type,
        ble_device: Any,
        name: str,
        disconnected_callback: Callable[[Any], None] | None = None,
        **kwargs: Any,
    ) -> FakeBleakClient:
        """Stand-in for bleak_retry_connector.establish_connection."""
        device = self.devices[ble_device.address]
        if device.connect_delay:
            await asyncio.sleep(device.connect_delay)
        if device.connect_error is not None:
            raise device.connect_error
        return FakeBleakClient(device, disconnected_callback)
