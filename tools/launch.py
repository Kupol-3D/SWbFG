#!/usr/bin/env python3
"""Cross-platform FlightGear launcher; defaults to a safe unarmed solo c172p flight.

Only baseline is supported: the stock c172p from the local FlightGear install.
No extra aircraft packages, addon paths or repository paths are ever injected.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

DEFAULTS = dict(fgfs='fgfs', mode='baseline', solo=True, server=None, local_ip='0.0.0.0',
                callsign='PIL001', server_port=5000, receive_port=5005,
                airport='KSFO', runway='28R', terrasync=True)
MODES = ('baseline',)
AIRCRAFT = dict(baseline='c172p')
UNSPECIFIED = ipaddress.IPv4Address('0.0.0.0')
BROADCAST = ipaddress.IPv4Address('255.255.255.255')
MULTICAST = ipaddress.ip_network('224.0.0.0/4')
LOOPBACK = ipaddress.ip_network('127.0.0.0/8')
HOST_CHARS = re.compile('[A-Za-z0-9.-]+')
NUMERIC_LOOKING = re.compile('[0-9.]+')

SAFETY_NOTE = """\
FGMS multiplayer is plain UDP: no authentication and no encryption.
This launcher only validates the arguments above and builds the FlightGear argv
(no shell); that validation is NOT cheat protection, NOT a firewall and NOT a
secure tunnel. It never creates SSH/UDP tunnels, never connects or configures a
VPN and never manages the host. Point --server at a server you can already
reach over plain UDP; nothing is looked up, downloaded or connected for you."""


def server_host(value):
    """Validate an FGMS server destination: plain IPv4 or ASCII DNS hostname.

    Nothing is resolved here: validation and --dry-run perform no network IO,
    so a DNS name is passed to FlightGear exactly as typed.
    """
    if not isinstance(value, str) or not value:
        raise ValueError('server must be a plain IPv4 address or an ASCII DNS hostname')
    if '\x00' in value or any(c.isspace() for c in value):
        raise ValueError('server must not contain whitespace or NUL bytes')
    if ':' in value:
        raise ValueError('IPv6 server addresses are not supported: FlightGear multiplay '
                         'sends IPv4 UDP; use an IPv4 address or a DNS name')
    if value.startswith('-') or not HOST_CHARS.fullmatch(value):
        raise ValueError('server must be a plain IPv4 or ASCII DNS hostname: no URI, '
                         'port, comma or shell characters')
    if NUMERIC_LOOKING.fullmatch(value):
        try:
            ip = ipaddress.IPv4Address(value)
        except ValueError:
            raise ValueError('server looks numeric but is not a valid IPv4 address: ' + value) from None
        if ip == UNSPECIFIED:
            raise ValueError('0.0.0.0 is a local receive bind, never a server destination')
        if ip in MULTICAST:
            raise ValueError('multicast groups are not FGMS server destinations')
        if ip == BROADCAST:
            raise ValueError('broadcast is not an FGMS server destination')
        return str(ip)
    if len(value) > 253 or any(not label or len(label) > 63 or label.startswith('-')
                               or label.endswith('-') for label in value.split('.')):
        raise ValueError('server is not a valid ASCII DNS hostname')
    return value


def receive_ip(value):
    """Validate the local receive bind: plain IPv4, or 0.0.0.0 for every interface.

    The receive side is a bind address, so it is never a DNS name; 0.0.0.0 is
    allowed here (and only here) so players need not know their own LAN/VPN IP.
    """
    if not isinstance(value, str) or not value:
        raise ValueError('local_ip must be a plain IPv4 address, or 0.0.0.0 to bind '
                         'every local interface; it is never a DNS name')
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        if ':' in value:
            raise ValueError('IPv6 receive addresses are not supported: FlightGear '
                             'multiplay uses IPv4 UDP') from None
        raise ValueError('local_ip must be a plain IPv4 address, or 0.0.0.0 to bind '
                         'every local interface; it is never a DNS name') from None
    return str(ip)


def build_args(config):
    if set(config) != set(DEFAULTS):
        raise ValueError('unknown or missing launcher config keys')
    if config['mode'] not in MODES:
        raise ValueError('unsupported value for mode: only baseline is supported here')
    if not isinstance(config['solo'], bool) or not isinstance(config['terrasync'], bool):
        raise ValueError('solo and terrasync must be JSON booleans')
    if not isinstance(config['callsign'], str) or not re.fullmatch('[A-Za-z0-9]{1,6}', config['callsign']):
        raise ValueError('callsign must be 1..6 unique ASCII letters/digits')
    if config['callsign'].lower().startswith('obs'):
        raise ValueError('callsigns beginning with obs are hidden by FGMS')
    for key in ('server_port', 'receive_port'):
        port = config[key]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError(key + ' must be an integer 1024..65535')
    for key in ('fgfs', 'airport', 'runway'):
        if not isinstance(config[key], str) or not config[key] or '\x00' in config[key]:
            raise ValueError(key + ' must be a nonempty string')
    args = [config['fgfs'], '--aircraft=' + AIRCRAFT[config['mode']],
            '--airport=' + config['airport'], '--runway=' + config['runway'],
            '--callsign=' + config['callsign']]
    if config['terrasync']:
        args.append('--enable-terrasync')
    if not config['solo']:
        if config['server'] is None:
            raise ValueError('network mode needs an explicit --server HOST; no public '
                             'server is assumed by default')
        server = server_host(config['server'])
        local = receive_ip(config['local_ip'])
        same_host = local == server
        if not same_host and local == str(UNSPECIFIED):
            try:
                same_host = ipaddress.IPv4Address(server) in LOOPBACK
            except ValueError:
                same_host = False  # a DNS name is not resolved, so not proven loopback
        if same_host and config['receive_port'] == config['server_port']:
            raise ValueError('server and client on one host must use different UDP ports')
        args += [f"--multiplay=in,10,{local},{config['receive_port']}",
                 f"--multiplay=out,10,{server},{config['server_port']}"]
    else:
        # Override remembered multiplayer settings as well: no accidental public MP.
        args += ['--prop:/sim/multiplay/txhost=', '--prop:/sim/multiplay/txport=0',
                 '--prop:/sim/multiplay/rxport=0']
    return args


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=SAFETY_NOTE,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, help='optional client JSON config (never executable)')
    parser.add_argument('--mode',
                        help='baseline: the default and only supported value, an unarmed stock '
                             'c172p flight; any other value is rejected as unsupported')
    network = parser.add_mutually_exclusive_group()
    network.add_argument('--solo', dest='solo', action='store_true', default=None,
                         help='default: no multiplayer; remembered MP settings are disabled')
    network.add_argument('--network', dest='solo', action='store_false',
                         help='join an FGMS server (plain UDP, no auth/encryption)')
    for option in ('fgfs', 'callsign', 'airport', 'runway'):
        parser.add_argument('--' + option)
    parser.add_argument('--server',
                        help='FGMS server: plain IPv4 or ASCII DNS hostname, passed to '
                             'FlightGear unresolved (no DNS during validation or --dry-run); '
                             '0.0.0.0, broadcast, multicast, IPv6, ports and URIs are rejected')
    parser.add_argument('--local-ip',
                        help='local UDP receive bind: plain IPv4, or 0.0.0.0 (default) to '
                             'bind every local interface; never a DNS name, never a server')
    parser.add_argument('--server-port', type=int)
    parser.add_argument('--receive-port', type=int)
    parser.add_argument('--dry-run', action='store_true', help='validate and print argv; do not run FG')
    args = parser.parse_args(argv)
    try:
        config = dict(DEFAULTS)
        if args.config:
            loaded = json.loads(args.config.read_text(encoding='utf-8'))
            if not isinstance(loaded, dict) or set(loaded) - set(DEFAULTS):
                raise ValueError('client config must be an object with known keys only')
            config.update(loaded)
        for key in DEFAULTS:
            value = getattr(args, key, None)
            if value is not None:
                config[key] = value
        command = build_args(config)
        print(subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command), flush=True)
        if args.dry_run:
            print('Dry run only: FlightGear was NOT started.')
            return 0
        if not shutil.which(command[0]):
            raise ValueError('fgfs not found; set --fgfs to your host executable path')
        return subprocess.call(command)
    except (OSError, ValueError, TypeError) as exc:
        print('FlightGear launcher: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
