# SPDX-License-Identifier: GPL-2.0-or-later
"""Space Arena client (TASK6): headless core plus the Tk/ttk form in view.py.

Run the form from the repository root: python -m client.space_ui [--profile PATH].
The GUI builds/validates a Profile, lists ships() per team, gates demo selection
with select_ship()/MockAdapter, shows RelayStub state for the future relay and
starts the one real launch (stock solo c172p) through SoloSession. Importing
this package, its core or its __main__, and --help, work without tkinter; only
opening the window needs Tk or a display, and their absence is reported as
exit 2. No import needs network, FlightGear or Blender.
"""
from .core import (DEMO_CATALOG, MAX_PROFILE_BYTES, MockAdapter, Profile, RelayStub,
                   Ship, SoloSession, build_solo_args, load_profile, save_profile,
                   select_ship, ships, validate_endpoint)

__all__ = ('DEMO_CATALOG', 'MAX_PROFILE_BYTES', 'MockAdapter', 'Profile', 'RelayStub',
           'Ship', 'SoloSession', 'build_solo_args', 'load_profile', 'save_profile',
           'select_ship', 'ships', 'validate_endpoint')
