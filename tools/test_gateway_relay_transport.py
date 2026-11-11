#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Real loopback TLS + WebSocket + UDP tests for gateway/relay.py (UdpRelay).

Everything here uses real sockets: a TLS 1.3 gate on 127.0.0.1 with throwaway CA
certificates (client hostname verification stays ON), the real websockets client,
and a real UDP peer.  That UDP peer is a FAKE backend: it only records datagrams
and replays frames.  It is NOT FGMS — no player list, no callsign table, no
playerexpires sweep — so these tests prove the relay's own transport and lease
rules, never FGMS behaviour, and never a real fgfs client or a VPS deployment.

Timings such as grace_seconds=0.2 or fgms_player_expires=0.05 exist to make the
lease state machine observable inside a fast test.  They are NOT FGMS runtime
values; the shipped defaults (30 s grace for playerexpires=10 plus sweep margin)
are asserted separately, and the real expiry needs a genuine FGMS run.

One test uses a scripted SessionChannel with an Event instead of a network peer
to force backpressure deterministically; that exercises the bounded queue only
and is explicitly not real TCP/WebSocket congestion.
"""
import asyncio
import contextlib
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
from gateway.mp_protocol import (  # noqa: E402
    HEADER_STRUCT,
    MAGIC_FGFS,
    MAX_PACKET_SIZE,
    MSG_POS,
    PROTOCOL_VERSION,
)
from gateway.relay import (  # noqa: E402
    DEFAULT_EXPIRY_MARGIN,
    DEFAULT_FGMS_PLAYER_EXPIRES,
    DEFAULT_GRACE_SECONDS,
    DEFAULT_MAX_LEASES,
    DEFAULT_QUEUE_LIMIT,
    DEFAULT_SEND_TIMEOUT,
    ERR_ACTIVE_SESSION,
    ERR_AT_CAPACITY,
    ERR_CHANNEL,
    ERR_CLOSING,
    ERR_ENDPOINT,
    ERR_NO_SESSION,
    ERR_PACKET,
    RELAY_LOGGER_NAME,
    UDP_BIND_HOST,
    UdpRelay,
)
import test_gateway_auth as auth  # noqa: E402  cert/log helpers, no TestCase reuse

IDENTITIES = ("PILOT-A", "PILOT-B", "PILOT-C")
CALLSIGNS = {"PILOT-A": "PIL001", "PILOT-B": "PIL002", "PILOT-C": "PIL003"}
BODY = b"opaque-body-filler"
MARK = "leakchecksecret"
QUIET = 0.35                                   # "nothing arrived" window


def mp_packet(callsign: str, body: bytes = BODY, msg_id: int = MSG_POS,
              magic: int = MAGIC_FGFS, version: int = PROTOCOL_VERSION) -> bytes:
    """One syntactically valid MP datagram; the body stays opaque filler."""
    name = callsign.encode("ascii") + b"\x00" * (8 - len(callsign))
    return HEADER_STRUCT.pack(magic, version, msg_id, HEADER_STRUCT.size + len(body),
                              0, 0, name) + body


class FakeBackend(asyncio.DatagramProtocol):
    """FAKE MP backend: records datagrams and can answer. Not FGMS, no game state."""

    def __init__(self) -> None:
        self.datagrams: list[tuple[bytes, tuple[str, int]]] = []
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data, addr) -> None:
        self.datagrams.append((bytes(data), (addr[0], addr[1])))

    def reply(self, payload: bytes, addr) -> None:
        self.transport.sendto(payload, addr)


class ScriptedChannel:
    """Stand-in reverse sink: sends block on an Event (queue test only, not TCP)."""

    def __init__(self, *, fail=False, block=None, close_block=None):
        self.sent: list[bytes] = []
        self.closed = 0
        self.fail, self.block = fail, block
        self.close_block = close_block
        self.entered = asyncio.Event()

    def channel(self, identity="PILOT-A", ticket=1):
        return SessionChannel(identity=identity, ticket=ticket, max_message_bytes=1200,
                              send_impl=self._send, close_impl=self._close)

    async def _send(self, frame: bytes) -> None:
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.fail:
            raise RuntimeError(f"send boom {MARK}")
        self.sent.append(bytes(frame))

    async def _close(self) -> None:
        if self.close_block is not None:      # a peer that never finishes closing
            await self.close_block.wait()
        self.closed += 1


class RelayTransportTests(unittest.IsolatedAsyncioTestCase):
    """Two real WS clients, one fake UDP backend, one relay endpoint per identity."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="space-relay-transport-")
        ca, ca_key = auth._issue("Space Arena Relay CA", is_ca=True)
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
        self.tokens = {identity: generate_token() for identity in IDENTITIES}
        self.registry = IdentityRegistry.from_tokens(self.tokens)
        self.gate = None
        self.relay = None
        self.backend = None
        self.backend_proto = None
        self.backend_addr = None
        self.probes = []
        self.logger = make_logger(f"test.relay.{self.id()}")
        self.records = auth._Capture()
        self.logger.addHandler(self.records)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.relay_logger = logging.getLogger(RELAY_LOGGER_NAME)
        self.relay_records = auth._Capture()
        self.relay_logger.addHandler(self.relay_records)
        self.relay_logger.setLevel(logging.DEBUG)
        self.client_logger = logging.getLogger(f"test.relay.client.{self.id()}")
        self.client_logger.setLevel(logging.CRITICAL)
        self.client_logger.propagate = False
        self.asyncio_logger = logging.getLogger("asyncio")
        self.asyncio_records = auth._Capture()
        self.asyncio_logger.addHandler(self.asyncio_records)

    async def asyncTearDown(self):
        for probe in self.probes:
            probe.close()
        if self.gate is not None:
            await self.gate.stop()                     # documented shutdown order
        if self.relay is not None:
            await self.relay.close()
        if self.backend is not None:
            self.backend.close()
        for line in self.asyncio_records.lines:
            self.assertRegex(line, auth.EXPECTED_ASYNCIO_NOISE)
        self.asyncio_logger.removeHandler(self.asyncio_records)
        self.logger.removeHandler(self.records)
        self.relay_logger.removeHandler(self.relay_records)

    # -- harness -------------------------------------------------------------
    async def _bind_backend(self):
        loop = asyncio.get_running_loop()
        self.backend, self.backend_proto = await loop.create_datagram_endpoint(
            FakeBackend, local_addr=(UDP_BIND_HOST, 0))
        self.backend_addr = tuple(self.backend.get_extra_info("sockname"))

    async def _start(self, **relay_kwargs):
        await self._bind_backend()
        relay_kwargs.setdefault("grace_seconds", 0.2)        # fast FAKE timings, not
        relay_kwargs.setdefault("fgms_player_expires", 0.05)  # FGMS runtime values
        relay_kwargs.setdefault("expiry_margin", 0.05)
        self.relay = UdpRelay(backend=self.backend_addr, callsigns=CALLSIGNS,
                              **relay_kwargs)
        self.gate = TlsAuthGate(registry=self.registry,
                                on_message=self.relay.on_message,
                                on_open=self.relay.on_open,
                                on_close=self.relay.on_close,
                                max_message_bytes=1200, logger=self.logger)
        self.port = await self.gate.start(host="127.0.0.1", port=0,
                                          ssl_context=self.server_ctx)
        return self.port

    def _client_ctx(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True                  # never weakened here
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_verify_locations(cafile=str(self.ca_file))
        return ctx

    async def _connect(self, identity, *, token=None, path=MP_PATH):
        bearer = self.tokens[identity] if token is None else token
        return await connect(f"wss://127.0.0.1:{self.port}{path}", ssl=self._client_ctx(),
                             additional_headers=[("Authorization", f"Bearer {bearer}")],
                             proxy=None, open_timeout=5, close_timeout=5,
                             logger=self.client_logger)

    async def _admit(self, identity):
        # The gate frees its slot after on_close ran, so waiting here makes a
        # reconnect deterministic instead of racing the previous teardown.
        self.assertTrue(await self._until(lambda: identity not in self.gate.active_identities))
        ws = await self._connect(identity)
        self.assertTrue(await self._until(lambda: identity in self.relay.endpoints),
                        "no relay endpoint appeared for the admitted session")
        return ws

    def _port(self, identity) -> int:
        endpoint = self.relay.endpoints[identity]
        self.assertEqual(UDP_BIND_HOST, endpoint[0])
        return endpoint[1]

    async def _send_ws(self, ws, payload):
        await asyncio.wait_for(ws.send(payload), timeout=5)

    async def _recv(self, ws):
        return await asyncio.wait_for(ws.recv(), timeout=5)

    async def _expect_frame(self, ws, frame):
        self.assertEqual(frame, await self._recv(ws))

    async def _expect_quiet(self, ws):
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
        self.assertNotIn(MARK, (received.reason or "").lower())
        if reason is not None:
            self.assertEqual(reason, received.reason)
        await ws.close()

    async def _until(self, probe, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            value = probe()
            if value:
                return value
            await asyncio.sleep(0.01)
        return probe()

    async def _until_endpoints_empty(self, timeout=5.0):
        return bool(await self._until(lambda: not self.relay.endpoints, timeout))

    def _forwarded(self):
        return self.backend_proto.datagrams

    def _relay_tasks(self):
        """Relay-internal coroutines still alive in this loop (writers, creations)."""
        return [task for task in asyncio.all_tasks()
                if getattr(task.get_coro(), "__name__", "")
                in ("_drain", "_create_endpoint")]

    def relay_log(self):
        return "\n".join(self.relay_records.lines)

    def gate_log(self):
        return "\n".join(self.records.lines)

    # -- two clients, one datagram each way ---------------------------------
    async def test_two_clients_two_ports_and_one_datagram_each_way(self):
        await self._start()
        async with await self._admit("PILOT-A") as ws_a:
            async with await self._admit("PILOT-B") as ws_b:
                self.assertEqual({"PILOT-A", "PILOT-B"}, set(self.relay.endpoints))
                port_a, port_b = self._port("PILOT-A"), self._port("PILOT-B")
                self.assertNotEqual(port_a, port_b)         # one endpoint per identity
                frame = mp_packet("PIL001")
                await self._send_ws(ws_a, frame)
                self.assertTrue(await self._until(lambda: self._forwarded()))
                self.assertEqual([(frame, (UDP_BIND_HOST, port_a))], self._forwarded())
                other = mp_packet("PIL002")                 # another pilot's Name
                self.backend_proto.reply(other, (UDP_BIND_HOST, port_a))
                await self._expect_frame(ws_a, other)
                await self._expect_quiet(ws_b)              # never a broadcast
                await self._send_ws(ws_b, mp_packet("PIL002"))
                self.assertTrue(await self._until(lambda: len(self._forwarded()) == 2))
                self.assertEqual((mp_packet("PIL002"), (UDP_BIND_HOST, port_b)),
                                 self._forwarded()[1])      # verbatim, one datagram

    async def test_backend_frame_arrives_before_the_client_sends_anything(self):
        await self._start()
        async with await self._admit("PILOT-A") as ws:
            frame = mp_packet("PIL002")
            self.backend_proto.reply(frame, (UDP_BIND_HOST, self._port("PILOT-A")))
            await self._expect_frame(ws, frame)             # independent writer task
            self.assertEqual([], self._forwarded())         # the client sent nothing

    # -- admission refusals must not touch the transport ---------------------
    async def test_unauthorized_and_duplicate_never_open_or_change_endpoints(self):
        await self._start()
        async with await self._admit("PILOT-A") as ws:
            snapshot = self.relay.endpoints
            with self.assertRaises(InvalidStatus):           # wrong token: no session
                await self._connect("PILOT-A", token=generate_token())
            async with await self._connect("PILOT-A") as duplicate:
                await self._expect_closed(duplicate, 1008, "unavailable")
            self.assertEqual(snapshot, self.relay.endpoints)  # the one port, unchanged
            self.assertEqual(self._port("PILOT-A"), snapshot["PILOT-A"][1])
            self.assertEqual([], self._forwarded())
            await self._send_ws(ws, mp_packet("PIL001"))      # original unaffected
            self.assertTrue(await self._until(lambda: self._forwarded()))

    async def test_unknown_identity_and_refusals_are_generic(self):
        await self._start()
        scripted = ScriptedChannel()
        with self.assertRaises(PolicyViolation) as caught:
            await self.relay.on_open("NO-SUCH-IDENTITY", scripted.channel("NO-SUCH"))
        self.assertEqual(ERR_NO_SESSION, str(caught.exception))
        self.assertNotIn("NO-SUCH", str(caught.exception))
        with self.assertRaises(PolicyViolation):
            await self.relay.on_message("NO-SUCH-IDENTITY", mp_packet("PIL001"))
        await self.relay.on_close("NO-SUCH-IDENTITY")        # nothing to release
        self.assertEqual({}, self.relay.endpoints)
        self.assertEqual([], self._forwarded())

    async def test_foreign_channel_and_duplicate_session_are_refused_generically(self):
        await self._start(grace_seconds=1.0, fgms_player_expires=0.4, expiry_margin=0.1)
        scripted = ScriptedChannel()
        with self.assertRaises(PolicyViolation) as caught:
            await self.relay.on_open("PILOT-A", scripted.channel("PILOT-B"))
        self.assertEqual(ERR_CHANNEL, str(caught.exception))    # cross-wired sender
        self.assertEqual({}, self.relay.endpoints)              # nothing was created
        await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        port = self._port("PILOT-A")
        with self.assertRaises(PolicyViolation) as caught:      # session keeps the slot
            await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        self.assertEqual(ERR_ACTIVE_SESSION, str(caught.exception))
        self.assertNotIn("pil001", self.relay_log().lower())
        self.assertEqual({"PILOT-A"}, set(self.relay.endpoints))
        self.assertEqual(port, self._port("PILOT-A"))           # not replaced
        await self.relay.on_close("PILOT-A")                    # now reclaimable
        await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        self.assertEqual(port, self._port("PILOT-A"))

    async def test_endpoint_creation_failure_is_generic_and_leaks_nothing(self):
        await self._start()
        original = self.relay._create_endpoint          # fault injection, not FGMS

        async def broken(_identity):
            raise OSError("no port for you " + MARK)

        self.relay._create_endpoint = broken
        scripted = ScriptedChannel()
        with self.assertRaises(PolicyViolation) as caught:
            await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        self.assertEqual(ERR_ENDPOINT, str(caught.exception))
        self.assertNotIn(MARK, str(caught.exception).lower())
        self.assertNotIn(MARK, self.relay_log().lower())
        self.assertEqual({}, self.relay.endpoints)       # no half-built lease or port
        self.relay._create_endpoint = original
        await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        self.assertIn("PILOT-A", self.relay.endpoints)   # the pending slot was freed

    async def test_cancelled_endpoint_creation_does_not_pin_capacity(self):
        await self._start(max_leases=1)
        original = self.relay._create_endpoint
        entered = asyncio.Event()

        async def blocked(identity):
            entered.set()
            await asyncio.Future()

        self.relay._create_endpoint = blocked
        opening = asyncio.create_task(self.relay.on_open(
            "PILOT-A", ScriptedChannel().channel("PILOT-A")))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
            self.relay._create_endpoint = original
        self.assertFalse(self.relay._pending, "cancelled creation pinned a relay slot")
        await self.relay.on_open("PILOT-B", ScriptedChannel().channel("PILOT-B"))
        self.assertIn("PILOT-B", self.relay.endpoints)

    async def test_close_with_pending_creation_leaves_no_task_or_slot(self):
        await self._start()
        original = self.relay._create_endpoint
        release = asyncio.Event()

        async def slow(identity):
            await release.wait()                      # creation stuck on purpose
            return await original(identity)

        self.relay._create_endpoint = slow
        opening = asyncio.create_task(self.relay.on_open(
            "PILOT-A", ScriptedChannel().channel("PILOT-A")))
        await asyncio.sleep(0.05)                     # let it reach the await
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(self.relay.close(), 2)      # close never waits for it
        self.assertLess(loop.time() - started, 1.0)
        self.assertEqual(set(), self.relay._pending)       # slot freed, not pinned
        self.assertEqual({}, self.relay._creations)
        self.assertEqual({}, self.relay.endpoints)
        with self.assertRaises(PolicyViolation):           # the create learns the end
            await asyncio.wait_for(opening, 2)
        self.relay._create_endpoint = original
        release.set()                                 # a late result installs nothing
        await asyncio.sleep(0.05)
        self.assertEqual({}, self.relay.endpoints)
        self.assertEqual(set(), self.relay._pending)
        self.assertEqual(set(), self.relay._tasks)
        self.assertEqual([], self._relay_tasks())

    async def test_writer_failure_with_unclosing_channel_is_bounded(self):
        stuck = asyncio.Event()                       # a close that never finishes
        scripted = ScriptedChannel(fail=True, close_block=stuck)
        await self._start(send_timeout=0.05, grace_seconds=0.5,
                          fgms_player_expires=0.2, expiry_margin=0.05)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        self.backend_proto.reply(mp_packet("PIL002"), self.relay.endpoints["PILOT-A"])
        self.assertTrue(await self._until(
            lambda: "channel close failed" in self.relay_log()))
        self.assertLess(loop.time() - started, 2.0)   # send_timeout bound, no hang
        self.assertEqual([], scripted.sent)
        self.assertEqual(0, scripted.closed)          # the stuck close never returned
        self.assertIn("reverse send failed", self.relay_log())
        self.assertTrue(await self._until(lambda: not self._relay_tasks()))
        self.assertIn("PILOT-A", self.relay.endpoints)     # grace still honoured
        stuck.set()

    async def test_old_endpoint_cannot_inject_into_a_newer_lease(self):
        await self._start(grace_seconds=0.1, fgms_player_expires=0.02, expiry_margin=0.02)
        first = ScriptedChannel()
        await self.relay.on_open("PILOT-A", first.channel("PILOT-A"))
        old = self.relay._leases["PILOT-A"].protocol   # the endpoint generation
        await self.relay.on_close("PILOT-A")
        self.assertTrue(await self._until_endpoints_empty())   # grace elapsed
        second = ScriptedChannel()
        await self.relay.on_open("PILOT-A", second.channel("PILOT-A"))
        for _ in range(3):                             # late callbacks of the dead one
            old.datagram_received(mp_packet("PIL002"), self.relay._backend)
        await asyncio.sleep(0.05)
        self.assertEqual([], second.sent)              # nothing was injected
        self.assertEqual(1, self.relay_log().count("callback from a closed endpoint"))
        good = mp_packet("PIL003")
        self.backend_proto.reply(good, self.relay.endpoints["PILOT-A"])   # live path
        self.assertTrue(await self._until(lambda: second.sent == [good]))

    # -- boundaries of configuration, leases and closing ---------------------
    def test_configuration_boundaries_are_inclusive(self):
        backend = ("127.0.0.1", 5000)
        UdpRelay(backend=backend, callsigns={"x": "A", "i" * 64: "BCDEF6"})   # minimum
        UdpRelay(backend=backend, callsigns={"x": "ABCDEF"}, grace_seconds=22.0,
                 fgms_player_expires=10, expiry_margin=2)      # grace == 2*e + m
        UdpRelay(backend=backend, callsigns={"x": "A"}, max_leases=1024,
                 queue_limit=4096, send_timeout=3600)
        for callsigns in ({"x": ""}, {"x": "A" * 7}, {"": "A"}, {"i" * 65: "A"},
                          {"x": "ABCDEF", "y": "abcdef"}, {"x": "OBS001"},
                          {"x": "abc", "y": "ABC"}):
            with self.subTest(callsigns=len(callsigns)):
                with self.assertRaises(GatewayError):
                    UdpRelay(backend=backend, callsigns=callsigns)
        for kwargs in ({"grace_seconds": 21.999},                    # below 2*10 + 2
                       {"max_leases": 0}, {"max_leases": 1025},
                       {"queue_limit": 0}, {"queue_limit": 4097},
                       {"send_timeout": 3600.001}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                with self.assertRaises(GatewayError):
                    UdpRelay(backend=backend, callsigns={"x": "A"}, **kwargs)

    async def test_lease_cap_and_closing_boundaries(self):
        await self._start(max_leases=1, queue_limit=1, grace_seconds=1.0,
                          fgms_player_expires=0.4, expiry_margin=0.1)
        await self.relay.on_open("PILOT-A", ScriptedChannel().channel("PILOT-A"))
        port = self._port("PILOT-A")
        with self.assertRaises(PolicyViolation) as caught:      # the cap is exactly 1
            await self.relay.on_open("PILOT-B", ScriptedChannel().channel("PILOT-B"))
        self.assertEqual(ERR_AT_CAPACITY, str(caught.exception))
        self.assertEqual({"PILOT-A"}, set(self.relay.endpoints))  # nothing added
        self.assertEqual(port, self._port("PILOT-A"))             # nothing evicted
        await self.relay.close()
        await self.relay.close()                                  # idempotent
        self.assertEqual({}, self.relay.endpoints)
        scripted = ScriptedChannel()
        for hook, args in (("on_open", ("PILOT-A", scripted.channel("PILOT-A"))),
                           ("on_message", ("PILOT-A", mp_packet("PIL001")))):
            with self.subTest(hook=hook):
                with self.assertRaises(PolicyViolation) as caught:
                    await getattr(self.relay, hook)(*args)
                self.assertEqual(ERR_CLOSING, str(caught.exception))
        await self.relay.on_close("PILOT-A")                      # a no-op, not a raise
        self.assertEqual({}, self.relay.endpoints)                # nothing resurrected
        self.assertEqual([], self._relay_tasks())

    async def test_repeated_bad_traffic_counts_but_warns_once_per_lease(self):
        blocked = asyncio.Event()
        scripted = ScriptedChannel(block=blocked)
        await self._start(queue_limit=2)
        await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        endpoint = self.relay.endpoints["PILOT-A"]
        loop = asyncio.get_running_loop()
        rogue, _proto = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=(UDP_BIND_HOST, 0))
        self.probes.append(rogue)
        for _ in range(6):
            self.backend_proto.reply(b"junk-not-an-mp-frame", endpoint)   # bad frame
            rogue.sendto(mp_packet("PIL001"), endpoint)                   # bad source
            self.backend_proto.reply(mp_packet("PIL002"), endpoint)       # overflow
        for _ in range(40):                            # let every callback run
            await asyncio.sleep(0.005)
        lease = self.relay._leases["PILOT-A"]
        self.assertGreaterEqual(lease.ignored, 12)     # every packet still counted
        # queue_limit 2 plus one frame in flight bound the drops at >= 3 of 6 frames,
        # i.e. several overflow events happened while only one line was logged.
        self.assertGreaterEqual(lease.dropped, 3)
        logged = self.relay_log()
        self.assertEqual(1, logged.count("invalid backend frame ignored"))
        self.assertEqual(1, logged.count("datagram from an unexpected source ignored"))
        self.assertEqual(1, logged.count("reverse queue overflow"))
        blocked.set()
        await self.relay.on_close("PILOT-A")          # detach even with a send in flight
        self.assertTrue(await self._until(lambda: not self._relay_tasks()))

    async def test_cancel_after_bind_closes_unattached_socket(self):
        await self._start()
        original = self.relay._create_endpoint
        created = []
        opening = None

        async def create_then_cancel(identity):
            transport = await original(identity)
            created.append(transport)
            # Queue cancellation before the awaiting on_open resumes with the
            # result. This is distinct from cancellation before a socket binds.
            asyncio.get_running_loop().call_soon(opening.cancel)
            return transport

        self.relay._create_endpoint = create_then_cancel
        opening = asyncio.create_task(self.relay.on_open(
            "PILOT-A", ScriptedChannel().channel("PILOT-A")))
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(opening, 5)
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].is_closing())
            self.assertFalse(self.relay._pending)
            self.assertFalse(self.relay.endpoints)
            self.assertFalse(self.relay._protocols)
        finally:
            self.relay._create_endpoint = original
            for transport in created:
                transport.close()

    async def test_repeated_on_close_does_not_extend_the_grace(self):
        await self._start(grace_seconds=0.3, fgms_player_expires=0.1, expiry_margin=0.05)
        scripted = ScriptedChannel()
        await self.relay.on_open("PILOT-A", scripted.channel("PILOT-A"))
        await self.relay.on_close("PILOT-A")
        for _ in range(5):                                      # double, triple, ...
            await self.relay.on_close("PILOT-A")
        self.assertEqual(1, self.relay_log().count("endpoint held for grace"))
        self.assertIn("PILOT-A", self.relay.endpoints)          # grace still running
        self.assertTrue(await self._until_endpoints_empty())     # and it does end

    # -- invalid client frames never reach the backend -----------------------
    async def test_spoofed_callsign_is_refused_and_never_forwarded(self):
        await self._start()
        async with await self._admit("PILOT-A") as ws:
            await self._send_ws(ws, mp_packet("PIL002"))      # borrowed callsign
            await self._expect_closed(ws, 1008, "rejected")
        self.assertEqual([], self._forwarded())
        self.assertIn("session closed by policy", self.gate_log())
        self.assertIn(ERR_PACKET, self.relay_log())            # generic constant only
        self.assertNotIn(MARK, (self.gate_log() + self.relay_log()).lower())

    async def test_malformed_frames_are_refused_and_never_forwarded(self):
        cases = (b"not an mp packet at all...........",         # bad magic
                 mp_packet("PIL001") + b"\x00",                 # MsgLen lies
                 mp_packet("PIL001", magic=0x53464746),         # internal relay magic
                 b"\x00" * 8,                                   # below the header
                 mp_packet("PIL001", version=0x00020001))       # wrong protocol
        await self._start(grace_seconds=2.0, fgms_player_expires=0.8, expiry_margin=0.1)
        for frame in cases:
            with self.subTest(size=len(frame)):
                async with await self._admit("PILOT-A") as ws:
                    await self._send_ws(ws, frame)
                    await self._expect_closed(ws, 1008, "rejected")
        self.assertEqual([], self._forwarded())
        self.assertIn("PILOT-A", self.relay.endpoints)         # held, not leaked twice

    async def test_oversize_and_text_frames_keep_the_gate_frame_rules(self):
        await self._start()
        async with await self._admit("PILOT-A") as ws:
            await self._send_ws(ws, b"x" * (MAX_PACKET_SIZE + 1))
            await self._expect_closed(ws, 1009)                # gate frame bound
        self.assertEqual([], self._forwarded())
        async with await self._admit("PILOT-A") as ws:
            await self._send_ws(ws, "text frame")
            await self._expect_closed(ws, 1003, "binary only")
        self.assertEqual([], self._forwarded())

    async def test_invalid_backend_frames_and_rogue_senders_are_ignored(self):
        await self._start()
        async with await self._admit("PILOT-A") as ws:
            port = self._port("PILOT-A")
            loop = asyncio.get_running_loop()
            rogue, _proto = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, local_addr=(UDP_BIND_HOST, 0))
            self.probes.append(rogue)
            rogue.sendto(mp_packet("PIL001"), (UDP_BIND_HOST, port))    # wrong source
            self.backend_proto.reply(b"junk-not-an-mp-frame", (UDP_BIND_HOST, port))
            self.backend_proto.reply(mp_packet("PIL001", magic=0x53464746),
                                     (UDP_BIND_HOST, port))             # relay magic
            await self._expect_quiet(ws)
            good = mp_packet("PIL002")
            self.backend_proto.reply(good, (UDP_BIND_HOST, port))       # still working
            await self._expect_frame(ws, good)
            self.assertIn("invalid backend frame ignored", self.relay_log())

    # -- reconnect, grace and capacity --------------------------------------
    async def test_reconnect_within_grace_reuses_the_port_and_drops_stale_queue(self):
        await self._start(grace_seconds=2.0, fgms_player_expires=0.8, expiry_margin=0.1)
        async with await self._admit("PILOT-A") as ws:
            port = self._port("PILOT-A")
            self.backend_proto.reply(mp_packet("PIL002"), (UDP_BIND_HOST, port))
            self.backend_proto.reply(mp_packet("PIL003"), (UDP_BIND_HOST, port))
            await self._send_ws(ws, mp_packet("PIL001"))      # let the loop turn over
            await self._until(lambda: self._forwarded())
        self.assertIn("PILOT-A", self.relay.endpoints)        # held for the grace
        async with await self._admit("PILOT-A") as again:
            self.assertEqual(port, self._port("PILOT-A"))     # same backend source
            await self._expect_quiet(again)                   # stale queue not carried
            fresh = mp_packet("PIL003")
            self.backend_proto.reply(fresh, (UDP_BIND_HOST, port))
            await self._expect_frame(again, fresh)

    async def test_grace_expiry_releases_the_port_for_a_new_one(self):
        await self._start(grace_seconds=0.1, fgms_player_expires=0.02, expiry_margin=0.02)
        async with await self._admit("PILOT-A") as ws:
            port = self._port("PILOT-A")
            await ws.close()
        self.assertTrue(await self._until_endpoints_empty())    # the sweep ran
        async with await self._admit("PILOT-A") as again:
            self.assertNotEqual(port, self._port("PILOT-A"))    # a fresh endpoint
        self.assertIn("relay endpoint released", self.relay_log())

    async def test_capacity_counts_grace_leases_and_never_evicts_them(self):
        await self._start(max_leases=2, grace_seconds=2.0,
                          fgms_player_expires=0.8, expiry_margin=0.1)
        async with await self._admit("PILOT-A") as ws_a:
            port_a = self._port("PILOT-A")
            async with await self._admit("PILOT-B") as ws_b:
                await ws_a.close()                          # A only enters its grace
                self.assertTrue(await self._until(
                    lambda: "endpoint held for grace" in self.relay_log()))
                async with await self._connect("PILOT-C") as third:
                    await self._expect_closed(third, 1008, "rejected")   # no capacity
                    self.assertIn(ERR_AT_CAPACITY, self.relay_log())
                    self.assertEqual(port_a, self.relay.endpoints["PILOT-A"][1])
                    self.assertEqual({"PILOT-A", "PILOT-B"}, set(self.relay.endpoints))

    # -- shutdown ------------------------------------------------------------
    async def test_close_is_idempotent_and_leaves_no_relay_tasks(self):
        await self._start()
        async with await self._admit("PILOT-A") as ws:
            await self._admit("PILOT-B")
            self.assertTrue(await self._until(lambda: len(self.relay.endpoints) == 2))
            await self.relay.close()
            await self.relay.close()                        # idempotent
            self.assertEqual({}, self.relay.endpoints)
            scripted = ScriptedChannel()
            with self.assertRaises(PolicyViolation):
                await self.relay.on_open("PILOT-A", scripted.channel())
            draining = [task for task in asyncio.all_tasks()
                        if getattr(task.get_coro(), "__name__", "") == "_drain"]
            self.assertEqual([], draining)
            with contextlib.suppress(ConnectionClosed):
                await self._recv(ws)

    async def test_writer_failure_closes_only_its_own_channel(self):
        await self._start(grace_seconds=1.0, fgms_player_expires=0.4, expiry_margin=0.1)
        scripted = ScriptedChannel(fail=True)
        await self.relay.on_open("PILOT-A", scripted.channel())
        port = self._port("PILOT-A")
        self.backend_proto.reply(mp_packet("PIL002"), (UDP_BIND_HOST, port))
        self.assertTrue(await self._until(lambda: scripted.closed >= 1))
        self.assertEqual([], scripted.sent)
        self.assertIn("reverse send failed", self.relay_log())
        self.assertNotIn(MARK, self.relay_log().lower())      # no raw exception text
        self.assertIn("PILOT-A", self.relay.endpoints)        # grace still honoured

    async def test_overflow_drops_oldest_frames_and_stays_bounded(self):
        blocked = asyncio.Event()
        scripted = ScriptedChannel(block=blocked)
        await self._start(queue_limit=3)
        await self.relay.on_open("PILOT-A", scripted.channel())
        port = self._port("PILOT-A")
        frames = [mp_packet("PIL002", body=bytes([order]) * 8) for order in range(8)]
        self.backend_proto.reply(frames[0], (UDP_BIND_HOST, port))
        await scripted.entered.wait()                       # first send is in flight
        for frame in frames[1:]:
            self.backend_proto.reply(frame, (UDP_BIND_HOST, port))
        for _ in range(40):                                 # let every callback run
            await asyncio.sleep(0.005)
        blocked.set()
        self.assertTrue(await self._until(lambda: len(scripted.sent) == 4))
        self.assertEqual([frames[0], frames[5], frames[6], frames[7]], scripted.sent)
        self.assertIn("reverse queue overflow", self.relay_log())

    # -- construction rules --------------------------------------------------
    def test_constructor_refuses_any_other_destination_or_bad_setting(self):
        good = {"backend": ("127.0.0.1", 5000), "callsigns": dict(CALLSIGNS)}
        UdpRelay(**good)                                          # the documented shape
        for backend in (("10.0.0.1", 5000), ("localhost", 5000), ("fgms.example", 5000),
                        ("::1", 5000), ("127.0.0.1", 0), ("127.0.0.1", 65536),
                        ("127.0.0.1", "5000"), ("127.0.0.1", True), (None, 5000)):
            with self.subTest(backend=str(backend)):
                with self.assertRaises(GatewayError):
                    UdpRelay(**{**good, "backend": backend})
        for callsigns in ({},                                   # empty table
                          {"PILOT-A": "PIL001", "PILOT-B": "pil001"},   # casefold dup
                          {"PILOT-A": "obs001"},                 # reserved observer name
                          {"PILOT-A": "TOOLONGNAME"},            # > 6 chars
                          {"PILOT-A": "PIL-01"},                 # punctuation
                          {"PILOT-A": "PIL 01"},                 # space
                          {"": "PIL001"},                        # empty identity
                          {1: "PIL001"},                         # non-str identity
                          {"PILOT-A": "PIL001", "PILOT-B": 7},   # non-str callsign
                          ("PILOT-A", "PIL001")):                # not a mapping
            with self.subTest(bindings=len(callsigns)):
                with self.assertRaises(GatewayError):
                    UdpRelay(**{**good, "callsigns": callsigns})
        for kwargs in ({"grace_seconds": 21.99},                       # < 2*10 + 2
                       {"grace_seconds": 0}, {"grace_seconds": float("nan")},
                       {"grace_seconds": True}, {"queue_limit": 0},
                       {"queue_limit": True}, {"max_leases": 0}, {"max_leases": 1.5},
                       {"send_timeout": 0}, {"send_timeout": float("inf")},
                       {"expiry_margin": -1}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                with self.assertRaises(GatewayError):
                    UdpRelay(**good, **kwargs)
        relay = UdpRelay(**good)                                # documented defaults
        self.assertEqual(DEFAULT_GRACE_SECONDS, 30.0)
        self.assertEqual(DEFAULT_FGMS_PLAYER_EXPIRES, 10.0)
        self.assertEqual(DEFAULT_EXPIRY_MARGIN, 2.0)
        self.assertEqual(DEFAULT_MAX_LEASES, 8)
        self.assertEqual(DEFAULT_QUEUE_LIMIT, 8)
        self.assertEqual(DEFAULT_SEND_TIMEOUT, 5.0)
        self.assertEqual({}, relay.endpoints)                   # construction did no I/O

    # -- log hygiene ---------------------------------------------------------
    async def test_logs_carry_no_tokens_callsigns_or_packet_bodies(self):
        await self._start(grace_seconds=1.0, fgms_player_expires=0.4, expiry_margin=0.1)
        secret_body = b"BODY-" + MARK.encode()
        async with await self._admit("PILOT-A") as ws:
            await self._send_ws(ws, mp_packet("PIL001", body=secret_body))
            self.assertTrue(await self._until(lambda: self._forwarded()))
            port = self._port("PILOT-A")
            self.backend_proto.reply(mp_packet("PIL002", body=secret_body),
                                     (UDP_BIND_HOST, port))
            await self._expect_frame(ws, mp_packet("PIL002", body=secret_body))
            self.backend_proto.reply(b"junk-" + MARK.encode(), (UDP_BIND_HOST, port))
            await self._expect_quiet(ws)                     # refused, not forwarded
            async with await self._connect("PILOT-A") as dup:
                await self._expect_closed(dup, 1008, "unavailable")
        self.assertTrue(await self._until(
            lambda: "endpoint held for grace" in self.relay_log()))
        logged = (self.relay_log() + "\n" + self.gate_log()).lower()
        # attach, refused backend frame, detach: the relay really did log this session
        self.assertGreaterEqual(len(self.relay_records.lines), 3)
        for secret in (MARK, self.tokens["PILOT-A"], "bearer", "pil001", "pil002",
                       "body-"):
            self.assertNotIn(secret, logged)
        self.assertNotIn(UDP_BIND_HOST, self.relay_log())    # relay logs no addresses


if __name__ == "__main__":
    unittest.main()
