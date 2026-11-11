#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Executable loopback tests for gateway/auth_gate.py (real TCP + TLS 1.3 + WS).

These tests start the gate on 127.0.0.1 with an ephemeral port and talk to it
with the real websockets client over real TLS sockets.  Certificates are
throwaway CA/server pairs written to a temp dir with `cryptography` (test-only
dependency); the client uses that CA and verifies the hostname.  The "echo"
callback here is test transport only: it is NOT FGMS, NOT Nasal, NOT game
traffic, and no UDP socket is created anywhere.  Passing these tests proves
nothing about FlightGear, FGMS or a VPS deployment (gateway/README.md).
"""
import asyncio
import logging
import re
import ssl
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidMessage, InvalidStatus

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.auth_gate import (  # noqa: E402  (path set above on purpose)
    MP_PATH,
    IdentityRegistry,
    TlsAuthGate,
    TlsRequiredError,
    GatewayError,
    generate_token,
    load_server_tls_context,
    make_logger,
    redact,
)

MAX_MESSAGE = 4096
# Only these asyncio log lines may appear: our tests abort TLS handshakes by design.
EXPECTED_ASYNCIO_NOISE = re.compile(
    r"(?i)(error on transport creation|unhandled exception in connection made|"
    r"connectionreseterror|connectionresetbypeer|sslsyscall|certificate verify|"
    r"handshake|alert|eof occurred|broken pipe|timed out)")
SOURCE = (ROOT / "gateway" / "auth_gate.py").read_text(encoding="utf-8")
README = (ROOT / "gateway" / "README.md").read_text(encoding="utf-8")


def _issue(common_name, san=None, issuer=None, issuer_key=None, is_ca=False):
    """Return (certificate, private key) signed by `issuer` (self-signed if None)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(subject)
               .issuer_name(issuer.subject if issuer is not None else subject)
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(days=1))
               .not_valid_after(now + timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None),
                              critical=True))
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    if not is_ca:
        builder = builder.add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False,
                          key_encipherment=True, data_encipherment=False,
                          key_agreement=False, key_cert_sign=False, crl_sign=False,
                          encipher_only=False, decipher_only=False), critical=True)
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(issuer_key if issuer_key is not None else key, hashes.SHA256()), key


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key):
    return key.private_bytes(encoding=serialization.Encoding.PEM,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:                        # a broken test must not hide data
            self.lines.append("<unformattable record>")


class GateAuthTests(unittest.IsolatedAsyncioTestCase):
    """Real loopback TLS/WebSocket behaviour of the admission gate."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="space-gate-test-")
        ca, ca_key = _issue("Space Arena Test CA", is_ca=True)
        rogue, rogue_key = _issue("Rogue Test CA", is_ca=True)
        server, server_key = _issue(
            "gateway.test",
            san=[x509.DNSName("gateway.test"), x509.IPAddress(
                __import__("ipaddress").IPv4Address("127.0.0.1"))],
            issuer=ca, issuer_key=ca_key)
        cls.dir = Path(cls.tmp.name)
        cls.ca_file = cls.dir / "ca.pem"
        cls.rogue_ca_file = cls.dir / "rogue-ca.pem"
        cls.cert_file = cls.dir / "server.pem"
        cls.key_file = cls.dir / "server.key"
        cls.ca_file.write_bytes(_pem(ca))
        cls.rogue_ca_file.write_bytes(_pem(rogue))
        cls.cert_file.write_bytes(_pem(server) + _pem(ca))
        cls.key_file.write_bytes(_key_pem(server_key))
        cls.key_file.chmod(0o600)
        # the rogue CA above exists only as rogue-ca.pem below: never trusted here
        cls.server_ctx = load_server_tls_context(str(cls.cert_file), str(cls.key_file))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.token_a = generate_token()
        self.token_b = generate_token()
        self.unknown_token = generate_token()
        self.registry = IdentityRegistry.from_tokens(
            {"PILOT-A": self.token_a, "PILOT-B": self.token_b})
        self.calls = []
        self.gate = None
        self.logger = make_logger(f"test.gate.{self.id()}")
        self.records = _Capture()
        self.logger.addHandler(self.records)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.client_logger = logging.getLogger(f"test.client.{self.id()}")
        self.client_logger.setLevel(logging.CRITICAL)
        self.client_logger.propagate = False
        # These tests abort TLS handshakes on purpose (bad CA, bad hostname,
        # plaintext client).  asyncio logs those resets through its own logger;
        # we capture them instead of silencing them and assert in tearDown that
        # NOTHING ELSE (no leaked task, no unexpected handler failure) showed up.
        self.asyncio_logger = logging.getLogger("asyncio")
        self.asyncio_records = _Capture()
        self.asyncio_logger.addHandler(self.asyncio_records)

    async def asyncTearDown(self):
        if self.gate is not None:
            await self.gate.stop()
        for line in self.asyncio_records.lines:
            self.assertRegex(line, EXPECTED_ASYNCIO_NOISE)
        self.asyncio_logger.removeHandler(self.asyncio_records)
        self.logger.removeHandler(self.records)
        self.logger.setLevel(logging.NOTSET)

    # -- helpers -------------------------------------------------------------
    async def _echo(self, identity, payload):
        self.calls.append((identity, bytes(payload)))
        return bytes(payload)

    async def _boom(self, identity, payload):
        self.calls.append((identity, bytes(payload)))
        if len(self.calls) == 1:                # only the first delivery fails
            raise RuntimeError("callback failed")
        return bytes(payload)

    def _gate(self, **kwargs):
        kwargs.setdefault("logger", self.logger)
        kwargs.setdefault("on_message", self._echo)
        self.gate = TlsAuthGate(registry=self.registry, **kwargs)
        return self.gate

    async def _start(self, **kwargs):
        return await self._gate(**kwargs).start(
            host="127.0.0.1", port=0, ssl_context=self.server_ctx)

    def _client_ctx(self, ca_file=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_verify_locations(cafile=str(ca_file or self.ca_file))
        return ctx

    async def _connect(self, port, *, token=None, path=MP_PATH, headers=None,
                       ca_file=None, server_hostname=None):
        auth = headers if headers is not None else (
            [] if token is None else [("Authorization", f"Bearer {token}")])
        return await connect(f"wss://127.0.0.1:{port}{path}",
                             ssl=self._client_ctx(ca_file), additional_headers=auth,
                             proxy=None, open_timeout=5, close_timeout=5,
                             server_hostname=server_hostname,
                             logger=self.client_logger)

    async def _expect_closed(self, ws, code):
        with self.assertRaises(ConnectionClosed) as caught:
            await asyncio.wait_for(ws.recv(), timeout=5)
        self.assertEqual(code, caught.exception.rcvd.code)
        await ws.close()

    async def _wait_for(self, predicate, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return predicate()

    def logged(self):
        return "\n".join(self.records.lines)

    # -- happy path over real TLS -------------------------------------------
    async def test_valid_token_binary_two_messages_boundaries(self):
        port = await self._start()
        async with await self._connect(port, token=self.token_a) as ws:
            cipher = ws.transport.get_extra_info("cipher")
            self.assertEqual("TLSv1.3", cipher[1])
            self.assertIsNotNone(ws.transport.get_extra_info("peercert"))
            self.assertEqual(("PILOT-A",), self.gate.active_identities)
            await ws.send(b"\x00")
            self.assertEqual(b"\x00", await asyncio.wait_for(ws.recv(), timeout=5))
            await ws.send(b"z" * MAX_MESSAGE)
            self.assertEqual(b"z" * MAX_MESSAGE, await asyncio.wait_for(ws.recv(), timeout=5))
        self.assertEqual([("PILOT-A", b"\x00"), ("PILOT-A", b"z" * MAX_MESSAGE)], self.calls)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))

    async def test_second_identity_admitted_up_to_max_sessions(self):
        port = await self._start(max_sessions=2)
        async with await self._connect(port, token=self.token_a) as first:
            async with await self._connect(port, token=self.token_b) as second:
                await first.send(b"a")
                await second.send(b"b")
                self.assertEqual(b"a", await asyncio.wait_for(first.recv(), timeout=5))
                self.assertEqual(b"b", await asyncio.wait_for(second.recv(), timeout=5))
                self.assertEqual({"PILOT-A", "PILOT-B"}, set(self.gate.active_identities))

    # -- authentication failures --------------------------------------------
    def _assert_unauthorized(self, ws_error, token=None):
        self.assertEqual(401, ws_error.response.status_code)
        body = ws_error.response.body.decode("utf-8", "replace").lower()
        self.assertNotIn("bearer", body)
        if token:
            self.assertNotIn(token, ws_error.response.body.decode("latin-1"))
            self.assertNotIn(token, self.logged())

    async def test_missing_authorization_rejected_401_without_callback(self):
        port = await self._start()
        with self.assertRaises(InvalidStatus) as caught:
            await self._connect(port)
        self._assert_unauthorized(caught.exception)
        self.assertEqual([], self.calls)

    async def test_wrong_token_rejected_401_without_callback(self):
        port = await self._start()
        with self.assertRaises(InvalidStatus) as caught:
            await self._connect(port, token=self.unknown_token)
        self._assert_unauthorized(caught.exception, token=self.unknown_token)
        self.assertEqual([], self.calls)

    async def test_duplicate_authorization_headers_rejected_401(self):
        port = await self._start()
        headers = [("Authorization", f"Bearer {self.token_a}"),
                   ("Authorization", f"Bearer {self.unknown_token}")]
        with self.assertRaises(InvalidStatus) as caught:
            await self._connect(port, headers=headers)
        self._assert_unauthorized(caught.exception, token=self.token_a)
        self.assertEqual([], self.calls)

    async def test_non_bearer_scheme_and_loose_value_rejected(self):
        port = await self._start()
        for value in (f"Basic {self.token_a}", f"Bearer  {self.token_a}",
                      "Bearer short", "Bearer", f"Bearer {self.token_a.upper()}"):
            with self.subTest(value=value[:12]):
                with self.assertRaises(InvalidStatus) as caught:
                    await self._connect(port, headers=[("Authorization", value)])
                self.assertEqual(401, caught.exception.response.status_code)
        self.assertEqual([], self.calls)

    async def test_lowercase_header_and_scheme_accepted(self):
        port = await self._start()
        headers = [("authorization", f"bearer {self.token_a}")]
        async with await self._connect(port, headers=headers) as ws:
            await ws.send(b"ping")
            self.assertEqual(b"ping", await asyncio.wait_for(ws.recv(), timeout=5))
        self.assertEqual([("PILOT-A", b"ping")], self.calls)

    async def test_path_must_be_exact_mp_and_never_a_query_secret(self):
        port = await self._start()
        for path in (f"/mp?token={self.token_a}", "/mp/", "/mp/x", "/", "/MP"):
            with self.subTest(path=path):
                with self.assertRaises(InvalidStatus) as caught:
                    await self._connect(port, token=self.token_a, path=path)
                self.assertEqual(404, caught.exception.response.status_code)
        self.assertEqual([], self.calls)
        self.assertNotIn(self.token_a, self.logged())

    # -- TLS trust ------------------------------------------------------------
    async def test_unknown_ca_rejected(self):
        port = await self._start()
        with self.assertRaises(ssl.SSLCertVerificationError):
            await self._connect(port, token=self.token_a, ca_file=self.rogue_ca_file)
        self.assertEqual([], self.calls)

    async def test_wrong_hostname_rejected(self):
        port = await self._start()
        with self.assertRaises(ssl.SSLCertVerificationError) as caught:
            await self._connect(port, token=self.token_a, server_hostname="wrong.test")
        self.assertIn("hostname mismatch", str(caught.exception).lower())
        self.assertEqual([], self.calls)

    async def test_plaintext_gate_cannot_start(self):
        gate = self._gate()
        with self.assertRaises(TlsRequiredError):
            await gate.start(host="127.0.0.1", port=0, ssl_context=None)
        weak = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        weak.minimum_version = ssl.TLSVersion.TLSv1_2
        weak.load_cert_chain(certfile=str(self.cert_file), keyfile=str(self.key_file))
        with self.assertRaises(TlsRequiredError):
            await gate.start(host="127.0.0.1", port=0, ssl_context=weak)
        self.assertEqual([], self.calls)

    async def test_plaintext_client_cannot_reach_tls_gate(self):
        port = await self._start()
        with self.assertRaises((OSError, InvalidMessage, ConnectionClosed, EOFError)):
            await asyncio.wait_for(
                connect(f"ws://127.0.0.1:{port}{MP_PATH}", proxy=None, open_timeout=5,
                        additional_headers=[("Authorization", f"Bearer {self.token_a}")],
                        logger=self.client_logger),
                timeout=5)
        self.assertEqual([], self.calls)

    async def test_non_loopback_bind_refused(self):
        gate = self._gate()
        for host in ("192.0.2.1", "0.0.0.0", "::"):
            with self.subTest(host=host):
                with self.assertRaises(GatewayError):
                    await gate.start(host=host, port=0, ssl_context=self.server_ctx)
        self.assertIsNone(gate._server)

    # -- frame policy ---------------------------------------------------------
    async def test_text_frame_rejected_without_callback(self):
        port = await self._start()
        async with await self._connect(port, token=self.token_a) as ws:
            await ws.send("not binary")
            await self._expect_closed(ws, 1003)
        self.assertEqual([], self.calls)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))

    async def test_oversized_frame_rejected_without_callback(self):
        port = await self._start()
        async with await self._connect(port, token=self.token_a) as ws:
            await ws.send(b"x" * (MAX_MESSAGE + 1))
            await self._expect_closed(ws, 1009)
        self.assertEqual([], self.calls)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))

    # -- session accounting ---------------------------------------------------
    async def test_duplicate_identity_and_capacity_rejected_then_reusable(self):
        port = await self._start(max_sessions=1)
        async with await self._connect(port, token=self.token_a) as first:
            await first.send(b"hold")
            await asyncio.wait_for(first.recv(), timeout=5)
            async with await self._connect(port, token=self.token_a) as dup:
                await self._expect_closed(dup, 1008)          # same identity, twice
            self.assertEqual([("PILOT-A", b"hold")], self.calls)
            async with await self._connect(port, token=self.token_b) as other:
                await self._expect_closed(other, 1008)        # capacity reached
            self.assertEqual([("PILOT-A", b"hold")], self.calls)
            self.assertEqual(("PILOT-A",), self.gate.active_identities)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        async with await self._connect(port, token=self.token_a) as again:
            await again.send(b"reborn")
            self.assertEqual(b"reborn", await asyncio.wait_for(again.recv(), timeout=5))

    async def test_reservation_released_when_callback_raises(self):
        port = await self._start(on_message=self._boom)
        async with await self._connect(port, token=self.token_a) as ws:
            await ws.send(b"bad")
            await self._expect_closed(ws, 1011)
        self.assertEqual([("PILOT-A", b"bad")], self.calls)
        self.assertTrue(await self._wait_for(lambda: self.gate.active_identities == ()))
        async with await self._connect(port, token=self.token_a) as again:
            await again.send(b"ok")
            self.assertEqual(b"ok", await asyncio.wait_for(again.recv(), timeout=5))

    # -- log hygiene ----------------------------------------------------------
    async def test_secrets_never_logged_even_at_debug(self):
        payload = b"SECRETPAYLOAD-" + self.token_a.encode()
        port = await self._start()
        async with await self._connect(port, token=self.token_a) as ws:
            await ws.send(payload)
            await asyncio.wait_for(ws.recv(), timeout=5)
        with self.assertRaises(InvalidStatus):
            await self._connect(port, token=self.unknown_token)
        with self.assertRaises(InvalidStatus):
            await self._connect(port, token=self.token_a, path=f"/mp?token={self.token_b}")
        logged = self.logged()
        self.assertGreater(len(self.records.lines), 5)         # DEBUG really ran
        self.assertIn("<payload redacted>", logged)             # frame dumps vetted
        for secret in (self.token_a, self.token_b, self.unknown_token,
                       "SECRETPAYLOAD", "Bearer "):
            self.assertNotIn(secret, logged)
        self.assertNotIn("Authorization: Bearer", logged)
        self.assertNotRegex(logged, r"(?i)authorization\s*:?\s*[0-9a-f]{64}")

    def test_redact_helper_covers_headers_frames_and_queries(self):
        token = "a" * 64
        self.assertEqual("< Authorization: <redacted>",
                         redact(f"< Authorization: Bearer {token}"))
        self.assertEqual("> BINARY <payload redacted> [binary, 3 bytes]",
                         redact("> BINARY b'abc' [binary, 3 bytes]"))
        self.assertEqual("< GET /mp?<redacted> HTTP/1.1",
                         redact(f"< GET /mp?token={token} HTTP/1.1"))
        self.assertEqual("token=<redacted> secret <redacted>",
                         redact("token=deadbeef secret hunter2"))
        self.assertEqual("server listening (loopback, TLS 1.3)",
                         redact("server listening (loopback, TLS 1.3)"))

    # -- configuration guards -------------------------------------------------
    def test_registry_holds_only_digests_and_is_immutable(self):
        self.assertNotIn(self.token_a, repr(self.registry))
        self.assertNotIn(self.token_b, repr(self.registry))
        stored = set(self.registry.digests.values())
        self.assertNotIn(self.token_a, stored)
        self.assertEqual(64, len(next(iter(stored))))
        with self.assertRaises(TypeError):
            self.registry.digests["PILOT-C"] = "x" * 64
        with self.assertRaises(GatewayError):
            IdentityRegistry.from_tokens({})
        with self.assertRaises(GatewayError):
            IdentityRegistry.from_tokens({"PILOT-A": "too-short"})

    def test_gate_requires_async_callback_and_registry(self):
        with self.assertRaises(GatewayError):
            TlsAuthGate(registry=self.registry, on_message=lambda i, p: None)
        with self.assertRaises(GatewayError):
            TlsAuthGate(registry={"PILOT-A": self.token_a}, on_message=self._echo)
        for kwargs in ({"max_sessions": 0}, {"max_message_bytes": 8},
                       {"max_queue": 0}, {"open_timeout": 0}):
            with self.subTest(**kwargs):
                with self.assertRaises(GatewayError):
                    TlsAuthGate(registry=self.registry, on_message=self._echo, **kwargs)

    def test_generate_token_is_256_bit_hex_and_unique(self):
        tokens = {generate_token() for _ in range(8)}
        self.assertEqual(8, len(tokens))
        for token in tokens:
            self.assertRegex(token, r"^[0-9a-f]{64}$")

    # -- source honesty -------------------------------------------------------
    def test_source_is_library_only_and_loopback_tls_only(self):
        self.assertTrue(re.search(r'^MP_PATH = "/mp"$', SOURCE, re.M))
        self.assertNotIn("__main__", SOURCE)
        self.assertNotIn("argparse", SOURCE)
        self.assertNotIn("asyncio.run", SOURCE)
        self.assertNotIn("def echo", SOURCE)
        self.assertNotIn("echo(", SOURCE)
        for udp_marker in ("AF_INET", "AF_INET6", "SOCK_DGRAM", "create_datagram_endpoint",
                           "socket.socket"):
            self.assertNotIn(udp_marker, SOURCE)
        for bypass in ("CERT_NONE", "check_hostname = False", "check_hostname=False"):
            self.assertNotIn(bypass, SOURCE)
        self.assertIn("hmac.compare_digest", SOURCE)
        self.assertIn("ssl.TLSVersion.TLSv1_3", SOURCE)
        self.assertIn("ssl=ssl_context", SOURCE)
        self.assertIn("compression=None", SOURCE)

    def test_requirements_and_readme_are_honest(self):
        reqs = (ROOT / "gateway" / "requirements.txt").read_text(encoding="utf-8")
        test_reqs = (ROOT / "gateway" / "requirements-test.txt").read_text(encoding="utf-8")
        self.assertIn("websockets==17.4", reqs)
        self.assertIn("cryptography==50.0.2", test_reqs)
        self.assertIn("-r requirements.txt", test_reqs)
        for claim in ("not production", "udp", "loopback", "tls 1.3",
                      "invitation", "rate limit"):
            self.assertIn(claim, README.lower())


if __name__ == "__main__":
    unittest.main()
