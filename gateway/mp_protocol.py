# SPDX-License-Identifier: GPL-2.0-or-later
"""MP packet-header protocol layer of the Space Arena gateway: pure functions.

This module holds the wire rules of ``T_MsgHdr`` and of this project's callsign
policy and nothing else: no socket, no asyncio, no threads, no logging, no file
and no FGMS traffic, so it is importable and testable on a machine that has no
FlightGear at all.  ``gateway/relay.py`` (transport, UDP endpoints and leases)
and ``gateway/auth_gate.py`` (TLS admission) are its only intended callers, and
both keep payloads opaque: a green run of the header tests proves these rules,
not the behaviour of a real FGMS or of two real fgfs clients
(docs/SPACE_ARENA_MP_GATEWAY_CONTRACT.md).

Checked: the ``T_MsgHdr`` envelope plus the callsign policy this project already
enforces in ``tools/launch.py``, and the binding of the in-packet callsign to an
expected authenticated identity.  Deliberately NOT decoded: everything after
byte 32.  Position/property payloads stay opaque byte copies; the C++ position
layouts are not reproduced in Python (padding and field evolution differ
between the pinned copies), so an authoritative codec needs its own source
review.

Pinned sources used (commit-relative paths, revisions listed in
docs/SPACE_ARENA_MP_GATEWAY_CONTRACT.md):

* FGMS ``26e3dbd4124a10ea825806b77d32e046527cfe9d``
  - ``src/flightgear/MultiPlayer/mpmessages.hxx:47`` ``CHAT_MSG_ID`` is 1
  - ``.../mpmessages.hxx:52,57`` ``MSG_MAGIC`` ``FGFS`` and ``PROTO_VER`` 1.1
  - ``.../mpmessages.hxx:69-84`` ``MSG_ID`` enum: 2..6 deprecated position/
    reset ids, 7 ``POS_DATA``, 8 ``MP_2017_DATA_ID``, 9/10 ``PING``/``PONG``
  - ``.../mpmessages.hxx:89,93`` ``MAX_CALLSIGN_LEN`` 8, ``MAX_CHAT_MSG_LEN`` 256
  - ``.../mpmessages.hxx:109-128`` ``T_MsgHdr``: six XDR uint32 + ``char Name[8]``,
    with ``ReplyPort`` documented as deprecated and "set to zero"
  - ``src/server/fg_server.hxx:72,75`` ``MAX_PACKET_SIZE`` 1200, ``RELAY_MAGIC``
  - ``src/server/fg_server.cxx:1044-1117`` ``PacketIsValid`` (reference only)
* FlightGear ``1a90b46e105f835299f85e63e735d4351850a60e``
  - ``src/MultiPlayer/multiplaymgr.cxx:68`` ``MAX_PACKET_SIZE`` 1200
  - ``.../multiplaymgr.cxx:1767`` ``FillMsgHdr(..., POS_DATA_ID, msgLen)``
  - ``.../multiplaymgr.cxx:1822-1851`` ``SendTextMessage``: ``CHAT_MSG_ID`` header
    plus one fixed ``T_ChatMsg`` per datagram
  - ``.../multiplaymgr.cxx:2607-2629`` ``FillMsgHdr``: magic/version/len, and
    ``MsgHdr->ReplyPort = 0``

Deviations from FGMS, all in the fail-closed direction: the full protocol
version must match (FGMS compares the major half only), ``MsgLen`` must equal
the number of bytes actually received, the internal relay magic is refused from
a client, only MsgId 1 and 7 are allowed, and the callsign must satisfy this
project's launcher rules.  FGMS's
``MsgLen >= sizeof(T_MsgHdr) + sizeof(T_PositionMsg)`` floor is NOT copied: it
depends on a C++ struct size this module refuses to guess, so position bodies
only have to be non-empty and inside ``MAX_PACKET_SIZE``.
"""
from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from struct import Struct

# T_MsgHdr on the wire, network (XDR) order: 6 uint32 then Name[8] -> 32 bytes.
HEADER_STRUCT = Struct("!6I8s")
HEADER_SIZE = HEADER_STRUCT.size        # asserted equal to 32 by the tests

MAX_PACKET_SIZE = 1200                  # pinned in both endpoints, see above
MAGIC_FGFS = 0x46474653                 # "FGFS": the only magic a client may use
PROTOCOL_VERSION = 0x00010001           # 1.1, compared exactly (stricter than FGMS)
RELAY_MAGIC = 0x53464746                # internal relay magic: never accepted, kept
#                                       # so tests can prove it is refused

MSG_CHAT = 1                            # CHAT_MSG_ID, pinned client + pinned FGMS
MSG_POS = 7                             # FGFS::POS_DATA (enum step after 1..6)
ALLOWED_MSG_IDS = frozenset((MSG_CHAT, MSG_POS))

# The pinned client writes ChatMsg.Text[MAX_CHAT_MSG_LEN - 1] = '\0' and sends
# sizeof(T_MsgHdr) + sizeof(T_ChatMsg) verbatim (multiplaymgr.cxx:1835-1845),
# with MsgLen set to that same sum (multiplaymgr.cxx:2610-2612).  MAX_CHAT_MSG_LEN
# is 256 in the pinned FGMS header, so a chat body is exactly this many bytes of
# a fixed Text buffer.  The pinned FG copy of T_ChatMsg is not part of this
# repository, so this constant covers that documented buffer only: a client that
# ships a different T_ChatMsg requires a new source review, not a wider limit.
CHAT_BODY_BYTES = 256

CALLSIGN_FIELD_BYTES = 8                # MAX_CALLSIGN_LEN, includes the NUL
CALLSIGN_MAX_LEN = 6                    # launcher policy, tools/launch.py:101-104: a
#                                       # project rule, NOT a claim that FlightGear
#                                       # accepts 6-character callsigns only
RESERVED_CALLSIGN_PREFIX = "obs"        # launcher refuses it: hidden by FGMS

_CALLSIGN_RE = re.compile(rb"[A-Za-z0-9]{1,%d}" % CALLSIGN_MAX_LEN)
_RESERVED_PREFIX = RESERVED_CALLSIGN_PREFIX.encode("ascii")

# Fixed, value-free messages: nothing from the packet ever reaches a log or a
# peer through these strings.
ERR_TYPE = "packet must be a bytes-like object"
ERR_LENGTH = "packet length out of bounds"
ERR_MAGIC = "message magic rejected"
ERR_VERSION = "protocol version rejected"
ERR_MSG_ID = "message id not allowed"
ERR_MSG_LEN = "message length mismatch"
ERR_BODY = "message body missing"
ERR_CHAT_BODY = "chat body length rejected"
ERR_REPLY_PORT = "reply port must be zero"
ERR_CALLSIGN = "callsign rejected"
ERR_CALLSIGN_MISMATCH = "callsign does not match the expected identity"
ERR_EXPECTED_CALLSIGN = "expected callsign rejected"

__all__ = [
    "ALLOWED_MSG_IDS",
    "CALLSIGN_FIELD_BYTES",
    "CALLSIGN_MAX_LEN",
    "CHAT_BODY_BYTES",
    "ERR_BODY",
    "ERR_CALLSIGN",
    "ERR_CALLSIGN_MISMATCH",
    "ERR_CHAT_BODY",
    "ERR_EXPECTED_CALLSIGN",
    "ERR_LENGTH",
    "ERR_MAGIC",
    "ERR_MSG_ID",
    "ERR_MSG_LEN",
    "ERR_REPLY_PORT",
    "ERR_TYPE",
    "ERR_VERSION",
    "HEADER_SIZE",
    "HEADER_STRUCT",
    "MAGIC_FGFS",
    "MAX_PACKET_SIZE",
    "MSG_CHAT",
    "MSG_POS",
    "PROTOCOL_VERSION",
    "RELAY_MAGIC",
    "RESERVED_CALLSIGN_PREFIX",
    "Header",
    "PacketError",
    "validate_packet",
    "wire_callsign",
]


class PacketError(ValueError):
    """A packet or an argument was refused; the message is always a constant."""


@dataclass(frozen=True)
class Header:
    """One parsed T_MsgHdr plus its untouched body; every field is a copy."""

    magic: int
    version: int
    msg_id: int
    msg_len: int
    radar_range: int        # opaque u32: never mapped to budget, faction or range
    reply_port: int         # deprecated, the pinned client always sends 0
    name_field: bytes       # the raw 8 Name bytes exactly as received
    callsign: str           # name_field before the NUL, ASCII [A-Za-z0-9]{1,6}
    body: bytes             # opaque payload: copied, never parsed here

    @property
    def body_len(self) -> int:
        return len(self.body)


def _decode_callsign(name_field: bytes) -> str:
    """Return the callsign of a Name[8] field, or raise PacketError."""
    end = name_field.find(b"\x00")       # FillMsgHdr always terminates the name
    if end < 1:                          # no NUL at all, or an empty name
        raise PacketError(ERR_CALLSIGN)
    if end > CALLSIGN_MAX_LEN:           # leaves no room for the NUL in Name[8]
        raise PacketError(ERR_CALLSIGN)
    if any(byte != 0 for byte in name_field[end:]):
        raise PacketError(ERR_CALLSIGN)  # padding after the terminator must be zero
    raw = name_field[:end]
    if _CALLSIGN_RE.fullmatch(raw) is None:
        raise PacketError(ERR_CALLSIGN)  # non-ASCII, space, punctuation, control
    if raw.lower().startswith(_RESERVED_PREFIX):
        raise PacketError(ERR_CALLSIGN)  # observer-style names get no match slot
    return raw.decode("ascii")


def _encode_callsign(callsign: str) -> bytes:
    """Wire form of a callsign this gateway could ever accept; else PacketError."""
    if not isinstance(callsign, str):
        raise PacketError(ERR_EXPECTED_CALLSIGN)
    try:
        raw = callsign.encode("ascii")
    except UnicodeEncodeError:
        raise PacketError(ERR_EXPECTED_CALLSIGN) from None
    if _CALLSIGN_RE.fullmatch(raw) is None or raw.lower().startswith(_RESERVED_PREFIX):
        raise PacketError(ERR_EXPECTED_CALLSIGN)
    return raw + b"\x00" * (CALLSIGN_FIELD_BYTES - len(raw))


def wire_callsign(callsign: str) -> bytes:
    """Name[8] wire form of an allowed callsign; raises PacketError otherwise.

    Lets a caller check its own identity bindings with exactly the policy a
    packet's Name field is checked against, so a binding that could never appear
    on the wire fails where it is configured instead of on the first packet.
    """
    return _encode_callsign(callsign)


def validate_packet(data: bytes, expected_callsign: str | None = None) -> Header:
    """Parse and validate one client -> relay MP datagram without mutating `data`.

    Returns a frozen `Header` on success; raises `PacketError` with a fixed
    message (never packet bytes, a callsign or a token) otherwise.  Ordering is
    fail-closed: transport bounds, then magic/version/MsgId, then MsgLen against
    the real length, then body rules, then the deprecated ReplyPort, then the
    callsign policy, and finally the byte-exact binding to `expected_callsign`
    when the caller knows the authenticated identity.

    `data` is never written to: bytearray/memoryview inputs are copied first and
    the returned `body` is a copy too, so a caller may keep validating and
    hashing its own buffer while relay bookkeeping holds the Header.  Position
    and property bodies are not decoded and their C++ sizes are not assumed.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise PacketError(ERR_TYPE)
    # Enforce the bound before copying; len(memoryview) counts elements, not
    # necessarily bytes. Released views are rejected with the same safe error.
    try:
        size = data.nbytes if isinstance(data, memoryview) else len(data)
    except (ValueError, TypeError):
        raise PacketError(ERR_TYPE) from None
    if not HEADER_SIZE <= size <= MAX_PACKET_SIZE:
        raise PacketError(ERR_LENGTH)
    try:
        packet = bytes(data)
    except (ValueError, TypeError):
        raise PacketError(ERR_TYPE) from None
    if not HEADER_SIZE <= len(packet) <= MAX_PACKET_SIZE:
        raise PacketError(ERR_LENGTH)

    magic, version, msg_id, msg_len, radar_range, reply_port, name_field = \
        HEADER_STRUCT.unpack_from(packet, 0)
    if magic != MAGIC_FGFS:
        # Also refuses RELAY_MAGIC and any little-endian/host-order packet: its
        # first four bytes are then not "FGFS".
        raise PacketError(ERR_MAGIC)
    if version != PROTOCOL_VERSION:
        raise PacketError(ERR_VERSION)
    if msg_id not in ALLOWED_MSG_IDS:
        raise PacketError(ERR_MSG_ID)    # 2..6 deprecated, 8..10, admin/relay, other
    if msg_len != len(packet):
        raise PacketError(ERR_MSG_LEN)   # claims and reality must be one number

    body = packet[HEADER_SIZE:]
    if not body:
        raise PacketError(ERR_BODY)      # a pinned CHAT or POS packet has a body
    if msg_id == MSG_CHAT and len(body) != CHAT_BODY_BYTES:
        raise PacketError(ERR_CHAT_BODY)
    if reply_port != 0:
        raise PacketError(ERR_REPLY_PORT)

    callsign = _decode_callsign(name_field)
    if expected_callsign is not None:
        # A name field or a finished TLS handshake alone does not prove who is
        # sending, so the lease identity is compared byte-for-byte (constant
        # time, on the raw 8 bytes rather than the truncated text).
        wanted = _encode_callsign(expected_callsign)
        if not hmac.compare_digest(name_field, wanted):
            raise PacketError(ERR_CALLSIGN_MISMATCH)

    return Header(magic=magic, version=version, msg_id=msg_id, msg_len=msg_len,
                  radar_range=radar_range, reply_port=reply_port,
                  name_field=name_field, callsign=callsign, body=body)
