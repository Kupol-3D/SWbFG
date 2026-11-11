#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the TASK7 offline match core: rules, budget, dedup, endings.

Stdlib unittest only.  Nothing here starts FlightGear, opens a socket, reads a
clock/random source or touches a file: one test asserts the module's own import
set and another runs a match with open/socket/urandom/random patched to raise.
These tests prove the offline model of match_core/CONTRACT.md; they are not
server enforcement and not a runtime check in real FlightGear.
"""
import ast
import copy
import json
import re
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from match_core import (MatchError, apply_event, create_match, restore_snapshot,
                        snapshot)  # noqa: E402
from match_core import core  # noqa: E402

REBEL_TYPES = ('xwing', 'ywing', 'uwing')
IMPERIAL_TYPES = ('tie', 'bomber', 'reaper')
TEST_TARGET = 3   # the 2x2 fixtures cannot reach 15; the 15-kill fixture asks for it


def catalog():
    return {'rebels': list(REBEL_TYPES), 'empire': list(IMPERIAL_TYPES)}


def event(event_id, type_, **fields):
    """One v1 event with the four common fields filled in."""
    data = {'version': 1, 'match_id': 'round-1', 'event_id': event_id, 'type': type_}
    data.update(fields)
    return data


def join(event_id, pilot, team, callsign):
    return event(event_id, 'join', player_id=pilot, team_id=team, callsign=callsign)


def spawn(event_id, pilot, ship, spawn_id):
    return event(event_id, 'spawn', player_id=pilot, ship_id=ship, spawn_id=spawn_id)


def kill(event_id, victim, killer):
    return event(event_id, 'death', victim_spawn_id=victim, killer_spawn_id=killer)


def ok(state, event_data):
    result = apply_event(state, event_data)
    if not result.accepted:
        raise AssertionError('%s rejected: %s' % (event_data.get('event_id'), result.reason))
    if result.state is state:
        raise AssertionError('accepted state aliases the input state')
    return result.state


def refuse(state, event_data, reason):
    """Rejected: reason code, equivalent state, no aliasing, ledger/slots intact."""
    before = copy.deepcopy(state)
    result = apply_event(state, event_data)
    assert result.accepted is False, '%s was accepted' % event_data.get('event_id')
    assert result.reason == reason, '%s: got %s, want %s' % (event_data.get('event_id'),
                                                            result.reason, reason)
    assert result.state == before, 'rejected event changed the state'
    assert result.state is not state, 'rejected state aliases the input state'
    return state


def lobby(**config):
    return create_match('round-1', catalog(), **config)


def joined(rebels=2, empire=2, target_kills=TEST_TARGET, **config):
    state = lobby(target_kills=target_kills, **config)
    for i in range(rebels):
        state = ok(state, join('join-r%d' % i, 'r%d' % i, 'rebels', 'RED%03d' % i))
    for i in range(empire):
        state = ok(state, join('join-e%d' % i, 'e%d' % i, 'empire', 'EMP%03d' % i))
    return state


def started(**config):
    return ok(joined(**config), event('start', 'start'))


def armed(**config):
    """Running round with one imperial ship in the air, so a killer exists."""
    return ok(started(**config), spawn('spawn-e0-0', 'e0', 'tie', 'spawn-e0-0'))


def airborne(**config):
    """Running round with one live ship per side."""
    return ok(armed(**config), spawn('spawn-r0-0', 'r0', 'xwing', 'spawn-r0-0'))


def containers(node, found):
    if isinstance(node, dict):
        found.add(id(node))
        for value in node.values():
            containers(value, found)
    elif isinstance(node, list):
        found.add(id(node))
        for value in node:
            containers(value, found)
    return found


class TestPurity(unittest.TestCase):
    """No network, clock, random, file or process access - and none imported."""

    def test_module_imports_only_pure_stdlib(self):
        source = (REPO / 'match_core' / 'core.py').read_text(encoding='utf-8')
        imported = set(re.findall(r'(?m)^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)',
                                  source))
        self.assertEqual(imported, {'copy', 'hashlib', 'json', 're', 'dataclasses'})
        for banned in ('eval(', 'exec(', 'open(', '__import__', '__subclasses__'):
            self.assertNotIn(banned, source)

    def test_full_match_needs_no_io_or_clock(self):
        def banned(*args, **kwargs):
            raise AssertionError('match_core attempted IO')
        with mock.patch('builtins.open', side_effect=banned), \
                mock.patch('os.urandom', side_effect=banned), \
                mock.patch('random.random', side_effect=banned), \
                mock.patch.object(socket, 'socket', side_effect=banned), \
                mock.patch.object(socket, 'create_connection', side_effect=banned), \
                mock.patch.object(socket, 'getaddrinfo', side_effect=banned):
            state = airborne()
            state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
            state = ok(state, event('tick-1', 'tick', elapsed_seconds=10))
            saved = json.loads(json.dumps(snapshot(state)))
            state = restore_snapshot(saved)
        self.assertEqual(state['scores']['empire'], 1)

    def test_same_inputs_produce_the_same_state_and_fingerprints(self):
        for target in (airborne(), airborne()):
            ok(target, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
            ok(target, event('tick-1', 'tick', elapsed_seconds=7))
        first, second = airborne(), airborne()
        self.assertEqual(first, second)
        self.assertEqual(first['event_ledger'], second['event_ledger'])
        self.assertEqual(len(set(first['event_ledger'].values())), len(first['history']))


def unused_imports(path):
    """AST scan: imported names never referenced and not exported via __all__.

    No linter runs in this offline test set, so the package guards its own
    import hygiene here: an unused import fakes a dependency and misleads
    readers of the module header (the DEMO_CATALOG case found in the TASK6/7
    audit lived in client/space_ui/view.py)."""
    tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imported[alias.asname] = node.lineno
                elif '.' not in alias.name:  # `import copy` binds `copy`
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


class TestImportHygiene(unittest.TestCase):
    def test_no_unused_imports_in_match_core(self):
        offenders = []
        for path in sorted((REPO / 'match_core').glob('*.py')):
            offenders += unused_imports(path)
        self.assertEqual(offenders, [])


class TestStateShape(unittest.TestCase):
    def test_state_is_json_compatible_and_replays_identically(self):
        state = airborne()
        revived = json.loads(json.dumps(state))
        self.assertEqual(revived, state)
        data = kill('kill-1', 'spawn-r0-0', 'spawn-e0-0')
        direct, restored = apply_event(state, data), apply_event(revived, data)
        self.assertEqual(direct.state, restored.state)
        self.assertEqual(direct.reason, restored.reason)

    def test_state_keeps_what_a_later_snapshot_must_validate(self):
        state = ok(airborne(), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        closed = state['spawns']['spawn-r0-0']
        self.assertEqual((closed['status'], closed['closed_by'], closed['killer_spawn_id']),
                         ('closed', 'death', 'spawn-e0-0'))
        self.assertEqual(state['seq'], len(state['history']))
        self.assertEqual([entry['seq'] for entry in state['history']],
                         list(range(1, len(state['history']) + 1)))
        self.assertEqual(sorted(state['event_ledger']),
                         sorted(entry['event_id'] for entry in state['history']))
        # the history is the replay source of restore_snapshot, so each entry
        # must carry its event next to the metadata
        for entry in state['history']:
            self.assertEqual(set(entry), {'seq', 'event_id', 'type', 'fingerprint', 'event'})
            self.assertEqual(entry['event']['event_id'], entry['event_id'])
            self.assertEqual(entry['event']['match_id'], state['match_id'])
            self.assertEqual(entry['event']['type'], entry['type'])


    def test_fingerprint_is_a_stable_canonical_payload_hash(self):
        # Pinned so a later snapshot validator can re-check replayed events:
        # the CONTRACT.md join example must always hash to this digest.
        example = {'version': 1, 'match_id': 'round-1', 'event_id': 'join-a',
                   'type': 'join', 'player_id': 'pilot-a', 'team_id': 'rebels',
                   'callsign': 'RED001'}
        self.assertEqual(core._fingerprint(example),
                         '0a4a45303566e28cbf38cc3036d31ee5d754dd02fa24af567ece74bdfe8919ae')

    def test_scores_can_be_recomputed_from_closed_spawns(self):
        """A restored snapshot does not have to be trusted on its score field."""
        state = airborne()
        state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        state = ok(state, spawn('spawn-r1-0', 'r1', 'ywing', 'spawn-r1-0'))
        state = ok(state, event('crash-1', 'crash', spawn_id='spawn-r1-0'))
        state = ok(state, spawn('spawn-r1-1', 'r1', 'uwing', 'spawn-r1-1'))
        state = ok(state, kill('kill-2', 'spawn-r1-1', 'spawn-e0-0'))
        recomputed = {'rebels': 0, 'empire': 0}
        for ship in state['spawns'].values():
            if ship['closed_by'] == 'death':
                team = state['spawns'][ship['killer_spawn_id']]['team_id']
                recomputed[team] += 1
        self.assertEqual(recomputed, state['scores'])


class TestNoAliasing(unittest.TestCase):
    def test_create_match_copies_the_catalog(self):
        source = catalog()
        state = create_match('round-1', source)
        self.assertEqual(state['catalog'], source)
        self.assertFalse(containers(source, set()) & containers(state['catalog'], set()))
        state['catalog']['rebels'].append('ghost')
        state['players']['injected'] = {}
        self.assertEqual(source, catalog())

    def test_accepted_event_leaves_its_inputs_untouched(self):
        state, data = airborne(), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0')
        state_copy, data_copy = copy.deepcopy(state), copy.deepcopy(data)
        result = apply_event(state, data)
        self.assertTrue(result.accepted)
        self.assertEqual((state, data), (state_copy, data_copy))
        self.assertFalse(containers(state, set()) & containers(result.state, set()))

    def test_rejected_event_leaves_its_inputs_untouched(self):
        state, data = airborne(), kill('bad-1', 'nope', 'spawn-e0-0')
        state_copy, data_copy = copy.deepcopy(state), copy.deepcopy(data)
        result = apply_event(state, data)
        self.assertFalse(result.accepted)
        self.assertEqual((state, data), (state_copy, data_copy))
        self.assertFalse(containers(state, set()) & containers(result.state, set()))


class TestConfigValidation(unittest.TestCase):
    def test_match_id_must_be_a_bounded_printable_string(self):
        for bad in ('', ' ', 'a' * 65, None, 7, 'round\n1', 'раунд'):
            with self.subTest(match_id=repr(bad)), self.assertRaises(MatchError):
                create_match(bad, catalog())

    def test_catalog_must_name_concrete_types_for_both_teams(self):
        for bad in (None, {}, {'rebels': ['xwing']},
                    {'rebels': ['xwing'], 'empire': ['tie'], 'pirates': ['ship']},
                    {'rebels': [], 'empire': ['tie']},
                    {'rebels': ['xwing', 'xwing'], 'empire': ['tie']},
                    {'rebels': 'xwing', 'empire': ['tie']},
                    {'rebels': [''], 'empire': ['tie']},
                    {'rebels': [7], 'empire': ['tie']}):
            with self.subTest(catalog=repr(bad)[:48]), self.assertRaises(MatchError):
                create_match('round-1', bad)

    def test_target_kills_is_a_positive_int_and_defaults_to_15(self):
        self.assertEqual(create_match('round-1', catalog())['config']['target_kills'], 15)
        for bad in (0, -3, True, False, None, '15', 15.0):
            with self.subTest(target_kills=repr(bad)):
                with self.assertRaises(MatchError):
                    create_match('round-1', catalog(), target_kills=bad)

    def test_fallbacks_are_opt_in_and_typed(self):
        config = create_match('round-1', catalog())['config']
        self.assertIs(config['exhaustion_enabled'], False)
        self.assertIsNone(config['time_limit_seconds'])
        for bad in (1, 'yes', None, 'true'):
            with self.subTest(exhaustion_enabled=repr(bad)):
                with self.assertRaises(MatchError):
                    create_match('round-1', catalog(), exhaustion_enabled=bad)
        for bad in (0, -1, True, 60.0, '60'):
            with self.subTest(time_limit_seconds=repr(bad)):
                with self.assertRaises(MatchError):
                    create_match('round-1', catalog(), time_limit_seconds=bad)
        self.assertEqual(create_match('round-1', catalog(), exhaustion_enabled=True,
                                     time_limit_seconds=1200)['config'],
                         {'target_kills': 15, 'exhaustion_enabled': True,
                          'time_limit_seconds': 1200})


class TestStateValidation(unittest.TestCase):
    def test_garbage_state_is_an_error_not_a_rejection(self):
        cases = (None, [], 'state', {'version': 2, 'match_id': 'round-1',
                                     'phase': 'lobby'},
                 {'version': 1, 'match_id': 'round-1'},
                 {'version': 1, 'match_id': 'round-1', 'phase': 'paused'},
                 dict(lobby(), history='not a list'), dict(lobby(), players=[]),
                 dict(lobby(), match_id=''))
        for bad in cases:
            with self.subTest(state=repr(bad)[:48]), self.assertRaises(MatchError):
                apply_event(bad, event('start', 'start'))

    def test_fast_guard_refuses_bad_types_without_leaking_exceptions(self):
        def broken(**overrides):
            state = airborne()
            state.update(overrides)
            return state
        cases = (
            dict(version=True), dict(version=1.0), dict(phase='paused'),
            dict(seq=True), dict(seq=-1), dict(seq=99),
            dict(scores={'rebels': -1, 'empire': 0}),
            dict(scores={'rebels': True, 'empire': 0}),
            dict(scores={'rebels': 0, 'empire': 0, 'pirates': 1}),
            dict(clock={'elapsed_seconds': -1}), dict(clock={'elapsed_seconds': 1.5}),
            dict(clock={'elapsed_seconds': True}), dict(clock={'seconds': 0}),
            dict(config={'target_kills': True, 'exhaustion_enabled': False,
                         'time_limit_seconds': None}),
            dict(config={'target_kills': 3, 'exhaustion_enabled': 1}),
            dict(catalog={'rebels': ['xwing']}),
            dict(players='none'), dict(spawns=None), dict(history={}),
            dict(event_ledger=[]), dict(result={'reason': 'kill_target', 'winner': None,
                                                'outcome': 'draw', 'seq': 1}),
            dict(cache='injected'))
        for override in cases:
            with self.subTest(fields=sorted(override)[:3]):
                with self.assertRaises(MatchError):
                    apply_event(broken(**override), event('start', 'start'))
        for missing in ('clock', 'history', 'event_ledger', 'catalog'):
            state = airborne()
            state.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(MatchError):
                apply_event(state, event('start', 'start'))

    def test_fast_guard_refuses_dangling_records_and_history_mismatches(self):
        def broken(mutate):
            state = json.loads(json.dumps(snapshot(airborne())['state']))
            mutate(state)
            return state

        def free_active(state):
            state['players']['r0']['active_spawn_id'] = None

        def orphan_alive_spawn(state):
            state['spawns']['spawn-x'] = {'spawn_id': 'spawn-x', 'player_id': 'r1',
                                          'ship_id': 'xwing', 'team_id': 'rebels',
                                          'status': 'alive', 'opened_seq': 7,
                                          'closed_seq': None, 'closed_by': None,
                                          'killer_spawn_id': None}

        def spent_foreign_type(state):
            state['players']['r0']['used_ship_ids'] = ['tie']

        def duplicate_used_type(state):
            state['players']['r0']['used_ship_ids'] = ['xwing', 'xwing']

        def history_without_event(state):
            state['history'][0].pop('event')

        def history_seq_shift(state):
            state['history'][-1]['seq'] = 99

        def ledger_lost_an_entry(state):
            state['event_ledger'].pop(state['history'][-1]['event_id'])

        for mutate in (free_active, orphan_alive_spawn, spent_foreign_type,
                       duplicate_used_type, history_without_event, history_seq_shift,
                       ledger_lost_an_entry):
            with self.subTest(case=mutate.__name__):
                with self.assertRaises(MatchError):
                    apply_event(broken(mutate), event('start', 'start'))


class TestMalformedValueMatrix(unittest.TestCase):
    """list/dict/null/bool inside any state part: MatchError, never TypeError/KeyError.

    Every injection leaves a structurally invalid state, so the fast guard
    shared by apply_event, snapshot and restore_snapshot must refuse it.  An
    escaping TypeError/KeyError fails the case even if the state is also
    refused, and so does an injection that stayed accidentally valid.  This
    regresses the guards that once hashed before checking element types
    (used_ship_ids entries, killer_spawn_id dict membership, result reason
    inside a frozenset) and the history seq that let True pass as 1.
    """

    ID_JUNK = ({}, [], None, True, 1)
    INT_JUNK = ({}, [], None, True, 1.5)

    def running(self):
        return ok(airborne(), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))

    def ended(self):
        return ok(airborne(target_kills=1), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))

    def injections(self):
        cases = []

        def add(label, base, path, junk):
            for value in junk:
                def mutate(state, path=path, value=value):
                    node = state
                    for key in path[:-1]:
                        node = node[key]
                    node[path[-1]] = value
                cases.append(('%s=%s' % (label, repr(value)[:18]), base, mutate))

        add('state.version', self.running, ('version',), ({}, [], None, 1.0, 2, True))
        add('state.match_id', self.running, ('match_id',), self.ID_JUNK)
        add('state.phase', self.running, ('phase',), ({}, [], None, 1, 'ENDED'))
        add('state.seq', self.running, ('seq',), self.INT_JUNK + (-1,))
        add('state.scores', self.running, ('scores', 'rebels'),
            self.INT_JUNK + (-1, '1'))
        add('state.clock', self.running, ('clock', 'elapsed_seconds'),
            self.INT_JUNK + (-1,))
        add('config.target_kills', self.running, ('config', 'target_kills'),
            self.INT_JUNK + (0, '3'))
        add('config.exhaustion', self.running, ('config', 'exhaustion_enabled'),
            ({}, [], None, 1, 'true'))
        add('config.time_limit', self.running, ('config', 'time_limit_seconds'),
            ({}, [], True, '60', 0))
        add('catalog.ship_type', self.running, ('catalog', 'rebels', 0), self.ID_JUNK)
        add('catalog.team_list', self.running, ('catalog', 'rebels'),
            ({}, [], None, 1, 'xwing'))
        add('player.id', self.running, ('players', 'r0', 'player_id'), self.ID_JUNK)
        add('player.team', self.running, ('players', 'r0', 'team_id'),
            self.ID_JUNK + ('pirates',))
        add('player.callsign', self.running, ('players', 'r0', 'callsign'),
            self.ID_JUNK + ('TOOLONG9',))
        add('player.joined_seq', self.running, ('players', 'r0', 'joined_seq'),
            self.INT_JUNK + (0,))
        add('player.active_spawn', self.running, ('players', 'r0', 'active_spawn_id'),
            ({}, [], True, 1, 'nope'))
        add('player.used_element', self.running,
            ('players', 'r0', 'used_ship_ids', 0), self.ID_JUNK + ('tie',))
        add('player.used_list', self.running, ('players', 'r0', 'used_ship_ids'),
            ({}, None, 1, 'xwing'))  # [] is structurally valid: replay-only forgery
        add('player.record', self.running, ('players', 'ghost'), ({}, [], None, 'x'))
        add('spawn.player', self.running, ('spawns', 'spawn-e0-0', 'player_id'),
            self.ID_JUNK + ('ghost',))
        add('spawn.ship', self.running, ('spawns', 'spawn-e0-0', 'ship_id'),
            self.ID_JUNK + ('nope',))
        add('spawn.team', self.running, ('spawns', 'spawn-e0-0', 'team_id'),
            self.ID_JUNK + ('pirates',))
        add('spawn.status', self.running, ('spawns', 'spawn-e0-0', 'status'),
            self.ID_JUNK + ('gone',))
        add('spawn.opened_seq', self.running, ('spawns', 'spawn-e0-0', 'opened_seq'),
            self.INT_JUNK + (0,))
        add('spawn.closed_by', self.running, ('spawns', 'spawn-r0-0', 'closed_by'),
            self.ID_JUNK + (123456,))
        add('spawn.closed_seq', self.running, ('spawns', 'spawn-r0-0', 'closed_seq'),
            ({}, [], None, True, 1.5))  # 1 would be a legal pos-int: replay-level only
        add('spawn.killer_closed', self.running,
            ('spawns', 'spawn-r0-0', 'killer_spawn_id'),
            self.ID_JUNK + ('spawn-r0-0', 'nope'))
        add('spawn.killer_alive', self.running,
            ('spawns', 'spawn-e0-0', 'killer_spawn_id'), ({}, [], True, 1, 'nope'))
        add('spawn.record', self.running, ('spawns', 'spawn-ghost'), ({}, [], None, 'x'))
        add('history.entry', self.running, ('history', 0), ({}, [], None, 'x'))
        add('history.whole', self.running, ('history',), ({}, [], None, 'x'))
        add('history.seq', self.running, ('history', 0, 'seq'),
            ({}, [], None, True, '1', 99))  # True==1 is exactly the guarded confusion
        add('history.event_id', self.running, ('history', 0, 'event_id'),
            self.ID_JUNK + ('ghost',))
        add('history.type', self.running, ('history', 0, 'type'), self.ID_JUNK + ('crash',))
        add('history.fingerprint', self.running, ('history', 0, 'fingerprint'),
            self.ID_JUNK + ('z' * 64,))
        add('history.event', self.running, ('history', 0, 'event'), ({}, [], None, 1))
        add('history.event_inner', self.running, ('history', 0, 'event', 'event_id'),
            self.ID_JUNK + ('ghost',))
        add('ledger.value', self.running, ('event_ledger', 'start'),
            self.ID_JUNK + ('x',))
        add('ledger.key', self.running, ('event_ledger', 'ghost'), ({}, [], None, 'x'))
        add('ledger.whole', self.running, ('event_ledger',), ({}, [], None, 'x'))
        add('result.whole', self.ended, ('result',), ({}, [], None, 'x'))
        add('result.reason', self.ended, ('result', 'reason'),
            self.ID_JUNK + ('timeout',))
        add('result.winner', self.ended, ('result', 'winner'),
            self.ID_JUNK + ('pirates',))
        add('result.outcome', self.ended, ('result', 'outcome'),
            self.ID_JUNK + ('lost',))
        add('result.seq', self.ended, ('result', 'seq'), self.INT_JUNK + (0, 99))
        for value in ({}, [], None, 'x'):
            def mutate(state, value=value):
                state['history'].append(value)
            cases.append(('history.append=%s' % (repr(value)[:18],), self.running,
                          mutate))
        return cases

    def test_all_three_public_entry_points_refuse_every_injection(self):
        checked = 0
        for label, base, mutate in self.injections():
            state = base()
            mutate(state)
            with self.subTest(case=label):
                with self.assertRaises(MatchError):
                    apply_event(state, event('probe', 'start'))
                with self.assertRaises(MatchError):
                    snapshot(state)
                with self.assertRaises(MatchError):
                    restore_snapshot({'version': 1, 'state': state})
            checked += 1
        self.assertGreater(checked, 200)


class TestEventValidation(unittest.TestCase):
    def setUp(self):
        self.state = airborne()

    def test_common_shape_rules(self):
        cases = [
            ('not-a-dict', 'malformed_event'),
            ({'version': 1, 'match_id': 'round-1', 'type': 'start'}, 'missing_field'),
            (event('x', 'death', victim_spawn_id='spawn-r0-0'), 'missing_field'),
            ({'version': 2, 'match_id': 'round-1', 'event_id': 'x', 'type': 'start'},
             'bad_version'),
            ({'version': True, 'match_id': 'round-1', 'event_id': 'x', 'type': 'start'},
             'bad_version'),
            ({'version': '1', 'match_id': 'round-1', 'event_id': 'x', 'type': 'start'},
             'bad_version'),
            (event('x', 'launch'), 'unknown_type'),
            ({'version': 1, 'match_id': 'round-1', 'event_id': 'x', 'type': 'start',
              'hp': 100}, 'unknown_field'),
            ({'version': 1, 'match_id': 'round-1', 'event_id': 'x', 'type': 'start',
              5: 'x'}, 'unknown_field'),
            (event('x', 'start', match_id='round-2'), 'wrong_match'),
            (event('', 'start'), 'bad_event_id'),
            ({'version': 1, 'match_id': '', 'event_id': 'x', 'type': 'start'},
             'bad_match_id'),
        ]
        for data, reason in cases:
            with self.subTest(event=repr(data)[:56]):
                refuse(self.state, data, reason)

    def test_field_values_are_never_coerced(self):
        cases = [
            (join('j', 'newbie', 'pirates', 'NEW01'), 'bad_team_id'),
            (join('j', '', 'rebels', 'NEW01'), 'bad_player_id'),
            (join('j', 'newbie', 'rebels', 'TOOLONG7'), 'bad_callsign'),
            (join('j', 'newbie', 'rebels', 'obs1'), 'bad_callsign'),
            (join('j', 'newbie', 'rebels', 'r-d'), 'bad_callsign'),
            (join('j', 'newbie', 'rebels', ''), 'bad_callsign'),
            (join('j', 'newbie', 'rebels', 12345), 'bad_callsign'),
            (spawn('s', 'r0', 'x' * 65, 'spawn-x'), 'bad_ship_id'),
            (event('t', 'tick', elapsed_seconds=True), 'bad_elapsed_seconds'),
            (event('t', 'tick', elapsed_seconds=1.5), 'bad_elapsed_seconds'),
            (event('t', 'tick', elapsed_seconds='10'), 'bad_elapsed_seconds'),
            (event('t', 'tick', elapsed_seconds=-1), 'bad_elapsed_seconds'),
            (kill('d', 'spawn-r0-0', None), 'bad_spawn_id'),
        ]
        for data, reason in cases:
            with self.subTest(event=repr(data)[:56]):
                refuse(self.state, data, reason)

    def test_start_carries_no_extra_fields(self):
        refuse(started(), event('start-2', 'start', target_kills=3), 'unknown_field')


class TestRoster(unittest.TestCase):
    def test_join_happens_in_lobby_and_locks_the_team(self):
        state = joined()
        self.assertEqual(state['phase'], 'lobby')
        self.assertEqual({p['team_id'] for p in state['players'].values()},
                         {'rebels', 'empire'})
        state = ok(state, event('start', 'start'))
        self.assertEqual(state['phase'], 'running')
        refuse(state, join('join-r9', 'r9', 'empire', 'NEW001'), 'roster_locked')
        self.assertNotIn('r9', state['players'])

    def test_repeat_join_never_grants_a_second_set_of_lives(self):
        state = refuse(joined(), join('join-again', 'r0', 'rebels', 'RED999'),
                       'already_joined')
        self.assertEqual(len(state['players']), 4)
        state = ok(state, event('start', 'start'))
        state = ok(state, spawn('spawn-r0-0', 'r0', 'xwing', 'spawn-r0-0'))
        state = refuse(state, join('join-again-2', 'r0', 'rebels', 'RED888'),
                       'roster_locked')
        self.assertEqual(state['players']['r0']['used_ship_ids'], ['xwing'])
        self.assertEqual(state['players']['r0']['callsign'], 'RED000')

    def test_callsign_is_display_policy_not_identity(self):
        state = refuse(joined(), join('j', 'newbie', 'rebels', 'red000'), 'callsign_taken')
        state = ok(state, join('j', 'newbie', 'rebels', 'NEW01'))
        self.assertEqual(state['players']['newbie']['callsign'], 'NEW01')

    def test_start_needs_both_teams_and_happens_once(self):
        refuse(ok(lobby(), join('j', 'r0', 'rebels', 'RED000')), event('start', 'start'),
               'roster_incomplete')
        refuse(lobby(), event('start', 'start'), 'roster_incomplete')
        refuse(started(), event('start-2', 'start'), 'already_started')

    def test_reconnect_renames_without_touching_team_budget_or_score(self):
        state = ok(airborne(), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        player, scores = copy.deepcopy(state['players']['r0']), copy.deepcopy(state['scores'])
        state = ok(state, event('re-1', 'reconnect', player_id='r0', callsign='RED777'))
        self.assertEqual(state['players']['r0']['callsign'], 'RED777')
        self.assertEqual(state['players']['r0']['team_id'], player['team_id'])
        self.assertEqual(state['players']['r0']['used_ship_ids'], player['used_ship_ids'])
        self.assertIsNone(state['players']['r0']['active_spawn_id'])
        self.assertEqual(state['scores'], scores)
        refuse(state, spawn('spawn-r0-again', 'r0', 'xwing', 'spawn-r0-again'), 'ship_spent')
        refuse(state, event('re-2', 'reconnect', player_id='ghost', callsign='GHO01'),
               'unknown_player')
        refuse(state, event('re-3', 'reconnect', player_id='e0', callsign='RED777'),
               'callsign_taken')


class TestSpawnBudget(unittest.TestCase):
    def setUp(self):
        self.state = armed()

    def test_spawn_spends_the_concrete_type_once_per_pilot(self):
        state = ok(self.state, spawn('spawn-r0-0', 'r0', 'xwing', 'spawn-r0-0'))
        self.assertEqual(state['players']['r0']['used_ship_ids'], ['xwing'])
        self.assertEqual(state['spawns']['spawn-r0-0']['ship_id'], 'xwing')
        refuse(state, spawn('spawn-r0-0b', 'r0', 'ywing', 'spawn-r0-0b'), 'spawn_busy')
        state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        refuse(state, spawn('spawn-r0-again', 'r0', 'xwing', 'spawn-r0-again'), 'ship_spent')
        state = ok(state, spawn('spawn-r0-1', 'r0', 'ywing', 'spawn-r0-1'))
        self.assertEqual(state['players']['r0']['used_ship_ids'], ['xwing', 'ywing'])
        self.assertEqual(state['players']['r0']['active_spawn_id'], 'spawn-r0-1')

    def test_foreign_and_unknown_ships_are_refused(self):
        refuse(self.state, spawn('s', 'r0', 'tie', 'spawn-x'), 'ship_not_in_team')
        refuse(self.state, spawn('s', 'r0', 'death-star', 'spawn-x'), 'ship_not_in_team')
        refuse(self.state, spawn('s', 'ghost', 'xwing', 'spawn-x'), 'unknown_player')

    def test_spawn_id_is_never_recycled_in_the_same_match(self):
        state = ok(self.state, spawn('shared-id', 'r0', 'xwing', 'shared-id'))
        refuse(state, spawn('s', 'e0', 'bomber', 'shared-id'), 'spawn_id_used')
        state = ok(state, kill('kill-1', 'shared-id', 'spawn-e0-0'))
        refuse(state, spawn('s', 'r1', 'ywing', 'shared-id'), 'spawn_id_used')
        self.assertEqual(state['players']['e0']['active_spawn_id'], 'spawn-e0-0')
        self.assertEqual(state['players']['r0']['used_ship_ids'], ['xwing'])
        self.assertEqual(state['players']['r0']['active_spawn_id'], None)

    def test_spawn_requires_a_running_round(self):
        refuse(joined(), spawn('s', 'r0', 'xwing', 'spawn-x'), 'not_running')


class TestCrash(unittest.TestCase):
    def test_crash_consumes_the_ship_without_a_point(self):
        state = airborne()
        state = ok(state, event('crash-1', 'crash', spawn_id='spawn-r0-0'))
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 0})
        closed = state['spawns']['spawn-r0-0']
        self.assertEqual((closed['status'], closed['closed_by'], closed['killer_spawn_id']),
                         ('closed', 'crash', None))
        self.assertIsNone(state['players']['r0']['active_spawn_id'])
        refuse(state, event('crash-2', 'crash', spawn_id='spawn-r0-0'), 'stale_spawn')
        refuse(state, event('crash-3', 'crash', spawn_id='nope'), 'unknown_spawn')
        refuse(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'), 'stale_spawn')
        refuse(state, spawn('spawn-r0-2', 'r0', 'xwing', 'spawn-r0-2'), 'ship_spent')
        state = ok(state, spawn('spawn-r0-1', 'r0', 'ywing', 'spawn-r0-1'))
        refuse(state, spawn('spawn-r0-3', 'r0', 'uwing', 'spawn-r0-3'), 'spawn_busy')


class TestDeathScoring(unittest.TestCase):
    def test_confirmed_kill_scores_once_for_the_enemy_team(self):
        state = ok(airborne(), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})
        self.assertEqual(state['spawns']['spawn-r0-0']['status'], 'closed')
        self.assertEqual(state['players']['e0']['active_spawn_id'], 'spawn-e0-0')
        self.assertEqual(state['players']['r0']['active_spawn_id'], None)
        refuse(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'), 'duplicate_event')
        refuse(state, kill('kill-2', 'spawn-r0-0', 'spawn-e0-0'), 'stale_spawn')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})

    def test_same_event_id_with_other_payload_conflicts(self):
        state = ok(airborne(), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        refuse(state, event('kill-1', 'tick', elapsed_seconds=1), 'event_id_conflict')
        refuse(state, kill('kill-1', 'spawn-e0-0', 'spawn-r0-0'), 'event_id_conflict')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})

    def test_friendly_self_stale_and_unknown_never_score(self):
        state = ok(airborne(), spawn('spawn-r1-0', 'r1', 'ywing', 'spawn-r1-0'))
        for victim, killer, reason in (('spawn-r1-0', 'spawn-r0-0', 'friendly_fire'),
                                       ('spawn-r0-0', 'spawn-r0-0', 'self_kill'),
                                       ('nope', 'spawn-e0-0', 'unknown_spawn'),
                                       ('spawn-r0-0', 'nope', 'unknown_spawn')):
            with self.subTest(victim=victim, killer=killer):
                refuse(state, kill('kill-x', victim, killer), reason)
        state = ok(state, event('crash-1', 'crash', spawn_id='spawn-e0-0'))
        refuse(state, kill('kill-9', 'spawn-r0-0', 'spawn-e0-0'), 'stale_spawn')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 0})

    def test_a_closed_spawn_id_stays_closed_after_a_respawn(self):
        state = ok(airborne(), kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        state = ok(state, spawn('spawn-r0-1', 'r0', 'ywing', 'spawn-r0-1'))
        refuse(state, kill('kill-2', 'spawn-r0-0', 'spawn-e0-0'), 'stale_spawn')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})

    def test_events_after_the_end_never_score(self):
        state = airborne(target_kills=2)
        state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        state = ok(state, spawn('spawn-r0-1', 'r0', 'ywing', 'spawn-r0-1'))
        state = ok(state, kill('kill-2', 'spawn-r0-1', 'spawn-e0-0'))
        self.assertEqual(state['scores']['empire'], 2)
        for data, reason in ((kill('kill-3', 'spawn-e0-0', 'spawn-r0-1'), 'match_ended'),
                             (event('crash-9', 'crash', spawn_id='spawn-e0-0'),
                              'match_ended')):
            refuse(state, data, reason)
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 2})


class TestShortMatch(unittest.TestCase):
    def test_two_kills_end_the_round_immediately(self):
        state = airborne(target_kills=2)
        state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        self.assertEqual(state['phase'], 'running')
        self.assertIsNone(state['result'])
        state = ok(state, spawn('spawn-r0-1', 'r0', 'ywing', 'spawn-r0-1'))
        state = ok(state, kill('kill-2', 'spawn-r0-1', 'spawn-e0-0'))
        self.assertEqual(state['phase'], 'ended')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 2})
        self.assertEqual(state['result'], {'reason': 'kill_target', 'winner': 'empire',
                                          'outcome': 'win', 'seq': state['seq']})
        for data, reason in ((event('tick-9', 'tick', elapsed_seconds=5), 'match_ended'),
                             (kill('kill-3', 'spawn-e0-0', 'spawn-r0-1'), 'match_ended'),
                             (spawn('spawn-e0-1', 'e0', 'bomber', 'spawn-e0-1'),
                              'match_ended'),
                             (event('re-9', 'reconnect', player_id='r0', callsign='RED900'),
                              'match_ended'),
                             (join('join-r9', 'r9', 'rebels', 'RED901'), 'match_ended')):
            refuse(state, data, reason)
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 2})
        self.assertEqual(state['seq'], len(state['history']))


class TestFifteenKillFixture(unittest.TestCase):
    """5 pilots x 3 types per side: exactly 14 then 15, never 16."""

    def trace(self):
        state = ok(joined(rebels=5, empire=5, target_kills=15), event('start', 'start'))
        killers = {}
        for i in range(5):
            pilot = 'e%d' % i
            state = ok(state, spawn('spawn-%s-0' % pilot, pilot, 'tie',
                                    'spawn-%s-0' % pilot))
            killers[pilot] = 'spawn-%s-0' % pilot
        result, kills = [(0, state)], 0
        for ship_index, ship in enumerate(REBEL_TYPES):
            for i in range(5):
                kills += 1
                pilot, victim = 'r%d' % i, 'spawn-r%d-%d' % (i, ship_index)
                state = ok(state, spawn('spawn-r%d-%d' % (i, ship_index), pilot, ship,
                                        victim))
                state = ok(state, kill('kill-%d' % kills, victim,
                                       killers['e%d' % (kills % 5)]))
                result.append((kills, state))
        return result

    def test_fifteen_confirmed_kills_end_it_with_exactly_15(self):
        trace = self.trace()
        self.assertEqual([kills for kills, _ in trace], list(range(16)))
        at_14 = trace[14][1]
        self.assertEqual(at_14['phase'], 'running')
        self.assertEqual(at_14['scores'], {'rebels': 0, 'empire': 14})
        self.assertIsNone(at_14['result'])
        final = trace[15][1]
        self.assertEqual(final['phase'], 'ended')
        self.assertEqual(final['scores'], {'rebels': 0, 'empire': 15})
        self.assertEqual(final['config']['target_kills'], 15)
        self.assertEqual(final['result']['reason'], 'kill_target')
        self.assertEqual(final['result']['winner'], 'empire')
        self.assertEqual(max(state['scores']['empire'] for _, state in trace), 15)

    def test_no_sixteenth_point_after_the_end(self):
        final = self.trace()[15][1]
        killer = final['players']['e0']['active_spawn_id']
        closed = sorted(sid for sid, s in final['spawns'].items() if s['status'] == 'closed')
        final = refuse(final, kill('extra-1', closed[0], killer), 'match_ended')
        final = refuse(final, event('extra-2', 'crash', spawn_id=killer), 'match_ended')
        final = refuse(final, event('extra-3', 'tick', elapsed_seconds=99), 'match_ended')
        self.assertEqual(final['scores'], {'rebels': 0, 'empire': 15})
        self.assertEqual(final['result']['reason'], 'kill_target')
        self.assertEqual(final['spawns'][killer]['status'], 'alive')


class TestTimeLimit(unittest.TestCase):
    def test_tick_is_authoritative_and_never_goes_backwards(self):
        state = airborne(time_limit_seconds=60)
        refuse(state, event('tick-0', 'tick', elapsed_seconds=-1), 'bad_elapsed_seconds')
        state = ok(state, event('tick-10', 'tick', elapsed_seconds=10))
        state = ok(state, event('tick-20', 'tick', elapsed_seconds=20))
        state = ok(state, event('tick-20b', 'tick', elapsed_seconds=20))
        refuse(state, event('tick-back', 'tick', elapsed_seconds=19), 'time_regress')
        self.assertEqual(state['clock']['elapsed_seconds'], 20)
        self.assertEqual(state['phase'], 'running')
        refuse(lobby(time_limit_seconds=60), event('tick-1', 'tick', elapsed_seconds=1),
               'not_running')

    def test_limit_ends_by_score_and_equal_scores_draw(self):
        state = ok(airborne(time_limit_seconds=60), kill('kill-1', 'spawn-r0-0',
                                                         'spawn-e0-0'))
        state = ok(state, event('tick-59', 'tick', elapsed_seconds=59))
        self.assertEqual(state['phase'], 'running')
        state = ok(state, event('tick-60', 'tick', elapsed_seconds=60))
        self.assertEqual(state['phase'], 'ended')
        self.assertEqual(state['result'], {'reason': 'time_limit', 'winner': 'empire',
                                          'outcome': 'win', 'seq': state['seq']})
        refuse(state, event('tick-61', 'tick', elapsed_seconds=61), 'match_ended')
        draw = ok(airborne(time_limit_seconds=30), event('tick-30', 'tick',
                                                         elapsed_seconds=30))
        self.assertEqual(draw['result']['reason'], 'time_limit')
        self.assertEqual(draw['result']['outcome'], 'draw')
        self.assertIsNone(draw['result']['winner'])
        self.assertEqual(draw['scores'], {'rebels': 0, 'empire': 0})


class TestExhaustion(unittest.TestCase):
    """rebels 2x1 type, empire 3x1 type, target 2: losses are scarce."""

    def small(self, **config):
        config.setdefault('target_kills', 2)   # 2 rebel losses are all that exists
        state = create_match('round-1', {'rebels': ['xwing'], 'empire': ['tie']}, **config)
        for pilot, team, call in (('r0', 'rebels', 'RED000'), ('r1', 'rebels', 'RED001'),
                                  ('e0', 'empire', 'EMP000'), ('e1', 'empire', 'EMP001'),
                                  ('e2', 'empire', 'EMP002')):
            state = ok(state, join('j-' + pilot, pilot, team, call))
        return ok(state, event('start', 'start'))

    def run_out_the_rebels(self, state):
        """One rebel killed, one crashed: no slot and no ship left, score 1-0."""
        state = ok(state, spawn('spawn-e0-0', 'e0', 'tie', 'spawn-e0-0'))
        state = ok(state, spawn('spawn-r0-0', 'r0', 'xwing', 'spawn-r0-0'))
        state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        state = ok(state, spawn('spawn-r1-0', 'r1', 'xwing', 'spawn-r1-0'))
        return ok(state, event('crash-1', 'crash', spawn_id='spawn-r1-0'))

    def test_no_fallback_by_default_so_the_round_just_waits(self):
        state = self.run_out_the_rebels(self.small())
        self.assertEqual(state['phase'], 'running')
        self.assertIsNone(state['result'])
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})
        refuse(state, spawn('spawn-r0-again', 'r0', 'xwing', 'spawn-r0-again'), 'ship_spent')

    def test_exhaustion_loss_is_a_separate_reason_code(self):
        state = self.run_out_the_rebels(self.small(exhaustion_enabled=True))
        self.assertEqual(state['phase'], 'ended')
        self.assertEqual(state['result']['reason'], 'exhaustion')
        self.assertEqual(state['result']['winner'], 'empire')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})

    def test_kill_target_wins_the_same_transition(self):
        state = create_match('round-1', {'rebels': ['xwing'], 'empire': ['tie']},
                             target_kills=1, exhaustion_enabled=True)
        state = ok(state, join('j-r0', 'r0', 'rebels', 'RED000'))
        state = ok(state, join('j-e0', 'e0', 'empire', 'EMP000'))
        state = ok(state, event('start', 'start'))
        state = ok(state, spawn('spawn-r0-0', 'r0', 'xwing', 'spawn-r0-0'))
        state = ok(state, spawn('spawn-e0-0', 'e0', 'tie', 'spawn-e0-0'))
        state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        self.assertEqual(state['result']['reason'], 'kill_target')
        self.assertEqual(state['result']['winner'], 'empire')

    def test_last_crash_of_a_side_ends_it_by_exhaustion(self):
        state = self.small(exhaustion_enabled=True)
        for index, pilot in enumerate(('e0', 'e1', 'e2')):
            sid = 'spawn-%s-0' % pilot
            state = ok(state, spawn(sid, pilot, 'tie', sid))
            state = ok(state, event('crash-%d' % index, 'crash', spawn_id=sid))
            self.assertEqual(state['phase'], 'running' if index < 2 else 'ended')
        self.assertEqual(state['result']['reason'], 'exhaustion')
        self.assertEqual(state['result']['winner'], 'rebels')

    def emptied(self, **scores):
        """SYNTHETIC state: both sides without slot or ship at the same moment.

        Honest transitions cannot produce it, because every death leaves its
        killer flying and both fall-backs are evaluated per event.  Only a
        future batch policy or a hand-restored ledger could reach it, so this
        fixture drives the private decision helper directly instead of the
        public fold, and no batch mode is invented for it.
        """
        state = self.small(exhaustion_enabled=True)
        state['scores'] = {'rebels': 1, 'empire': 1}
        state['scores'].update(scores)
        for pilot, ship in (('r0', 'xwing'), ('r1', 'xwing'), ('e0', 'tie'), ('e1', 'tie'),
                            ('e2', 'tie')):
            state['players'][pilot]['used_ship_ids'] = [ship]
        return state

    def test_synthetic_state_does_not_survive_a_round_trip(self):
        """The counters above are not real transitions: snapshot must refuse
        to describe them, so no verified DTO is ever handed out for them;
        restore_snapshot refuses the same state handed to it directly."""
        with self.assertRaises(MatchError):
            snapshot(self.emptied())
        with self.assertRaises(MatchError):
            restore_snapshot({'version': 1, 'state': self.emptied()})

    def test_simultaneous_exhaustion_compares_kill_points(self):
        state = self.emptied()
        self.assertTrue(core._end_by_exhaustion(state))
        self.assertEqual(state['phase'], 'ended')
        self.assertEqual(state['result']['reason'], 'exhaustion')
        self.assertIsNone(state['result']['winner'])
        self.assertEqual(state['result']['outcome'], 'draw')
        ahead = self.emptied(rebels=3)
        self.assertTrue(core._end_by_exhaustion(ahead))
        self.assertEqual(ahead['result']['winner'], 'rebels')
        self.assertEqual(ahead['result']['outcome'], 'win')


class TestSmallRosterNeedsAFallback(unittest.TestCase):
    """TEAM_PLAN: 4x4 with three types gives 12 losses, so 15 needs a fallback."""

    def roster(self, **config):
        state = lobby(**config)
        for i in range(4):
            state = ok(state, join('j-r%d' % i, 'r%d' % i, 'rebels', 'RED%03d' % i))
            state = ok(state, join('j-e%d' % i, 'e%d' % i, 'empire', 'EMP%03d' % i))
        return state

    def test_start_refuses_a_target_nobody_can_reach(self):
        refuse(self.roster(), event('start', 'start'), 'unreachable_target')
        refuse(self.roster(target_kills=13), event('start', 'start'), 'unreachable_target')
        ok(self.roster(target_kills=12), event('start', 'start'))
        ok(self.roster(exhaustion_enabled=True), event('start', 'start'))
        ok(self.roster(time_limit_seconds=1200), event('start', 'start'))

    def test_exhaustion_finishes_the_4x4x3_round_at_twelve_losses(self):
        state = ok(self.roster(exhaustion_enabled=True), event('start', 'start'))
        killers = {}
        for i in range(4):
            state = ok(state, spawn('spawn-e%d-0' % i, 'e%d' % i, 'tie', 'spawn-e%d-0' % i))
            killers[i] = 'spawn-e%d-0' % i
        kills = 0
        for ship_index, ship in enumerate(REBEL_TYPES):
            for i in range(4):
                kills += 1
                victim = 'spawn-r%d-%d' % (i, ship_index)
                state = ok(state, spawn(victim, 'r%d' % i, ship, victim))
                state = ok(state, kill('kill-%d' % kills, victim, killers[kills % 4]))
                self.assertEqual(state['phase'], 'running' if kills < 12 else 'ended')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 12})
        self.assertEqual(state['result']['reason'], 'exhaustion')
        self.assertEqual(state['result']['winner'], 'empire')
        self.assertLess(state['scores']['empire'], state['config']['target_kills'])

    def test_time_limit_also_finishes_a_round_that_cannot_reach_15(self):
        state = ok(self.roster(time_limit_seconds=30), event('start', 'start'))
        state = ok(state, spawn('spawn-e0-0', 'e0', 'tie', 'spawn-e0-0'))
        state = ok(state, spawn('spawn-r0-0', 'r0', 'xwing', 'spawn-r0-0'))
        state = ok(state, kill('kill-1', 'spawn-r0-0', 'spawn-e0-0'))
        state = ok(state, event('tick-30', 'tick', elapsed_seconds=30))
        self.assertEqual(state['result']['reason'], 'time_limit')
        self.assertEqual(state['result']['winner'], 'empire')


if __name__ == '__main__':
    unittest.main()
