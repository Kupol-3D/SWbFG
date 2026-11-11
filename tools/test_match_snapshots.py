#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Snapshot DTO tests for the TASK7 match core: round trip, replay, corruption.

Stdlib unittest only.  ``snapshot``/``restore_snapshot`` are in-memory JSON
DTOs: this file opens no file (``open`` is patched to raise), uses no socket,
clock or random source, and starts no FlightGear.  Everything goes through the
public API, so the tests show that a restored state is re-derived from its own
event history rather than trusted field by field, that snapshot itself only
hands out a DTO its own accepted events reproduce, and that a diverging
snapshot is refused instead of repaired.
"""
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from match_core import (MatchError, apply_event, create_match, restore_snapshot,  # noqa: E402
                        snapshot)
from match_core import core  # noqa: E402


def event(event_id, type_, **fields):
    data = {'version': 1, 'match_id': 'round-1', 'event_id': event_id, 'type': type_}
    data.update(fields)
    return data


def ok(state, event_data):
    result = apply_event(state, event_data)
    if not result.accepted:
        raise AssertionError('%s rejected: %s' % (event_data.get('event_id'), result.reason))
    return result.state


def refuse(state, event_data, reason):
    result = apply_event(state, event_data)
    assert not result.accepted, '%s was accepted' % event_data.get('event_id')
    assert result.reason == reason, '%s: %s != %s' % (event_data.get('event_id'),
                                                     result.reason, reason)
    return state


def catalog():
    return {'rebels': ['xwing', 'ywing', 'uwing'], 'empire': ['tie', 'bomber', 'reaper']}


def roster(**config):
    config.setdefault('target_kills', 5)
    state = create_match('round-1', catalog(), **config)
    for pilot, team, call in (('r0', 'rebels', 'RED000'), ('r1', 'rebels', 'RED001'),
                              ('e0', 'empire', 'EMP000'), ('e1', 'empire', 'EMP001')):
        state = ok(state, event('join-' + pilot, 'join', player_id=pilot, team_id=team,
                                callsign=call))
    return ok(state, event('start', 'start'))


def mid_match(**config):
    """Running round: one confirmed kill, one live ship per side, t=45."""
    state = roster(**config)
    state = ok(state, event('spawn-e0-0', 'spawn', player_id='e0', ship_id='tie',
                            spawn_id='spawn-e0-0'))
    state = ok(state, event('spawn-r0-0', 'spawn', player_id='r0', ship_id='xwing',
                            spawn_id='spawn-r0-0'))
    state = ok(state, event('kill-1', 'death', victim_spawn_id='spawn-r0-0',
                            killer_spawn_id='spawn-e0-0'))
    state = ok(state, event('spawn-r1-0', 'spawn', player_id='r1', ship_id='ywing',
                            spawn_id='spawn-r1-0'))
    return ok(state, event('tick-45', 'tick', elapsed_seconds=45))


def ended_match():
    """Round already ended by the kill target, so no further scoring is possible."""
    state = roster(target_kills=1)
    state = ok(state, event('spawn-e0-0', 'spawn', player_id='e0', ship_id='tie',
                            spawn_id='spawn-e0-0'))
    state = ok(state, event('spawn-r0-0', 'spawn', player_id='r0', ship_id='xwing',
                            spawn_id='spawn-r0-0'))
    return ok(state, event('kill-1', 'death', victim_spawn_id='spawn-r0-0',
                           killer_spawn_id='spawn-e0-0'))


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


def text(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def stored(state, event_id):
    """The event payload the ledger remembers for one accepted event id."""
    return next(entry['event'] for entry in state['history']
                if entry['event_id'] == event_id)


def entry_at(state, event_id):
    return next(entry for entry in state['history'] if entry['event_id'] == event_id)


class TestSnapshotRoundTrip(unittest.TestCase):
    def assert_round_trip(self, state):
        payload = snapshot(state)
        self.assertEqual(set(payload), {'version', 'state'})
        self.assertIs(payload['version'], 1)
        revived = restore_snapshot(json.loads(text(payload)))
        self.assertEqual(revived, state)
        self.assertEqual(restore_snapshot({'version': 1, 'state': state}), state)
        self.assertFalse(containers(payload, set()) & containers(state, set()))
        self.assertFalse(containers(revived, set()) & containers(payload, set()))
        return revived

    def test_lobby_round_trip(self):
        self.assert_round_trip(roster())

    def test_running_round_trip_keeps_lives_team_active_kills_time_and_ledger(self):
        state = mid_match()
        revived = self.assert_round_trip(state)
        self.assertEqual(revived['phase'], 'running')
        self.assertEqual(revived['scores'], {'rebels': 0, 'empire': 1})
        self.assertEqual(revived['clock'], {'elapsed_seconds': 45})
        self.assertEqual(revived['players']['r0'], {'player_id': 'r0', 'team_id': 'rebels',
                                                   'callsign': 'RED000',
                                                   'used_ship_ids': ['xwing'],
                                                   'active_spawn_id': None,
                                                   'joined_seq': 1})
        self.assertEqual(revived['players']['r1']['active_spawn_id'], 'spawn-r1-0')
        self.assertEqual(revived['spawns']['spawn-r0-0']['status'], 'closed')
        self.assertEqual(revived['spawns']['spawn-r0-0']['killer_spawn_id'], 'spawn-e0-0')
        self.assertEqual(revived['spawns']['spawn-e0-0']['status'], 'alive')
        self.assertEqual(revived['seq'], len(revived['history']))
        self.assertEqual(sorted(revived['event_ledger']),
                         sorted(entry['event_id'] for entry in revived['history']))

    def test_ended_round_trip_keeps_the_result(self):
        state = ended_match()
        revived = self.assert_round_trip(state)
        self.assertEqual(revived['phase'], 'ended')
        self.assertEqual(revived['result'], {'reason': 'kill_target', 'winner': 'empire',
                                            'outcome': 'win', 'seq': state['seq']})

    def test_restore_is_idempotent_and_does_not_renumber_history(self):
        once = self.assert_round_trip(mid_match())
        twice = restore_snapshot(snapshot(once))
        self.assertEqual(twice, once)
        self.assertEqual([entry['seq'] for entry in twice['history']],
                         [entry['seq'] for entry in mid_match()['history']])

    def test_restored_branch_continues_like_the_original(self):
        state = mid_match()
        revived = restore_snapshot(json.loads(text(snapshot(state))))
        next_event = event('spawn-r1-1', 'spawn', player_id='r1', ship_id='uwing',
                           spawn_id='spawn-r1-1')
        first, second = apply_event(state, next_event), apply_event(revived, next_event)
        self.assertEqual(first.reason, second.reason)
        self.assertEqual(first.state, second.state)

    def test_mutating_a_restored_branch_touches_nothing_else(self):
        state = ended_match()
        payload = snapshot(state)
        revived = restore_snapshot(payload)
        revived['result']['winner'] = 'rebels'
        revived['players']['r0']['used_ship_ids'].append('ghost')
        revived['history'].pop()
        self.assertEqual(state['result']['winner'], 'empire')
        self.assertEqual(payload['state']['result']['winner'], 'empire')
        self.assertEqual(payload['state']['players']['r0']['used_ship_ids'], ['xwing'])
        self.assertEqual(state['players']['r0']['used_ship_ids'], ['xwing'])
        self.assertEqual(len(payload['state']['history']), len(state['history']))

    def test_every_ending_reason_survives_the_round_trip(self):
        timed = ok(mid_match(time_limit_seconds=60),
                   event('tick-60', 'tick', elapsed_seconds=60))
        self.assertEqual(timed['result']['reason'], 'time_limit')
        drained = create_match('round-1', {'rebels': ['xwing'], 'empire': ['tie']},
                               target_kills=2, exhaustion_enabled=True)
        for pilot, team, call in (('r0', 'rebels', 'RED000'), ('e0', 'empire', 'EMP000')):
            drained = ok(drained, event('join-' + pilot, 'join', player_id=pilot,
                                        team_id=team, callsign=call))
        drained = ok(drained, event('start', 'start'))
        drained = ok(drained, event('spawn-e0-0', 'spawn', player_id='e0', ship_id='tie',
                                    spawn_id='spawn-e0-0'))
        drained = ok(drained, event('spawn-r0-0', 'spawn', player_id='r0', ship_id='xwing',
                                    spawn_id='spawn-r0-0'))
        drained = ok(drained, event('kill-1', 'death', victim_spawn_id='spawn-r0-0',
                                   killer_spawn_id='spawn-e0-0'))
        self.assertEqual(drained['result']['reason'], 'exhaustion')
        for state in (timed, drained):
            self.assertEqual(restore_snapshot(json.loads(text(snapshot(state)))), state)
            self.assertEqual(restore_snapshot(snapshot(state))['config'], state['config'])

    def test_snapshot_refuses_states_it_cannot_describe(self):
        for bad in (None, [], 'state', {'version': 1}, {'version': 1, 'state': {}},
                    dict(mid_match(), seq=True), dict(mid_match(), clock={'x': 1})):
            with self.subTest(state=repr(bad)[:40]), self.assertRaises(MatchError):
                snapshot(bad)

    def test_round_trip_needs_no_file_or_network(self):
        def banned(*args, **kwargs):
            raise AssertionError('snapshot attempted IO')
        with mock.patch('builtins.open', side_effect=banned), \
                mock.patch('os.urandom', side_effect=banned), \
                mock.patch('random.random', side_effect=banned), \
                mock.patch.object(socket, 'socket', side_effect=banned), \
                mock.patch.object(socket, 'getaddrinfo', side_effect=banned):
            revived = restore_snapshot(json.loads(text(snapshot(mid_match()))))
            revived = ok(revived, event('tick-50', 'tick', elapsed_seconds=50))
        self.assertEqual(revived['clock']['elapsed_seconds'], 50)


class TestSnapshotShape(unittest.TestCase):
    def test_dto_must_be_exactly_version_and_state(self):
        good = snapshot(mid_match())
        for bad in (None, [], 'snapshot', 1, {}, {'version': 1}, {'state': good['state']},
                    dict(good, checksum='x'), dict(good, version=1, extra=None)):
            with self.subTest(payload=repr(bad)[:40]), self.assertRaises(MatchError):
                restore_snapshot(bad)

    def test_snapshot_version_must_be_the_integer_one(self):
        state = snapshot(mid_match())['state']
        for version in (True, False, 1.0, '1', 2, None, 0):
            with self.subTest(version=repr(version)):
                with self.assertRaises(MatchError):
                    restore_snapshot({'version': version, 'state': state})

    def test_state_version_must_be_the_integer_one(self):
        for version in (True, False, 1.0, '1', 2, None):
            with self.subTest(version=repr(version)):
                with self.assertRaises(MatchError):
                    restore_snapshot({'version': 1,
                                      'state': dict(mid_match(), version=version)})

    def test_nan_and_infinity_never_restore(self):
        for field, value in ((('scores', 'rebels'), float('nan')),
                             (('scores', 'empire'), float('inf')),
                             (('clock', 'elapsed_seconds'), float('nan')),
                             (('seq',), float('nan'))):
            payload = json.loads(text(snapshot(mid_match())))
            target = payload['state']
            for name in field[:-1]:
                target = target[name]
            target[field[-1]] = value
            payload = json.loads(json.dumps(payload, allow_nan=True))
            with self.subTest(field='.'.join(field)), self.assertRaises(MatchError):
                restore_snapshot(payload)

    def test_unknown_fields_are_refused_at_every_level(self):
        marker = 'LEAKFIELD-9f2c'

        def inject(state):
            state[marker] = 'LEAKVALUE'
        for mutate in (
                inject,
                lambda state: state['players']['r0'].__setitem__(marker, 1),
                lambda state: state['spawns']['spawn-e0-0'].__setitem__(marker, 1),
                lambda state: state['history'][0].__setitem__(marker, 1),
                lambda state: state['history'][0]['event'].__setitem__(marker, 1),
                lambda state: state['config'].__setitem__(marker, 1),
                lambda state: state['clock'].__setitem__('elapsed', 1)):
            state = mid_match()
            mutate(state)
            with self.assertRaises(MatchError) as caught:
                restore_snapshot(snapshot(state))
            self.assertNotIn(marker, str(caught.exception))


class TestSnapshotProvesHistory(unittest.TestCase):
    """snapshot is the README's verified DTO: a state whose own events do not
    reproduce its counters is refused by snapshot itself, not only later by
    restore_snapshot.  Every forged state below is structurally valid, so
    only the replay-and-canonical check can catch it."""

    def forge(self, mutate, state=None):
        tampered = json.loads(text(snapshot(state if state is not None
                                               else mid_match())))['state']
        mutate(tampered)
        with self.assertRaises(MatchError):
            snapshot(tampered)
        with self.assertRaises(MatchError):
            restore_snapshot({'version': 1, 'state': tampered})

    def test_forged_score_never_leaves_snapshot(self):
        for delta in (1, -1):
            self.forge(lambda s, delta=delta: s['scores'].update(
                empire=s['scores']['empire'] + delta))

    def test_forged_spent_types_never_leave_snapshot(self):
        self.forge(lambda s: [p.update(used_ship_ids=[])
                              for p in s['players'].values()])
        self.forge(lambda s: s['players']['e0'].update(used_ship_ids=['bomber']))

    def test_dropped_history_entry_with_fixed_counters_never_leaves_snapshot(self):
        def drop_last_tick(state):
            entry = state['history'].pop()
            state['event_ledger'].pop(entry['event_id'])
            state['seq'] = len(state['history'])
        self.forge(drop_last_tick)   # structurally consistent: clock still says 45

    def test_well_formed_wrong_fingerprint_never_leaves_snapshot(self):
        self.forge(lambda s: entry_at(s, 'kill-1').update(fingerprint='0' * 64))

    def test_history_payload_with_recomputed_fingerprint_never_leaves_snapshot(self):
        def shift_tick(state):
            entry = entry_at(state, 'tick-45')
            entry['event']['elapsed_seconds'] = 40
            entry['fingerprint'] = core._fingerprint(entry['event'])
        self.forge(shift_tick)   # digest fits again, but the rebuild stops at t=40

    def test_extra_history_entry_with_valid_fingerprint_never_leaves_snapshot(self):
        def forge_kill(state):
            forged = event('forged-kill', 'death', victim_spawn_id='spawn-r0-0',
                           killer_spawn_id='spawn-e0-0')
            digest = core._fingerprint(forged)
            state['history'].append({'seq': len(state['history']) + 1,
                                     'event_id': 'forged-kill', 'type': 'death',
                                     'fingerprint': digest, 'event': forged})
            state['event_ledger']['forged-kill'] = digest
            state['seq'] += 1
        self.forge(forge_kill)   # the replayed kill hits an already closed spawn

    def test_forged_result_of_an_ended_round_never_leaves_snapshot(self):
        self.forge(lambda s: s['result'].update(winner='rebels'), state=ended_match())


class TestRestoreReusesTheRules(unittest.TestCase):
    def setUp(self):
        self.state = mid_match()
        self.restored = restore_snapshot(snapshot(self.state))

    def test_reconnect_after_restore_does_not_return_spent_ships(self):
        state = ok(self.restored, event('re-1', 'reconnect', player_id='r0',
                                        callsign='RED777'))
        self.assertEqual(state['players']['r0']['used_ship_ids'], ['xwing'])
        refuse(state, event('spawn-r0-x', 'spawn', player_id='r0', ship_id='xwing',
                            spawn_id='spawn-r0-x'), 'ship_spent')
        state = ok(state, event('spawn-r0-y', 'spawn', player_id='r0', ship_id='ywing',
                                spawn_id='spawn-r0-y'))
        refuse(state, event('spawn-r0-z', 'spawn', player_id='r0', ship_id='uwing',
                            spawn_id='spawn-r0-z'), 'spawn_busy')
        state = ok(state, event('crash-r1', 'crash', spawn_id='spawn-r1-0'))
        state = ok(state, event('spawn-r1-1', 'spawn', player_id='r1', ship_id='uwing',
                                spawn_id='spawn-r1-1'))
        state = ok(state, event('crash-r1-1', 'crash', spawn_id='spawn-r1-1'))
        refuse(state, event('spawn-r1-2', 'spawn', player_id='r1', ship_id='uwing',
                            spawn_id='spawn-r1-2'), 'ship_spent')
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})

    def test_next_unused_ship_spawns_once_after_restore(self):
        state = ok(self.restored, event('crash-e0', 'crash', spawn_id='spawn-e0-0'))
        state = ok(state, event('spawn-e0-1', 'spawn', player_id='e0', ship_id='bomber',
                                spawn_id='spawn-e0-1'))
        self.assertEqual(state['players']['e0']['used_ship_ids'], ['tie', 'bomber'])
        refuse(state, event('spawn-e0-2', 'spawn', player_id='e0', ship_id='bomber',
                            spawn_id='spawn-e0-2'), 'spawn_busy')
        state = ok(state, event('crash-e0-1', 'crash', spawn_id='spawn-e0-1'))
        refuse(state, event('spawn-e0-3', 'spawn', player_id='e0', ship_id='bomber',
                            spawn_id='spawn-e0-3'), 'ship_spent')
        refuse(state, event('spawn-e0-4', 'spawn', player_id='e0', ship_id='reaper',
                            spawn_id='spawn-e0-1'), 'spawn_id_used')

    def test_the_ledger_still_remembers_every_accepted_event(self):
        original = stored(self.restored, 'kill-1')      # the accepted confirmed kill
        refuse(self.restored, original, 'duplicate_event')
        refuse(self.restored, dict(original, victim_spawn_id='spawn-r1-0'),
               'event_id_conflict')
        refuse(self.restored, event('kill-new-id', 'death', victim_spawn_id='spawn-r0-0',
                                   killer_spawn_id='spawn-e0-0'), 'stale_spawn')
        self.assertEqual(self.restored['scores'], {'rebels': 0, 'empire': 1})
        self.assertEqual(self.restored['seq'], len(self.restored['history']))
        self.assertEqual(restore_snapshot(snapshot(self.restored)), self.restored)

    def test_ended_state_after_restore_never_gives_a_sixteenth_or_first_extra_point(self):
        state = restore_snapshot(snapshot(ended_match()))
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})
        for data, reason in (
                (event('later-kill', 'death', victim_spawn_id='spawn-r0-0',
                       killer_spawn_id='spawn-e0-0'), 'match_ended'),
                (event('later-spawn', 'spawn', player_id='e0', ship_id='bomber',
                       spawn_id='spawn-e0-1'), 'match_ended'),
                (event('later-tick', 'tick', elapsed_seconds=90), 'match_ended'),
                (event('later-join', 'join', player_id='r9', team_id='rebels',
                       callsign='RED999'), 'match_ended')):
            state = refuse(state, data, reason)
        self.assertEqual(state['scores'], {'rebels': 0, 'empire': 1})
        self.assertEqual(state['result']['reason'], 'kill_target')


class TestRestoreRefusesCorruption(unittest.TestCase):
    """Every case is a tampered field: refuse, never repair, never echo input."""

    def tamper(self, mutate, state=None):
        payload = json.loads(text(snapshot(state if state is not None else mid_match())))
        mutate(payload['state'])
        return payload

    def assert_refused(self, mutate, state=None):
        payload = self.tamper(mutate, state)
        frozen = text(payload)
        with self.assertRaises(MatchError):
            restore_snapshot(payload)
        self.assertEqual(text(payload), frozen, 'restore must not modify a payload')

    # --- budget and life bookkeeping ---------------------------------------
    def test_used_types_reset(self):
        self.assert_refused(lambda s: [p.update({'used_ship_ids': []})
                                       for p in s['players'].values()])

    def test_used_type_duplicate_claimed(self):
        self.assert_refused(lambda s: s['players']['e0'].update(used_ship_ids=['tie', 'tie']))

    def test_active_ship_freed(self):
        self.assert_refused(lambda s: s['players']['r1'].update(active_spawn_id=None))

    def test_extra_active_orphan_ship(self):
        def inject(state):
            state['spawns']['spawn-orphan'] = {
                'spawn_id': 'spawn-orphan', 'player_id': 'e1', 'ship_id': 'bomber',
                'team_id': 'empire', 'status': 'alive', 'opened_seq': 9,
                'closed_seq': None, 'closed_by': None, 'killer_spawn_id': None}
            state['players']['e1']['active_spawn_id'] = 'spawn-orphan'
        self.assert_refused(inject)

    def test_active_ship_claimed_from_another_pilot(self):
        self.assert_refused(lambda s: s['players']['r0'].update(active_spawn_id='spawn-r1-0'))

    def test_closed_ship_reopened_consistently(self):
        """Shape-clean forgery: only the replay can notice the ship never flew again."""
        def reopen(state):
            spawn = state['spawns']['spawn-r0-0']
            spawn.update(status='alive', closed_seq=None, closed_by=None,
                         killer_spawn_id=None)
            state['players']['r0']['active_spawn_id'] = 'spawn-r0-0'
        self.assert_refused(reopen)

    def test_spent_ship_claimed_as_a_different_type(self):
        self.assert_refused(lambda s: s['spawns']['spawn-r0-0'].update(ship_id='ywing'))

    def test_spawn_of_a_pilot_on_the_other_side(self):
        self.assert_refused(lambda s: s['spawns']['spawn-r0-0'].update(team_id='empire'))

    def test_history_entry_removed(self):
        self.assert_refused(lambda s: s['history'].pop(2))

    # --- counters and result ------------------------------------------------
    def test_score_counter_changed(self):
        for delta in (1, -1):
            self.assert_refused(lambda s, delta=delta: s['scores'].update(
                empire=s['scores']['empire'] + delta))

    def test_clock_changed(self):
        for seconds in (0, 10, 9999):
            self.assert_refused(lambda s, seconds=seconds: s['clock'].update(
                elapsed_seconds=seconds))

    def test_phase_flipped(self):
        self.assert_refused(lambda s: s.update(phase='lobby'))
        self.assert_refused(lambda s: s.update(phase='ended'), state=None)

    def test_result_tampering(self):
        ended = ended_match()
        self.assert_refused(lambda s: s.update(result=None), state=ended)
        for field, value in (('reason', 'time_limit'), ('winner', 'rebels'),
                             ('outcome', 'draw'), ('seq', 1)):
            self.assert_refused(lambda s, field=field, value=value: s['result'].update(
                {field: value}), state=ended)

    def test_result_added_to_a_running_round(self):
        self.assert_refused(lambda s: s.update(
            result={'reason': 'kill_target', 'winner': 'empire', 'outcome': 'win',
                    'seq': s['seq']}))

    def test_seq_counter_changed(self):
        self.assert_refused(lambda s: s.update(seq=s['seq'] + 1))

    # --- history and ledger --------------------------------------------------
    def test_history_sequence_numbers(self):
        self.assert_refused(lambda s: s['history'][1].update(seq=42))
        self.assert_refused(lambda s: s['history'].reverse())

    def test_history_identity_and_metadata(self):
        self.assert_refused(lambda s: entry_at(s, 'kill-1').update(event_id='kill-999'))
        self.assert_refused(lambda s: entry_at(s, 'kill-1').update(type='crash'))
        self.assert_refused(lambda s: entry_at(s, 'kill-1').update(fingerprint='0' * 64))
        self.assert_refused(lambda s: entry_at(s, 'kill-1').pop('event'))

    def test_history_payload_changed(self):
        self.assert_refused(lambda s: s['history'][0]['event'].update(callsign='RED555'))
        self.assert_refused(lambda s: entry_at(s, 'kill-1')['event'].update(
            killer_spawn_id='spawn-r1-0'))

    def test_history_entry_duplicated(self):
        def duplicate(state):
            clone = dict(entry_at(state, 'kill-1'))
            clone['seq'] = len(state['history']) + 1
            state['history'].append(clone)
        self.assert_refused(duplicate)

    def test_ledger_tampering(self):
        def drop(state):
            state['event_ledger'].pop(state['history'][-1]['event_id'])

        def forge(state):
            state['event_ledger']['ghost-event'] = '0' * 64

        def swap(state):
            state['event_ledger'][state['history'][2]['event_id']] = '1' * 64
        for mutate in (drop, forge, swap):
            with self.subTest(case=mutate.__name__):
                self.assert_refused(mutate)

    # --- config and identity -------------------------------------------------
    def test_config_type_forgery(self):
        for field, value in (('target_kills', True), ('target_kills', '5'),
                             ('exhaustion_enabled', 1), ('time_limit_seconds', '60')):
            self.assert_refused(lambda s, field=field, value=value: s['config'].update(
                {field: value}))

    def test_config_lowering_the_target_cannot_retroactively_end_a_round(self):
        self.assert_refused(lambda s: s['config'].update(target_kills=1))

    def test_partial_match_id_rebrand_is_refused(self):
        self.assert_refused(lambda s: s.update(match_id='round-2'))

    def test_lobby_state_carrying_effects_is_refused(self):
        lobby = roster()
        self.assert_refused(lambda s: s.update(scores={'rebels': 0, 'empire': 2}),
                            state=lobby)
        self.assert_refused(lambda s: s.update(history=s['history'][:4] + [
            {'seq': 5, 'event_id': 'forged-kill', 'type': 'death', 'fingerprint': '0' * 64,
             'event': event('forged-kill', 'death', victim_spawn_id='spawn-r0-0',
                            killer_spawn_id='spawn-e0-0')}], seq=5), state=lobby)

    # --- trust boundary, stated rather than tested as protection -------------
    def test_consistency_is_the_only_criterion_no_provenance_is_checked(self):
        """The documented boundary: no secrets and no signatures are involved.

        A state that somebody folded locally - not the authoritative adapter -
        restores exactly like a stored snapshot, because internal consistency
        is all this library can look at; nothing binds a snapshot to a real
        session.  Editing a stored history in place is still caught: every
        fingerprint covers the whole event payload, so a forged or rebranded
        event no longer matches its own digest.
        """
        locally = ok(mid_match(), event('tick-50', 'tick', elapsed_seconds=50))
        revived = restore_snapshot({'version': 1, 'state': locally})
        self.assertEqual(revived['clock']['elapsed_seconds'], 50)
        self.assertFalse(revived is locally)
        self.assert_refused(lambda s: s['history'].append({
            'seq': len(s['history']) + 1, 'event_id': 'forged-tick', 'type': 'tick',
            'fingerprint': '0' * 64,
            'event': event('forged-tick', 'tick', elapsed_seconds=9999)}))
        self.assert_refused(lambda s: (s.update(match_id='round-2'),
                                       [entry['event'].update(match_id='round-2')
                                        for entry in s['history']]))

    def test_error_messages_never_contain_input_data(self):
        marker = 'LEAK-MARKER-9f2c'
        cases = []

        def unknown_top_level(payload):
            payload[marker] = marker
            return payload

        def unknown_state_field(payload):
            payload['state'][marker] = marker
            return payload

        def rebranded_match(payload):
            payload['state'].update(match_id=marker)
            return payload

        def forged_spawn(payload):
            payload['state']['spawns'][marker] = 'anything'
            return payload

        def long_callsign(payload):
            payload['state']['players']['r0'].update(callsign=marker)
            return payload

        def bad_fingerprint(payload):
            payload['state']['event_ledger']['x'] = marker
            return payload
        for build in (unknown_top_level, unknown_state_field, rebranded_match,
                      forged_spawn, long_callsign, bad_fingerprint):
            payload = build(json.loads(text(snapshot(mid_match()))))
            with self.subTest(case=build.__name__):
                with self.assertRaises(MatchError) as caught:
                    restore_snapshot(payload)
                cases.append(str(caught.exception))
        for state_case in (lambda s: s['players'].update({marker: {}}),
                           lambda s: s['event_ledger'].update({marker: marker}),
                           lambda s: s['spawns'].update({marker: marker})):
            state = mid_match()
            state_case(state)
            with self.subTest(state_case=state_case.__name__):
                with self.assertRaises(MatchError) as caught:
                    snapshot(state)
                cases.append(str(caught.exception))
        with self.assertRaises(MatchError) as caught:
            restore_snapshot([marker, marker])
        cases.append(str(caught.exception))
        for message in cases:
            self.assertTrue(message)
            self.assertNotIn(marker, message)


if __name__ == '__main__':
    unittest.main()
