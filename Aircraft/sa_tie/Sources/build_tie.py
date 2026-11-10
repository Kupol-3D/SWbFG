#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Original, deterministic TIE/ln greybox; Python stdlib only, no Blender.

Author: Space Arena contributors, with OpenAI Codex assistance (2026).
Design coordinates: metres, x aft, y starboard, z up. AC export explicitly
maps (x,y,z) to (x,z,-y); SimGear's ACProcessPolicy reverses that mapping.
The source is this file; exporting never modifies it.
"""

import argparse
from dataclasses import dataclass, field
from hashlib import sha256
from math import cos, pi, sin, sqrt
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]
# name, diffuse RGB, emission RGB. All opaque; no external textures.
MATERIALS = (
    ("hull-grey", (0.46, 0.49, 0.53), (0, 0, 0)),
    ("frame-grey", (0.25, 0.28, 0.32), (0, 0, 0)),
    ("solar-dark", (0.035, 0.045, 0.065), (0, 0, 0)),
    ("solar-ribs", (0.13, 0.15, 0.19), (0, 0, 0)),
    ("interior", (0.12, 0.14, 0.17), (0, 0, 0)),
    ("seat", (0.055, 0.065, 0.08), (0, 0, 0)),
    ("engine-lens", (0.12, 0.015, 0.02), (0, 0, 0)),
    ("panel-light", (0.04, 0.09, 0.06), (0, 0, 0)),
    ("switch-ivory", (0.78, 0.8, 0.71), (0, 0, 0)),
    ("muzzle-dark", (0.025, 0.03, 0.035), (0, 0, 0)),
)


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def mul(a, k):
    return tuple(x * k for x in a)


def cross(a, b):
    return (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2],
            a[0]*b[1] - a[1]*b[0])


def unit(a):
    return mul(a, 1 / sqrt(sum(x*x for x in a)))


@dataclass
class Node:
    name: str
    material: int = 0
    origin: tuple = (0, 0, 0)
    vertices: list = field(default_factory=list)
    faces: list = field(default_factory=list)
    children: list = field(default_factory=list)
    smooth: bool = False
    two_sided: bool = False

    def polygon(self, points):
        """Triangulate convex faces before export; avoid runtime tessellation."""
        base = len(self.vertices)
        self.vertices.extend(points)
        self.faces.extend((base, base+i, base+i+1)
                          for i in range(1, len(points)-1))


def box(node, lo, hi):
    x, y, z = lo
    X, Y, Z = hi
    p = [(x,y,z), (X,y,z), (X,Y,z), (x,Y,z),
         (x,y,Z), (X,y,Z), (X,Y,Z), (x,Y,Z)]
    for face in ((0,3,2,1), (4,5,6,7), (0,1,5,4),
                 (1,2,6,5), (2,3,7,6), (3,0,4,7)):
        node.polygon([p[i] for i in face])


def tube(node, start, end, radius, sides=12, caps=True, y_scale=1.0):
    axis = unit(sub(end, start))
    helper = (0, 0, 1) if abs(axis[2]) < 0.9 else (0, 1, 0)
    u = unit(cross(helper, axis))
    v = cross(axis, u)
    rings = [[add(c, add(mul(u, radius*cos(2*pi*i/sides)),
                          mul(v, radius*sin(2*pi*i/sides))))
              for i in range(sides)] for c in (start, end)]
    if y_scale != 1.0:
        rings = [[(p[0], c[1]+(p[1]-c[1])*y_scale, p[2]) for p in ring]
                 for c, ring in zip((start,end),rings)]
    for i in range(sides):
        j = (i+1) % sides
        node.polygon([rings[0][i], rings[0][j], rings[1][j], rings[1][i]])
    if caps:
        node.polygon(list(reversed(rings[0])))
        node.polygon(rings[1])


def ring_x(node, x, outer, inner, sides=32):
    for i in range(sides):
        a, b = 2*pi*i/sides, 2*pi*(i+1)/sides
        node.polygon([(x, outer*cos(a), outer*sin(a)),
                      (x, outer*cos(b), outer*sin(b)),
                      (x, inner*cos(b), inner*sin(b)),
                      (x, inner*cos(a), inner*sin(a))])


def shell(node, radius, inward=False, steps=18, sides=48):
    # No front cap: the opening is shared by exterior and interior.
    start = 42*pi/180
    rings = []
    for k in range(steps):
        t = start + (pi-start)*k/steps
        rings.append([(-radius*cos(t), radius*sin(t)*cos(2*pi*i/sides),
                       radius*sin(t)*sin(2*pi*i/sides)) for i in range(sides)])
    for k in range(steps-1):
        for i in range(sides):
            j = (i+1) % sides
            p = [rings[k][i], rings[k+1][i], rings[k+1][j], rings[k][j]]
            node.polygon(p if inward else list(reversed(p)))
    for i in range(sides):
        p = [rings[-1][i], (radius, 0, 0), rings[-1][(i+1) % sides]]
        node.polygon(p if inward else list(reversed(p)))


def panel(side):
    # Flat-ended vertical hexagon, 7.2 m long, 8.8 m high, 0.20 m thick.
    y = side * 3.238
    outline = [(-3.6,0), (-1.8,-4.4), (1.8,-4.4),
               (3.6,0), (1.8,4.4), (-1.8,4.4)]
    result = Node("panel-" + ("port" if side < 0 else "starboard"))
    cells = Node(result.name + "-cells", 2)
    frame = Node(result.name + "-frame", 1)
    ribs = Node(result.name + "-ribs", 3)
    for face_y in (y-0.10, y+0.10):
        poly = [(x, face_y, z) for x, z in outline]
        # In the x/z plane the given order has a -y normal.
        cells.polygon(poly if face_y < y else list(reversed(poly)))
        # Geometry ribs instead of an imported texture.
        for x in [i*0.30 for i in range(-11, 12)]:
            half_height = 4.4 if abs(x) <= 1.8 else (3.6-abs(x))*4.4/1.8
            rib_y = face_y + (-0.008 if face_y < y else 0.008)
            box(ribs, (x-0.012, rib_y-0.004, -half_height+0.15),
                (x+0.012, rib_y+0.004, half_height-0.15))
        # Six radial structural beams on each face.
        for x, z in outline:
            tube(frame, (0, face_y, 0), (0.96*x, face_y, 0.96*z),
                 0.055, 6, y_scale=0.012/0.055)
    for i, (x, z) in enumerate(outline):
        X, Z = outline[(i+1) % 6]
        # Wide border stays INSIDE the agreed bounding dimensions.
        poly = [(x, y-0.1, z), (X, y-0.1, Z),
                (X, y+0.1, Z), (x, y+0.1, z)]
        frame.polygon(list(reversed(poly)))
        for face_y in (y-0.104, y+0.104):
            border = [(x,face_y,z), (X,face_y,Z),
                      (0.94*X,face_y,0.94*Z), (0.94*x,face_y,0.94*z)]
            frame.polygon(border if face_y < y else list(reversed(border)))
    result.children = [cells, frame, ribs]
    return result


def build():
    world = Node("sa-tie")
    hull = Node("hull")
    skin = Node("hull-shell", smooth=True)
    shell(skin, 1.55)
    hull.children.append(skin)
    hull.children.extend(panel(side) for side in (-1, 1))
    struts = Node("wing-struts", 0)
    for side in (-1, 1):
        tube(struts, (0,side*1.28,0), (0,side*3.238,0), 0.26, 12)
        tube(struts, (0,side*2.9,0), (0,side*3.238,0), 0.47, 12)
    hull.children.append(struts)
    hatch = Node("top-hatch", 1)
    tube(hatch, (0,0,1.45), (0,0,1.58), 0.46, 16)
    hull.children.append(hatch)
    window = Node("viewport-frame", 0, two_sided=True)
    # Short bevel closes the shell thickness at the front aperture.
    ring_x(window, -1.151875, 1.037153, 0.94, 48)
    ring_x(window, -1.19, 1.04, 0.94, 8)
    ring_x(window, -1.20, 0.48, 0.44, 8)
    for i in range(8):
        a = 2*pi*(i+0.5)/8  # keep the horizontal sightline free
        tube(window, (-1.20, 0.44*cos(a), 0.44*sin(a)),
             (-1.20, 0.96*cos(a), 0.96*sin(a)), 0.024, 6)
    hull.children.append(window)
    for i, y in enumerate((-0.62, 0.62), 1):
        engine = Node(f"engine-{i}", 1, origin=(1.63,y,0.12))
        tube(engine, (-0.38,0,0), (0,0,0), 0.19, 16)
        glow = Node(f"engine-{i}-glow", 6, origin=(0.006,0,0))
        # Aft-facing lens, independently animated, recessed inside the rim.
        points = [(0,0.145*cos(2*pi*j/16),0.145*sin(2*pi*j/16))
                  for j in range(16)]
        glow.polygon(points)
        engine.children.append(glow)
        hull.children.append(engine)
    for i, y in enumerate((-0.52, 0.52), 1):
        cannon = Node(f"cannon-{i}", 1)
        tube(cannon, (-0.95,y,-0.82), (-1.72,y,-0.82), 0.09, 12, False)
        muzzle = Node(f"muzzle-{i}", 9, origin=(-1.725,y,-0.82))
        # Real named geometry at the exit, + local origin for integration.
        muzzle.polygon([(0,0.068*cos(2*pi*j/12),0.068*sin(2*pi*j/12))
                        for j in reversed(range(12))])
        hull.children.extend([cannon, muzzle])

    cockpit = Node("cockpit")
    lining = Node("cockpit-lining", 4, smooth=True)
    shell(lining, 1.43, inward=True)
    cockpit.children.append(lining)
    seat = Node("pilot-seat", 5)
    box(seat, (-0.15,-0.34,-0.77), (0.72,0.34,-0.57))
    box(seat, (0.54,-0.34,-0.57), (0.76,0.34,0.70))
    box(seat, (0.52,-0.25,0.70), (0.77,0.25,0.90))
    cockpit.children.append(seat)
    floor = Node("cockpit-floor", 4)
    box(floor, (-0.75,-0.67,-1.09), (0.77,0.67,-1.03))
    cockpit.children.append(floor)
    console = Node("cockpit-console", 1)
    box(console, (-0.95,-0.67,-0.62), (-0.65,0.67,-0.37))
    cockpit.children.append(console)
    controls = Node("control-grips", 5)
    for y in (-0.28, 0.28):
        tube(controls, (-0.54,y,-0.57), (-0.50,y,-0.25), 0.04, 8)
    cockpit.children.append(controls)
    switch = Node("panel-light-switch", 8, origin=(-0.61,0.48,-0.40))
    box(switch, (-0.07,-0.07,-0.07), (0.02,0.07,0.07))
    cockpit.children.append(switch)
    indicator = Node("panel-light-indicator", 7)
    box(indicator, (-0.645,0.57,-0.46), (-0.625,0.65,-0.35))
    cockpit.children.append(indicator)
    lights = Node("panel-light-strips", 7)
    box(lights, (-0.79,-0.60,-0.365), (-0.70,-0.06,-0.35))
    box(lights, (-0.79,0.06,-0.365), (-0.70,0.24,-0.35))
    cockpit.children.append(lights)
    world.children = [hull, cockpit]
    return world


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def ac_point(p):
    x, y, z = p
    return " ".join(f"{v:.6f}" for v in (x, z, -y))


def export_ac(world):
    lines = ["AC3Db"]
    for name, rgb, emission in MATERIALS:
        color = " ".join(str(v) for v in rgb)
        emit = " ".join(str(v) for v in emission)
        lines.append(f'MATERIAL "{name}" rgb {color} amb 0.3 0.3 0.3 '
                     f'emis {emit} spec 0.18 0.18 0.18 shi 24 trans 0')

    def write(node, root=False):
        kind = "world" if root else ("poly" if node.vertices else "group")
        lines.extend([f"OBJECT {kind}", f'name "{node.name}"',
                      "loc " + ac_point(node.origin)])
        if node.vertices:
            lines.append("crease 45")
            lines.append(f"numvert {len(node.vertices)}")
            lines.extend(ac_point(p) for p in node.vertices)
            lines.append(f"numsurf {len(node.faces)}")
            flags = (0x10 if node.smooth else 0) | (0x20 if node.two_sided else 0)
            for face in node.faces:
                lines.extend([f"SURF 0x{flags:02x}", f"mat {node.material}", "refs 3"])
                lines.extend(f"{i} 0 0" for i in face)
        lines.append(f"kids {len(node.children)}")
        for child in node.children:
            write(child)

    write(world, True)
    return "\n".join(lines) + "\n"


def outputs():
    ac = export_ac(build()).encode("ascii")
    source = Path(__file__).read_bytes()
    manifest = (f"{sha256(source).hexdigest()}  Sources/build_tie.py\n"
                f"{sha256(ac).hexdigest()}  Models/tie.ac\n").encode("ascii")
    return {"Models/tie.ac": ac, "Sources/SHA256SUMS": manifest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()
    for relative, data in outputs().items():
        path = PACKAGE / relative
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                parser.exit(1, f"Stale or missing export: {relative}\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    print(f"TIE/ln: {sum(len(n.faces) for n in walk(build()))} triangles; "
          + ("export verified" if args.check else "export written"))


if __name__ == "__main__":
    main()
