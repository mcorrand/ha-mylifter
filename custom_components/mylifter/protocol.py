"""MyLifter BLE protocol: constants, frame builders and notification parsers.

Originally from PyLifter (https://github.com/sblu/PyLifter), files
``PyLifter/pylifter/protocol.py`` and the notification parsing in
``PyLifter/pylifter/client.py``, upstream commit
029abf219ff47dc0a836f3adef492b44b4846e3e.

Copyright (C) 2025-2026 sblu and PyLifter contributors
Copyright (C) 2026 ha-mylifter contributors

This program is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. This program is
distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even
the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details: <https://www.gnu.org/licenses/>.

MODIFIED for ha-mylifter:
- Ported to Python 3.14 typing, docstrings and HA code style; dropped star exports.
- Notification parsing moved out of ``PyLifterClient._notification_handler`` into
  pure functions returning typed dataclasses; payload sizes are validated.
- ``parse_move_response`` decodes weight as unsigned (``H``) to match the decoding
  actually used by upstream's client (upstream protocol.py used signed ``h``).
- Override (``GO_OVERRIDE``) and factory-calibration frame builders were not vendored:
  they bypass limits and are not used by this integration.
- Added ``build_get_passkey_packet``, ``build_set_passkey_packet``,
  ``build_clear_error_packet`` and query builders.
- Added device error code descriptions.
"""

from dataclasses import dataclass
from enum import IntEnum
import struct

# UUIDs
MYLIFTER_SERVICE_UUID = "2d88fb13-e261-4eb9-934b-5a4fea3e3b25"
COMMAND_CHAR_UUID = "a886c7ec-31ee-48d6-9aa8-35291b21780f"
RESPONSE_CHAR_UUID = "00eff2b2-e420-4d23-9bdd-802af59aeb6f"

PASSKEY_LENGTH = 6
MAX_SPEED = 100


class CommandCode(IntEnum):
    """Command codes (first byte of every frame)."""

    NACK = 0x00
    ACK = 0x01
    PASSKEY = 0x03  # GET (empty payload) and SET (6-byte payload) share the code
    GET_PROTOCOL_VERSION = 0x05
    CLEAR_ERROR = 0x06
    GET_NAME = 0x08
    SET_NAME = 0x09
    GET_VERSION = 0x0A
    MOVE = 0x23
    GO_OVERRIDE = 0x25
    CALIBRATE = 0x32
    CLEAR_CALIBRATION = 0x33
    GET_STATS = 0x34
    GET_LINK_INFO = 0x40
    CLEAR_LINK_INFO = 0x41
    GET_LINK_NAME = 0x42
    SET_LINK_NAME = 0x43
    GET_LINK_ITEM = 0x44
    SET_LINK_ITEM = 0x45
    FIRMWARE_FILE_START = 0x50
    FIRMWARE_FILE_BLOCK_START = 0x51
    FIRMWARE_FILE_BLOCK_DATA = 0x52
    FIRMWARE_FILE_VALIDATE_CHECK = 0x53
    FIRMWARE_FILE_FINALIZE = 0x54
    FIRMWARE_FILE_ABORT = 0x55
    FACTORY_CALIBRATE = 0xFA


class MoveCode(IntEnum):
    """Move codes carried in a MOVE frame."""

    STOP = 0
    UP = 1
    DOWN = 2
    SMART_UP = 3
    SMART_DOWN = 4
    MOVE_REFERENCE = 5
    STOP_ERROR = 6
    OVERRIDE_UP = 7
    OVERRIDE_DOWN = 8


class SmartPointCode(IntEnum):
    """Smart point (calibration) codes."""

    REFERENCE = 0
    TOP = 1
    BOTTOM = 2


class DeviceError(IntEnum):
    """Known error codes reported in the MOVE response."""

    NONE = 0x00
    SYNC = 0x09
    SOFT_LIMIT = 0x81
    SMART_POINT_NOT_SET = 0x83
    END_OF_TRAVEL = 0x86


DEVICE_ERROR_DESCRIPTIONS: dict[int, str] = {
    DeviceError.NONE: "no error",
    DeviceError.SYNC: "sync error (position echo mismatch)",
    DeviceError.SOFT_LIMIT: "soft limit reached",
    DeviceError.SMART_POINT_NOT_SET: "smart point not set",
    DeviceError.END_OF_TRAVEL: "hard limit / end of travel",
}


def describe_device_error(code: int) -> str:
    """Return a human readable description of a device error code."""
    return DEVICE_ERROR_DESCRIPTIONS.get(code, f"unknown device error 0x{code:02X}")


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------


def build_packet(command_code: int, payload: bytes = b"") -> bytes:
    """Construct a frame: ``[command (1B)][payload length (1B)][payload]``."""
    if len(payload) > 0xFF:
        raise ValueError("payload too long")
    return struct.pack("BB", command_code, len(payload)) + payload


def build_move_packet(
    move_code: MoveCode, speed: int = MAX_SPEED, avg_pos: int = 0
) -> bytes:
    """Construct a MOVE frame.

    Payload: ``[move code (1B)][speed (1B)][echoed position (int32 LE)]``.
    The echoed position must be the last position reported by the device.
    """
    if not 0 <= speed <= MAX_SPEED:
        raise ValueError("speed must be 0-100")
    return build_packet(
        CommandCode.MOVE, struct.pack("<BBi", move_code, speed, avg_pos)
    )


def build_get_passkey_packet() -> bytes:
    """Construct a GET_PASSKEY frame (empty payload)."""
    return build_packet(CommandCode.PASSKEY)


def build_set_passkey_packet(passkey: bytes) -> bytes:
    """Construct a SET_PASSKEY frame (6-byte payload)."""
    if len(passkey) != PASSKEY_LENGTH:
        raise ValueError("passkey must be 6 bytes")
    return build_packet(CommandCode.PASSKEY, passkey)


def build_clear_error_packet() -> bytes:
    """Construct a CLEAR_ERROR frame."""
    return build_packet(CommandCode.CLEAR_ERROR)


def build_get_version_packet() -> bytes:
    """Construct a GET_VERSION frame."""
    return build_packet(CommandCode.GET_VERSION)


def build_get_protocol_version_packet() -> bytes:
    """Construct a GET_PROTOCOL_VERSION frame."""
    return build_packet(CommandCode.GET_PROTOCOL_VERSION)


def build_get_stats_packet() -> bytes:
    """Construct a GET_STATS frame."""
    return build_packet(CommandCode.GET_STATS)


def build_set_smart_point_packet(point: SmartPointCode) -> bytes:
    """Construct a CALIBRATE (set smart point) frame."""
    return build_packet(CommandCode.CALIBRATE, struct.pack("B", point))


def build_clear_smart_point_packet(point: SmartPointCode) -> bytes:
    """Construct a CLEAR_CALIBRATION (clear smart point) frame."""
    return build_packet(CommandCode.CLEAR_CALIBRATION, struct.pack("B", point))


# ---------------------------------------------------------------------------
# Notification parsing
# ---------------------------------------------------------------------------


class ProtocolError(ValueError):
    """A notification could not be decoded."""


@dataclass(frozen=True, slots=True)
class MoveStatus:
    """Response to a MOVE frame."""

    move_status: int
    error_code: int
    position: int
    weight: int


@dataclass(frozen=True, slots=True)
class Ack:
    """Generic acknowledgement."""

    command: int


@dataclass(frozen=True, slots=True)
class Nack:
    """Generic negative acknowledgement."""

    command: int
    reason: int | None


@dataclass(frozen=True, slots=True)
class Passkey:
    """Passkey sent by the device after the button was pressed."""

    passkey: bytes


@dataclass(frozen=True, slots=True)
class Version:
    """Hardware and firmware version."""

    hw_major: int
    hw_minor: int
    hw_version: int
    fw_major: int
    fw_minor: int

    @property
    def firmware(self) -> str:
        """Firmware as shown by the official app, e.g. ``3.1``."""
        return f"{self.fw_major}.{self.fw_minor}"

    @property
    def hardware(self) -> str:
        """Hardware version string."""
        return f"{self.hw_major}.{self.hw_minor}.{self.hw_version}"


@dataclass(frozen=True, slots=True)
class ProtocolVersion:
    """Protocol version (nibble encoded, 0x41 -> 4.1)."""

    raw: int

    @property
    def version(self) -> str:
        """Version string."""
        return f"{(self.raw >> 4) & 0x0F}.{self.raw & 0x0F}"


@dataclass(frozen=True, slots=True)
class Stats:
    """Usage statistics."""

    total_cycles: int
    total_time: int
    min_raw_temp: int
    max_raw_temp: int
    reset_count: int
    error_count: int
    error_classes: int


@dataclass(frozen=True, slots=True)
class Unknown:
    """A frame this integration does not decode."""

    command: int
    payload: bytes


type Notification = (
    MoveStatus | Ack | Nack | Passkey | Version | ProtocolVersion | Stats | Unknown
)


def parse_move_response(payload: bytes) -> MoveStatus:
    """Parse the 8-byte MOVE response payload (without command/length bytes)."""
    if len(payload) < 8:
        raise ProtocolError(f"MOVE response too short: {len(payload)} bytes")
    move_status, error_code, position, weight = struct.unpack_from("<BBiH", payload)
    return MoveStatus(move_status, error_code, position, weight)


def parse_notification(data: bytes) -> Notification:
    """Decode a notification frame received on the response characteristic."""
    if len(data) < 2:
        raise ProtocolError(f"frame too short: {data.hex()}")
    # Like upstream, the payload is everything after the command and length bytes; the
    # length byte is not enforced because firmware behaviour is only partly verified.
    command = data[0]
    payload = bytes(data[2:])

    match command:
        case CommandCode.MOVE:
            return parse_move_response(payload)
        case CommandCode.ACK:
            if not payload:
                raise ProtocolError("ACK without command")
            return Ack(payload[0])
        case CommandCode.NACK:
            if not payload:
                raise ProtocolError("NACK without command")
            return Nack(payload[0], payload[1] if len(payload) > 1 else None)
        case CommandCode.PASSKEY:
            if len(payload) < PASSKEY_LENGTH:
                raise ProtocolError("passkey frame too short")
            return Passkey(payload[:PASSKEY_LENGTH])
        case CommandCode.GET_VERSION:
            if len(payload) < 8:
                raise ProtocolError("version frame too short")
            hw_min, hw_maj, hw_ver, _tag, _unk, fw_min, fw_maj = struct.unpack_from(
                "<BBBBHBB", payload
            )
            return Version(hw_maj, hw_min, hw_ver, fw_maj, fw_min)
        case CommandCode.GET_PROTOCOL_VERSION:
            if not payload:
                raise ProtocolError("protocol version frame empty")
            return ProtocolVersion(payload[0])
        case CommandCode.GET_STATS:
            if len(payload) < 18:
                raise ProtocolError("stats frame too short")
            return Stats(*struct.unpack_from("<HIHHHHI", payload))
    return Unknown(command, payload)
