#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Original, deterministic AC3D greybox. No imported geometry or textures."""
import argparse
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def geometry():
    lines = ['AC3Db']
    for name, rgb in [('hull', '0.22 0.32 0.42'), ('frame', '0.85 0.65 0.15'),
                      ('engine', '0.12 0.65 0.8')]:
        lines.append(f'MATERIAL "{name}" rgb {rgb} amb 0.2 0.2 0.2 emis 0 0 0 spec 0.2 0.2 0.2 shi 16 trans 0')
    # Model axes aft/right/up. An open cockpit sits above the low hull.
    boxes = [
        ('hull', (0, 0, -0.15), (5, 2.2, 0.6), 0),
        ('cockpit', (-0.5, 0, 0.22), (1.8, 1.7, 0.12), 1),
        ('cockpit-left', (-0.5, -0.9, 0.55), (1.8, 0.1, 0.65), 1),
        ('cockpit-right', (-0.5, 0.9, 0.55), (1.8, 0.1, 0.65), 1),
        ('engine-1', (1.5, -1.6, -0.05), (1.6, 0.7, 0.7), 2),
        ('engine-2', (1.5, 1.6, -0.05), (1.6, 0.7, 0.7), 2),
        ('muzzle-1', (-2.4, -0.7, 0.1), (0.3, 0.12, 0.12), 1),
        ('muzzle-2', (-2.4, 0.7, 0.1), (0.3, 0.12, 0.12), 1),
    ]
    lines += ['OBJECT world', 'name "lab-root"', 'loc 0 0 0', f'kids {len(boxes)}']
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    for name, loc, size, material in boxes:
        # SimGear AC reader maps AC(x,y,z) -> FG(x,-z,y).
        ac_loc = (loc[0], loc[2], -loc[1])
        lines += ['OBJECT poly', f'name "{name}"', 'loc ' + ' '.join(map(str, ac_loc)), 'numvert 8']
        for a, b, c in [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                         (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]:
            x, y, z = (v*s/2 for v, s in zip((a,b,c), size))
            lines.append(f'{x:.6f} {z:.6f} {-y:.6f}')
        lines.append('numsurf 12')
        for a,b,c,d in faces:
            for tri in ((a,b,c),(a,c,d)):
                lines += ['SURF 0x0', f'mat {material}', 'refs 3']
                lines += [f'{i} 0 0' for i in tri]
        lines.append('kids 0')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = PACKAGE / 'Models/lab.ac'
    if args.check:
        if target.read_text() != geometry():
            raise SystemExit('lab.ac differs from its original source')
        print('Original geometry matches (96 triangles).')
    else:
        target.write_text(geometry())
