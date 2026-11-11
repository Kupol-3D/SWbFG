#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Real loopback TLS + WebSocket tests for the gate's lifecycle hooks.

These tests open real TCP/TLS 1.3 sockets on 127.0.0.1 with throwaway CA and
server certificates and talk to gateway/auth_gate.py with the real websockets
client; hostname verification stays ON (see the client context below).  They
cover ONLY the lifecycle additions on top of the existing admission contract:
optional ``on_open(identity, SessionChannel)`` / ``on_close(identity)``, the
ticket-bound send-only channel (a reverse stream while the peer sends nothing),
`PolicyViolation` -> generic close 1008, other callback failures -> generic
close 1011, and slot release under every failure.

`test_gateway_auth` is imported for its certificate/log helpers only; its
GateAuthTests class is deliberately NOT subclassed, so the 24 existing admission
tests keep running exactly once.  Nothing here starts FlightGear, FGMS, Docker
or a UDP endpoint, so a green run proves nothing about the relay transport or a
match (gateway/README.md).
"""
import asyncio
import contextlib
import dataclasses
import logging
import ssl
import sys
import tempfile
import unittest
from ipaddress import IPv4Address
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for entry in (str(ROOT), str(TOOLS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from cryptography import x509  # noqa: E402  (test-only, same as the auth suite)
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

from gateway.auth_gate import (  # noqa: E402
    GatewayError,
    IdentityRegistry,
    MP_PATH,
    PolicyViolation,
    SessionChannel,
    TlsAuthGate,
    generate_token,
    load_server_tls_context,
    make_logger,
)
from gateway.relay import (  # noqa: E402  (read-only use, relay.py is not changed)
    HEADER_STRUCT,
    MAGIC_FGFS,
    MSG_POS,
    PROTOCOL_VERSION,
    PacketError,
    validate_packet,
)
import test_gateway_auth as auth  # noqa: E402  helpers only, never GateAuthTests

PILLOT, OTHER = "PIL001", "PIL002"      # callsign-shaped identities
MARKER = "packetdetailsecret"           # must never reach a peer or a log
MAX_BYTES = 64                          # tiny configured frame bound for the guards
QUIET = 0.4                             # "nothing arrived" observation window


def mp_packet(callsign: str, msg_id: int = MSG_POS, body: bytes = b"\x00" * 8) -> bytes:
    """One MP datagram whose MsgLen matches reality; the body stays opaque filler."""
    name = callsign.encode("ascii") + b"\x00" * (8 - len(callsign))
    return HEADER_STRUCT.pack(MAGIC_FGFS, PROTOCOL_VERSION, msg_id,
                              HEADER_STRUCT.size + len(body), 0, 0, name) + body


class SyncCall:
    def __call__(self, *_args):
        return None


class AsyncCall:
    async def __call__(self, *_args):
        return None


class SessionChannelBufferTests(unittest.IsolatedAsyncioTestCase):
    def channel(self):
        async def unused(*args):
            raise AssertionError("rejected buffer reached the transport")

        return SessionChannel("fixture", 1, 1200, unused, unused)

    async def test_rejects_large_buffer_before_copy(self):
        class NoCopy(bytearray):
            def __bytes__(self):
                raise AssertionError("oversized channel buffer was copied")

        with self.assertRaises(GatewayError):
            await self.channel().send(NoCopy(1201))

    async def test_counts_memoryview_bytes_not_elements(self):
        view = memoryview(bytes(2400)).cast("I")
        self.assertLess(len(view), 1200)
        with self.assertRaises(GatewayError):
            await self.channel().send(view)

    async def test_released_view_returns_a_generic_error(self):
        view = memoryview(b"fixture")
        view.release()
        with self.assertRaises(GatewayError) as caught:
            await self.channel().send(view)
        self.assertNotIn("fixture", str(caught.exception))


class GateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Real TLS/WebSocket behaviour of on_open / on_close / SessionChannel."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="space-gate-lifecycle-")
        ca, ca_key = auth._issue("Space Arena Lifecycle CA", is_ca=True)
        server, server_key = auth._issue(
            "gateway.test",
            san=[x509.DNSName("gateway.test"), x509.IPAddress(IPv4Address("127.0.0.1"))],
            issuer=ca, issuer_key=ca_key)
        cls.dir = Path(cls.tmp.name)
        cls.ca_file = cls.dir / "ca.pem"
        cls.ca_file.write_bytes(auth._pem(ca))
        cert_file, key_file = cls.dir / "server.pem", cls.dir / "server.key"
        cert_file.write_bytes(auth._pem(server) + auth._pem(ca))
        key_file.write_bytes(auth._key_pem(server_key))
        key_file.chmod(0o600)
        cls.server_ctx = load_server_tls_context(str(cert_file), str(key_file))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.token_a, self.token_b = generate_token(), generate_token()
        self.registry = IdentityRegistry.from_tokens({PILLOT: self.token_a,
                                                      OTHER: self.token_b})
        self.gate = None
        self.messages, self.events, self.channels, self.tasks = [], [], [], []
        self.open_extra = self.close_extra = self.message_hook = None
        self.logger = make_logger(f"test.lifecycle.{self.id()}")
        self.records = auth._Capture()
        self.logger.addHandler(self.records)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.client_logger = logging.getLogger(f"test.lifecycle.client.{self.id()}")
        self.client_logger.setLevel(logging.CRITICAL)
        self.client_logger.propagate = False
        self.asyncio_logger = logging.getLogger("asyncio")
        self.asyncio_records = auth._Capture()
        self.asyncio_logger.addHandler(self.asyncio_records)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):   # task noise must not hide one
                await task
        if self.gate is not None:
            await self.gate.stop()
        for line in self.asyncio_records.lines:
            self.assertRegex(line, auth.EXPECTED_ASYNCIO_NOISE)
        self.asyncio_logger.removeHandler(self.asyncio_records)
        self.logger.removeHandler(self.records)
        self.logger.setLevel(logging.NOTSET)

    # -- callbacks under test (they also record what happened) ---------------
    async def _on_message(self, identity, payload):
        self.events.append(("message", identity))
        self.messages.append((identity, bytes(payload)))
        if self.message_hook is None:
            return None
        return await self.message_hook(identity, payload)

    async def _on_open(self, identity, channel):
        self.events.append(("open", identity, channel.ticket))
        self.channels.append(channel)
        if self.open_extra is not None:
            await self.open_extra(identity, channel)

    async def _on_close(self, identity):
        self.events.append(("close", identity))
        if self.close_extra is not None:
            await self.close_extra(identity)

    # -- harness -------------------------------------------------------------
    def _gate(self, **kwargs):
        kwargs.setdefault("logger", self.logger)
        kwargs.setdefault("on_message", self._on_message)
        kwargs.setdefault("on_open", self._on_open)
        kwargs.setdefault("on_close", self._on_close)
        self.message_hook = kwargs.pop("message_hook", None)
        self.gate = TlsAuthGate(registry=self.registry, **kwargs)
        return self.gate

    async def _start(self, **kwargs):
        return await self._gate(**kwargs).start(host="127.0.0.1", port=0,
                                                ssl_context=self.server_ctx)

    def _client_ctx(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True                 # never weakened in this suite
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_verify_locations(cafile=str(self.ca_file))
        return ctx

    async def _connect(self, port, *, token, path=MP_PATH):
        return await connect(f"wss://127.0.0.1:{port}{path}", ssl=self._client_ctx(),
                             additional_headers=[("Authorization", f"Bearer {token}")],
                             proxy=None, open_timeout=5, close_timeout=5,
                             logger=self.client_logger)

    async def _recv(self, ws):
        return await asyncio.wait_for(ws.recv(), timeout=5)

    async def _send(self, ws, payload):
        await asyncio.wait_for(ws.send(payload), timeout=5)

    async def _expect_nothing(self, ws):
        """The peer must stay silent: a frame here means a leak into its session."""
        try:
            arrived = await asyncio.wait_for(ws.recv(), timeout=QUIET)
        except asyncio.TimeoutError:
            return
        self.fail(f"unexpected frame: {arrived!r}")

    async def _expect_closed(self, ws, code, reason=None):
        with self.assertRaises(ConnectionClosed) as caught:
            await self._recv(ws)
        received = caught.exception.rcvd
        self.assertEqual(code, received.code)
        self.assertNotIn(MARKER, (received.reason or "").lower())  # no packet details
        if reason is not None:
            self.assertEqual(reason, received.reason)
        await ws.close()
        return received

    async def _wait_for(self, predicate, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return predicate()

    async def _wait_events(self, expected):
        """Hook order is asserted only after the server finished its own teardown."""
        self.assertTrue(await self._wait_for(lambda: self.events == expected),
                        f"events={self.events!r} expected={expected!r}")

    def _spawn(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        self.tasks.append(task)
        return task

    def logged(self):
        return "\n".join(self.records.lines)

    def leaks_in_log(self):
        return [line for line in self.records.lines if MARKER in line.lower()]

    # -- reverse stream, independent of any client send ----------------------
    async def test_on_open_pushes_before_any_client_send(self):
        async def producer(identity, channel):
            self._spawn(self._push(channel, (b"hello-1", b"hello-2")))

        port = await self._start()
        self.open_extra = producer
        async with await self._connect(port, token=self.token_a) as ws:
            self.assertEqual(b"hello-1", await self._recv(ws))
            self.assertEqual(b"hello-2", await self._recv(ws))
            self.assertEqual([], self.messages)             # the client never sent
            self.assertEqual([("open", PILLOT, 1)], self.events)
            self.assertEqual((PILLOT,), self.gate.active_identities)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT)])

    async def _push(self, channel, frames):
        for frame in frames:
            await asyncio.sleep(0.02)
            await channel.send(frame)

    async def test_hook_order_open_message_close_on_client_exit(self):
        port = await self._start()
        async with await self._connect(port, token=self.token_a) as ws:
            await self._send(ws, b"move")
            self.assertTrue(await self._wait_for(lambda: len(self.messages) == 1))
            self.assertEqual([("open", PILLOT, 1), ("message", PILLOT)], self.events)
            self.assertEqual([(PILLOT, b"move")], self.messages)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        self.assertEqual([("open", PILLOT, 1), ("message", PILLOT), ("close", PILLOT)],
                         self.events)

    async def test_no_hooks_for_unauthorized_unknown_path_duplicate_or_capacity(self):
        port = await self._start(max_sessions=1)
        with self.assertRaises(InvalidStatus):               # wrong token: no session
            await self._connect(port, token=generate_token())
        with self.assertRaises(InvalidStatus):               # wrong path: no session
            await self._connect(port, token=self.token_a, path="/other")
        self.assertEqual([], self.events)
        async with await self._connect(port, token=self.token_a) as first:
            async with await self._connect(port, token=self.token_a) as duplicate:
                await self._expect_closed(duplicate, 1008, "unavailable")
            async with await self._connect(port, token=self.token_b) as capped:
                await self._expect_closed(capped, 1008, "unavailable")
            self.assertEqual([("open", PILLOT, 1)], self.events)   # refusal is silent
            await self.channels[0].send(b"still-mine")             # original unharmed
            self.assertEqual(b"still-mine", await self._recv(first))
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT)])

    async def test_text_frame_is_still_rejected_with_hooks_active(self):
        port = await self._start()
        async with await self._connect(port, token=self.token_a) as ws:
            await self._send(ws, "not binary")
            await self._expect_closed(ws, 1003, "binary only")
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT)])
        self.assertEqual([], self.messages)                  # the callback never saw it

    # -- PolicyViolation: generic 1008, never packet details -----------------
    async def test_policy_violation_from_message_closes_generic_1008(self):
        async def reject(identity, payload):
            raise PolicyViolation(f"bad msglen {len(payload)} {MARKER}")

        port = await self._start(message_hook=reject)
        async with await self._connect(port, token=self.token_a) as ws:
            await self._send(ws, b"\x00\x01\x02")
            await self._expect_closed(ws, 1008, "rejected")
        await self._wait_events([("open", PILLOT, 1), ("message", PILLOT),
                                 ("close", PILLOT)])
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        self.assertIn("session closed by policy", self.logged())
        self.assertEqual([], self.leaks_in_log())

    async def test_policy_violation_from_on_open_closes_1008_before_messages(self):
        async def refuse(identity, channel):
            raise PolicyViolation(f"no lease for {identity} {MARKER}")

        port = await self._start()
        self.open_extra = refuse
        async with await self._connect(port, token=self.token_a) as ws:
            await self._expect_closed(ws, 1008, "rejected")
        self.assertEqual([], self.messages)
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT)])
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        self.assertEqual([], self.leaks_in_log())

    # -- other callback failures: generic 1011, slot always released ---------
    async def test_open_failure_closes_1011_runs_close_and_releases(self):
        async def boom(identity, channel):
            raise RuntimeError(f"open boom {MARKER}")

        port = await self._start()
        self.open_extra = boom
        async with await self._connect(port, token=self.token_a) as ws:
            await self._expect_closed(ws, 1011, "internal")
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT)])
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        self.assertIn("session closed after a callback failure", self.logged())
        self.assertEqual([], self.leaks_in_log())

    async def test_message_failure_closes_1011_and_releases_for_reconnect(self):
        async def boom(identity, payload):
            raise RuntimeError(f"message boom {MARKER}")

        port = await self._start(message_hook=boom)
        async with await self._connect(port, token=self.token_a) as ws:
            await self._send(ws, b"junk")
            await self._expect_closed(ws, 1011, "internal")
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        self.assertEqual([], self.leaks_in_log())
        async with await self._connect(port, token=self.token_a) as again:
            self.assertTrue(await self._wait_for(lambda: len(self.channels) == 2))
            await self._send(again, b"retry")
            self.assertEqual(2, self.channels[-1].ticket)

    async def test_handler_cancellation_runs_close_and_releases(self):
        held = {}

        async def grab(identity, channel):        # the gate calls us inside its task
            held["task"] = asyncio.current_task()
            held["channel"] = channel

        port = await self._start()
        self.open_extra = grab
        async with await self._connect(port, token=self.token_a) as ws:
            self.assertTrue(await self._wait_for(lambda: "task" in held))
            held["task"].cancel()                 # same teardown as gate.stop()
            with contextlib.suppress(ConnectionClosed):
                await self._recv(ws)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT)])
        async with await self._connect(port, token=self.token_a) as again:
            self.assertTrue(await self._wait_for(lambda: len(self.channels) == 2))
            await self._send(again, b"after-cancel")
            self.assertTrue(await self._wait_for(lambda: len(self.messages) == 1))

    async def test_close_failure_still_releases_the_slot(self):
        async def bad_close(identity):
            raise RuntimeError(f"close boom {MARKER}")

        port = await self._start()
        self.close_extra = bad_close
        async with await self._connect(port, token=self.token_a) as ws:
            await self._send(ws, b"first")
            self.assertTrue(await self._wait_for(lambda: len(self.messages) == 1))
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        self.assertIn("close callback failed", self.logged())
        self.assertEqual([], self.leaks_in_log())
        async with await self._connect(port, token=self.token_a) as again:
            self.assertTrue(await self._wait_for(lambda: len(self.channels) == 2))
            await self._send(again, b"second")
            self.assertTrue(await self._wait_for(lambda: len(self.messages) == 2))

    # -- the channel belongs to one admission, not to the identity name -----
    async def test_stale_channel_cannot_send_into_a_new_session(self):
        port = await self._start()
        async with await self._connect(port, token=self.token_a) as first:
            self.assertTrue(await self._wait_for(lambda: len(self.channels) == 1))
            await self.channels[0].send(b"mine")
            self.assertEqual(b"mine", await self._recv(first))
            stale = self.channels[0]
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        async with await self._connect(port, token=self.token_a) as second:
            self.assertTrue(await self._wait_for(lambda: len(self.channels) == 2))
            fresh = self.channels[-1]
            self.assertEqual(stale.identity, fresh.identity)
            self.assertEqual((stale.ticket, fresh.ticket), (1, 2))
            with self.assertRaises(GatewayError) as caught:
                await stale.send(b"into-the-new-session")
            self.assertNotIn(PILLOT, str(caught.exception))    # generic, no identity
            await self._expect_nothing(second)                 # nothing leaked to the new
            await fresh.send(b"fresh-session")                 # session; the new one works
            self.assertEqual(b"fresh-session", await self._recv(second))
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT),
                                 ("open", PILLOT, 2), ("close", PILLOT)])

    async def test_send_after_the_session_is_gone_fails_fast(self):
        outcome = {}

        async def producer(identity, channel):        # on_open must not block the gate,
            self._spawn(self._blast(channel, outcome))  # so the stream runs as a task

        port = await self._start()
        self.open_extra = producer
        ws = await self._connect(port, token=self.token_a)
        self.assertEqual(b"tick", await self._recv(ws))
        await ws.close()                              # the client leaves on purpose
        self.assertTrue(await self._wait_for(lambda: "error" in outcome),
                        f"outcome={outcome!r} events={self.events!r}")
        self.assertIsInstance(outcome["error"], GatewayError)
        self.assertNotIn(PILLOT, str(outcome["error"]))
        self.assertEqual((), self.gate.active_identities)      # nothing borrowed

    async def _blast(self, channel, outcome):
        for _ in range(200):                          # bounded: a bug cannot hang here
            try:
                await channel.send(b"tick")
            except GatewayError as exc:               # the ticket is gone: the contract
                outcome["error"] = exc
                return
            except Exception as exc:                  # peer left, slot not yet released
                outcome.setdefault("earlier", type(exc).__name__)
            await asyncio.sleep(0.02)

    async def test_channel_send_guards_shape_and_size_without_echoing(self):
        probes = ("text payload", None, 7, b"", b"x" * (MAX_BYTES + 1), bytearray(b"y") * 200)
        rejected = []

        async def probe(identity, channel):
            for value in probes:
                try:
                    await channel.send(value)
                except GatewayError as exc:
                    rejected.append(exc)
                else:
                    rejected.append(None)                   # must never be accepted
            await channel.send(b"x" * MAX_BYTES)            # the valid frame still fits

        port = await self._start(max_message_bytes=MAX_BYTES)
        self.open_extra = probe
        async with await self._connect(port, token=self.token_a) as ws:
            self.assertEqual(b"x" * MAX_BYTES, await self._recv(ws))   # session survives
        self.assertEqual(len(probes), len(rejected))
        for refusal in rejected:
            self.assertIsInstance(refusal, GatewayError)
            for secret in (PILLOT, "text payload", "x" * 8, "y" * 8):
                self.assertNotIn(secret, str(refusal))      # shape/size only, no echo

    async def test_channel_close_uses_generic_1008(self):
        async def closer(identity, channel):
            self._spawn(self._close_after(channel))

        port = await self._start()
        self.open_extra = closer
        async with await self._connect(port, token=self.token_a) as ws:
            await self._expect_closed(ws, 1008, "rejected")
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        await self._wait_events([("open", PILLOT, 1), ("close", PILLOT)])

    async def _close_after(self, channel):
        await asyncio.sleep(0.02)
        await channel.close()

    # -- relay-shaped integration over real TLS, and pure channel checks -----
    async def test_relay_style_hooks_bind_callsign_to_identity(self):
        class RelayStyle:                # shape of the next TASK5 step, no transport
            async def on_message(self, identity, payload):
                try:
                    validate_packet(payload, expected_callsign=identity)
                except PacketError as exc:
                    raise PolicyViolation(f"packet refused: {exc}") from exc
                return None

            async def on_open(self, identity, channel):
                await channel.send(b"welcome")

            async def on_close(self, identity):
                return None

        style = RelayStyle()
        gate = TlsAuthGate(registry=self.registry, on_message=style.on_message,
                           on_open=style.on_open, on_close=style.on_close,
                           max_message_bytes=1200, logger=self.logger)
        self.gate = gate
        port = await gate.start(host="127.0.0.1", port=0, ssl_context=self.server_ctx)
        async with await self._connect(port, token=self.token_a) as honest:
            self.assertEqual(b"welcome", await self._recv(honest))   # unsolicited frame
            await self._send(honest, mp_packet(PILLOT))               # own callsign
            await self._expect_nothing(honest)                        # stays admitted
            self.assertEqual((PILLOT,), self.gate.active_identities)
        async with await self._connect(port, token=self.token_a) as borrower:
            self.assertEqual(b"welcome", await self._recv(borrower))
            await self._send(borrower, mp_packet(OTHER))              # borrowed callsign
            await self._expect_closed(borrower, 1008, "rejected")

    async def test_channel_is_frozen_send_only_and_validates_payloads(self):
        sent = []

        async def send_impl(frame):
            sent.append(frame)

        async def close_impl():
            sent.append("closed")

        channel = SessionChannel(identity=PILLOT, ticket=7, max_message_bytes=MAX_BYTES,
                                 send_impl=send_impl, close_impl=close_impl)
        self.assertNotIn("recv", dir(channel))            # no read side, ever
        self.assertNotIn("recv", dir(SessionChannel))
        self.assertIn(PILLOT, repr(channel))
        self.assertNotIn("send_impl", repr(channel))      # wired internals stay hidden
        with self.assertRaises(dataclasses.FrozenInstanceError):
            channel.ticket = 8
        await channel.send(bytearray(b"ok"))              # bytes-like is fine
        await channel.close()
        self.assertEqual([b"ok", "closed"], sent)
        for bad in ("text", None, 5, b"", b"z" * (MAX_BYTES + 1)):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(GatewayError) as caught:
                    await channel.send(bad)
                self.assertNotIn(PILLOT, str(caught.exception))

    def test_constructor_requires_async_optional_hooks(self):
        base = {"registry": self.registry, "on_message": self._on_message}
        TlsAuthGate(**base)                                # hooks stay optional
        TlsAuthGate(**base, on_open=None, on_close=None)
        for kwargs in ({"on_open": lambda identity, channel: None},
                       {"on_close": lambda identity: None},
                       {"on_open": object()}, {"on_close": SyncCall()}):
            with self.subTest(**{key: type(value).__name__ for key, value in kwargs.items()}):
                with self.assertRaises(GatewayError):
                    TlsAuthGate(**base, **kwargs)
        template = TlsAuthGate(registry=self.registry, on_message=self._on_message,
                               on_open=AsyncCall(), on_close=AsyncCall(),
                               max_message_bytes=1200)     # documented next-step shape
        self.assertIsInstance(template, TlsAuthGate)


if __name__ == "__main__":
    unittest.main()
