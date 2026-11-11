"""Headless core of the space_ui client screen (the Tk form comes next increment).

Public API for that GUI: Profile (frozen, strict JSON whitelist, explicit-path
load/save only), DEMO_CATALOG + select_ship()/MockAdapter (demo selection, never
a spawn, a connection or a slot), RelayStub (future relay interface, always
error/unavailable today) and SoloSession (the one real launch: stock solo c172p
through tools/launch.py, argv only, no shell, no blocking wait).

Importing this module needs no tkinter and performs no network IO; the core
never imports the GUI and the GUI imports only this core.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import json
import os
from pathlib import Path
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
from dataclasses import dataclass

_TOOLS = Path(__file__).resolve().parents[2] / 'tools'
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))  # small adapter: reuse the launcher policy, do not fork it
import launch  # noqa: E402

SCHEMA_VERSION = 1
MAX_PROFILE_BYTES = 64 * 1024
BAD_ENDPOINT = ('endpoint must be empty (solo) or wss://ASCII-host[:port]/mp '
                'with a port between 1 and 65535')
ENDPOINT_RE = re.compile(r'wss://([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?/mp')
_DEMO_OK = 'demo selection only: nothing is spawned, no slot is taken, no network is used'
_BLOCKED = 'not available in the demo catalog yet'


@dataclass(frozen=True)
class Ship:
    """One demo catalog entry; available means selectable for a demo check."""
    id: str
    team_id: str
    label: str
    available: bool
    reason: str


DEMO_CATALOG = {
    'rebels': (Ship('xwing', 'rebels', 'X-wing', True, _DEMO_OK),
               Ship('ywing', 'rebels', 'Y-wing', False, _BLOCKED),
               Ship('uwing', 'rebels', 'U-wing', False, _BLOCKED)),
    'empire': (Ship('tie', 'empire', 'TIE fighter', True, _DEMO_OK),
               Ship('bomber', 'empire', 'TIE bomber', False, _BLOCKED),
               Ship('reaper', 'empire', 'TIE reaper', False, _BLOCKED)),
}


def ships(team_id):
    """Full demo catalog for a team, blocked entries included. Pure lookup, no IO."""
    try:
        return DEMO_CATALOG[team_id]
    except (KeyError, TypeError):
        raise ValueError('team is not in the demo catalog') from None


def _lookup(team_id, ship_id):
    return next((s for s in ships(team_id) if s.id == ship_id), None)


def select_ship(team_id, ship_id):
    """Resolve a UI selection: blocked ships raise with their reason, unknown pairs
    raise. Nothing here ever spawns, connects or decrements slots."""
    found = _lookup(team_id, ship_id)
    if found is None:
        raise ValueError('ship is not in this team demo catalog')
    if not found.available:
        raise ValueError(found.reason)
    return found


def validate_endpoint(value):
    """Optional relay endpoint: empty means solo; otherwise only wss://ASCII-host[:port]/mp.

    No user-info, password, query, fragment, whitespace or control characters;
    the host goes through the existing launcher policy and a port, when given,
    must be 1..65535. Nothing is resolved or connected and a rejected value is
    never echoed back."""
    if not isinstance(value, str):
        raise ValueError('endpoint must be a string')
    if not value:
        return value
    match = ENDPOINT_RE.fullmatch(value)
    if not match:
        raise ValueError(BAD_ENDPOINT)
    host, port = match.groups()
    try:
        launch.server_host(host)  # policy only: this performs no DNS and no IO
    except ValueError:
        raise ValueError(BAD_ENDPOINT) from None
    if port is not None and (port != str(int(port)) or not 1 <= int(port) <= 65535):
        raise ValueError(BAD_ENDPOINT)
    return value


def _validate_callsign(value):
    """Callsign policy is delegated to tools/launch.py so exactly one policy exists."""
    try:
        launch.build_args(dict(launch.DEFAULTS, callsign=value))
    except ValueError:
        raise ValueError('callsign must be 1..6 ASCII letters/digits and must not '
                         'start with obs') from None


def _clean(value):
    """Nonempty and free of NUL/control characters; spaces are fine (paths may have them)."""
    return bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


@dataclass(frozen=True)
class Profile:
    """Frozen client profile. The JSON whitelist is exactly these six fields;
    unknown fields (tokens included) are refused without echoing their values.

    load_profile()/save_profile() only touch the explicit path passed by the
    caller: nothing is read from or written to $HOME automatically, and saved
    data never holds secrets or fields beyond the whitelist."""

    schema_version: int = SCHEMA_VERSION
    fgfs: str = 'fgfs'
    callsign: str = 'PIL001'
    endpoint: str = ''
    team_id: str = 'rebels'
    ship_id: str = 'xwing'

    FIELDS = ('schema_version', 'fgfs', 'callsign', 'endpoint', 'team_id', 'ship_id')

    def __post_init__(self):
        sv = self.schema_version
        if not isinstance(sv, int) or isinstance(sv, bool) or sv != SCHEMA_VERSION:
            raise ValueError('profile schema_version must be exactly ' + str(SCHEMA_VERSION))
        for name in ('fgfs', 'callsign', 'endpoint', 'team_id', 'ship_id'):
            if not isinstance(getattr(self, name), str):
                raise ValueError('profile ' + name + ' must be a string')
        for name in ('fgfs', 'callsign', 'team_id', 'ship_id'):
            if not _clean(getattr(self, name)):
                raise ValueError('profile ' + name + ' must be nonempty and free of '
                                 'NUL/control characters (spaces are allowed)')
        validate_endpoint(self.endpoint)
        _validate_callsign(self.callsign)
        if _lookup(self.team_id, self.ship_id) is None:
            raise ValueError('team_id/ship_id pair is not in the demo catalog')

    @classmethod
    def from_dict(cls, data):
        """Strict whitelist parse: unknown or missing keys and wrong types are refused."""
        if not isinstance(data, dict):
            raise ValueError('profile must be a JSON object')
        if set(data) - set(cls.FIELDS):
            raise ValueError('profile contains unknown fields')
        if set(cls.FIELDS) - set(data):
            raise ValueError('profile is missing required fields')
        return cls(**data)

    def to_dict(self):
        return {name: getattr(self, name) for name in self.FIELDS}


def load_profile(path):
    """Read a profile from one explicit path: size-capped, never writes, never searches $HOME.

    Only regular files are read (a FIFO would block), and at most
    MAX_PROFILE_BYTES+1 bytes are taken from the open file: a stat() size is a
    hint, not a bound, because the file may grow between stat and read."""
    path = Path(path)
    try:
        info = path.stat()
    except OSError:
        raise ValueError('profile file was not found') from None
    if not stat_module.S_ISREG(info.st_mode):
        raise ValueError('profile must be a regular file')
    if info.st_size > MAX_PROFILE_BYTES:
        raise ValueError('profile file is too large')
    try:
        with path.open('rb') as handle:
            raw = handle.read(MAX_PROFILE_BYTES + 1)
    except OSError:
        raise ValueError('profile file could not be read') from None
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError('profile file is too large')
    try:
        data = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise ValueError('profile is not readable JSON') from None
    return Profile.from_dict(data)


def save_profile(path, profile):
    """Atomically write exactly the whitelisted fields to one explicit path."""
    path = Path(path)
    text = json.dumps(profile.to_dict(), sort_keys=True, indent=2) + '\n'
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


class MockAdapter:
    """Demo authorization/catalog adapter for the GUI until an authority exists.

    Answers come from DEMO_CATALOG only: authorize() never spawns an aircraft,
    never connects anywhere and never consumes or releases match slots."""

    def catalog(self, team_id):
        return ships(team_id)

    def authorize(self, team_id, ship_id):
        """Return (granted, reason); unknown or blocked selections are denied."""
        try:
            ship = select_ship(team_id, ship_id)
        except ValueError as exc:
            return False, str(exc)
        return True, ship.reason


class RelayStub:
    """State interface for the future relay adapter: preparing/connecting/ready/error.

    connect() always fails closed with an 'unavailable' reason: without a real
    handshake there is no ready and no 'connected' state to show, so the GUI
    cannot paint a green connection that does not exist."""

    STATES = ('preparing', 'connecting', 'ready', 'error')

    def __init__(self):
        self.state = 'preparing'
        self.reason = ''

    def connect(self):
        self.state = 'error'
        self.reason = 'relay unavailable: there is no gateway handshake yet'
        return self.state


def build_solo_args(profile):
    """Solo c172p argv through tools.launch.build_args: solo=True, server=None,
    terrasync=False. The demo team/ship choice and the endpoint never enter
    argv; no shell is involved and nothing is started here."""
    return launch.build_args(dict(launch.DEFAULTS, fgfs=profile.fgfs,
                                  callsign=profile.callsign,
                                  solo=True, server=None, terrasync=False))


class SoloSession:
    """The one real launch behind the start button: the stock solo c172p, one child at a time.

    Changing the demo ship never changes this aircraft: c172p is the solo test
    tool, not an X-wing. The child gets an argv list (no shell), DEVNULL stdin
    and inherited stdout/stderr; launch() returns immediately, poll() reports
    the exit code, nothing is waited for or terminated here (foreign processes
    are never touched). A repeat launch while the child still runs is refused.
    resolver and spawn are injectable for tests; defaults are shutil.which and
    subprocess.Popen."""

    def __init__(self, resolver=None, spawn=None):
        self._resolver = resolver if resolver is not None else shutil.which
        self._spawn = spawn if spawn is not None else subprocess.Popen
        self.process = None

    def poll(self):
        """Exit code of the last child, or None while it runs / was never started."""
        return None if self.process is None else self.process.poll()

    def launch(self, profile):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError('a solo FlightGear is still running; wait for it to exit')
        argv = build_solo_args(profile)
        program = argv[0]
        if os.sep in program or (os.altsep and os.altsep in program):
            executable = program  # an explicit path is used as given
        else:
            executable = self._resolver(program)
        if not executable:
            raise FileNotFoundError('fgfs executable not found; point the profile '
                                    'fgfs field at it')
        try:
            self.process = self._spawn([executable] + argv[1:], shell=False,
                                       stdin=subprocess.DEVNULL)
        except OSError as exc:
            self.process = None
            raise RuntimeError('could not start FlightGear: ' + type(exc).__name__) from None
        return self.process
