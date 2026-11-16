"""Protocol encode/decode, using the examples and layouts from PyLifter's PROTOCOL.md."""

import struct

import pytest

from custom_components.mylifter.protocol import (
    Ack,
    CommandCode,
    MoveCode,
    MoveStatus,
    Nack,
    Passkey,
    ProtocolError,
    ProtocolVersion,
    SmartPointCode,
    Stats,
    Unknown,
    Version,
    build_clear_error_packet,
    build_clear_smart_point_packet,
    build_get_passkey_packet,
    build_get_version_packet,
    build_move_packet,
    build_packet,
    build_set_passkey_packet,
    build_set_smart_point_packet,
    describe_device_error,
    parse_move_response,
    parse_notification,
)


def test_packet_structure() -> None:
    """[Command Code (1 byte)][Payload Length (1 byte)][Payload (N bytes)]."""
    assert build_packet(0x34) == b"\x34\x00"
    assert build_packet(0x09, b"abc") == b"\x09\x03abc"
    with pytest.raises(ValueError):
        build_packet(0x09, bytes(256))


def test_move_packet_little_endian() -> None:
    """Move: move_code uint8, speed uint8, average_position_data int32 LE."""
    assert build_move_packet(MoveCode.STOP, 0, 0) == bytes.fromhex("2306000000000000")
    # Echoing a negative position (PROTOCOL.md mentions -4000/-5000).
    assert build_move_packet(MoveCode.SMART_UP, 100, -5000) == bytes(
        [0x23, 6, 3, 100]
    ) + struct.pack("<i", -5000)
    assert build_move_packet(MoveCode.SMART_DOWN, 100, 1234)[2] == 4
    with pytest.raises(ValueError):
        build_move_packet(MoveCode.UP, 101)


def test_move_codes_match_spec() -> None:
    """MoveCode enum values from PROTOCOL.md."""
    assert [c.value for c in MoveCode] == list(range(9))
    assert MoveCode.SMART_UP == 3
    assert MoveCode.SMART_DOWN == 4


def test_passkey_packets() -> None:
    """Get Passkey has 0 input bytes, Set Passkey 6, both use 0x03."""
    assert build_get_passkey_packet() == b"\x03\x00"
    key = bytes.fromhex("85f14a7953b3")
    assert build_set_passkey_packet(key) == b"\x03\x06" + key
    with pytest.raises(ValueError):
        build_set_passkey_packet(b"\x01\x02")


def test_other_builders() -> None:
    """Smart point, clear error and version builders."""
    assert build_set_smart_point_packet(SmartPointCode.TOP) == b"\x32\x01\x01"
    assert build_clear_smart_point_packet(SmartPointCode.BOTTOM) == b"\x33\x01\x02"
    assert build_clear_error_packet() == b"\x06\x00"
    assert build_get_version_packet() == b"\x0a\x00"


def test_parse_move_response() -> None:
    """Move response: move_status u8, error_code u8, position i32, weight u16."""
    payload = struct.pack("<BBiH", 1, 0x81, -4000, 513)
    assert parse_move_response(payload) == MoveStatus(1, 0x81, -4000, 513)
    assert parse_notification(b"\x23\x08" + payload) == MoveStatus(1, 0x81, -4000, 513)
    # Weight is unsigned (as decoded by upstream's client).
    payload = struct.pack("<BBiH", 0, 0, 0, 0xFFFF)
    assert parse_move_response(payload).weight == 65535
    with pytest.raises(ProtocolError):
        parse_move_response(b"\x00" * 7)


def test_parse_ack_nack_passkey() -> None:
    """ACK, NACK and the passkey notification."""
    assert parse_notification(b"\x01\x01\x03") == Ack(CommandCode.PASSKEY)
    assert parse_notification(b"\x00\x02\x03\x05") == Nack(0x03, 5)
    assert parse_notification(b"\x00\x01\x03") == Nack(0x03, None)
    key = bytes.fromhex("11364686966b")
    assert parse_notification(b"\x03\x06" + key) == Passkey(key)
    with pytest.raises(ProtocolError):
        parse_notification(b"\x03\x02\x01\x02")
    with pytest.raises(ProtocolError):
        parse_notification(b"\x01\x00")


def test_parse_version() -> None:
    """Bytes 7,6 = 3,2 -> v3.2 (PROTOCOL.md example)."""
    payload = bytes([1, 2, 3, 0, 0, 0, 2, 3])
    version = parse_notification(b"\x0a\x08" + payload)
    assert isinstance(version, Version)
    assert version.firmware == "3.2"
    assert version.hardware == "2.1.3"
    with pytest.raises(ProtocolError):
        parse_notification(b"\x0a\x02\x01\x02")


def test_parse_protocol_version() -> None:
    """Nibble encoded: 0x41 -> 4.1."""
    result = parse_notification(b"\x05\x01\x41")
    assert result == ProtocolVersion(0x41)
    assert result.version == "4.1"


def test_parse_stats() -> None:
    """Stats: cycles u16, time u32, min/max temp u16, resets u16, errors u16, classes u32."""
    payload = struct.pack("<HIHHHHI", 10, 3600, 20, 40, 2, 1, 0x80)
    assert parse_notification(b"\x34\x12" + payload) == Stats(
        10, 3600, 20, 40, 2, 1, 0x80
    )


def test_parse_unknown_and_short() -> None:
    """Unknown commands are passed through; truncated frames rejected."""
    assert parse_notification(b"\x42\x01\x07") == Unknown(0x42, b"\x07")
    with pytest.raises(ProtocolError):
        parse_notification(b"\x23")


def test_describe_errors() -> None:
    """Known and unknown device error codes."""
    assert describe_device_error(0x09).startswith("sync")
    assert describe_device_error(0x83) == "smart point not set"
    assert describe_device_error(0x42) == "unknown device error 0x42"
