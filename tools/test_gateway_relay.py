#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure unit tests for gateway/mp_protocol.py (T_MsgHdr, no transport).

Every packet here is a byte fixture assembled in this file.  Nothing starts
FlightGear or FGMS, nothing binds a port, no TLS/WS/UDP socket is created and
no simulator is involved: a green run shows only that the header rules in
gateway/mp_protocol.py behave as documented, which is far from relay acceptance
(TASK5) and says nothing about a real FGMS or two real fgfs clients.

The purity guards below belong to this protocol module and must stay here even
though gateway/relay.py now owns the asyncio transport: the protocol layer keeps
its no-I/O contract, and the transport is tested separately in
tools/test_gateway_relay_transport.py.

Position fixtures are declared opaque filler on purpose.  They are NOT native
FGMS/FG position messages and must never be described as valid T_PositionMsg
payloads: the C++ layout/padding is not reproduced anywhere in Python
(docs/SPACE_ARENA_MP_GATEWAY_CONTRACT.md).  The tests even accept arbitrary
POS body lengths, which is only possible because no position codec exists.
"""
import ast
import dataclasses
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway import mp_protocol  # noqa: E402  (path set above on purpose)
from gateway.mp_protocol import (  # noqa: E402
    ALLOWED_MSG_IDS,
    CALLSIGN_FIELD_BYTES,
    CHAT_BODY_BYTES,
    HEADER_SIZE,
    HEADER_STRUCT,
    MAGIC_FGFS,
    MAX_PACKET_SIZE,
    MSG_CHAT,
    MSG_POS,
    PROTOCOL_VERSION,
    RELAY_MAGIC,
    ERR_BODY,
    ERR_CALLSIGN,
    ERR_CALLSIGN_MISMATCH,
    ERR_CHAT_BODY,
    ERR_EXPECTED_CALLSIGN,
    ERR_LENGTH,
    ERR_MAGIC,
    ERR_MSG_ID,
    ERR_MSG_LEN,
    ERR_REPLY_PORT,
    ERR_TYPE,
    ERR_VERSION,
    Header,
    PacketError,
    validate_packet,
)

SOURCE = (ROOT / "gateway" / "mp_protocol.py").read_text(encoding="utf-8")
CALLSIGN = "PIL001"

# Opaque filler ONLY: 64 bytes that prove the relay keeps a body un-parsed.
# This is deliberately NOT a native FGMS/FG position message and no claim is
# made about T_PositionMsg field order or padding.
NOT_AN_FG_POSITION_BODY = b"\xde\xad\xbe\xef" * 16
CHAT_TEXT = b"arena check-in"
# The pinned client sends one fixed Text[256] buffer per chat datagram; the
# relay never decodes it, so the content below is irrelevant to validation.
CHAT_BODY = CHAT_TEXT + b"\x00" * (CHAT_BODY_BYTES - len(CHAT_TEXT))

# Import surface a pure protocol module may use; anything I/O related is a bug.
ALLOWED_IMPORTS = frozenset({"__future__", "dataclasses", "hmac", "re", "struct"})
FORBIDDEN_IMPORTS = frozenset((
    "asyncio", "socket", "ssl", "select", "threading", "multiprocessing",
    "subprocess", "ctypes", "os", "sys", "logging", "websockets", "shutil",
))


def name_bytes(name, filler=b"\x00"):
    """Name[8] as the pinned client writes it: text, NUL, then `filler` padding."""
    raw = name.encode("ascii") if isinstance(name, str) else bytes(name)
    if len(raw) > CALLSIGN_FIELD_BYTES:
        raise AssertionError("test misuse: Name[8] cannot hold more bytes")
    return raw + filler * (CALLSIGN_FIELD_BYTES - len(raw))


_UNSET = object()


def default_body(msg_id):
    return CHAT_BODY if msg_id == MSG_CHAT else NOT_AN_FG_POSITION_BODY


def build_packet(msg_id=MSG_POS, *, magic=MAGIC_FGFS, version=PROTOCOL_VERSION,
                 msg_len=_UNSET, radar_range=0, reply_port=0, name=None,
                 body=_UNSET, network_order=True):
    """Pack one header/body fixture in network (XDR) order unless told otherwise."""
    payload = default_body(msg_id) if body is _UNSET else (b"" if body is None else bytes(body))
    field = name_bytes(CALLSIGN) if name is None else bytes(name)
    declared = HEADER_SIZE + len(payload) if msg_len is _UNSET else msg_len
    fmt = HEADER_STRUCT.format if network_order else HEADER_STRUCT.format.replace("!", "<", 1)
    return struct.Struct(fmt).pack(magic, version, msg_id, declared,
                                   radar_range, reply_port, field) + payload


def top_level_imports(source):
    """Return the module names the protocol module imports (AST-proof)."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return frozenset(names)


class TestHeaderLayout(unittest.TestCase):
    def test_struct_is_network_order_thirty_two_bytes(self):
        self.assertEqual(HEADER_STRUCT.format, "!6I8s")
        self.assertEqual(HEADER_SIZE, 32)
        self.assertEqual(HEADER_STRUCT.size, 32)
        self.assertEqual(MAX_PACKET_SIZE, 1200)
        self.assertEqual(MAGIC_FGFS, 0x46474653)
        self.assertEqual(PROTOCOL_VERSION, 0x00010001)

    def test_allowlist_is_chat_and_pos_only(self):
        self.assertEqual(ALLOWED_MSG_IDS, frozenset({MSG_CHAT, MSG_POS}))
        self.assertEqual((MSG_CHAT, MSG_POS), (1, 7))
        self.assertEqual(RELAY_MAGIC, 0x53464746)

    def test_chat_body_matches_pinned_fixed_buffer(self):
        self.assertEqual(len(CHAT_BODY), CHAT_BODY_BYTES)
        self.assertEqual(HEADER_SIZE + CHAT_BODY_BYTES, 288)


class TestValidPackets(unittest.TestCase):
    def test_valid_chat_returns_parsed_header(self):
        packet = build_packet(MSG_CHAT, radar_range=120)
        header = validate_packet(packet, expected_callsign=CALLSIGN)
        self.assertIsInstance(header, Header)
        self.assertEqual(header.magic, MAGIC_FGFS)
        self.assertEqual(header.version, PROTOCOL_VERSION)
        self.assertEqual(header.msg_id, MSG_CHAT)
        self.assertEqual(header.msg_len, len(packet))
        self.assertEqual(header.body_len, CHAT_BODY_BYTES)
        self.assertEqual(header.reply_port, 0)
        self.assertEqual(header.callsign, CALLSIGN)
        self.assertEqual(header.name_field, name_bytes(CALLSIGN))
        self.assertEqual(header.body, CHAT_BODY)

    def test_valid_pos_keeps_body_opaque_byte_for_byte(self):
        packet = build_packet(MSG_POS)
        header = validate_packet(packet)
        self.assertEqual(header.msg_id, MSG_POS)
        self.assertEqual(header.body, NOT_AN_FG_POSITION_BODY)
        # An opaque body is never interpreted: any NULs or high bytes are fine,
        # and so is any length, which no real position codec could tolerate.
        for body in (b"\xff", b"\x00" * 3, NOT_AN_FG_POSITION_BODY,
                     b"\x00" * (MAX_PACKET_SIZE - HEADER_SIZE)):
            header = validate_packet(build_packet(MSG_POS, body=body))
            self.assertEqual(header.body, body)

    def test_radar_range_is_opaque_u32(self):
        for value in (0, 1, 255, 0x7fffffff, 0xffffffff):
            header = validate_packet(build_packet(MSG_POS, radar_range=value))
            self.assertEqual(header.radar_range, value)

    def test_maximum_packet_is_accepted(self):
        packet = build_packet(MSG_POS, body=b"\x00" * (MAX_PACKET_SIZE - HEADER_SIZE))
        self.assertEqual(len(packet), MAX_PACKET_SIZE)
        self.assertEqual(validate_packet(packet).msg_len, MAX_PACKET_SIZE)

    def test_eight_byte_name_field_is_copied_verbatim(self):
        header = validate_packet(build_packet(MSG_POS, name=name_bytes("A1")))
        self.assertEqual(header.name_field, name_bytes("A1"))
        self.assertEqual(header.callsign, "A1")

    def test_header_is_frozen(self):
        header = validate_packet(build_packet(MSG_POS))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            header.callsign = "OTHER1"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            header.msg_id = MSG_CHAT

    def test_input_is_never_mutated(self):
        packet = bytearray(build_packet(MSG_POS))
        snapshot = bytes(packet)
        header = validate_packet(packet, expected_callsign=CALLSIGN)
        self.assertEqual(bytes(packet), snapshot)
        packet[HEADER_SIZE] = 0x00                 # later caller edits
        packet[:4] = b"GSGF"                       # even a rewritten magic
        self.assertEqual(header.body, snapshot[HEADER_SIZE:])
        self.assertEqual(header.magic, MAGIC_FGFS)

    def test_bytes_like_inputs_are_accepted(self):
        packet = build_packet(MSG_CHAT)
        self.assertEqual(validate_packet(packet).msg_id, MSG_CHAT)
        self.assertEqual(validate_packet(bytearray(packet)).msg_id, MSG_CHAT)
        self.assertEqual(validate_packet(memoryview(packet)).msg_id, MSG_CHAT)


class TestTransportBounds(unittest.TestCase):
    def test_undersized_packets_are_refused(self):
        whole = build_packet(MSG_POS)
        for size in (0, 1, 8, 16, HEADER_SIZE - 1):
            with self.subTest(size=size):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(whole[:size])
                self.assertEqual(str(caught.exception), ERR_LENGTH)

    def test_oversized_packets_are_refused(self):
        for size in (MAX_PACKET_SIZE + 1, 2048, 4096, 70000):
            with self.subTest(size=size):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_POS, body=b"\x00" * (size - HEADER_SIZE)))
                self.assertEqual(str(caught.exception), ERR_LENGTH)

    def test_non_bytes_input_is_refused(self):
        for value in (CALLSIGN, build_packet(MSG_POS).decode("latin-1"), None, 42,
                      [0] * HEADER_SIZE, {"magic": MAGIC_FGFS}):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(value)
                self.assertEqual(str(caught.exception), ERR_TYPE)

    def test_empty_body_is_refused(self):
        for msg_id in (MSG_CHAT, MSG_POS):
            with self.subTest(msg_id=msg_id):
                packet = build_packet(msg_id, body=None)
                self.assertEqual(len(packet), HEADER_SIZE)
                with self.assertRaises(PacketError) as caught:
                    validate_packet(packet)
                self.assertEqual(str(caught.exception), ERR_BODY)


class TestMagicVersionAndMsgId(unittest.TestCase):
    def test_wrong_endian_packet_is_refused_by_magic(self):
        packet = build_packet(MSG_POS, network_order=False)
        # A little-endian "FGFS" reads as the internal relay magic, which the
        # strict big-endian comparison refuses as well.
        self.assertEqual(packet[:4], struct.pack(">I", RELAY_MAGIC))
        with self.assertRaises(PacketError) as caught:
            validate_packet(packet)
        self.assertEqual(str(caught.exception), ERR_MAGIC)

    def test_relay_and_other_magics_are_refused(self):
        for magic in (RELAY_MAGIC, 0, 0xffffffff, MAGIC_FGFS ^ 1, 0x46474654):
            with self.subTest(magic=hex(magic)):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_POS, magic=magic))
                self.assertEqual(str(caught.exception), ERR_MAGIC)

    def test_protocol_version_must_match_exactly(self):
        for version in (0, 0x00010000, 0x00010002, 0x00020001, 0x01000100):
            with self.subTest(version=hex(version)):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_POS, version=version))
                self.assertEqual(str(caught.exception), ERR_VERSION)

    def test_deprecated_relay_admin_and_unknown_msg_ids_are_refused(self):
        rejected = [0, 2, 3, 4, 5, 6, 8, 9, 10, 11, 42, 0x7fffffff, 0xffffffff]
        self.assertEqual(sorted(set(rejected) & set(ALLOWED_MSG_IDS)), [])
        for msg_id in rejected:
            with self.subTest(msg_id=msg_id):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(msg_id))
                self.assertEqual(str(caught.exception), ERR_MSG_ID)

    def test_chat_body_must_be_the_fixed_buffer(self):
        for size in (1, 63, CHAT_BODY_BYTES - 1, CHAT_BODY_BYTES + 1, 1024):
            with self.subTest(size=size):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_CHAT, body=b"\x00" * size))
                self.assertEqual(str(caught.exception), ERR_CHAT_BODY)


class TestMsgLenAndReplyPort(unittest.TestCase):
    def test_declared_length_must_equal_actual_length(self):
        actual = HEADER_SIZE + len(NOT_AN_FG_POSITION_BODY)
        for declared in (0, 32, actual - 1, actual + 1, MAX_PACKET_SIZE, 10 ** 9):
            with self.subTest(declared=declared):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_POS, body=NOT_AN_FG_POSITION_BODY,
                                                 msg_len=declared))
                self.assertEqual(str(caught.exception), ERR_MSG_LEN)

    def test_trailing_bytes_after_declared_length_are_refused(self):
        packet = build_packet(MSG_POS)
        with self.assertRaises(PacketError) as caught:
            validate_packet(packet + b"\x00" * 4)
        self.assertEqual(str(caught.exception), ERR_MSG_LEN)

    def test_reply_port_is_deprecated_and_must_be_zero(self):
        for port in (1, 7, 5000, 0x01000000, 0xffff, 0xffffffff):
            with self.subTest(port=port):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_POS, reply_port=port))
                self.assertEqual(str(caught.exception), ERR_REPLY_PORT)


class TestCallsignPolicy(unittest.TestCase):
    def assert_name_refused(self, field):
        with self.assertRaises(PacketError) as caught:
            validate_packet(build_packet(MSG_POS, name=field))
        self.assertEqual(str(caught.exception), ERR_CALLSIGN)

    def test_empty_and_unterminated_names_are_refused(self):
        for field in (name_bytes(""), name_bytes("PIL0012"), name_bytes("PIL001XY")):
            with self.subTest(field=field):
                self.assert_name_refused(field)

    def test_seven_character_name_has_no_launcher_room(self):
        self.assert_name_refused(name_bytes("PIL0012"))

    def test_padding_after_terminator_must_be_zero(self):
        for field in (b"PIL1\x00XY\x00", b"PIL1\x00\x00 \x00", b"PIL\x00\xff\x00\x00\x00"):
            with self.subTest(field=field):
                self.assertEqual(len(field), CALLSIGN_FIELD_BYTES)
                self.assert_name_refused(field)

    def test_non_ascii_and_punctuation_names_are_refused(self):
        for field in (name_bytes(b"\xc3\xa9PIL"), name_bytes(b"PIL\x00\xc3\xa9\x00\x00"),
                      name_bytes("PIL-1"), name_bytes("PIL 1"), name_bytes("PIL.1"),
                      name_bytes(b"PIL\x01"), name_bytes("_PIL01"), name_bytes("PIL#1")):
            with self.subTest(field=field):
                self.assert_name_refused(field)

    def test_launcher_shape_names_are_accepted(self):
        for name in ("A", "a1", "PIL001", "pil001", "Z9", "000000"):
            with self.subTest(name=name):
                header = validate_packet(build_packet(MSG_POS, name=name_bytes(name)))
                self.assertEqual(header.callsign, name)

    def test_observer_prefixed_names_are_refused_case_insensitively(self):
        for name in ("obs", "OBS", "ObS", "obs001", "OBS001", "obs9", "oBsEr"):
            with self.subTest(name=name):
                self.assert_name_refused(name_bytes(name))

    def test_observer_infix_is_not_a_reserved_prefix(self):
        for name in ("cobras", "XOBS", "ABOBS1"):
            with self.subTest(name=name):
                header = validate_packet(build_packet(MSG_POS, name=name_bytes(name)))
                self.assertEqual(header.callsign, name)

    def test_expected_callsign_binding_is_byte_exact(self):
        for other in ("PIL002", "pil001", "PIL01", "A1", "PIL00X"):
            with self.subTest(other=other):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_POS), expected_callsign=other)
                self.assertEqual(str(caught.exception), ERR_CALLSIGN_MISMATCH)

    def test_expected_callsign_must_itself_be_a_wire_name(self):
        for other in ("", "obs001", "OBS", "PIL0012", "PIL_01", "\u00e9 PIL",
                      b"PIL001", 7, ["PIL001"], object()):
            with self.subTest(other=repr(other)):
                packet = build_packet(MSG_POS)
                with self.assertRaises(PacketError) as caught:
                    validate_packet(packet, expected_callsign=other)
                self.assertEqual(str(caught.exception), ERR_EXPECTED_CALLSIGN)

    def test_expected_callsign_matches_and_spoof_is_refused(self):
        packet = build_packet(MSG_POS, name=name_bytes("PIL002"))
        self.assertEqual(validate_packet(packet, expected_callsign="PIL002").callsign,
                         "PIL002")
        with self.assertRaises(PacketError) as caught:
            validate_packet(packet, expected_callsign="PIL001")
        self.assertEqual(str(caught.exception), ERR_CALLSIGN_MISMATCH)

    def test_policy_check_runs_before_the_identity_comparison(self):
        # Even a caller that expects an observer-style identity is refused on the
        # header policy first: no lease may ever be keyed to a reserved name.
        with self.assertRaises(PacketError) as caught:
            validate_packet(build_packet(MSG_POS, name=name_bytes("OBS001")),
                            expected_callsign="OBS001")
        self.assertEqual(str(caught.exception), ERR_CALLSIGN)


class TestBufferBounds(unittest.TestCase):
    def test_oversized_buffer_is_refused_before_copy(self):
        class NoCopy(bytearray):
            def __bytes__(self):
                raise AssertionError("oversized input was copied")

        with self.assertRaises(PacketError) as caught:
            validate_packet(NoCopy(MAX_PACKET_SIZE + 1))
        self.assertEqual(str(caught.exception), ERR_LENGTH)

    def test_memoryview_bound_counts_bytes_not_elements(self):
        view = memoryview(bytes(MAX_PACKET_SIZE * 2)).cast('I')
        self.assertLess(len(view), MAX_PACKET_SIZE)
        with self.assertRaises(PacketError) as caught:
            validate_packet(view)
        self.assertEqual(str(caught.exception), ERR_LENGTH)

    def test_released_view_has_a_fixed_error(self):
        view = memoryview(build_packet())
        view.release()
        with self.assertRaises(PacketError) as caught:
            validate_packet(view)
        self.assertEqual(str(caught.exception), ERR_TYPE)


class TestErrorHygiene(unittest.TestCase):
    def catalogue(self):
        """Every rejection path, labelled with the data an attacker sent."""
        pos = build_packet(MSG_POS)
        return {
            ERR_LENGTH: [pos[:8], build_packet(MSG_POS, body=b"\x00" * 2000)],
            ERR_TYPE: ["FGFS-text-frame", None],
            ERR_MAGIC: [build_packet(MSG_POS, network_order=False),
                        build_packet(MSG_POS, magic=RELAY_MAGIC)],
            ERR_VERSION: [build_packet(MSG_POS, version=0x00020001)],
            ERR_MSG_ID: [build_packet(9), build_packet(4)],
            ERR_MSG_LEN: [build_packet(MSG_POS, msg_len=10 ** 9), pos + b"\x00"],
            ERR_BODY: [build_packet(MSG_CHAT, body=None)],
            ERR_CHAT_BODY: [build_packet(MSG_CHAT, body=b"A" * 255)],
            ERR_REPLY_PORT: [build_packet(MSG_POS, reply_port=5000)],
            ERR_CALLSIGN: [build_packet(MSG_POS, name=name_bytes("OBS999")),
                           build_packet(MSG_POS, name=name_bytes("PIL001XY"))],
            ERR_CALLSIGN_MISMATCH: [],
        }

    def test_messages_are_fixed_constants_without_packet_data(self):
        secrets = (CALLSIGN, "PIL", "OBS", "arena", "FGFS", "GSGF",
                   NOT_AN_FG_POSITION_BODY.hex(), CHAT_TEXT.decode("ascii"))
        seen = set()
        for expected, packets in self.catalogue().items():
            seen.add(expected)
            for packet in packets:
                with self.subTest(expected=expected, packet=str(packet)[:24]):
                    with self.assertRaises(PacketError) as caught:
                        validate_packet(packet)
                    message = str(caught.exception)
                    self.assertEqual(message, expected)
                    self.assertLessEqual(len(message), 64)
                    for secret in secrets:
                        self.assertNotIn(secret.lower(), message.lower())
        for expected_call in ("PIL002", "obs001"):
            with self.subTest(expected_call=expected_call):
                with self.assertRaises(PacketError) as caught:
                    validate_packet(build_packet(MSG_POS),
                                    expected_callsign=expected_call)
                message = str(caught.exception)
                seen.add(message)
                for secret in secrets:
                    self.assertNotIn(secret.lower(), message.lower())
        self.assertEqual(seen, {value for name, value in vars(mp_protocol).items()
                                if name.startswith("ERR_")})

    def test_error_constants_are_distinct_and_static(self):
        messages = [value for name, value in vars(mp_protocol).items()
                    if name.startswith("ERR_")]
        self.assertEqual(len(messages), len(set(messages)))
        for message in messages:
            self.assertTrue(message and message.strip() == message)
            for placeholder in ("%", "{", "}", ": ", "\n"):
                self.assertNotIn(placeholder, message)

    def test_packet_error_is_a_value_error(self):
        self.assertTrue(issubclass(PacketError, ValueError))
        with self.assertRaises(ValueError):
            validate_packet(b"too short")
        self.assertEqual(str(PacketError(ERR_MAGIC)), ERR_MAGIC)


class TestModulePurity(unittest.TestCase):
    def test_imports_stay_inside_the_pure_allowlist(self):
        imported = top_level_imports(SOURCE)
        self.assertTrue(imported <= ALLOWED_IMPORTS, sorted(imported - ALLOWED_IMPORTS))
        self.assertEqual(imported & FORBIDDEN_IMPORTS, frozenset())

    def test_no_transport_api_is_reached_for(self):
        for token in ("import socket", "import asyncio", "sendto", "recvfrom",
                      "bind(", "websockets", "async def"):
            with self.subTest(token=token):
                self.assertNotIn(token, SOURCE)

    def test_public_surface_is_validation_only(self):
        self.assertEqual(mp_protocol.__all__.count("validate_packet"), 1)
        self.assertNotIn("Relay", mp_protocol.__all__)
        self.assertTrue(callable(validate_packet))
        with self.assertRaises(TypeError):
            validate_packet()


if __name__ == "__main__":
    unittest.main()
