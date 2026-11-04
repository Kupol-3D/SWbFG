#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Structural checks for the space-arena FGMS compose scaffolding.

Stdlib JSON parsing plus exact-shape assertions on server/space/{compose.json,
compose.local.json,fgms.conf} and README honesty.  These are deployment-file
STRUCTURE checks only: Docker is never executed here (the agent has no Docker
access), no FGMS process is started or simulated in Python, and passing them
proves nothing about runtime networking.  The gateway contour is NOT verified
working until a human runs Docker, TLS/gateway and two clients.  The removed
legacy host project is out of scope: nothing here reads deleted files.
"""
import contextlib
import importlib.util
import io
import ipaddress
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPACE = ROOT / 'server' / 'space'
BASE = SPACE / 'compose.json'
LOCAL = SPACE / 'compose.local.json'
CONF = SPACE / 'fgms.conf'
README = SPACE / 'README.md'
DOCKERFILE = ROOT / 'server' / 'Dockerfile'
PINNED_FGMS = '26e3dbd4124a10ea825806b77d32e046527cfe9d'


def load(path):
    with path.open(encoding='utf-8') as handle:
        return json.load(handle)


def walk_keys(node, key):
    """Every value stored under `key` anywhere in a decoded JSON document."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
            yield from walk_keys(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from walk_keys(value, key)


def parse_conf(path):
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        if key in values:
            raise AssertionError('duplicate config key: ' + key)
        values[key] = value
    return values


def merge(base, override):
    """Compose-like merge good enough for these files: maps deep-merge,
    sequences are replaced (base declares no ports, so replacement is exact)."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = merge(base.get(key), value) if key in base else value
        return merged
    return override


class TestBaseCompose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load(BASE)
        cls.service = cls.doc['services']['fgms']

    def test_parses_with_stdlib_json_and_isolates_project(self):
        self.assertEqual(self.doc['name'], 'space-arena')
        self.assertEqual(list(self.doc['services']), ['fgms'])
        self.assertNotIn('restart', self.service)  # never auto-start
        self.assertNotIn('${', BASE.read_text(encoding='utf-8'))  # no old .env

    def test_default_declares_no_host_ports_anywhere(self):
        self.assertEqual(list(walk_keys(self.doc, 'ports')), [])
        self.assertEqual(list(walk_keys(self.doc, 'published')), [])

    def test_hardening_flags_exact(self):
        self.assertIs(self.service['init'], True)
        self.assertIs(self.service['read_only'], True)
        self.assertEqual(self.service['user'], '65534:65534')
        self.assertEqual(self.service['cap_drop'], ['ALL'])
        self.assertIn('no-new-privileges:true', self.service['security_opt'])

    def test_no_host_network_privilege_or_docker_socket(self):
        text = BASE.read_text(encoding='utf-8')
        self.assertEqual(list(walk_keys(self.doc, 'network_mode')), [])
        self.assertEqual(list(walk_keys(self.doc, 'privileged')), [])
        self.assertNotIn('docker.sock', text)
        self.assertNotIn('"host"', text)

    def test_only_private_internal_bridge_network(self):
        self.assertEqual(self.doc['networks'], {'arena': {'driver': 'bridge',
                                                          'internal': True}})
        self.assertEqual(self.service['networks'], ['arena'])

    def test_expose_is_metadata_only(self):
        self.assertEqual(self.service['expose'], ['5000/udp'])

    def test_readonly_config_bind_exact(self):
        self.assertEqual(self.service['volumes'], [{
            'type': 'bind', 'source': './fgms.conf',
            'target': '/config/fgms.conf', 'read_only': True}])
        self.assertTrue(CONF.is_file())

    def test_tmpfs_and_logging_limits_exact(self):
        self.assertEqual(self.service['tmpfs'],
                         ['/tmp:rw,nosuid,nodev,size=16m,mode=1777'])
        self.assertEqual(list(self.service['logging']), ['options'])
        self.assertEqual(self.service['logging']['options'],
                         {'max-size': '10m', 'max-file': '3'})

    def test_build_context_pins_fgms_revision(self):
        self.assertEqual(self.service['build'], {'context': '..',
                                                 'dockerfile': 'Dockerfile'})
        resolved = (SPACE / '..' / 'Dockerfile').resolve()
        self.assertEqual(resolved, DOCKERFILE.resolve())
        self.assertTrue(DOCKERFILE.is_file())
        text = DOCKERFILE.read_text(encoding='utf-8')
        self.assertEqual(text.count(PINNED_FGMS), 2)  # fetch plus HEAD re-check
        self.assertIn('/usr/local/bin/fgms', text)
        self.assertEqual(self.service['image'], 'space-arena-fgms:0.13.10')


class TestLocalOverride(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load(LOCAL)
        cls.service = cls.doc['services']['fgms']
        cls.merged = merge(load(BASE), cls.doc)

    def test_exact_single_loopback_publish(self):
        self.assertEqual(self.service['ports'], [{
            'target': 5000, 'published': '5500',
            'host_ip': '127.0.0.1', 'protocol': 'udp'}])
        self.assertEqual(list(walk_keys(self.doc, 'ports'))[0],
                         self.service['ports'])

    def test_no_public_bind_in_override_text(self):
        text = LOCAL.read_text(encoding='utf-8')
        self.assertNotIn('0.0.0.0', text)
        for value in walk_keys(self.doc, 'host_ip'):
            self.assertEqual(ipaddress.IPv4Address(value),
                             ipaddress.IPv4Address('127.0.0.1'))

    def test_private_arena_network_preserved(self):
        self.assertIn('arena', self.service['networks'])
        self.assertIs(self.merged['networks']['arena']['internal'], True)
        for name, network in self.doc.get('networks', {}).items():
            self.assertEqual(network['driver'], 'bridge')
            if name == 'arena':
                self.assertIs(network['internal'], True)

    def test_override_adds_no_hardening_regressions(self):
        for forbidden in ('privileged', 'network_mode', 'cap_add',
                          'security_opt', 'volumes', 'tmpfs', 'restart'):
            self.assertNotIn(forbidden, self.service)
        self.assertNotIn('docker.sock', LOCAL.read_text(encoding='utf-8'))

    def test_merged_document_publishes_only_loopback(self):
        published = list(walk_keys(self.merged, 'ports'))
        self.assertEqual(published, [self.service['ports']])
        self.assertIs(self.merged['services']['fgms']['read_only'], True)
        self.assertEqual(self.merged['services']['fgms']['cap_drop'], ['ALL'])


class TestFgmsConf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = parse_conf(CONF)

    def test_identity_and_container_bind(self):
        self.assertEqual(self.values['server.name'], 'SpaceArena')
        self.assertEqual(self.values['server.address'], '0.0.0.0')
        self.assertEqual(self.values['server.port'], '5000')
        self.assertEqual(self.values['server.logfile'], '/tmp/fgms.log')

    def test_admin_telnet_tracker_relay_all_off(self):
        self.assertEqual(self.values['server.telnet_port'], '0')
        self.assertEqual(self.values['server.admin_port'], '0')
        self.assertEqual(self.values['server.admin_cli'], 'false')
        self.assertEqual(self.values['server.tracked'], 'false')
        self.assertEqual(self.values['server.daemon'], 'false')
        self.assertEqual(self.values['server.is_hub'], 'false')
        self.assertFalse([key for key in self.values if key.startswith('relay.')])

    def test_expiry_and_radar_ranges_exact(self):
        # Agreed gameplay values asserted directly; no removed legacy conf
        # file is read here.
        self.assertEqual(self.values['server.playerexpires'], '10')
        self.assertEqual(self.values['server.out_of_reach'], '100')
        self.assertEqual(self.values['server.max_radar_range'], '100')

    def test_no_personal_ips_tokens_or_keys(self):
        text = CONF.read_text(encoding='utf-8')
        for candidate in re.findall(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', text):
            self.assertEqual(candidate, '0.0.0.0')
        self.assertNotIn('100.64', text)
        for secret in ('token', 'password', 'secret', 'BEGIN '):
            self.assertNotIn(secret, text.lower())


class TestReadmeHonesty(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = README.read_text(encoding='utf-8')
        cls.lowered = re.sub(r'\s+', ' ', cls.text).lower()
        ports = load(LOCAL)['services']['fgms']['ports'][0]
        cls.port = ports['published']
        cls.host_ip = ports['host_ip']

    def test_exact_host_commands_with_project_flag(self):
        # Only verbs the Docker Compose CLI actually has.
        for verb in ('config', 'build', 'up -d', 'ps', 'exec fgms tail',
                     'down --timeout 10'):
            self.assertIn('-p space-arena -f compose.json ' + verb, self.text)

    def test_referenced_paths_exist(self):
        self.assertIn('tools/launch.py', self.text)
        self.assertTrue((ROOT / 'tools' / 'launch.py').is_file())

    def test_local_override_section_matches_override_file(self):
        # The README smoke section must mirror compose.local.json itself,
        # not a hardcoded port that could drift.
        self.assertIn('-f compose.json -f compose.local.json up -d', self.text)
        self.assertIn('{}:{}'.format(self.host_ip, self.port), self.text)
        self.assertIn('--server-port ' + self.port, self.text)
        self.assertIn('--dry-run', self.text)

    def test_advertised_launcher_flags_exist_in_cli(self):
        # Build the real argparse CLI (--help exits before any FG call) so
        # loop-generated options like the callsign flag are covered too.
        spec = importlib.util.spec_from_file_location(
            'space_arena_launch', ROOT / 'tools' / 'launch.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        help_text = io.StringIO()
        with self.assertRaises(SystemExit) as stop:
            with contextlib.redirect_stdout(help_text):
                module.main(['--help'])
        self.assertEqual(stop.exception.code, 0)
        for flag in ('--mode', '--network', '--server', '--server-port',
                     '--local-ip', '--callsign', '--dry-run'):
            self.assertIn(flag, help_text.getvalue())

    def test_honest_scaffolding_status_and_cleanup_scope(self):
        self.assertIn('scaffolding', self.lowered)
        self.assertIn('gateway', self.lowered)
        self.assertIn('smoke', self.lowered)
        # Some form of explicit not-done / not-verified statement must be
        # present; exact wording may change, the honesty must not.
        self.assertRegex(self.lowered,
                         r'не\s+(подтвер|провер|реализ|защищ|готов)')
        self.assertRegex(self.lowered, r'-p\s+space-arena')
        self.assertIn('только свой', self.lowered)
        # prune may only appear as an explicit prohibition, never as advice
        for match in re.finditer(r'prune', self.lowered):
            window = self.lowered[max(0, match.start() - 40):match.start()]
            self.assertRegex(window, r'(никакого|нельзя|запрет|не выполня)')

    def test_no_fake_gateway_or_caddy_service(self):
        for name in ('caddy', 'nginx', 'gateway'):
            self.assertNotIn(name, json.dumps(load(BASE)).lower())


if __name__ == '__main__':
    unittest.main()
