#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Launcher policy tests: solo c172p default, loopback argv, dry run, hostile input.

Stdlib unittest only. FlightGear, Docker and host services are never started, and no
DNS answer or UDP packet is ever needed: every dry-run and config case runs with the
resolver and the process spawn patched to raise, so an accidental IO attempt fails
the test instead of touching the machine. The loopback route below is local
diagnostics only, not the final public client route.
"""
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
REPO = TOOLS.parent

import launch  # noqa: E402


@contextlib.contextmanager
def sealed():
    """No DNS, no socket, no child process: any attempt raises AssertionError."""
    def banned(*args, **kwargs):
        raise AssertionError('launcher attempted network or process IO')
    patches = [mock.patch.object(launch.subprocess, name, side_effect=banned)
               for name in ('call', 'run', 'check_call', 'Popen')]
    patches += [mock.patch.object(socket, name, side_effect=banned)
                for name in ('getaddrinfo', 'gethostbyname', 'create_connection', 'socket')]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


def quiet(argv):
    """Run main(argv) swallowing its output; return (exit_code, stderr_text)."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        code = launch.main(argv)
    return code, err.getvalue()


class SoloDefaults(unittest.TestCase):
    def test_baseline_is_the_only_mode_and_the_default(self):
        self.assertEqual(launch.MODES, ('baseline',))
        self.assertEqual(launch.AIRCRAFT, {'baseline': 'c172p'})
        self.assertEqual(launch.DEFAULTS['mode'], 'baseline')
        self.assertTrue(launch.DEFAULTS['solo'])
        self.assertIsNone(launch.DEFAULTS['server'])
        self.assertEqual(launch.DEFAULTS['local_ip'], '0.0.0.0')

    def test_default_argv_is_stock_c172p_with_multiplay_disabled(self):
        argv = launch.build_args(dict(launch.DEFAULTS))
        self.assertEqual(argv[0], 'fgfs')
        self.assertIn('--aircraft=c172p', argv)
        self.assertIn('--callsign=PIL001', argv)
        self.assertIn('--enable-terrasync', argv)
        self.assertEqual([a for a in argv if a.startswith('--multiplay=')], [])
        for prop in ('--prop:/sim/multiplay/txhost=', '--prop:/sim/multiplay/txport=0',
                     '--prop:/sim/multiplay/rxport=0'):
            self.assertIn(prop, argv)

    def test_solo_ignores_any_server_values_and_still_disables_multiplay(self):
        argv = launch.build_args(dict(launch.DEFAULTS, server='nonsense host',
                                      local_ip='also nonsense'))
        self.assertEqual([a for a in argv if a.startswith('--multiplay=')], [])
        self.assertIn('--prop:/sim/multiplay/txhost=', argv)

    def test_argv_never_carries_absolute_repo_or_addon_paths(self):
        for over in ({}, dict(solo=False, server='127.0.0.1', local_ip='127.0.0.1')):
            with self.subTest(over=over):
                argv = launch.build_args(dict(launch.DEFAULTS, **over))
                self.assertEqual([a for a in argv if a.startswith('/')], [])
                self.assertEqual([a for a in argv if str(REPO) in a], [])
                self.assertEqual([a for a in argv
                                  if a.startswith(('--addon', '--fg-aircraft', '--config'))], [])


class DirectLoopbackArgv(unittest.TestCase):
    """The loopback smoke route documented in server/space/README.md."""

    def network(self, **over):
        return dict(launch.DEFAULTS, solo=False, **over)

    def test_documented_loopback_dry_run_argv(self):
        argv = launch.build_args(self.network(server='127.0.0.1', local_ip='127.0.0.1',
                                              server_port=5500))
        self.assertIn('--aircraft=c172p', argv)
        self.assertIn('--multiplay=in,10,127.0.0.1,5005', argv)
        self.assertIn('--multiplay=out,10,127.0.0.1,5500', argv)

    def test_dns_server_is_passed_on_unresolved(self):
        argv = launch.build_args(self.network(server='fgms.example.invalid'))
        self.assertIn('--multiplay=out,10,fgms.example.invalid,5000', argv)
        self.assertIn('--multiplay=in,10,0.0.0.0,5005', argv)

    def test_one_host_never_shares_one_udp_port(self):
        with self.assertRaises(ValueError) as ctx:
            launch.build_args(self.network(server='127.0.0.1', local_ip='127.0.0.1',
                                           server_port=5500, receive_port=5500))
        self.assertIn('different UDP ports', str(ctx.exception))
        with self.assertRaises(ValueError):  # a wildcard bind onto loopback is one host too
            launch.build_args(self.network(server='127.0.0.1', receive_port=5000))
        argv = launch.build_args(self.network(server='203.0.113.7', receive_port=5000))
        self.assertIn('--multiplay=in,10,0.0.0.0,5000', argv)


class DryRunWithoutIo(unittest.TestCase):
    def test_dry_run_prints_argv_and_starts_nothing(self):
        with sealed(), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(launch.main(['--dry-run']), 0)
        printed = out.getvalue()
        self.assertIn('--aircraft=c172p', printed)
        self.assertIn('NOT started', printed)

    def test_dry_run_with_dns_server_neither_resolves_nor_spawns(self):
        with sealed(), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(launch.main(['--mode', 'baseline', '--network', '--server',
                                          'fgms.example.invalid', '--dry-run']), 0)
        self.assertIn('--multiplay=out,10,fgms.example.invalid,5000', out.getvalue())

    def test_network_without_server_fails_closed_without_io(self):
        with sealed():
            code, err = quiet(['--network', '--dry-run'])
        self.assertEqual(code, 1)
        self.assertIn('no public', err.lower())

    def test_start_uses_an_argv_list_and_no_shell(self):
        with mock.patch.object(launch.shutil, 'which', return_value='/fixture/fgfs'), \
                mock.patch.object(launch.subprocess, 'call', return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launch.main([]), 0)
        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], 'fgfs')
        self.assertIn('--aircraft=c172p', argv)
        self.assertNotIn('shell', run.call_args.kwargs)


class HostileInput(unittest.TestCase):
    def network(self, **over):
        return dict(launch.DEFAULTS, solo=False, **over)

    def test_shell_uri_and_ipv6_destinations_are_rejected(self):
        bad = ('fgms.example.invalid:5000', 'udp://203.0.113.7', 'http://fgms.example.invalid/',
               'a.invalid,b.invalid', 'user@a.invalid', '-evil.invalid', 'a b', 'a\tb', 'a\nb',
               'a\x00b', 'a..invalid', '.a.invalid', 'a.invalid.', 'a_b.invalid', 'a;b', 'a|b',
               'a$(id)', 'a`id`', 'a\\b', 'a"b', "a'b", 'фгмс.рф', '::1', 'fe80::1%eth0',
               '[::1]:5500', '203.0.113.256', '0303.0.113.7', '203.0.113', '0.0.0.0',
               '255.255.255.255', '224.0.0.1', 'x' * 254, '')
        for server in bad:
            with self.subTest(server=server), self.assertRaises(ValueError):
                launch.build_args(self.network(server=server))

    def test_receive_bind_is_plain_ipv4_only(self):
        for local in ('localhost', 'fgms.example.invalid', '::1', '0.0.0.0.0', ' 203.0.113.7',
                      '203.0.113.7 ', '203.0.113.7\x00', '', None, 5):
            with self.subTest(local=local), self.assertRaises(ValueError):
                launch.build_args(self.network(server='203.0.113.7', local_ip=local))

    def test_ports_and_callsign_are_validated(self):
        for port in (0, 1023, 65536, True, '5000', None, 5000.0):
            for key in ('server_port', 'receive_port'):
                with self.subTest(key=key, port=port), self.assertRaises(ValueError):
                    launch.build_args(self.network(server='203.0.113.7', **{key: port}))
        for callsign in ('', 'obs001', 'OBS1', 'A' * 7, 'PI;reboot', 'P IL', 'PIL\x0001',
                         'ünicode', 'a/b'):
            with self.subTest(callsign=callsign), self.assertRaises(ValueError):
                launch.build_args(dict(launch.DEFAULTS, callsign=callsign))

    def test_client_config_must_be_an_object_of_known_keys(self):
        for payload in ('[1, 2]', '"baseline"', 'null', '{"nope": 1}', '{"fgfs": 5}',
                        '{"solo": "yes"}', 'not json'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp, sealed():
                path = Path(tmp) / 'client.json'
                path.write_text(payload, encoding='utf-8')
                code, _ = quiet(['--config', str(path), '--dry-run'])
                self.assertEqual(code, 1)

    def test_unreadable_client_config_fails_closed(self):
        with sealed():
            code, err = quiet(['--config', str(REPO / 'client' / 'absent.json'), '--dry-run'])
        self.assertEqual(code, 1)
        self.assertIn('launcher', err.lower())


class SupportedMode(unittest.TestCase):
    def test_every_other_mode_value_is_rejected_as_unsupported(self):
        for mode in ('flight', 'arena', 'BASELINE', 'baseline ', 'baseline/x', '',
                     None, 0, True, ['baseline']):
            with self.subTest(mode=mode), self.assertRaises(ValueError) as ctx:
                launch.build_args(dict(launch.DEFAULTS, mode=mode))
            message = str(ctx.exception)
            self.assertIn('unsupported value for mode', message)
            self.assertIn('baseline', message)
            self.assertNotIn(repr(mode), message)  # the rejected value is never echoed back

    def test_cli_rejects_other_modes_before_any_exec(self):
        for mode in ('flight', 'arena', 'whatever'):
            with self.subTest(mode=mode), sealed():
                code, err = quiet(['--mode', mode])
            self.assertEqual(code, 1)
            self.assertIn('unsupported value for mode', err)

    def test_shipped_example_profile_still_builds_baseline_argv(self):
        profile = json.loads((REPO / 'client' / 'client.example.json').read_text(encoding='utf-8'))
        self.assertEqual(set(profile), set(launch.DEFAULTS))
        self.assertEqual(profile['mode'], 'baseline')
        self.assertTrue(profile['solo'])
        argv = launch.build_args(dict(launch.DEFAULTS, **profile))
        self.assertIn('--aircraft=c172p', argv)

    def test_help_states_plain_udp_scope_and_the_single_mode(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit) as stop:
            launch.main(['--help'])
        self.assertEqual(stop.exception.code, 0)
        printed = ' '.join(out.getvalue().lower().split())
        for phrase in ('plain udp', 'no authentication', 'no encryption', 'not a secure tunnel',
                       'never manages the host', 'only supported value'):
            self.assertIn(phrase, printed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
