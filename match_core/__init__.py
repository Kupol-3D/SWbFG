# SPDX-License-Identifier: GPL-2.0-or-later
"""Space Arena match core (TASK7): deterministic offline rules, no server.

Importing this package starts nothing and touches no socket, clock, random
source or file.  The public surface is ``create_match``, ``apply_event``,
``snapshot`` and ``restore_snapshot`` plus the result/error types; see
match_core/CONTRACT.md for the event and snapshot format and for the limits
that integration still has to close.  Snapshot storage stays with the caller:
restore only accepts a consistent history from trusted authoritative storage
and verifies no signature.
"""
from .core import (MatchError, Transition, apply_event, create_match, restore_snapshot,
                   snapshot)

__all__ = ('MatchError', 'Transition', 'apply_event', 'create_match', 'restore_snapshot',
           'snapshot')
