"""Manual real-Tk smoke of the space_ui form: python3 -m client.space_ui.smoke

On a machine with Tk and a display this really constructs the window, forces a
layout pass with update_idletasks() (which is what catches geometry mistakes
such as mixing pack and grid under one master), then destroys the window. No
FlightGear is started, nothing is clicked, saved or connected. Exit 0 means the
window built and tore down cleanly and prints the sizes; exit 2 explains a
missing Tk or display and is NOT a pass.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import sys


def main():
    try:
        from . import view
    except ImportError:  # tkinter or its _tkinter library missing
        print('space_ui smoke: tkinter is not available; this smoke needs real Tk, '
              'not a stub. NOT TESTED.', file=sys.stderr)
        return 2
    try:
        root = view.tk.Tk()
    except view.TclError as exc:
        print('space_ui smoke: window could not be opened: check the display and Tk '
              'installation (' + type(exc).__name__ + '). NOT TESTED.', file=sys.stderr)
        return 2
    try:
        app = view.ClientApp(root)
        root.update_idletasks()
        visible = (root.winfo_width(), root.winfo_height())
        requested = (root.winfo_reqwidth(), root.winfo_reqheight())
        if visible[0] < 2 or visible[1] < 2:
            print('space_ui smoke: window never became visible: NOT TESTED.',
                  file=sys.stderr)
            root.destroy()
            return 2
        scrollable = app.canvas.winfo_height()
        print(f'space_ui smoke: window built and destroyed cleanly; visible '
              f'{visible[0]}x{visible[1]}, requested {requested[0]}x{requested[1]}, '
              f'scroll viewport {scrollable}px; FlightGear was NOT started.')
        root.destroy()
    except view.TclError as exc:
        print('space_ui smoke: form build failed: ' + type(exc).__name__ + ': '
              + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
