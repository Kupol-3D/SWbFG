#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""TLS + Bearer admission gate for the Space Arena transport spike.

Library only: no CLI, no autostart, no echo default, no UDP, no FGMS, no match
logic.  It authenticates the handshake (TLS 1.3, exact path ``/mp``,
``Authorization: Bearer <256-bit token>``), admits one session per identity up
to ``max_sessions``, then hands binary frames to the caller's async
``on_message(identity, payload)``.  Not production: loopback bind only, no rate
limit, no invitations, expiry, revocation or storage (see gateway/README.md).

Lifecycle hooks are optional and run for an admitted session only:
``await on_open(identity, channel)`` before the first frame is read, where
``channel`` is a ticket-bound, send-only `SessionChannel` (it has no recv), and
``await on_close(identity)`` when that same session ends.  A reverse stream can
therefore push frames while the peer sends nothing at all.  A callback rejects
application data with `PolicyViolation` (generic close 1008); any other callback
failure closes 1011 and logs one value-free line.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import re
import secrets
import ssl
import traceback
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from ipaddress import ip_address
from types import MappingProxyType

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed

# Exact path only: a query string is NOT accepted, so secrets never live in URLs.
MP_PATH = "/mp"
MAX_MESSAGE_BYTES = 4096     # spike frame bound, deliberately NOT an FGMP size
MAX_SESSIONS, MAX_QUEUE, OPEN_TIMEOUT = 8, 4, 5.0
CLOSE_POLICY, CLOSE_UNSUPPORTED_DATA, CLOSE_INTERNAL = 1008, 1003, 1011
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")                  # 256-bit opaque token
_BEARER_RE = re.compile(r"bearer ([0-9a-f]{64})", re.IGNORECASE)


class GatewayError(RuntimeError):
    """Configuration/usage error; messages never carry tokens or payloads."""


class TlsRequiredError(GatewayError):
    """Raised when a gate would start without a TLS >= 1.3 server context."""


class PolicyViolation(GatewayError):
    """Application-level reject of one frame, raised by a caller's callback.

    The gate closes with the generic policy code and never forwards or logs this
    message, so it must not be used to carry packet contents to a peer either.
    """


def generate_token() -> str:
    """Return a fresh 256-bit opaque token; minting stays with the caller."""
    return secrets.token_hex(32)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class IdentityRegistry:
    """Immutable identity -> SHA-256(token) map; plaintext tokens are not kept."""

    digests: Mapping[str, str]

    @classmethod
    def from_tokens(cls, tokens: Mapping[str, str]) -> "IdentityRegistry":
        if not tokens:
            raise GatewayError("registry needs at least one identity")
        digests = {}
        for identity, token in tokens.items():
            if not isinstance(identity, str) or not 0 < len(identity) <= 64:
                raise GatewayError("identity must be a short non-empty string")
            if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
                raise GatewayError(f"token for {identity} must be 64 hex chars")
            digests[identity] = _digest(token)
        return cls(MappingProxyType(digests))

    def lookup(self, token: str) -> str | None:
        """Constant-time compare against every entry (no early exit)."""
        if not _TOKEN_RE.fullmatch(token):
            return None
        presented, found = _digest(token), None
        for identity, stored in self.digests.items():
            if hmac.compare_digest(presented, stored):
                found = identity
        return found


# websockets dumps request headers and frame payloads at DEBUG, so the gate
# hands its logger a filter that strips them before any handler sees the record.
_FRAME_RE = re.compile(r"^([<>]) (TEXT|BINARY|CLOSE|PING|PONG|CONTINUATION)\b.*(\[[^[\]]*\])$")
_HEADER_RE = re.compile(r"^([<>])\s+([A-Za-z0-9!#$%&'*+.^_`|~-]+)\s*:\s*.*$")
_SECRET_RE = re.compile(r"(?i)\b(authorization|bearer|token|secret|password|"
                        r"passphrase|api[-_]?key)\b([\s:=]+)\S+")
_QUERY_RE = re.compile(r"\?\S*")
_REDACTED_HEADERS = frozenset(("authorization", "proxy-authorization", "cookie",
                               "set-cookie", "sec-websocket-key",
                               "sec-websocket-accept", "www-authenticate"))


def redact(text: str) -> str:
    """Replace handshake headers, frame payloads, tokens and query strings."""
    frame = _FRAME_RE.match(text)
    if frame:
        return f"{frame.group(1)} {frame.group(2)} <payload redacted> {frame.group(3)}"
    header = _HEADER_RE.match(text)
    if header and header.group(2).lower() in _REDACTED_HEADERS:
        return f"{header.group(1)} {header.group(2)}: <redacted>"
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    return _QUERY_RE.sub("?<redacted>", text)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            safe = redact(record.getMessage())
        except Exception:                       # never emit an unvetted record
            record.msg, record.args = "<unloggable record redacted>", None
            return True
        if safe != record.getMessage():
            record.msg, record.args = safe, None
        if record.exc_info:
            trace = "".join(traceback.format_exception(*record.exc_info))
            if redact(trace) != trace:
                record.exc_text, record.exc_info = redact(trace), None
        return True


def make_logger(name: str = "space_gateway.gate") -> logging.Logger:
    """Return a logger that cannot emit credentials, paths or payloads."""
    logger = logging.getLogger(name)
    if not any(isinstance(item, _RedactingFilter) for item in logger.filters):
        logger.addFilter(_RedactingFilter())
    return logger


def _is_async(func: object) -> bool:
    """True for an async function or an async ``__call__`` (awaitable callback)."""
    return (inspect.iscoroutinefunction(func)
            or inspect.iscoroutinefunction(getattr(func, "__call__", None)))


@dataclass(frozen=True)
class SessionChannel:
    """Send half of ONE admitted session; the gate wires the two callables.

    It is frozen, ticket-bound and send-only: there is no recv here and no raw
    connection object, so a reverse producer cannot read the peer, cannot pick
    which session it writes to and cannot outlive its own admission — a sender
    retained from a finished session is refused before it reaches the wire.
    ``send_impl``/``close_impl`` are gate internals; callers never pass them.
    """

    identity: str
    ticket: int                                 # this admission, not the identity
    max_message_bytes: int
    send_impl: Callable[[bytes], Awaitable[None]] = field(repr=False)
    close_impl: Callable[[], Awaitable[None]] = field(repr=False)

    async def send(self, payload) -> None:
        """Queue exactly one binary frame; rejects shape/size without echoing them."""
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise GatewayError("channel send needs a binary payload")
        try:
            size = payload.nbytes if isinstance(payload, memoryview) else len(payload)
        except (ValueError, TypeError):
            raise GatewayError("channel send needs a valid binary payload") from None
        if not 1 <= size <= self.max_message_bytes:
            raise GatewayError("channel send payload is outside the configured size")
        try:
            frame = bytes(payload)
        except (ValueError, TypeError):
            raise GatewayError("channel send needs a valid binary payload") from None
        if not 1 <= len(frame) <= self.max_message_bytes:
            raise GatewayError("channel send payload is outside the configured size")
        await self.send_impl(frame)             # ticket check lives in the gate

    async def close(self) -> None:
        """End this session with the generic policy close, never an app reason."""
        await self.close_impl()


OnMessage = Callable[[str, bytes], Awaitable["bytes | None"]]
OnOpen = Callable[[str, SessionChannel], Awaitable[None]]
OnClose = Callable[[str], Awaitable[None]]


class TlsAuthGate:
    """Admit authenticated identities, then stream binary frames to on_message."""

    def __init__(self, *, registry: IdentityRegistry, on_message: OnMessage,
                 on_open: OnOpen | None = None, on_close: OnClose | None = None,
                 max_sessions: int = MAX_SESSIONS,
                 max_message_bytes: int = MAX_MESSAGE_BYTES, max_queue: int = MAX_QUEUE,
                 open_timeout: float = OPEN_TIMEOUT,
                 logger: logging.Logger | None = None) -> None:
        if not isinstance(registry, IdentityRegistry):
            raise GatewayError("registry must be an IdentityRegistry")
        if not _is_async(on_message):
            raise GatewayError("on_message must be an async callable")
        for name, hook in (("on_open", on_open), ("on_close", on_close)):
            if hook is not None and not _is_async(hook):
                raise GatewayError(f"{name} must be an async callable or None")
        if not 1 <= int(max_sessions) <= 1024 or not 16 <= int(max_message_bytes) <= 65536:
            raise GatewayError("max_sessions/max_message_bytes out of range")
        if not 1 <= int(max_queue) <= 64 or not 0 < float(open_timeout) <= 60:
            raise GatewayError("max_queue/open_timeout out of range")
        self._registry, self._on_message = registry, on_message
        self._on_open, self._on_close = on_open, on_close
        self._max_sessions, self._max_bytes = int(max_sessions), int(max_message_bytes)
        self._max_queue, self._open_timeout = int(max_queue), float(open_timeout)
        self._log = make_logger("space_gateway.gate" if logger is None
                                else logger.name)
        self._active: dict[str, int] = {}
        self._tickets, self._server = 0, None

    @property
    def active_identities(self) -> tuple[str, ...]:
        return tuple(self._active)

    async def start(self, *, host: str, port: int,
                    ssl_context: ssl.SSLContext | None) -> int:
        """Start on a loopback address with TLS 1.3; return the bound port."""
        if not isinstance(ssl_context, ssl.SSLContext):
            raise TlsRequiredError("a server SSLContext is required; plaintext is refused")
        if ssl_context.minimum_version < ssl.TLSVersion.TLSv1_3:
            raise TlsRequiredError("TLS 1.3 minimum is required")
        if self._server is not None:
            raise GatewayError("gate already started")
        try:
            if not ip_address(host).is_loopback:
                raise ValueError(host)
        except ValueError as exc:
            raise GatewayError("this spike binds loopback addresses only") from exc
        self._server = await serve(
            self._handle, host, int(port), ssl=ssl_context,
            process_request=self._process_request, compression=None,
            max_size=self._max_bytes, max_queue=self._max_queue,
            open_timeout=self._open_timeout, logger=self._log)
        self._log.info("gate listening (loopback, TLS 1.3)")
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
            self._log.info("gate stopped")

    def _authenticate(self, headers: Headers) -> str | None:
        values = headers.get_all("Authorization")
        if len(values) != 1:                    # missing or duplicated header
            return None
        presented = _BEARER_RE.fullmatch(values[0].strip())
        return None if presented is None else self._registry.lookup(presented.group(1))

    def _process_request(self, connection: ServerConnection, request):
        if request.path != MP_PATH:             # '/mp' only, never a secret in a query
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if self._authenticate(request.headers) is None:
            self._log.warning("handshake rejected: unauthorized")
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    def _reserve(self, identity: str) -> int | None:
        if identity in self._active or len(self._active) >= self._max_sessions:
            return None
        self._tickets += 1
        self._active[identity] = self._tickets
        return self._tickets

    def _release(self, identity: str, ticket: int) -> None:
        if self._active.get(identity) == ticket:
            del self._active[identity]

    async def _handle(self, connection: ServerConnection) -> None:
        request = connection.request
        identity = None if request is None else self._authenticate(request.headers)
        if identity is None:                    # fail closed, before any callback
            await connection.close(code=CLOSE_POLICY, reason="unauthorized")
            return
        ticket = self._reserve(identity)
        if ticket is None:
            self._log.warning("session rejected: duplicate identity or capacity")
            await connection.close(code=CLOSE_POLICY, reason="unavailable")
            return
        self._log.info("session admitted")
        try:
            try:                                # only an admitted session has hooks
                await self._session(identity, ticket, connection)
            finally:                            # its own end, whatever caused it
                await self._closed(identity)    # a failure there is logged, not raised
        except PolicyViolation:                 # app-level reject: generic, no details
            self._log.warning("session closed by policy")
            await connection.close(code=CLOSE_POLICY, reason="rejected")
        except ConnectionClosed:                # the peer left while we were working
            self._log.info("session closed by peer")
        except Exception:                       # callback bug; no raw exception text
            self._log.error("session closed after a callback failure")
            await connection.close(code=CLOSE_INTERNAL, reason="internal")
        finally:                                # nested: the slot always comes back
            self._release(identity, ticket)
            self._log.debug("session closed identity=%s", identity)

    async def _session(self, identity: str, ticket: int,
                       connection: ServerConnection) -> None:
        """on_open first, so a producer may send before the peer ever sends."""
        if self._on_open is not None:
            await self._on_open(identity, self._channel(identity, ticket, connection))
        while True:
            try:
                message = await connection.recv()
            except ConnectionClosed:
                return                          # includes oversized 1009 closes
            if isinstance(message, str):
                self._log.warning("text frame rejected")
                await connection.close(code=CLOSE_UNSUPPORTED_DATA, reason="binary only")
                return
            reply = await self._on_message(identity, bytes(message))
            if reply is None:
                continue
            if not isinstance(reply, (bytes, bytearray, memoryview)):
                self._log.error("callback returned non-bytes")
                await connection.close(code=CLOSE_INTERNAL, reason="bad reply")
                return
            await connection.send(bytes(reply))

    async def _closed(self, identity: str) -> None:
        """Run on_close for an admitted session; its failure must not trap the gate."""
        if self._on_close is None:
            return
        try:
            await self._on_close(identity)
        except Exception:                       # CancelledError must still propagate
            self._log.error("close callback failed")

    def _channel(self, identity: str, ticket: int,
                 connection: ServerConnection) -> SessionChannel:
        """Bind the send side to exactly this admission, never to the identity name."""
        async def _send(frame: bytes) -> None:
            if self._active.get(identity) != ticket:
                raise GatewayError("channel belongs to a finished session")
            await connection.send(frame)

        async def _close() -> None:
            # This admission's connection only, and closing an already closed one
            # is a no-op: a retained channel can never touch a later session.
            await connection.close(code=CLOSE_POLICY, reason="rejected")

        return SessionChannel(identity=identity, ticket=ticket,
                              max_message_bytes=self._max_bytes,
                              send_impl=_send, close_impl=_close)


def load_server_tls_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """Load a server context with stdlib ssl, pinned to TLS 1.3 and above."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    except OSError as exc:                      # path only: contents stay secret
        raise GatewayError("could not load server certificate/key") from exc
    return context
