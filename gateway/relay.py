# SPDX-License-Identifier: GPL-2.0-or-later
"""Local MP transport relay: one dedicated UDP endpoint per admitted identity.

The gate owns TLS, admission, slots and framing; this module owns UDP and the
reconnect lease.  It plugs into the existing ``auth_gate`` hooks, with the gate
frame bound pinned to the MP packet size:

    relay = UdpRelay(backend=("127.0.0.1", 5000),
                     callsigns={"PILOT-A": "PIL001", "PILOT-B": "PIL002"})
    gate = TlsAuthGate(registry=registry, on_message=relay.on_message,
                       on_open=relay.on_open, on_close=relay.on_close,
                       max_message_bytes=1200)

Client -> backend: one WebSocket binary message is one datagram.  It is checked
by ``gateway.mp_protocol`` (header, MsgLen, MsgId allowlist and the callsign
bound to the authenticated identity) and forwarded verbatim, exactly once,
through that identity's own endpoint.  Anything invalid raises
`PolicyViolation`, the gate closes 1008 with a generic reason and the backend
receives nothing.  The destination is the fixed literal IPv4 loopback address
given at construction: a peer, URL or packet never chooses a destination, no
name is resolved and no LAN or public address can be reached.  Every reverse
write goes to the ``SessionChannel`` this identity handed to ``on_open``, and a
channel that names a different identity is refused before anything is queued.

Backend -> client: a datagram is accepted only from that same fixed backend
address (an explicit source check, not a routing accident), header-validated
WITHOUT an expected callsign (it carries another pilot's Name) and queued for the
current admission only.  A dedicated writer task drains the bounded queue and
calls ``channel.send``, so the reverse stream flows while the client sends
nothing.  On overflow the oldest queued frame is dropped and counted: bounded
memory and a bounded number of tasks beat an unbounded backlog.  Each endpoint
owns a generation token, so a late callback from a released endpoint can never be
delivered through a newer endpoint of the same identity.  Bad sources, invalid
frames and overflows are counted per packet but logged at most once per lease:
broken traffic cannot turn into a log flood.  A send failure or ``send_timeout``
closes that session's channel — with the same timeout — and nothing else.

Reconnect: ``on_close`` detaches the session, its queue and its writer at once,
but keeps the UDP endpoint for ``grace_seconds`` so a fast reconnect gets the
same local source port — FGMS looks a player up by callsign and drops updates
whose source address changed until ``playerexpires``.  Because FGMS runs its TTL
sweep before a blocking ``select(..., m_PlayerExpires)`` with second-granularity
clocks, deletion is not guaranteed at exactly ``playerexpires``; that is why
``grace_seconds`` must cover ``2 * fgms_player_expires + expiry_margin``.  During
grace nothing is delivered or queued, a grace endpoint is never evicted to make
room, and the port guarantee holds inside this process only (no restart
persistence).  Shutdown is ``await gate.stop()`` then ``await relay.close()``.

Still not production and not a simulator test: the backend in the tests is a
FAKE UDP peer, not FGMS, no real fgfs was involved, and nothing here scores,
damages or respawns (docs/SPACE_ARENA_MP_GATEWAY_CONTRACT.md).
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .auth_gate import GatewayError, PolicyViolation, SessionChannel, make_logger
from .mp_protocol import (
    ALLOWED_MSG_IDS,
    CALLSIGN_FIELD_BYTES,
    CALLSIGN_MAX_LEN,
    CHAT_BODY_BYTES,
    HEADER_SIZE,
    HEADER_STRUCT,
    MAGIC_FGFS,
    MAX_PACKET_SIZE,
    MSG_CHAT,
    MSG_POS,
    PROTOCOL_VERSION,
    RELAY_MAGIC,
    RESERVED_CALLSIGN_PREFIX,
    Header,
    PacketError,
    validate_packet,
    wire_callsign,
)

RELAY_LOGGER_NAME = "space_gateway.relay"     # tests attach handlers by this name
UDP_BIND_HOST = "127.0.0.1"                   # the only address this relay binds

DEFAULT_GRACE_SECONDS = 30.0                  # >= 2*10 + 2 for playerexpires=10
DEFAULT_FGMS_PLAYER_EXPIRES = 10.0            # pinned FGMS default
DEFAULT_EXPIRY_MARGIN = 2.0                   # sweep interval plus clock slack
DEFAULT_MAX_LEASES = 8
DEFAULT_QUEUE_LIMIT = 8
DEFAULT_SEND_TIMEOUT = 5.0

# Generic failure texts: no callsign, token, address or packet content ever.
ERR_BACKEND = "backend destination rejected"
ERR_BINDINGS = "callsign bindings rejected"
ERR_SETTINGS = "relay settings out of range"
ERR_CLOSING = "relay is closing"
ERR_NO_SESSION = "no backend session for this identity"
ERR_ACTIVE_SESSION = "this identity already has a live session"
ERR_CHANNEL = "session channel does not match this identity"
ERR_AT_CAPACITY = "relay endpoint capacity reached"
ERR_ENDPOINT = "backend endpoint unavailable"
ERR_PACKET = "packet refused"

# Fixed warning reasons.  Each one produces at most one log line per lease (or
# per relay for the ones that have no lease): counters keep counting every
# packet, so a flood of bad traffic stays visible in the numbers but cannot
# become a flood of log lines.  No packet, peer or exception text is stored or
# logged, so de-duplication costs a few short constants and a set of reasons.
WARN_SOURCE = "datagram from an unexpected source ignored"
WARN_FRAME = "invalid backend frame ignored"
WARN_OVERFLOW = "reverse queue overflow"
WARN_STALE = "callback from a closed endpoint ignored"
WARN_ENDPOINT_ERROR = "backend endpoint error"
WARN_ENDPOINT_CREATE = "backend endpoint could not be created"
WARN_ENDPOINT_CLOSE = "endpoint close failed"
WARN_CHANNEL_CLOSE = "channel close failed or timed out"

__all__ = [
    "ALLOWED_MSG_IDS",
    "CALLSIGN_FIELD_BYTES",
    "CALLSIGN_MAX_LEN",
    "CHAT_BODY_BYTES",
    "DEFAULT_EXPIRY_MARGIN",
    "DEFAULT_FGMS_PLAYER_EXPIRES",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MAX_LEASES",
    "DEFAULT_QUEUE_LIMIT",
    "DEFAULT_SEND_TIMEOUT",
    "ERR_ACTIVE_SESSION",
    "ERR_AT_CAPACITY",
    "ERR_BACKEND",
    "ERR_BINDINGS",
    "ERR_CHANNEL",
    "ERR_CLOSING",
    "ERR_ENDPOINT",
    "ERR_NO_SESSION",
    "ERR_PACKET",
    "ERR_SETTINGS",
    "HEADER_SIZE",
    "HEADER_STRUCT",
    "MAGIC_FGFS",
    "MAX_PACKET_SIZE",
    "MSG_CHAT",
    "MSG_POS",
    "PROTOCOL_VERSION",
    "RELAY_LOGGER_NAME",
    "RELAY_MAGIC",
    "RESERVED_CALLSIGN_PREFIX",
    "Header",
    "PacketError",
    "UdpRelay",
    "UDP_BIND_HOST",
    "validate_packet",
    "wire_callsign",
]


def _count(name: str, value: int, low: int, high: int) -> int:
    """An int setting (never a bool) inside a sane inclusive range."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise GatewayError(ERR_SETTINGS)
    return value


def _seconds(name: str, value: float) -> float:
    """A finite positive duration (never a bool, never NaN/inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatewayError(ERR_SETTINGS)
    if not math.isfinite(float(value)) or not 0.0 < float(value) <= 3600.0:
        raise GatewayError(ERR_SETTINGS)
    return float(value)


def _backend(value) -> tuple[str, int]:
    """Fixed IPv4 loopback literal plus port: no DNS, no LAN, no public target."""
    try:
        host, port = value
    except (TypeError, ValueError):
        raise GatewayError(ERR_BACKEND) from None
    if not isinstance(host, str):
        raise GatewayError(ERR_BACKEND)
    try:
        address = ipaddress.IPv4Address(host)      # a literal only, never a name
    except ValueError:
        raise GatewayError(ERR_BACKEND) from None
    if not address.is_loopback:
        raise GatewayError(ERR_BACKEND)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise GatewayError(ERR_BACKEND)
    return str(address), port


def _bindings(callsigns: Mapping[str, str]) -> Mapping[str, str]:
    """Immutable identity -> callsign table, each side valid and callsign-unique."""
    if not isinstance(callsigns, Mapping) or not callsigns:
        raise GatewayError(ERR_BINDINGS)
    checked, seen = {}, set()
    for identity, callsign in callsigns.items():
        if not isinstance(identity, str) or not 1 <= len(identity) <= 64:
            raise GatewayError(ERR_BINDINGS)
        try:
            wire_callsign(callsign if isinstance(callsign, str) else "")
        except PacketError:
            raise GatewayError(ERR_BINDINGS) from None
        if callsign.casefold() in seen:            # one pilot, one endpoint
            raise GatewayError(ERR_BINDINGS)
        seen.add(callsign.casefold())
        checked[identity] = callsign
    return MappingProxyType(checked)


@dataclass
class _Lease:
    """One identity's endpoint plus the session currently allowed to use it."""

    identity: str
    callsign: str
    transport: asyncio.DatagramTransport
    local_addr: tuple[str, int]
    channel: SessionChannel | None = None
    queue: asyncio.Queue | None = None
    writer: asyncio.Task | None = None
    timer: object | None = None
    active: bool = False
    forwarded: int = 0        # WS -> backend datagrams
    delivered: int = 0        # backend -> WS frames
    dropped: int = 0          # overflow or detached-queue discards, counted only
    ignored: int = 0          # backend frames refused or dropped by policy
    protocol: "_BackendEndpoint | None" = None   # the endpoint generation it owns
    warned: set[str] = field(default_factory=set)   # fixed reasons warned once each


class _BackendEndpoint(asyncio.DatagramProtocol):
    """Endpoint whose only accepted peer is the fixed configured backend address.

    The instance itself is the generation token of one endpoint: a late callback
    from a closed protocol can therefore never be attributed to a newer endpoint
    of the same identity.
    """

    def __init__(self, relay: "UdpRelay", identity: str) -> None:
        self._relay, self._identity = relay, identity
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data, addr) -> None:
        self._relay._from_backend(self._identity, self, bytes(data), addr)

    def error_received(self, exc) -> None:                  # e.g. ICMP refused
        self._relay._note(WARN_ENDPOINT_ERROR)              # the exception stays out

    def connection_lost(self, exc) -> None:
        return None


class UdpRelay:
    """Bounded two-way transport between admitted WS sessions and one MP backend."""

    def __init__(self, *, backend: tuple[str, int], callsigns: Mapping[str, str],
                 grace_seconds: float = DEFAULT_GRACE_SECONDS,
                 fgms_player_expires: float = DEFAULT_FGMS_PLAYER_EXPIRES,
                 expiry_margin: float = DEFAULT_EXPIRY_MARGIN,
                 max_leases: int = DEFAULT_MAX_LEASES,
                 queue_limit: int = DEFAULT_QUEUE_LIMIT,
                 send_timeout: float = DEFAULT_SEND_TIMEOUT) -> None:
        # Construction performs no I/O: no socket, no task, no event loop needed.
        self._backend = _backend(backend)
        self._callsigns = _bindings(callsigns)
        expires = _seconds("fgms_player_expires", fgms_player_expires)
        margin = _seconds("expiry_margin", expiry_margin)
        grace = _seconds("grace_seconds", grace_seconds)
        if grace < 2.0 * expires + margin:            # sweep + clock slack, see above
            raise GatewayError(ERR_SETTINGS)
        self._grace = grace
        self._max_leases = _count("max_leases", max_leases, 1, 1024)
        self._queue_limit = _count("queue_limit", queue_limit, 1, 4096)
        self._send_timeout = _seconds("send_timeout", send_timeout)
        self._leases: dict[str, _Lease] = {}
        self._pending: set[str] = set()               # creations counted in the cap
        self._creations: dict[str, asyncio.Task] = {}  # OUR creation tasks, closable
        self._protocols: dict[str, _BackendEndpoint] = {}   # hand-off, per identity
        self._tasks: set[asyncio.Task] = set()
        self._warned: set[str] = set()                # relay-level once-each reasons
        self._stale = 0                               # callbacks of dead endpoints
        self._log = make_logger(RELAY_LOGGER_NAME)    # redacting gate logger
        self._closing = False

    @property
    def endpoints(self) -> dict[str, tuple[str, int]]:
        """Read-only snapshot identity -> local UDP addr, grace holds included."""
        return {identity: lease.local_addr for identity, lease in self._leases.items()}

    # -- gate hooks ----------------------------------------------------------
    async def on_open(self, identity: str, channel: SessionChannel) -> None:
        """Create or reclaim the endpoint for one already-admitted identity."""
        callsign = self._callsigns.get(identity) if isinstance(identity, str) else None
        if self._closing or callsign is None:          # unknown identity: fail closed
            raise PolicyViolation(ERR_CLOSING if self._closing else ERR_NO_SESSION)
        # The reverse path may only ever write to the channel of THIS identity: a
        # channel object carries its own identity, and a mismatch is a bug or an
        # attempt to cross-wire two sessions, never something to route around.
        if not isinstance(channel, SessionChannel) or channel.identity != identity:
            self._log.warning("%s", ERR_CHANNEL)
            raise PolicyViolation(ERR_CHANNEL)
        held = self._leases.get(identity)
        if held is not None and held.active:           # a live session keeps its slot
            self._log.warning("%s", ERR_ACTIVE_SESSION)
            raise PolicyViolation(ERR_ACTIVE_SESSION)
        if held is not None:                           # reconnect inside grace: same port
            self._attach(held, channel)
            self._log.info("relay endpoint reclaimed within grace")
            return
        if identity in self._pending:
            raise PolicyViolation(ERR_NO_SESSION)
        if len(self._leases) + len(self._pending) >= self._max_leases:
            self._log.warning("%s", ERR_AT_CAPACITY)
            raise PolicyViolation(ERR_AT_CAPACITY)  # grace ports are never evicted
        self._pending.add(identity)                    # the cap counts pending work
        transport = None
        creation = None
        installed = False
        try:
            # The endpoint is created in a task OWNED by the relay, so close() can
            # finish an in-flight creation instead of waiting for it or leaking it.
            creation = self._spawn(self._create_endpoint(identity))
            self._creations[identity] = creation
            try:
                transport = await creation
            except asyncio.CancelledError:
                if not self._closing:
                    raise                            # the caller was cancelled: honest
                raise PolicyViolation(ERR_CLOSING) from None
            except GatewayError:                       # a gate error stays itself
                raise
            except (OSError, ValueError, RuntimeError):
                self._note(WARN_ENDPOINT_CREATE)       # a bind that failed outright
                raise PolicyViolation(ERR_ENDPOINT) from None
            finally:
                self._creations.pop(identity, None)
            protocol = self._protocols.pop(identity, None)
            try:
                sock = transport.get_extra_info("sockname") if transport else None
                local_addr = tuple(sock) if sock else ()
            except GatewayError:                       # never rewrite a gate error
                raise
            except (OSError, ValueError, RuntimeError):
                local_addr = ()
            if transport is None or protocol is None or not local_addr:
                self._note(WARN_ENDPOINT_CREATE)       # no generation: no lease ever
                raise PolicyViolation(ERR_ENDPOINT)
            if self._closing or identity in self._leases:
                raise PolicyViolation(ERR_CLOSING)
            lease = _Lease(identity=identity, callsign=callsign, transport=transport,
                           local_addr=local_addr, protocol=protocol)
            self._leases[identity] = lease
            self._attach(lease, channel)
            installed = True
            self._log.info("relay endpoint attached")
        finally:
            # Every exit path frees the slot: success, refusal, OSError or a
            # cancellation from the caller, so capacity can never stay pinned.
            self._pending.discard(identity)
            self._protocols.pop(identity, None)
            if not installed:
                # Cancellation may win after the creation task returned a bound
                # socket but before this task resumed to assign its result.
                if (transport is None and creation is not None and creation.done()
                        and not creation.cancelled()):
                    try:
                        transport = creation.result()
                    except Exception:
                        pass                          # already handled, no raw log
                self._close_transport(transport)      # never a half-built lease

    async def on_message(self, identity: str, payload: bytes) -> None:
        """Validate one client frame and forward it as exactly one UDP datagram."""
        lease = self._leases.get(identity) if isinstance(identity, str) else None
        if self._closing or lease is None or not lease.active:
            raise PolicyViolation(ERR_CLOSING if self._closing else ERR_NO_SESSION)
        try:
            validate_packet(payload, expected_callsign=lease.callsign)
        except PacketError:
            lease.ignored += 1
            self._log.warning("%s", ERR_PACKET)             # constant, never packet
            raise PolicyViolation(ERR_PACKET) from None
        # The destination is only ever the fixed constructor backend: a peer, a URL
        # or a packet header can never steer this send.  One call = one datagram.
        lease.transport.sendto(bytes(payload), self._backend)
        lease.forwarded += 1
        return None                                       # no WS reply from the relay

    async def on_close(self, identity: str) -> None:
        """Drop this session's queue/writer and hold the endpoint for the grace."""
        lease = self._leases.get(identity) if isinstance(identity, str) else None
        if lease is None:               # refusals never reached the gate's hook
            return
        was_active = lease.active
        await self._end_session(lease)
        if was_active and not self._closing:
            self._log.info("relay session detached; endpoint held for grace")

    async def _end_session(self, lease: _Lease) -> None:
        """One reverse path ends: stop it now, keep the port only for the grace."""
        if lease.timer is not None and not lease.active:
            return                      # already detached and scheduled: stay put
        await self._detach(lease)
        if self._closing:
            self._release(lease)        # a closing relay must not resurrect anything
            return
        lease.timer = asyncio.get_running_loop().call_later(
            self._grace, self._expire, lease.identity, lease)

    async def close(self) -> None:
        """Stop accepting, then stop every creation, channel, writer, timer, endpoint."""
        self._closing = True                       # idempotent: repeat calls are cheap
        creations, self._creations = list(self._creations.values()), {}
        for task in creations:
            task.cancel()                          # our own creation work, never an
        self._pending.clear()                      # application task of the gate
        self._protocols.clear()                    # a late create installs no lease
        leases, self._leases = list(self._leases.values()), {}
        for lease in leases:
            channel = lease.channel
            await self._detach(lease)
            self._release(lease)
            await self._close_channel(channel)     # the peer learns that it ended
        for task in list(self._tasks):
            await self._finish(task)               # cancelled writers and creations
        self._log.info("relay closed")

    # -- session plumbing ----------------------------------------------------
    async def _create_endpoint(self, identity: str) -> asyncio.DatagramTransport:
        """Bind one ephemeral IPv4 loopback endpoint for one identity.

        The endpoint is deliberately unconnected: the relay's destination is
        always the fixed backend literal given at construction (named in every
        `sendto`) and the reverse path is admitted only from exactly that address,
        so a peer, a URL or a packet can never choose where traffic goes and no
        name is ever resolved.  Keeping the socket unconnected means one socket
        per identity can be handed to a reconnecting session and can be closed
        independently, and the source filter stays an explicit, testable check
        rather than a side effect of socket state.  Whether a NAT or an intervening
        router preserves this source address has to be verified on the real route.

        The protocol instance is published in `_protocols` for `on_open`: it is the
        generation token of this endpoint, so a callback from an endpoint that has
        already been released can never be attributed to a newer one.
        """
        loop = asyncio.get_running_loop()
        protocol = _BackendEndpoint(self, identity)
        _transport, created = await loop.create_datagram_endpoint(
            lambda: protocol, local_addr=(UDP_BIND_HOST, 0))
        self._protocols[identity] = created
        return created.transport

    def _attach(self, lease: _Lease, channel: SessionChannel) -> None:
        """Bind a fresh admission: new bounded queue, new writer, grace timer off."""
        self._cancel_timer(lease)
        lease.channel, lease.queue = channel, asyncio.Queue(maxsize=self._queue_limit)
        lease.active = True
        lease.writer = self._spawn(self._drain(lease))

    async def _detach(self, lease: _Lease) -> None:
        """End the current session's reverse path; nothing carries over."""
        lease.active = False
        lease.channel = None
        queued = self._clear_queue(lease.queue)
        lease.queue = None
        if queued:
            lease.dropped += queued
            self._log.warning("dropped %d queued frame(s) at detach", queued)
        task, lease.writer = lease.writer, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await self._finish(task)

    def _release(self, lease: _Lease) -> None:
        """Close an endpoint and forget the lease; no resurrection afterwards."""
        self._cancel_timer(lease)
        if self._leases.get(lease.identity) is lease:
            self._leases.pop(lease.identity, None)
        lease.protocol = None                    # its callbacks become stale
        self._close_transport(lease.transport)

    def _close_transport(self, transport) -> None:
        """Close an endpoint that never became (or no longer is) a lease."""
        if transport is None:
            return
        try:
            transport.close()
        except Exception:                         # a socket close must not trap us
            self._note(WARN_ENDPOINT_CLOSE)

    async def _close_channel(self, channel) -> None:
        """Close one session channel with the same bound as a send.

        Bounded by send_timeout, so a client that stopped reading cannot keep
        relay shutdown or a failing writer task hanging; a close that ignores
        cancellation is the one thing no timeout can fix, and the gate's own
        shutdown order (gate.stop() first) avoids that path.
        """
        if channel is None:
            return
        try:
            await asyncio.wait_for(channel.close(), self._send_timeout)
        except Exception:                         # timeout or a stuck peer
            self._note(WARN_CHANNEL_CLOSE)

    def _warn_once(self, seen: set[str], reason: str, count: int = 0) -> None:
        """At most one line per fixed reason; counters keep counting packets."""
        if reason not in seen:
            seen.add(reason)
            if count:
                self._log.warning("%s; %d oldest frame(s) dropped", reason, count)
            else:
                self._log.warning("%s", reason)

    def _note(self, reason: str) -> None:
        """A relay-level warning that has no lease to belong to, once per reason."""
        self._warn_once(self._warned, reason)

    def _expire(self, identity: str, lease: _Lease) -> None:
        """Grace elapsed: the port goes back, FGMS expiry has had its slack."""
        if self._leases.get(identity) is not lease:
            return                                # reclaimed or already replaced
        lease.timer = None
        self._release(lease)
        self._log.info("grace elapsed; relay endpoint released")

    async def _drain(self, lease: _Lease) -> None:
        """Independent writer: pushes backend frames even when the client is idle."""
        channel, queue = lease.channel, lease.queue
        try:
            while lease.active and channel is not None and queue is not None:
                frame = await queue.get()
                await asyncio.wait_for(channel.send(frame), self._send_timeout)
                lease.delivered += 1
        except asyncio.CancelledError:            # detach or shutdown owns this end
            raise
        except Exception:                         # send failure or send_timeout
            self._log.warning("reverse send failed; closing that session")
            await self._close_channel(channel)    # bounded, and never this task
            if self._leases.get(lease.identity) is lease:
                await self._end_session(lease)    # same detach and grace as on_close

    def _from_backend(self, identity: str, endpoint: "_BackendEndpoint", data: bytes,
                      addr) -> None:
        """Protocol callback: current endpoint and fixed backend address only."""
        lease = self._leases.get(identity)
        if lease is None or lease.protocol is not endpoint:
            self._stale += 1                      # an endpoint that is already gone
            self._note(WARN_STALE)
            return
        if addr != self._backend:                 # explicit source check, load-bearing
            lease.ignored += 1
            self._warn_once(lease.warned, WARN_SOURCE)
            return
        if not lease.active or lease.queue is None:
            lease.ignored += 1                    # grace or detached: never queued
            return
        try:
            validate_packet(data)                 # another pilot's Name: no expected
        except PacketError:
            lease.ignored += 1
            self._warn_once(lease.warned, WARN_FRAME)
            return
        self._enqueue(lease, data)

    def _enqueue(self, lease: _Lease, frame: bytes) -> None:
        """Bounded reverse queue: overflow drops the OLDEST frame and counts it."""
        queue = lease.queue
        dropped = 0
        while queue is not None and queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        if dropped:
            lease.dropped += dropped
            self._warn_once(lease.warned, WARN_OVERFLOW, dropped)  # counter only
        if queue is not None:
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:              # cannot happen after the drain
                lease.dropped += 1

    def _clear_queue(self, queue: asyncio.Queue | None) -> int:
        count = 0
        while queue is not None:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            count += 1
        return count

    def _cancel_timer(self, lease: _Lease) -> None:
        timer, lease.timer = lease.timer, None
        if timer is not None:
            timer.cancel()

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _finish(self, task: asyncio.Task) -> None:
        """Await a cancelled task unless it IS us (no self-await at on_close)."""
        if task is None or task is asyncio.current_task():
            return
        try:
            await task
        except (asyncio.CancelledError, Exception):
            return                                 # already logged where it happened
