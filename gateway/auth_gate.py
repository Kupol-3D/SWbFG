#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""TLS + Bearer admission gate for the Space Arena transport spike.

Library only: no CLI, no autostart, no echo default, no UDP, no FGMS, no match
logic.  It authenticates the handshake (TLS 1.3, exact path ``/mp``,
``Authorization: Bearer <256-bit token>``), admits one session per identity up
to ``max_sessions``, then hands binary frames to the caller's async
``on_message(identity, payload)``.  Not production: loopback bind only, no rate
limit, no invitations, expiry, revocation or storage (see gateway/README.md).
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
from dataclasses import dataclass
from http import HTTPStatus
from ipaddress import ip_address
from types import MappingProxyType

from websockets.asyncio.server import Server, ServerConnection, serve
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


OnMessage = Callable[[str, bytes], Awaitable["bytes | None"]]


class TlsAuthGate:
    """Admit authenticated identities, then stream binary frames to on_message."""

    def __init__(self, *, registry: IdentityRegistry, on_message: OnMessage,
                 max_sessions: int = MAX_SESSIONS,
                 max_message_bytes: int = MAX_MESSAGE_BYTES, max_queue: int = MAX_QUEUE,
                 open_timeout: float = OPEN_TIMEOUT,
                 logger: logging.Logger | None = None) -> None:
        if not isinstance(registry, IdentityRegistry):
            raise GatewayError("registry must be an IdentityRegistry")
        if not (inspect.iscoroutinefunction(on_message)
                or inspect.iscoroutinefunction(getattr(on_message, "__call__", None))):
            raise GatewayError("on_message must be an async callable")
        if not 1 <= int(max_sessions) <= 1024 or not 16 <= int(max_message_bytes) <= 65536:
            raise GatewayError("max_sessions/max_message_bytes out of range")
        if not 1 <= int(max_queue) <= 64 or not 0 < float(open_timeout) <= 60:
            raise GatewayError("max_queue/open_timeout out of range")
        self._registry, self._on_message = registry, on_message
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
            while True:
                try:
                    message = await connection.recv()
                except ConnectionClosed:
                    break                       # includes oversized 1009 closes
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
        finally:                                # error, disconnect or cancellation
            self._release(identity, ticket)
            self._log.debug("session closed identity=%s", identity)


def load_server_tls_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """Load a server context with stdlib ssl, pinned to TLS 1.3 and above."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    except OSError as exc:                      # path only: contents stay secret
        raise GatewayError("could not load server certificate/key") from exc
    return context
