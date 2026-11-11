"""Entry point for the Space Arena client: python -m client.space_ui [--profile PATH].

--help and importing this module need no tkinter and open no window: the Tk
form lives in view.py and is imported lazily inside main(). Without tkinter
(or its _tkinter library) and without a display, main() prints one honest
message to stderr and returns 2; a headless fake GUI is never offered.

A --profile is read only from that explicit path: never from $HOME, never a
legacy client config. Loading executes nothing, and nothing is auto-saved or
auto-started: the only launch is the clearly labelled solo c172p button.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import argparse
import sys
from pathlib import Path

from .core import load_profile

EXIT_OK = 0
EXIT_UNAVAILABLE = 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m client.space_ui',
        description='Space Arena client form: profile, demo faction/ship selection, '
                    'and one clearly labelled diagnostic solo c172p launch.',
        epilog='Nothing starts at launch: the profile is read only from the explicit '
               '--profile path, loading it never executes anything, and there is no '
               'autosave. Without Tk or a display this command explains that and exits 2.')
    parser.add_argument('--profile', type=Path,
                        help='explicit path to a profile JSON written by this client '
                             '(client/space_ui/profile.example.json is the template); '
                             'never read automatically from $HOME')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    profile = None
    if args.profile is not None:
        try:
            profile = load_profile(args.profile)
        except ValueError as exc:  # core errors are generic and never echo values
            print('space_ui: profile was not loaded: ' + str(exc), file=sys.stderr)
            return EXIT_UNAVAILABLE
    try:
        from . import view  # lazy: tkinter is needed only when the window opens
    except ImportError:  # tkinter missing, or its _tkinter shared library missing
        print('space_ui: tkinter is not available in this Python; install the Tk '
              'package of your distribution, then run again. Nothing was started.',
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        return view.run(profile)
    except view.TclError as exc:
        print('space_ui: failed to open or configure the window: check the display '
              'and the Tk installation, then run again. Nothing was started. '
              '(Tk: ' + type(exc).__name__ + ')', file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == '__main__':
    sys.exit(main())
