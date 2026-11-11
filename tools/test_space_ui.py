#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""space_ui core tests: profile whitelist, endpoint policy, demo catalog, relay stub, solo argv.

Stdlib unittest only; no tkinter, no FlightGear, no Docker, no network: sealed()
patches subprocess and the resolver so any accidental IO attempt fails the test
instead of touching the machine. Child processes are simulated by injected
fakes; the real fgfs is never started.
"""
import ast
import contextlib
import dataclasses
import importlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
for _p in (str(TOOLS), str(REPO / 'client')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import launch  # noqa: E402
import space_ui  # noqa: E402
from space_ui import __main__ as ui_main  # noqa: E402
from space_ui import core  # noqa: E402


@contextlib.contextmanager
def sealed():
    """No DNS, no socket, no child process: any attempt raises AssertionError."""
    def banned(*args, **kwargs):
        raise AssertionError('space_ui attempted network or process IO')
    patches = [mock.patch.object(mod.subprocess, name, side_effect=banned)
               for mod in (core, launch)
               for name in ('call', 'run', 'check_call', 'Popen')]
    patches += [mock.patch.object(socket, name, side_effect=banned)
                for name in ('getaddrinfo', 'gethostbyname', 'create_connection', 'socket')]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


class FakeChild:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class ProfileBasics(unittest.TestCase):
    def test_defaults_are_whitelisted_and_frozen(self):
        p = core.Profile()
        self.assertEqual((p.schema_version, p.fgfs, p.callsign, p.endpoint,
                          p.team_id, p.ship_id), (1, 'fgfs', 'PIL001', '', 'rebels', 'xwing'))
        self.assertEqual(set(p.to_dict()), set(core.Profile.FIELDS))
        self.assertNotIn('token', p.to_dict())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.callsign = 'OTHER'

    def test_roundtrip_only_at_the_explicit_path(self):
        p = core.Profile(callsign='RB1', endpoint='wss://relay.invalid:7443/mp',
                         team_id='empire', ship_id='tie')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            core.save_profile(path, p)
            self.assertEqual(list(Path(tmp).iterdir()), [path])  # atomic temp cleaned up
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), p.to_dict())
            self.assertEqual(core.load_profile(path), p)

    def test_missing_and_oversized_profiles_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                core.load_profile(Path(tmp) / 'absent.json')
            big = Path(tmp) / 'big.json'
            big.write_text(json.dumps(core.Profile().to_dict()) + ' ' * 64, encoding='utf-8')
            with mock.patch.object(core, 'MAX_PROFILE_BYTES', 32):
                with self.assertRaises(ValueError):
                    core.load_profile(big)

    def test_unknown_fields_tokens_and_types_refused_without_echo(self):
        secret = 'hunter2tokenvalue'
        for over in ({'token': secret}, {'api_key': secret}, {'schema_version': 2},
                     {'schema_version': True}, {'schema_version': '1'}, {'fgfs': 5},
                     {'endpoint': None}, {'team_id': 1}):
            data = core.Profile().to_dict()
            data.update(over)
            with self.subTest(over=over), self.assertRaises(ValueError) as ctx:
                core.Profile.from_dict(data)
            self.assertNotIn(secret, str(ctx.exception))  # rejected values are never echoed
        for broken in ([], 'text', None, 7):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                core.Profile.from_dict(broken)
        with self.assertRaises(ValueError):
            core.Profile.from_dict({'fgfs': 'fgfs'})  # missing the other fields

    def test_endpoint_uri_policy_never_echoes(self):
        bad = ('ws://relay.invalid/mp', 'http://relay.invalid/mp', 'WSS://relay.invalid/mp',
               'wss://relay.invalid', 'wss://relay.invalid/mp/extra', 'wss:///mp', 'wss://:443/mp',
               'wss://relay.invalid:0/mp', 'wss://relay.invalid:65536/mp', 'wss://relay.invalid:0443/mp',
               'wss://user:pw@relay.invalid/mp', 'wss://relay.invalid/mp?x=1', 'wss://relay.invalid/mp#f',
               'wss://re lay.invalid/mp', 'wss://relay.invalid\t/mp', 'wss://relay.invalid\x00/mp',
               'wss://203.0.113.256/mp', 'wss://0.0.0.0/mp', 'relay.invalid')
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError) as ctx:
                core.Profile(endpoint=value)
            self.assertNotIn(value, str(ctx.exception))  # rejected values are never echoed
        with self.assertRaises(ValueError) as ctx:
            core.Profile(endpoint='wss://203.0.113.256/mp')
        self.assertNotIn('203.0.113.256', str(ctx.exception))
        for value in ('', 'wss://relay.invalid/mp', 'wss://relay.invalid:7443/mp', 'wss://127.0.0.1/mp'):
            self.assertEqual(core.validate_endpoint(value), value)

    def test_fgfs_paths_and_launcher_callsign_policy(self):
        for path in ('fgfs', '/opt/FlightGear/bin/fgfs', '/opt/My FlightGear/fgfs'):
            with self.subTest(path=path):
                self.assertEqual(core.Profile(fgfs=path).fgfs, path)
        for path in ('', 'fg\x00s', 'fg\ns'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                core.Profile(fgfs=path)
        for callsign in ('P', 'PIL001', 'AB12CD'):
            core.Profile(callsign=callsign)
        for callsign in ('', 'obs1', 'OBS9', 'A' * 7, 'P IL', 'unicode-ünï', 'a-b', 'PIL\x001'):
            with self.subTest(callsign=callsign), self.assertRaises(ValueError):
                core.Profile(callsign=callsign)


class DemoCatalog(unittest.TestCase):
    def test_teams_and_first_fighters_available(self):
        self.assertEqual(set(core.DEMO_CATALOG), {'rebels', 'empire'})
        self.assertEqual([s.id for s in core.ships('rebels')], ['xwing', 'ywing', 'uwing'])
        self.assertEqual([s.id for s in core.ships('empire')], ['tie', 'bomber', 'reaper'])
        for team, first in (('rebels', 'xwing'), ('empire', 'tie')):
            with self.subTest(team=team):
                self.assertEqual([s.id for s in core.ships(team) if s.available], [first])
                self.assertIn('demo', core.select_ship(team, first).reason.lower())
                for s in core.ships(team):  # every entry carries an explanation
                    self.assertTrue(s.reason)

    def test_blocked_ships_raise_with_reason(self):
        for team, ship in (('rebels', 'ywing'), ('rebels', 'uwing'),
                           ('empire', 'bomber'), ('empire', 'reaper')):
            with self.subTest(ship=ship), self.assertRaises(ValueError) as ctx:
                core.select_ship(team, ship)
            self.assertIn('demo catalog', str(ctx.exception))

    def test_unknown_pair_and_wrong_team_refused(self):
        for team, ship in (('rebels', 'tie'), ('empire', 'xwing'), ('bajor', 'xwing'),
                           ('rebels', 'deathstar'), ('rebels', '')):
            with self.subTest(pair=(team, ship)), self.assertRaises(ValueError):
                core.select_ship(team, ship)
        with self.assertRaises(ValueError):
            core.Profile(team_id='bajor', ship_id='xwing')
        with self.assertRaises(ValueError):
            core.Profile(team_id='rebels', ship_id='tie')

    def test_mock_adapter_denies_blocks_and_marks_demo_grants(self):
        adapter = core.MockAdapter()
        for team, ship in (('rebels', 'ywing'), ('rebels', 'reaper'), ('bajor', 'xwing')):
            granted, reason = adapter.authorize(team, ship)
            with self.subTest(pair=(team, ship)):
                self.assertFalse(granted)
                self.assertTrue(reason)
        granted, reason = adapter.authorize('rebels', 'xwing')
        self.assertTrue(granted)
        self.assertIn('demo', reason.lower())
        self.assertEqual([s.id for s in adapter.catalog('empire')],
                         [s.id for s in core.DEMO_CATALOG['empire']])


class RelayStubStates(unittest.TestCase):
    def test_connect_is_always_error_unavailable(self):
        relay = core.RelayStub()
        self.assertEqual(relay.state, 'preparing')
        self.assertEqual(relay.connect(), 'error')
        self.assertEqual(relay.state, 'error')
        self.assertIn('unavailable', relay.reason.lower())
        self.assertNotIn(relay.state, ('ready', 'connected'))

    def test_state_set_has_no_connected(self):
        self.assertEqual(core.RelayStub.STATES, ('preparing', 'connecting', 'ready', 'error'))
        self.assertNotIn('connected', core.RelayStub.STATES)


class SoloArgv(unittest.TestCase):
    def test_solo_argv_is_c172p_multiplay_off_no_terrasync(self):
        with sealed():
            argv = core.build_solo_args(core.Profile())
        self.assertEqual(argv[0], 'fgfs')
        self.assertTrue(all(isinstance(a, str) for a in argv))
        self.assertIn('--aircraft=c172p', argv)
        self.assertIn('--callsign=PIL001', argv)
        self.assertNotIn('--enable-terrasync', argv)
        self.assertEqual([a for a in argv if a.startswith('--multiplay=')], [])
        for prop in ('--prop:/sim/multiplay/txhost=', '--prop:/sim/multiplay/txport=0',
                     '--prop:/sim/multiplay/rxport=0'):
            self.assertIn(prop, argv)  # remembered MP settings stay disabled

    def test_demo_choice_and_endpoint_never_enter_argv(self):
        profiles = (core.Profile(),
                    core.Profile(endpoint='wss://relay.invalid/mp', team_id='empire', ship_id='tie'))
        for profile in profiles:
            with self.subTest(profile=profile.team_id), sealed():
                joined = ' '.join(core.build_solo_args(profile))
            for banned in ('wss', 'relay.invalid', 'rebels', 'empire', 'xwing', 'tie', 'token'):
                self.assertNotIn(banned, joined)
            self.assertIn('--aircraft=c172p', joined)  # c172p is the only solo tool

    def test_profile_catalog_relay_and_argv_build_do_no_io(self):
        with sealed():
            core.Profile(endpoint='wss://relay.invalid/mp', team_id='empire', ship_id='tie')
            core.select_ship('rebels', 'xwing')
            granted, _ = core.MockAdapter().authorize('rebels', 'xwing')
            self.assertTrue(granted)
            core.RelayStub().connect()
            core.build_solo_args(core.Profile())

    def test_default_session_fails_cleanly_without_reaching_popen(self):
        with sealed(), mock.patch.object(core.shutil, 'which', return_value=None):
            session = core.SoloSession()
            self.assertIs(session._spawn, subprocess.Popen)  # the patched fake
            with self.assertRaises(FileNotFoundError):
                session.launch(core.Profile())
            self.assertIsNone(session.process)


class SoloSessionLifecycle(unittest.TestCase):
    def session(self, spawn, which=lambda name: '/fixture/fgfs'):
        return core.SoloSession(resolver=which, spawn=spawn)

    def test_launch_argv_list_no_shell_devnull_inherited_output(self):
        calls = []

        def spawn(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeChild()

        session = self.session(spawn)
        child = session.launch(core.Profile())
        self.assertIs(child, session.process)
        argv, kwargs = calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], '/fixture/fgfs')
        self.assertIn('--aircraft=c172p', argv)
        self.assertIs(kwargs['shell'], False)
        self.assertIs(kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('stdout', kwargs)  # stdout/stderr are inherited, never pipes
        self.assertNotIn('stderr', kwargs)
        self.assertIsNone(session.poll())

    def test_explicit_path_with_spaces_bypasses_the_resolver(self):
        which = mock.Mock()
        calls = []

        def spawn(argv, **kwargs):
            calls.append(argv)
            return FakeChild()

        self.session(spawn, which=which).launch(core.Profile(fgfs='/opt/My FlightGear/fgfs'))
        self.assertEqual(calls[0][0], '/opt/My FlightGear/fgfs')  # one argv item, no shell
        which.assert_not_called()

    def test_missing_executable_fails_without_spawning(self):
        spawn = mock.Mock()
        with self.assertRaises(FileNotFoundError):
            self.session(spawn, which=lambda name: None).launch(core.Profile())
        spawn.assert_not_called()

    def test_spawn_error_is_generic_and_session_stays_reusable(self):
        def boom(argv, **kwargs):
            raise OSError('private os message /fixture details')

        session = self.session(boom)
        with self.assertRaises(RuntimeError) as ctx:
            session.launch(core.Profile())
        self.assertNotIn('private', str(ctx.exception))
        self.assertIsNone(session.process)

    def test_duplicate_launch_refused_until_exit_then_restart(self):
        children = [FakeChild(), FakeChild()]
        spawn = mock.Mock(side_effect=children)
        session = self.session(spawn)
        session.launch(core.Profile())
        with self.assertRaises(RuntimeError):  # second press while running
            session.launch(core.Profile())
        self.assertEqual(spawn.call_count, 1)
        self.assertIsNone(session.poll())
        session.process.exit_code = 3
        self.assertEqual(session.poll(), 3)
        self.assertIs(session.launch(core.Profile()), children[1])  # restart after exit
        with self.assertRaises(RuntimeError):
            session.launch(core.Profile())


class ImportBoundaries(unittest.TestCase):
    def test_package_reexports_the_core_api(self):
        for name in ('DEMO_CATALOG', 'MAX_PROFILE_BYTES', 'MockAdapter', 'Profile', 'RelayStub',
                     'Ship', 'SoloSession', 'build_solo_args', 'load_profile', 'save_profile',
                     'select_ship', 'ships', 'validate_endpoint'):
            self.assertTrue(hasattr(space_ui, name), name)

    def test_core_imported_without_tkinter(self):
        self.assertIn('space_ui.core', sys.modules)
        self.assertNotIn('tkinter', sys.modules)


def unused_imports(path):
    """AST scan: imported names that are never referenced (nor listed in __all__).

    No linter runs in this offline test set, so the shipped package guards its
    own import hygiene here: an unused import (the DEMO_CATALOG case in view.py)
    is dead weight that fakes a dependency and misleads readers of the header."""
    tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imported[alias.asname] = node.lineno
                elif '.' not in alias.name:  # `import launch` binds `launch`
                    imported[alias.name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != '*':
                    imported[alias.asname or alias.name] = node.lineno
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    exported = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List)):
                exported = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return sorted('%s:%d: %s' % (path.name, line, name)
                  for name, line in imported.items()
                  if name not in used and name not in exported)


class StaticHygiene(unittest.TestCase):
    def test_no_unused_imports_in_shipped_modules(self):
        offenders = []
        for path in sorted((REPO / 'client' / 'space_ui').glob('*.py')):
            offenders += unused_imports(path)
        self.assertEqual(offenders, [])

    def test_view_no_longer_imports_the_catalog_it_never_uses(self):
        source = (REPO / 'client' / 'space_ui' / 'view.py').read_text(encoding='utf-8')
        import_line = next(l for l in source.splitlines()
                           if l.startswith('from .core import'))
        self.assertNotIn('DEMO_CATALOG', import_line)


class EntryPoints(unittest.TestCase):
    """python -m client.space_ui boundaries, testable without Tk or a display:
    help needs no GUI, missing Tk/display exits 2 with one honest line, an
    invalid --profile stops before the GUI, and nothing ever spawns."""

    def fake_view(self, calls):
        module = types.ModuleType('space_ui.view')

        class TclError(Exception):
            pass

        def run(profile):
            calls.append(profile)
            return 0

        module.TclError = TclError
        module.run = run
        return module

    def test_help_needs_no_tkinter_and_does_no_io(self):
        with sealed(), contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit) as stop:
            ui_main.main(['--help'])
        self.assertEqual(stop.exception.code, 0)
        self.assertIn('--profile', out.getvalue())
        self.assertNotIn('tkinter', sys.modules)

    def test_missing_tkinter_exits_2_without_traceback(self):
        self.assertFalse(hasattr(space_ui, 'view'))  # the core never pulls the GUI in
        with sealed(), mock.patch.dict(sys.modules, {'space_ui.view': None}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ui_main.main([]), 2)
        message = err.getvalue()
        self.assertTrue(message.startswith('space_ui:'))
        self.assertIn('tkinter', message.lower())
        self.assertNotIn('Traceback', message)

    def test_missing_display_exits_2_via_tclerror(self):
        view = self.fake_view([])

        def boom(profile):
            raise view.TclError('no display name and no $DISPLAY')

        view.run = boom
        with sealed(), mock.patch.object(space_ui, 'view', view, create=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ui_main.main([]), 2)
        message = err.getvalue().lower()
        self.assertIn('window', message)  # honest: open/configure failed, not "no display" claimed
        self.assertIn('display', message)
        self.assertNotIn('traceback', message)

    def test_invalid_profile_stops_before_gui_and_popen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.json'
            path.write_text('{"fgfs": "fgfs", "token": "s3cr3tvalue"}', encoding='utf-8')
            with sealed(), mock.patch.dict(sys.modules, {'space_ui.view': None}), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ui_main.main(['--profile', str(path)]), 2)
        message = err.getvalue().lower()
        self.assertIn('profile', message)
        self.assertNotIn('tkinter', message)  # it failed on the profile, not on Tk
        self.assertNotIn('s3cr3tvalue', message)

    def test_example_profile_loads_for_the_gui_and_starts_nothing(self):
        example = REPO / 'client' / 'space_ui' / 'profile.example.json'
        self.assertEqual(core.load_profile(example), core.Profile())
        self.assertEqual(set(json.loads(example.read_text(encoding='utf-8'))),
                         set(core.Profile.FIELDS))
        seen = []
        with sealed(), mock.patch.object(space_ui, 'view', self.fake_view(seen), create=True):
            self.assertEqual(ui_main.main(['--profile', str(example)]), 0)
        self.assertEqual(seen, [core.Profile()])

    def test_imported_modules_stay_gui_free(self):
        self.assertIn('space_ui.core', sys.modules)
        self.assertIn('space_ui.__main__', sys.modules)
        self.assertNotIn('tkinter', sys.modules)
        self.assertNotIn('space_ui.view', sys.modules)


@contextlib.contextmanager
def stubbed_view():
    """Import client/space_ui/view over a minimal tkinter stub for boundary checks.

    Stub widgets record master, options and the pack/grid/place manager used and
    raise the same kind of TclError real Tk raises when two managers mix under
    one master. Nothing is drawn and no event loop runs, so this proves layout
    contracts, never pixels. sys.modules and the package attribute are restored
    afterwards: the view module built here never leaks into other tests."""
    names = ('tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox',
             'space_ui.view')
    saved = {name: sys.modules.pop(name, None) for name in names}

    class TclError(Exception):
        pass

    class StubVar:
        def __init__(self, master=None, value=None, **kwargs):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class StubWidget:
        def __init__(self, master=None, screen=(1280, 1024), **options):
            self.calls = []
            self.children = []
            self.managers = set()
            self.master = master
            self.manager = None
            self.screen = screen
            self.options = dict(options)
            self.after_calls = []
            self.cancelled = []
            self.destroyed = False
            self.geometry_spec = None
            self.minsize_spec = None
            if isinstance(master, StubWidget):
                master.children.append(self)

        def pack(self, **kwargs):
            self._claim('pack')

        def grid(self, **kwargs):
            self._claim('grid')

        def place(self, **kwargs):
            self._claim('place')

        def _claim(self, name):
            if self.manager == name:
                return
            self.manager = name
            if self.master is not None:
                if self.master.managers and name not in self.master.managers:
                    raise TclError('geometry managers collide: ' + name + ' and '
                                   + ','.join(sorted(self.master.managers)))
                self.master.managers.add(name)

        def get_children(self):
            return ()

        def selection(self):
            return ()

        def configure(self, *args, **kwargs):
            self.options.update(kwargs)

        def winfo_screenwidth(self):
            return self.screen[0]

        def winfo_screenheight(self):
            return self.screen[1]

        def geometry(self, spec=None):
            if spec:
                self.geometry_spec = spec

        def minsize(self, width=None, height=None):
            self.minsize_spec = (width, height)

        def after(self, delay, func):
            self.after_calls.append((delay, func))
            return 'after#' + str(len(self.after_calls))

        def after_cancel(self, job):
            self.cancelled.append(job)

        def destroy(self):
            self.destroyed = True

        def __getattr__(self, name):
            if name.startswith('__'):
                raise AttributeError(name)

            def _recorded(*args, **kwargs):  # generic no-op that is still visible
                self.calls.append((name, args, kwargs))
                return None

            return _recorded

    tk_stub = types.ModuleType('tkinter')
    tk_stub.TclError = TclError
    tk_stub.StringVar = StubVar
    tk_stub.Canvas = StubWidget
    tk_stub.Frame = StubWidget
    ttk_stub = types.ModuleType('tkinter.ttk')
    for widget in ('Style', 'Frame', 'Label', 'LabelFrame', 'Entry', 'Button',
                   'Radiobutton', 'Treeview', 'Scrollbar'):
        setattr(ttk_stub, widget, StubWidget)
    filedialog_stub = types.ModuleType('tkinter.filedialog')
    filedialog_stub.askopenfilename = lambda **kw: ''
    filedialog_stub.asksaveasfilename = lambda **kw: ''
    box_stub = types.ModuleType('tkinter.messagebox')
    box = {'answer': False, 'calls': []}

    def askyesno(title, message, parent=None, **kwargs):
        box['calls'].append((title, message))
        return box['answer']

    box_stub.askyesno = askyesno
    box_stub.showerror = lambda *a, **k: None
    box_stub.showinfo = lambda *a, **k: None
    tk_stub.ttk = ttk_stub
    tk_stub.filedialog = filedialog_stub
    tk_stub.messagebox = box_stub
    sys.modules['tkinter'] = tk_stub
    sys.modules['tkinter.ttk'] = ttk_stub
    sys.modules['tkinter.filedialog'] = filedialog_stub
    sys.modules['tkinter.messagebox'] = box_stub
    try:
        env = types.SimpleNamespace(view=importlib.import_module('space_ui.view'),
                                    TclError=TclError, StubWidget=StubWidget, box=box)
        yield env
    finally:
        with contextlib.suppress(AttributeError):
            delattr(space_ui, 'view')
        for name in names:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]


def walk_widgets(widget):
    yield widget
    for child in widget.children:
        yield from walk_widgets(child)


class ProfileIoGuards(unittest.TestCase):
    def test_bounded_read_rejects_growth_beyond_the_stat_hint(self):
        class SmallStatPath:
            def __init__(self, real):
                self.real = Path(real)

            def stat(self):
                return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 10, 0, 0, 0))

            def open(self, mode='rb', **kwargs):
                return self.real.open(mode)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'grow.json'
            path.write_bytes(json.dumps(core.Profile().to_dict()).encode('utf-8') + b' ' * 400)
            with mock.patch.object(core, 'Path', SmallStatPath), \
                    mock.patch.object(core, 'MAX_PROFILE_BYTES', 64):
                with self.assertRaises(ValueError) as ctx:  # stat lied: read stays bounded
                    core.load_profile(path)
            self.assertIn('too large', str(ctx.exception))

    def test_nonregular_profile_refused_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:  # reading a FIFO would block forever
            fifo = Path(tmp) / 'fifo.json'
            os.mkfifo(fifo)
            with self.assertRaises(ValueError) as ctx:
                core.load_profile(fifo)
            self.assertIn('regular file', str(ctx.exception))


class HeadlessFormGuards(unittest.TestCase):
    """Layout and behavior contracts of view.ClientApp on the tkinter boundary stub.

    Nothing is rendered here; these guard the manager-mixing crash class found in
    review, clamped window geometry, DEMO status resets, the single poll timer and
    the non-killing close. Real pixels still require smoke.py on a Tk machine."""

    def app(self, env, screen=(1280, 1024)):
        root = env.StubWidget(screen=screen)
        return env.view.ClientApp(root), root

    def test_stub_self_check_catches_pack_grid_mix(self):
        with stubbed_view() as env:
            parent = env.StubWidget()
            env.StubWidget(parent).grid(row=0)
            with self.assertRaises(env.TclError):
                env.StubWidget(parent).pack()

    def test_form_builds_and_status_label_uses_the_parents_manager(self):
        with stubbed_view() as env:
            app, root = self.app(env)  # the review bug raised TclError right here
            self.assertEqual(app.profile_status.manager, 'grid')
            prof = app.profile_status.master
            self.assertTrue(prof.options['text'].startswith('Профиль'))
            self.assertEqual(prof.managers, {'grid'})
            for widget in walk_widgets(root):  # one manager per master everywhere
                self.assertLessEqual(len(widget.managers), 1)
            self.assertTrue(hasattr(app, 'canvas'))  # scrollable body exists

    def test_window_geometry_is_clamped_to_the_screen(self):
        with stubbed_view() as env:
            app, root = self.app(env, screen=(1280, 1024))
            self.assertEqual(root.geometry_spec, '960x720')
            self.assertLessEqual(root.minsize_spec, (1280, 1024))
            app, root = self.app(env, screen=(800, 600))
            self.assertEqual(root.geometry_spec, '800x600')
            self.assertLessEqual(root.minsize_spec, (800, 600))

    def test_selection_change_voids_grant_and_shows_block_reason(self):
        with stubbed_view() as env:
            app, root = self.app(env)
            app._say(app.demo_status, 'DEMO-проверка: выбор разрешён — demo', env.view.OK)
            app.tree = types.SimpleNamespace(selection=lambda: ('ywing',))
            app._selection_changed()
            text = app.demo_status.options['text']
            self.assertIn('DEMO-выбор недоступен', text)
            self.assertIn('demo catalog', text)  # the blocking reason is shown
            self.assertNotIn('разрешён', text)  # the old grant is gone
            app.tree = types.SimpleNamespace(selection=lambda: ('xwing',))
            app._selection_changed()
            text = app.demo_status.options['text']
            self.assertIn('не запуск', text)
            self.assertIn('не слот', text)

    def test_poll_os_error_is_handled_and_timer_stays_single(self):
        with stubbed_view() as env:
            app, root = self.app(env)

            class GrumpyChild:
                def poll(self):
                    raise OSError('private os detail')

            app.session.process = GrumpyChild()
            app._schedule_poll()
            app._schedule_poll()  # must not stack a second timer
            self.assertEqual(len(root.after_calls), 1)
            app._poll_child()  # poll raises OSError inside: handled, no traceback
            self.assertIsNone(app._poll_job)
            self.assertNotIn('private', app.launch_status.options['text'])
            self.assertIn('не удался', app.launch_status.options['text'])
            self.assertEqual(app.launch_button.options.get('state'), 'normal')

    def test_close_confirms_keeps_fg_running_and_cleans_up(self):
        with stubbed_view() as env:
            app, root = self.app(env)
            running = FakeChild()
            app.session.process = running
            env.box['answer'] = False
            app._on_close()
            self.assertFalse(root.destroyed)  # user said: keep window
            title, message = env.box['calls'][-1]
            self.assertIn('Закрыть только клиент', title)
            self.assertIn('FG продолжит', message)  # no-kill promise stated
            app._poll_job = 'after#1'
            env.box['answer'] = True
            app._on_close()
            self.assertTrue(root.destroyed)
            self.assertIn('after#1', root.cancelled)
            self.assertEqual(sum(1 for c in app.canvas.calls
                                 if c[0] == 'unbind_all'), 3)
            self.assertIsNone(running.poll())  # nobody touched the child

    def test_target_version_falls_back_on_malformed_data(self):
        with stubbed_view() as env:
            self.assertRegex(env.view.target_version(), r'^\d+\.\d+(\.\d+)?$')

            class BadPath:  # list where a mapping is expected: no traceback, just ''
                def __init__(self, *args):
                    pass

                def resolve(self):
                    return self

                @property
                def parents(self):
                    return (self, self, self)

                def __truediv__(self, other):
                    return self

                def read_text(self, encoding=None):
                    return '[1, 2]'

            with mock.patch.object(env.view, 'Path', BadPath):
                self.assertEqual(env.view.target_version(), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
