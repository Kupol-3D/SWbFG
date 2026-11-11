# SPDX-License-Identifier: GPL-2.0-or-later
"""Deterministic offline match rules of Space Arena (TASK7).

Three public entry points: ``create_match(...)`` builds a JSON-compatible
state, ``apply_event(state, event)`` folds one trusted, already-ordered event
into a new state, and ``snapshot(state)`` / ``restore_snapshot(payload)``
convert a state to and from the in-memory JSON DTO
``{'version': 1, 'state': {...}}``.  The module imports ``copy``, ``hashlib``,
``json``, ``re`` and ``dataclasses`` and nothing else: no socket, no clock, no
random, no file, no FG, no thread, so the same config plus the same event
order always produces the same state (match_core/CONTRACT.md).

Restore is a replay, not a load: it rebuilds the match from ``config``,
``catalog``, ``match_id`` and the ordered accepted-event history, refuses any
entry that is not reproducible, and then requires the canonical JSON of the
rebuilt state to equal the stored one.  Spent types, live/closed spawns,
scores, result, clock and the dedup ledger are therefore never trusted as
fields; they are recomputed.  Nothing is repaired on divergence and no second
copy of a ledger is kept, so a state cannot be "fixed" by editing counters.
``snapshot`` runs the same proof through ``restore_snapshot`` before handing
out a DTO, so a verified snapshot cannot describe a state that would not
survive a restore.

Trust boundary: the reducer input is a trusted authoritative event stream and
a snapshot is only accepted from trusted authoritative storage.  This library
holds no secrets and verifies no signature, so it checks structure and
internal consistency, not provenance: a history that was forged as a whole,
consistently, is outside what it can detect.  A well-formed event is NOT proof
that its sender owned ``player_id`` or that a kill really happened; binding
requests to authenticated identity and proving destruction are separate
integration tasks.  Field validation prevents accidental corruption, nothing
more, and this code has not been exercised against a running FlightGear.

v1 limits, deliberately conservative rather than invented rules:
  * no posthumous kill: ``death`` needs the killer spawn still alive, so a
    shot in flight can never score after its shooter has died;
  * target 15 is never lowered automatically.  With no fallback configured,
    ``start`` refuses a target above the smallest initial team budget
    (``unreachable_target``), and even after that check a large roster without
    an explicit fallback can still fail to reach 15 once crashes consume
    ships;
  * simultaneous events are not batched: every fall back is evaluated per
    event, so exhaustion of both sides in one step cannot happen here.
"""

import copy
import hashlib
import json
import re
from dataclasses import dataclass

STATE_VERSION = 1
EVENT_VERSION = 1
SNAPSHOT_VERSION = 1
TEAMS = ('rebels', 'empire')
PHASES = ('lobby', 'running', 'ended')

MAX_ID_LEN = 64
CALLSIGN_MAX_LEN = 6            # project policy, same rule as gateway/mp_protocol
RESERVED_CALLSIGN_PREFIX = 'obs'

_ID_RE = re.compile(r'\A[!-~]{1,%d}\Z' % MAX_ID_LEN)   # printable, no spaces
_CALLSIGN_RE = re.compile(r'\A[A-Za-z0-9]{1,%d}\Z' % CALLSIGN_MAX_LEN)
_HEX_RE = re.compile(r'\A[0-9a-f]{64}\Z')              # sha256 hex digest

STATE_FIELDS = frozenset(('version', 'match_id', 'phase', 'config', 'catalog', 'players',
                          'spawns', 'scores', 'clock', 'seq', 'history', 'event_ledger',
                          'result'))
CONFIG_FIELDS = frozenset(('target_kills', 'exhaustion_enabled', 'time_limit_seconds'))
PLAYER_FIELDS = frozenset(('player_id', 'team_id', 'callsign', 'used_ship_ids',
                           'active_spawn_id', 'joined_seq'))
SPAWN_FIELDS = frozenset(('spawn_id', 'player_id', 'ship_id', 'team_id', 'status',
                          'opened_seq', 'closed_seq', 'closed_by', 'killer_spawn_id'))
HISTORY_FIELDS = frozenset(('seq', 'event_id', 'type', 'fingerprint', 'event'))
RESULT_FIELDS = frozenset(('reason', 'winner', 'outcome', 'seq'))
END_REASONS = frozenset(('kill_target', 'time_limit', 'exhaustion'))
STATUSES = ('alive', 'closed')
CLOSE_CAUSES = ('death', 'crash')
_HASH_LEN = 64


class MatchError(ValueError):
    """Invalid match config or state: the caller cannot represent the request.

    Rejected *events* are never errors: ``apply_event`` reports them as
    ``accepted=False`` with a reason code and an unchanged state.
    """


@dataclass(frozen=True)
class Transition:
    """Result of one fold.  ``state`` never aliases the passed-in state."""
    state: dict
    accepted: bool
    reason: str


class _Bad(Exception):
    """Internal: a value failed validation and carries its reason code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _id_ok(value):
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _hash_ok(value):
    return isinstance(value, str) and bool(_HEX_RE.match(value))


def _callsign_ok(value):
    return (isinstance(value, str) and bool(_CALLSIGN_RE.match(value))
            and not value.lower().startswith(RESERVED_CALLSIGN_PREFIX))


def _pos_int_ok(value):
    return _is_int(value) and value >= 1


def _id(value):
    if not _id_ok(value):
        raise _Bad('id')
    return value


def _team(value):
    if value not in TEAMS:
        raise _Bad('team')
    return value


def _callsign(value):
    if not _callsign_ok(value):
        raise _Bad('callsign')
    return value


def _non_neg_int(value):
    if not _is_int(value) or value < 0:
        raise _Bad('elapsed')
    return value


def _catalog(value):
    """Two team lists of concrete, unique ship types; nothing is guessed."""
    if not isinstance(value, dict) or set(value) != set(TEAMS):
        raise _Bad('catalog must be an object with exactly the keys rebels and empire')
    out = {}
    for team in TEAMS:
        ships = value[team]
        if isinstance(ships, (str, bytes)) or not isinstance(ships, (list, tuple)):
            raise _Bad('catalog.%s must be a list of ship type ids' % team)
        ids = [_id(s) for s in ships]
        if not ids:
            raise _Bad('catalog.%s must not be empty' % team)
        if len(set(ids)) != len(ids):
            raise _Bad('catalog.%s ship type ids must be unique' % team)
        out[team] = ids
    return out


def _check_config(config):
    """Validate a config object: exact keys, exact types, no implicit coercion."""
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise _Bad('config must have exactly target_kills, exhaustion_enabled and '
                   'time_limit_seconds')
    if not _pos_int_ok(config['target_kills']):
        raise _Bad('target_kills must be a positive integer')
    if not isinstance(config['exhaustion_enabled'], bool):
        raise _Bad('exhaustion_enabled must be a boolean')
    limit = config['time_limit_seconds']
    if limit is not None and not _pos_int_ok(limit):
        raise _Bad('time_limit_seconds must be a positive integer or None')


def create_match(match_id, catalog, *, target_kills=15, exhaustion_enabled=False,
                 time_limit_seconds=None):
    """Build the lobby state of one round.  Pure: the inputs are only read.

    ``target_kills=15``/``exhaustion_enabled=False``/``time_limit_seconds=None``
    are the plan's defaults, not a fixed rule: the fallbacks stay opt-in so
    nothing is ever reported as an agreed rule that nobody agreed on.
    """
    config = {'target_kills': target_kills, 'exhaustion_enabled': exhaustion_enabled,
              'time_limit_seconds': time_limit_seconds}
    try:
        mid = _id(match_id)
        cat = _catalog(catalog)
        _check_config(config)
    except _Bad as exc:
        raise MatchError(str(exc)) from None
    return {
        'version': STATE_VERSION,
        'match_id': mid,
        'phase': 'lobby',
        'config': dict(config),
        'catalog': copy.deepcopy(cat),
        'players': {},
        'spawns': {},
        'scores': {team: 0 for team in TEAMS},
        'clock': {'elapsed_seconds': 0},
        'seq': 0,
        'history': [],       # ordered accepted events: {seq, event_id, type, fingerprint}
        'event_ledger': {},  # event_id -> fingerprint of the accepted payload
        'result': None,
    }


# --- event shape -----------------------------------------------------------

_COMMON = ('version', 'match_id', 'event_id', 'type')
_SPEC = {
    'join': (('player_id', _id, 'bad_player_id'),
             ('team_id', _team, 'bad_team_id'),
             ('callsign', _callsign, 'bad_callsign')),
    'start': (),
    'reconnect': (('player_id', _id, 'bad_player_id'),
                  ('callsign', _callsign, 'bad_callsign')),
    'spawn': (('player_id', _id, 'bad_player_id'),
              ('ship_id', _id, 'bad_ship_id'),
              ('spawn_id', _id, 'bad_spawn_id')),
    'death': (('victim_spawn_id', _id, 'bad_spawn_id'),
              ('killer_spawn_id', _id, 'bad_spawn_id')),
    'crash': (('spawn_id', _id, 'bad_spawn_id'),),
    'tick': (('elapsed_seconds', _non_neg_int, 'bad_elapsed_seconds'),),
}


def _validate(event, state):
    """Return ``(reason, normalized_event)``; reason is None when usable.

    No coercion: booleans are not integers, strings are not numbers, absent
    fields are not defaulted and unknown fields are refused, so a typo can
    never silently become a rule.
    """
    if not isinstance(event, dict):
        return 'malformed_event', None
    keys = set(event)
    if not set(_COMMON) <= keys:
        return 'missing_field', None
    if not _is_int(event['version']) or event['version'] != EVENT_VERSION:
        return 'bad_version', None
    kind = event['type']
    if not isinstance(kind, str) or kind not in _SPEC:
        return 'unknown_type', None
    try:
        match_id = _id(event['match_id'])
    except _Bad:
        return 'bad_match_id', None
    try:
        event_id = _id(event['event_id'])
    except _Bad:
        return 'bad_event_id', None
    if match_id != state['match_id']:
        return 'wrong_match', None
    allowed = set(_COMMON) | {name for name, _, _ in _SPEC[kind]}
    if keys - allowed:
        return 'unknown_field', None
    out = {'version': EVENT_VERSION, 'match_id': match_id, 'event_id': event_id,
           'type': kind}
    for name, check, reason in _SPEC[kind]:
        if name not in event:
            return 'missing_field', None
        try:
            out[name] = check(event[name])
        except _Bad:
            return reason, None
    return None, out


def _fingerprint(event):
    """Canonical payload hash of a validated event (deterministic, no salt)."""
    canonical = json.dumps(event, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _has_fields(value, fields):
    """True when value is an object whose keys are exactly `fields`."""
    return (isinstance(value, dict) and all(isinstance(key, str) for key in value)
            and frozenset(value) == frozenset(fields))


def _used_ships_ok(used, types):
    """List of unique catalog ids; element types are checked before hashing."""
    if not isinstance(used, list) or not all(_id_ok(ship) for ship in used):
        return False
    return len(used) == len(set(used)) and all(ship in types for ship in used)


def _check_players(players, catalog):
    """Structure of the roster; team lock and spent types stay visible per pilot."""
    if not isinstance(players, dict):
        raise MatchError('state players must be an object')
    for pid, player in players.items():
        if (not _id_ok(pid) or not _has_fields(player, PLAYER_FIELDS)
                or player['player_id'] != pid or player['team_id'] not in TEAMS
                or not _callsign_ok(player['callsign'])
                or not _pos_int_ok(player['joined_seq'])
                or not (player['active_spawn_id'] is None
                        or _id_ok(player['active_spawn_id']))
                or not _used_ships_ok(player['used_ship_ids'], catalog[player['team_id']])):
            raise MatchError('state player record is invalid')
    return players


def _check_spawns(spawns, catalog, players):
    """Structure of every appearance, including the bookkeeping of closed ones."""
    if not isinstance(spawns, dict):
        raise MatchError('state spawns must be an object')
    for sid, spawn in spawns.items():
        if not _id_ok(sid) or not _has_fields(spawn, SPAWN_FIELDS):
            raise MatchError('state spawn record is invalid')
        alive = spawn['status'] == 'alive'
        closed_by = spawn['closed_by']
        killer = spawn['killer_spawn_id']
        if (not _id_ok(spawn['spawn_id']) or spawn['spawn_id'] != sid
                or not _id_ok(spawn['player_id']) or spawn['player_id'] not in players
                or spawn['team_id'] not in TEAMS
                or players[spawn['player_id']]['team_id'] != spawn['team_id']
                or spawn['ship_id'] not in catalog[spawn['team_id']]
                or spawn['status'] not in STATUSES
                or not _pos_int_ok(spawn['opened_seq'])
                or alive != (spawn['closed_seq'] is None)
                or alive != (closed_by is None)
                or (closed_by is not None and closed_by not in CLOSE_CAUSES)
                or (spawn['closed_seq'] is not None and not _pos_int_ok(spawn['closed_seq']))
                or ((closed_by == 'death') != (killer is not None))
                or (killer is not None and (not _id_ok(killer) or killer == sid
                                            or killer not in spawns))):
            raise MatchError('state spawn record is invalid')
    for sid, spawn in spawns.items():
        flying = players[spawn['player_id']]['active_spawn_id']
        if spawn['status'] == 'alive' and flying != sid:
            raise MatchError('state has an alive spawn that no pilot is flying')
    for pid, player in players.items():
        active = player['active_spawn_id']
        if (active is not None and (active not in spawns
                                    or spawns[active]['player_id'] != pid
                                    or spawns[active]['status'] != 'alive')):
            raise MatchError('state active_spawn_id is inconsistent')
    return spawns


def _check_history(history, ledger, seq):
    """Ordered accepted events plus the dedup ledger that must describe them."""
    if not isinstance(history, list) or not isinstance(ledger, dict):
        raise MatchError('state history must be a list and event_ledger an object')
    event_ids = []
    for index, entry in enumerate(history):
        if (not _has_fields(entry, HISTORY_FIELDS) or not _is_int(entry['seq'])
                or entry['seq'] != index + 1
                or not _id_ok(entry['event_id']) or not _hash_ok(entry['fingerprint'])
                or not isinstance(entry['event'], dict)
                or entry['event'].get('event_id') != entry['event_id']
                or entry['event'].get('type') != entry['type']):
            raise MatchError('state history entry is invalid')
        event_ids.append(entry['event_id'])
    if seq != len(history) or len(event_ids) != len(set(event_ids)):
        raise MatchError('state seq does not match its history')
    if (not all(_id_ok(name) and _hash_ok(digest) for name, digest in ledger.items())
            or sorted(ledger) != sorted(event_ids)):
        raise MatchError('state event_ledger does not match its history')


def _check_result(result, phase, seq):
    if result is None:
        if phase == 'ended':
            raise MatchError('an ended state must carry its result')
        return
    if (not _has_fields(result, RESULT_FIELDS)
            or not isinstance(result['reason'], str) or result['reason'] not in END_REASONS
            or result['outcome'] not in ('win', 'draw')
            or result['winner'] not in TEAMS + (None,)
            or not _pos_int_ok(result['seq']) or result['seq'] > seq
            or (result['outcome'] == 'draw') != (result['winner'] is None)
            or phase != 'ended'):
        raise MatchError('state result is invalid')


def _check_state(state):
    """Fast structural guard for every state handed to the public API.

    It refuses bool-as-int, negative or float counters, missing and unknown
    fields, wrong types and dangling references without letting a KeyError or
    TypeError escape, and no message ever contains input data.  Cross-field
    truth - scores, spent types, spawn bookkeeping, ledger contents - is not
    re-derived here; only ``snapshot`` and ``restore_snapshot`` prove it by
    replaying.  ``apply_event`` stays on this fast structural guard.
    """
    if not _has_fields(state, STATE_FIELDS):
        raise MatchError('state must be an object with exactly the v1 state fields')
    if not _is_int(state['version']) or state['version'] != STATE_VERSION:
        raise MatchError('state version must be the integer 1')
    if not _id_ok(state['match_id']) or state['phase'] not in PHASES:
        raise MatchError('state match_id or phase is invalid')
    try:
        _check_config(state['config'])
        catalog = _catalog(state['catalog'])
    except _Bad:
        raise MatchError('state config or catalog is invalid') from None
    scores, clock = state['scores'], state['clock']
    if (not _has_fields(scores, TEAMS)
            or not all(_is_int(scores[team]) and scores[team] >= 0 for team in TEAMS)):
        raise MatchError('state scores are invalid')
    if (not _has_fields(clock, ('elapsed_seconds',))
            or not _is_int(clock['elapsed_seconds']) or clock['elapsed_seconds'] < 0):
        raise MatchError('state clock is invalid')
    if not _is_int(state['seq']) or state['seq'] < 0:
        raise MatchError('state seq must be a non-negative integer')
    _check_result(state['result'], state['phase'], state['seq'])
    players = _check_players(state['players'], catalog)
    _check_spawns(state['spawns'], catalog, players)
    _check_history(state['history'], state['event_ledger'], state['seq'])


def _canonical(value):
    """Strict canonical JSON: sorted keys, no NaN/Infinity, no silent coercion."""
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise MatchError('value is not JSON-compatible') from None


def snapshot(state):
    """Return the verified in-memory DTO ``{'version': 1, 'state': copy}``.

    The state must prove itself exactly the way ``restore_snapshot`` proves a
    payload: its accepted history is replayed and the rebuild compared in
    canonical form, so the DTO never carries scores, spent types, spawns,
    a clock, a result or a ledger that its own events do not reproduce.
    ``restore_snapshot`` never calls ``snapshot``, so this cannot recurse.
    No file and no database: encoding and storage belong to the caller, and
    the DTO shares no mutable object with the state it was taken from.
    """
    rebuilt = restore_snapshot({'version': SNAPSHOT_VERSION, 'state': state})
    return {'version': SNAPSHOT_VERSION, 'state': copy.deepcopy(rebuilt)}


def _replay(state):
    """Fold the stored history into a fresh match; refuse the slightest divergence.

    This is the only consistency proof: rebuilt from config, catalog and
    events, so spent types, live/closed spawns, scores, clock, result and the
    ledger cannot be forged by editing those fields alone.  It costs one fold
    per stored event and is never called from inside another fold.
    """
    config = state['config']
    rebuilt = create_match(state['match_id'], state['catalog'],
                           target_kills=config['target_kills'],
                           exhaustion_enabled=config['exhaustion_enabled'],
                           time_limit_seconds=config['time_limit_seconds'])
    for entry in state['history']:
        reason, normalized = _validate(entry['event'], rebuilt)
        if (reason is not None or normalized != entry['event']
                or entry['fingerprint'] != _fingerprint(normalized)):
            raise MatchError('snapshot history entry is not a reproducible v1 event')
        if _fold(rebuilt, normalized) != 'ok':
            raise MatchError('snapshot history cannot be replayed')
    return rebuilt


def restore_snapshot(payload):
    """Rebuild a state from a snapshot DTO, refusing to repair anything.

    The payload is re-validated field by field, its history is replayed and
    the canonical JSON of the rebuild must equal the stored state, so a
    snapshot that disagrees with its own events is refused instead of loaded.
    Authenticity is still assumed: storage provenance cannot be checked here
    without secrets or signatures, and a history forged consistently as a whole
    is not detectable by this library.
    """
    if not _has_fields(payload, ('version', 'state')):
        raise MatchError('snapshot must be an object with exactly version and state')
    if not _is_int(payload['version']) or payload['version'] != SNAPSHOT_VERSION:
        raise MatchError('snapshot version must be the integer 1')
    state = payload['state']
    _check_state(state)
    rebuilt = _replay(state)
    if _canonical(rebuilt) != _canonical(state):
        raise MatchError('snapshot does not match its own event history')
    return rebuilt


# --- reducer ---------------------------------------------------------------

def apply_event(state, event):
    """Fold one trusted event; inputs are never mutated and never aliased.

    Accepted: a new state plus ``reason='ok'``.  Rejected: an equivalent copy
    of the input state plus a machine-readable reason code, and the ledger,
    history and slots are untouched.
    """
    _check_state(state)
    reason, ev = _validate(event, state)
    if reason is not None:
        return Transition(copy.deepcopy(state), False, reason)
    new_state = copy.deepcopy(state)
    reason = _fold(new_state, ev)
    return Transition(new_state, reason == 'ok', reason)


def _fold(state, ev):
    """Fold an already validated event into a private copy; shared by both paths.

    The replay of ``restore_snapshot`` calls this directly, so restoring never
    re-validates the whole state per event and never replays recursively.
    """
    fingerprint = _fingerprint(ev)
    seen = state['event_ledger'].get(ev['event_id'])
    if seen is not None:
        return 'duplicate_event' if seen == fingerprint else 'event_id_conflict'
    return _HANDLERS[ev['type']](state, ev, fingerprint)


def _accept(state, ev, fingerprint):
    state['seq'] += 1
    state['history'].append({'seq': state['seq'], 'event_id': ev['event_id'],
                             'type': ev['type'], 'fingerprint': fingerprint,
                             'event': copy.deepcopy(ev)})
    state['event_ledger'][ev['event_id']] = fingerprint
    return 'ok'


def _running(state):
    if state['phase'] == 'lobby':
        return 'not_running'
    if state['phase'] == 'ended':
        return 'match_ended'
    return None


def _taken(state, callsign, ignore_player=None):
    wanted = callsign.lower()
    return any(p['callsign'].lower() == wanted
               for pid, p in state['players'].items() if pid != ignore_player)


def _h_join(state, ev, fp):
    if state['phase'] == 'ended':
        return 'match_ended'
    if state['phase'] != 'lobby':
        return 'roster_locked'
    if ev['player_id'] in state['players']:
        return 'already_joined'
    if _taken(state, ev['callsign']):
        return 'callsign_taken'
    state['players'][ev['player_id']] = {
        'player_id': ev['player_id'], 'team_id': ev['team_id'],
        'callsign': ev['callsign'], 'used_ship_ids': [],
        'active_spawn_id': None, 'joined_seq': state['seq'] + 1}
    return _accept(state, ev, fp)


def _h_start(state, ev, fp):
    if state['phase'] == 'running':
        return 'already_started'
    if state['phase'] == 'ended':
        return 'match_ended'
    counts = {team: sum(1 for p in state['players'].values() if p['team_id'] == team)
              for team in TEAMS}
    if not all(counts.values()):
        return 'roster_incomplete'
    cfg = state['config']
    if not cfg['exhaustion_enabled'] and cfg['time_limit_seconds'] is None:
        budgets = {team: counts[team] * len(state['catalog'][team]) for team in TEAMS}
        if cfg['target_kills'] > min(budgets.values()):
            return 'unreachable_target'
    state['phase'] = 'running'
    return _accept(state, ev, fp)


def _h_reconnect(state, ev, fp):
    if state['phase'] == 'ended':
        return 'match_ended'
    player = state['players'].get(ev['player_id'])
    if player is None:
        return 'unknown_player'
    if _taken(state, ev['callsign'], ignore_player=ev['player_id']):
        return 'callsign_taken'
    player['callsign'] = ev['callsign']   # display name only: no slot, team or score change
    return _accept(state, ev, fp)


def _h_spawn(state, ev, fp):
    blocked = _running(state)
    if blocked:
        return blocked
    player = state['players'].get(ev['player_id'])
    if player is None:
        return 'unknown_player'
    if ev['spawn_id'] in state['spawns']:
        return 'spawn_id_used'
    if player['active_spawn_id'] is not None:
        return 'spawn_busy'
    ship = ev['ship_id']
    if ship not in state['catalog'][player['team_id']]:
        return 'ship_not_in_team'
    if ship in player['used_ship_ids']:
        return 'ship_spent'
    state['spawns'][ev['spawn_id']] = {
        'spawn_id': ev['spawn_id'], 'player_id': ev['player_id'],
        'team_id': player['team_id'], 'ship_id': ship, 'status': 'alive',
        'opened_seq': state['seq'] + 1, 'closed_seq': None, 'closed_by': None,
        'killer_spawn_id': None}
    player['used_ship_ids'].append(ship)
    player['active_spawn_id'] = ev['spawn_id']
    return _accept(state, ev, fp)


def _close(state, spawn, cause, killer_spawn_id=None):
    spawn['status'] = 'closed'
    spawn['closed_seq'] = state['seq'] + 1
    spawn['closed_by'] = cause
    spawn['killer_spawn_id'] = killer_spawn_id
    state['players'][spawn['player_id']]['active_spawn_id'] = None


def _h_death(state, ev, fp):
    blocked = _running(state)
    if blocked:
        return blocked
    victim = state['spawns'].get(ev['victim_spawn_id'])
    killer = state['spawns'].get(ev['killer_spawn_id'])
    if victim is None or killer is None:
        return 'unknown_spawn'
    if victim['status'] != 'alive' or killer['status'] != 'alive':
        return 'stale_spawn'        # includes a repeat of a finished spawn with a new event id
    if victim['player_id'] == killer['player_id']:
        return 'self_kill'
    if victim['team_id'] == killer['team_id']:
        return 'friendly_fire'
    _close(state, victim, 'death', killer['spawn_id'])
    state['scores'][killer['team_id']] += 1
    _accept(state, ev, fp)
    if not _end_by_target(state):
        _end_by_exhaustion(state)   # the 15 always wins the same transition
    return 'ok'


def _h_crash(state, ev, fp):
    blocked = _running(state)
    if blocked:
        return blocked
    spawn = state['spawns'].get(ev['spawn_id'])
    if spawn is None:
        return 'unknown_spawn'
    if spawn['status'] != 'alive':
        return 'stale_spawn'
    _close(state, spawn, 'crash')
    _accept(state, ev, fp)
    _end_by_exhaustion(state)
    return 'ok'


def _h_tick(state, ev, fp):
    blocked = _running(state)
    if blocked:
        return blocked
    elapsed = ev['elapsed_seconds']
    if elapsed < state['clock']['elapsed_seconds']:
        return 'time_regress'       # the round clock never moves backwards
    state['clock']['elapsed_seconds'] = elapsed
    _accept(state, ev, fp)
    limit = state['config']['time_limit_seconds']
    if limit is not None and elapsed >= limit:
        _end(state, 'time_limit', _by_score(state))
    return 'ok'


_HANDLERS = {'join': _h_join, 'start': _h_start, 'reconnect': _h_reconnect,
             'spawn': _h_spawn, 'death': _h_death, 'crash': _h_crash,
             'tick': _h_tick}


# --- ending ----------------------------------------------------------------

def _team_capacity(state, team):
    """Unused (pilot, concrete type) slots plus still-alive ships of one team."""
    types = len(state['catalog'][team])
    unused = sum(types - len(p['used_ship_ids'])
                 for p in state['players'].values() if p['team_id'] == team)
    alive = sum(1 for s in state['spawns'].values()
                if s['team_id'] == team and s['status'] == 'alive')
    return unused + alive


def _by_score(state):
    best = max(TEAMS, key=lambda team: state['scores'][team])
    return None if len({state['scores'][t] for t in TEAMS}) == 1 else best


def _end(state, reason, winner=None):
    state['phase'] = 'ended'
    state['result'] = {'reason': reason, 'winner': winner,
                       'outcome': 'draw' if winner is None else 'win',
                       'seq': state['seq']}


def _end_by_target(state):
    """First side to reach the configured target wins; checked before exhaustion."""
    target = state['config']['target_kills']
    for team in TEAMS:
        if state['scores'][team] >= target:
            _end(state, 'kill_target', team)
            return True
    return False


def _end_by_exhaustion(state):
    """Only when explicitly configured: no slot and no live ship left is a loss."""
    if not state['config']['exhaustion_enabled']:
        return False
    empty = [team for team in TEAMS if _team_capacity(state, team) == 0]
    if not empty:
        return False
    winner = _by_score(state) if len(empty) == 2 else _other(empty[0])
    _end(state, 'exhaustion', winner)
    return True


def _other(team):
    return TEAMS[1] if team == TEAMS[0] else TEAMS[0]


__all__ = ('MatchError', 'Transition', 'apply_event', 'create_match', 'restore_snapshot',
           'snapshot')
