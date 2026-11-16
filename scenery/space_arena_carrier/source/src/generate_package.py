#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Deterministic generator of the TASK4 space_arena greybox package.

Author / original geometry source: this file.  Own simple geometry only:
no extracted game assets, no textures, no binaries.  The committed
.ac / .xml / .stg / manifest.json files are this script's exact
deterministic output; tools/test_space_arena.py regenerates everything
into a temp directory and byte-compares the artifacts, so the committed
package can never drift from this source.

Scenery layout (FGData release/2024.1 Docs/README.scenery + SimGear
release/2024.1 bucket algorithm):
  Objects/w130n30/w123n37/942042.stg          OBJECT_STATIC_AGL placements
  Objects/w130n30/w123n37/Models/space_arena/  own .ac models + wrappers
The bucket for negative longitudes is "w130" because SimGear floors
lon/10 downwards (README.scenery shows w130n30/w123n37 for KSFO).

Model files use the standard AC3Db ASCII dialect (Inivis AC3D file
format, the dialect every FGData .ac uses): header "AC3Db", MATERIAL
lines with rgb/amb/emis/spec/shi/trans, OBJECT world + OBJECT poly with
numvert / numsurf / SURF 0x30 / mat / refs (UV 0 0, no textures).

Coordinate and unit conventions (mirrored in manifest.json and README.md):
  * Units: meters; angles degrees; lon/lat WGS84 decimal degrees,
    west/south negative.
  * AC3D model axes as stored in the .ac files: +x aft, +y up, +z left
    (standard FlightGear convention); the model front faces -x.  Design
    helper axes used below are (f forward, l left, u up) mapped as
    x = -f, y = u, z = l.
  * OBJECT_STATIC_AGL line:
      OBJECT_STATIC_AGL <path> <lon> <lat> <agl_offset_m> <heading>
    - <path> is relative to the tile directory holding the STG;
    - <agl_offset_m> is meters ABOVE GROUND LEVEL at the anchor (the AGL
      variant of OBJECT_STATIC per Docs/README.scenery).  Every placement
      writes 0.0: the model origin sits on the terrain/water surface at
      the anchor.  This is deliberate: the real terrain MSL at the
      anchors was never measured here, so no MSL elevation could be
      stated honestly; 0.0 here is an AGL offset, NOT a 0.0 MSL value;
    - <heading> is degrees counter-clockwise from north per
      Docs/README.scenery; the manifest records the intended true
      bearing (clockwise) in heading_deg and the written value in
      stg_heading_deg.
  * Terrain height at the site is UNVERIFIED (no FlightGear and no
    elevation service reachable in this build environment); the design
    band is 0..70 m MSL coastal terrace.  Once terrain MSL is measured
    in FG/UFO, the placements can switch to exact-MSL OBJECT_STATIC
    entries (see VALIDATION.md).

NOT verified in this environment (FlightGear 2024.1.7 is not installed):
STG loading, heading sign interpretation at runtime, terrain
intersection, visual scale/appearance, and support/collision behavior of
any surface.  See manifest.json verification section and VALIDATION.md.
"""

import hashlib
import json
import math
import os
import sys

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FG_TARGET = "2024.1.7"
TRI_LIMIT = 25000  # TASK4 working triangle budget for the arena package

# --- Site region ------------------------------------------------------------
# One 1-degree tile district south of KSFO that ships in the default
# scenery set: bucket w130n30, tile w123n37.  The site sits on the Half
# Moon Bay coastal plain ~20 km south of KSFO ARP; no stock airport is
# inside the site box.
BUCKET = "w130n30"
TILE = "w123n37"
TILE_LON_RANGE = (-123.0, -122.0)
TILE_LAT_RANGE = (37.0, 38.0)

M_PER_DEG_LAT = 111320.0

REBELS_ANCHOR = (-122.4300, 37.4300)   # lon, lat WGS84
EMPIRE_ANCHOR = (-122.4550, 37.4150)

# AGL offset written into every OBJECT_STATIC_AGL placement line (meters
# above the terrain/water surface at the anchor).  It is 0.0 on purpose:
# the terrain MSL at the anchors was never measured in this environment,
# so no MSL elevation is claimed; 0.0 AGL pins each model origin to the
# ground regardless of the (unmeasured) terrain height.
STG_AGL_OFFSET_M = 0.0
TERRAIN_DESIGN_BAND_M = (0.0, 70.0)

# Reference points from general knowledge, NOT read from FGData.
KSFO_ARP = (-122.3750, 37.6188)
KHAF_ARP = (-122.5018, 37.5119)

REBEL_KEEPOUT_M = 160.0
EMPIRE_KEEPOUT_M = 320.0
ROUTE_HALF_WIDTH_M = 75.0
ROUTE_LENGTH_M = 3000.0
ROUTE_FLOOR_ABOVE_DECK_M = 15.0   # minimum corridor floor above deck top
GEAR_ALLOWANCE_M = 1.5            # deck surface -> aircraft CG design value


def m_per_deg_lon(lat_deg):
    return M_PER_DEG_LAT * math.cos(math.radians(lat_deg))


def enu_to_lonlat(anchor, east_m, north_m):
    """Small-offset flat-earth approximation (error << 1 m at site scale)."""
    lat = anchor[1] + north_m / M_PER_DEG_LAT
    lon = anchor[0] + east_m / m_per_deg_lon(anchor[1])
    return round(lon, 6), round(lat, 6)


def subtile_id(lon, lat):
    """Numeric STG id per SimGear SGBucket (verified against the documented
    FGData example w130n30/w123n37/942050.stg for KSFO)."""
    lon_floor = math.floor(lon)
    lat_floor = math.floor(lat)
    span = 0.25 if 22.0 <= lat < 62.0 else 1.0  # bucket span at site latitude
    x = int((lon - lon_floor) / span + 1e-12)
    y = int((lat - lat_floor) * 8 + 1e-12)
    return ((lon_floor + 180) << 14) + ((lat_floor + 90) << 6) + (y << 3) + x


def heading_cw_to_stg_ccw(h_cw):
    """Intended true bearing (clockwise from north) -> STG heading field,
    which Docs/README.scenery defines counter-clockwise from north."""
    return (360.0 - (h_cw % 360.0)) % 360.0


# ---------------------------------------------------------------------------
# AC3Db ASCII writer (Inivis AC3D file format version 0xb; no textures).

def _mat_line(name, rgb):
    r, g, b = ("%.3f" % c for c in rgb)
    return ('MATERIAL "%s" rgb %s %s %s  amb %s %s %s  emis 0.000 0.000 '
            "0.000  spec 0.200 0.200 0.200  shi 32  trans 0"
            % (name, r, g, b, r, g, b))


class Model:
    """Greybox model in DESIGN axes (f forward, l left, u up, meters).

    Polygons are stored in design coordinates and converted to AC3D file
    axes (+x aft, +y up, +z left) on write: x=-f, y=u, z=l.
    """

    def __init__(self, name):
        self.name = name
        self.polys = []  # list of (material_name, [design points])

    def _add(self, mat, pts):
        self.polys.append((mat, list(pts)))

    def box(self, mat, cf, cl, cu, sf, sl, su):
        f0, f1 = cf - sf / 2.0, cf + sf / 2.0
        l0, l1 = cl - sl / 2.0, cl + sl / 2.0
        u0, u1 = cu - su / 2.0, cu + su / 2.0
        v = {(f, l, u): (f, l, u) for f in (f0, f1) for l in (l0, l1)
             for u in (u0, u1)}
        add = self._add
        add(mat, [v[(f0, l0, u1)], v[(f1, l0, u1)], v[(f1, l1, u1)],
                  v[(f0, l1, u1)]])                      # top
        add(mat, [v[(f0, l0, u0)], v[(f0, l1, u0)], v[(f1, l1, u0)],
                  v[(f1, l0, u0)]])                      # bottom
        add(mat, [v[(f1, l0, u1)], v[(f1, l0, u0)], v[(f1, l1, u0)],
                  v[(f1, l1, u1)]])                      # +f face
        add(mat, [v[(f0, l0, u1)], v[(f0, l1, u1)], v[(f0, l1, u0)],
                  v[(f0, l0, u0)]])                      # -f face
        add(mat, [v[(f0, l1, u1)], v[(f1, l1, u1)], v[(f1, l1, u0)],
                  v[(f0, l1, u0)]])                      # +l face
        add(mat, [v[(f0, l0, u1)], v[(f0, l0, u0)], v[(f1, l0, u0)],
                  v[(f1, l0, u1)]])                      # -l face

    def octa(self, mat, cf, cl, cu, rf, rl, ru):
        p = [(cf - rf, cl, cu), (cf + rf, cl, cu), (cf, cl - rl, cu),
             (cf, cl + rl, cu), (cf, cl, cu - ru), (cf, cl, cu + ru)]
        for a, b, c in ((0, 2, 5), (2, 1, 5), (1, 3, 5), (3, 0, 5),
                        (2, 0, 4), (1, 2, 4), (3, 1, 4), (0, 3, 4)):
            self._add(mat, [p[a], p[b], p[c]])

    def prism_u(self, mat, cf, cl, cu, radius, height, seg=8):
        """Vertical prism, u from cu to cu+height, regular `seg`-gon."""
        ring0, ring1 = [], []
        for i in range(seg):
            a = 2.0 * math.pi * i / seg
            x, y = cf + radius * math.cos(a), cl + radius * math.sin(a)
            ring0.append((x, y, cu))
            ring1.append((x, y, cu + height))
        for i in range(seg):
            j = (i + 1) % seg
            self._add(mat, [ring0[i], ring0[j], ring1[j], ring1[i]])
        for i in range(1, seg - 1):  # flat caps as fans
            self._add(mat, [ring0[0], ring0[i], ring0[i + 1]])
            self._add(mat, [ring1[0], ring1[i + 1], ring1[i]])

    def prism_f(self, mat, cf, cl, cu, radius, length, seg=8):
        """Horizontal prism along the forward axis, centered on cf."""
        half = length / 2.0
        ring0, ring1 = [], []
        for i in range(seg):
            a = 2.0 * math.pi * i / seg
            y, z = cl + radius * math.cos(a), cu + radius * math.sin(a)
            ring0.append((cf - half, y, z))
            ring1.append((cf + half, y, z))
        for i in range(seg):
            j = (i + 1) % seg
            self._add(mat, [ring0[i], ring0[j], ring1[j], ring1[i]])
        for i in range(1, seg - 1):
            self._add(mat, [ring0[0], ring0[i + 1], ring0[i]])
            self._add(mat, [ring1[0], ring1[i], ring1[i + 1]])

    def wedge(self, mat, f0, f1, lw0, lw1, u0, u1):
        """Tapered hull along f: half-width lw0 at f0, lw1 at f1, u0..u1."""
        a0 = (f0, -lw0, u0)
        a1 = (f0, lw0, u0)
        b0 = (f1, -lw1, u0)
        b1 = (f1, lw1, u0)
        at0 = (f0, -lw0, u1)
        at1 = (f0, lw0, u1)
        bt0 = (f1, -lw1, u1)
        bt1 = (f1, lw1, u1)
        add = self._add
        add(mat, [a0, b0, b1, a1])          # bottom
        add(mat, [at0, at1, bt1, bt0])      # top
        add(mat, [a0, a1, at1, at0])        # back (-f) face
        add(mat, [b0, bt0, bt1, b1])        # nose (+f) face
        add(mat, [a0, at0, bt0, b0])        # -l slanted side
        add(mat, [a1, b1, bt1, at1])        # +l slanted side

    # -- derived quantities ---------------------------------------------------
    def triangles(self):
        return sum(len(pts) - 2 for _, pts in self.polys)

    def design_aabb(self):
        lo = [1e30] * 3
        hi = [-1e30] * 3
        for _, pts in self.polys:
            for p in pts:
                for i in range(3):
                    lo[i] = min(lo[i], p[i])
                    hi[i] = max(hi[i], p[i])
        return {"f": [round(lo[0], 3), round(hi[0], 3)],
                "l": [round(lo[1], 3), round(hi[1], 3)],
                "u": [round(lo[2], 3), round(hi[2], 3)]}

    def ac_text(self):
        mats = sorted({mat for mat, _ in self.polys})
        mat_index = {m: i for i, m in enumerate(mats)}
        lines = ["AC3Db"]
        for m in mats:
            lines.append(_mat_line(m, PALETTE[m]))
        lines += ["OBJECT world", "kids 1", "OBJECT poly",
                  'name "%s"' % self.name, "loc 0 0 0", "texture -1"]
        verts = []
        refs = []  # (mat_index, [vert indices])
        for mat, pts in self.polys:
            vis = []
            for p in pts:
                vis.append(len(verts))
                verts.append((-p[0], p[2], p[1]))  # design -> AC3D axes
            refs.append((mat_index[mat], vis))
        lines.append("numvert %d" % len(verts))
        for x, y, z in verts:
            lines.append("%.3f %.3f %.3f" % (x, y, z))
        lines.append("numsurf %d" % len(refs))
        for mi, vis in refs:
            lines.append("SURF 0x30")
            lines.append("mat %d" % mi)
            lines.append("refs %d" % len(vis))
            for vi in vis:
                lines.append("%d 0 0" % vi)
        lines.append("kids 0")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Palette: Rebels warm sand/white + red, Empire grey + green (solid colors).
PALETTE = {
    "reb_sand": (0.700, 0.640, 0.500),
    "reb_wall": (0.840, 0.810, 0.720),
    "reb_red": (0.750, 0.110, 0.070),
    "reb_dark": (0.420, 0.380, 0.320),
    "emp_grey": (0.420, 0.440, 0.470),
    "emp_dark": (0.240, 0.260, 0.290),
    "emp_green": (0.090, 0.620, 0.300),
}


def build_reb_platform():
    m = Model("reb_platform")
    m.box("reb_sand", 0, 0, 5.7, 20, 20, 0.6)        # deck slab, top +6.0
    for sf in (-8, 0, 8):
        for sl in (-8, 8):
            m.box("reb_dark", sf, sl, 2.7, 1.2, 1.2, 5.4)   # legs
    m.prism_u("reb_red", 0, 0, 6.0, 5.0, 0.12, seg=8)  # pad marker disc
    for sf in (-1, 1):
        for sl in (-1, 1):
            m.box("reb_red", sf * 9.4, sl * 9.4, 6.15, 0.8, 0.8, 0.3)
    return m


def build_reb_hangar():
    m = Model("reb_hangar")
    m.box("reb_wall", 0, 0, 1.75, 22.0, 12.0, 3.5)    # stepped quonset body
    m.box("reb_wall", 0, 0, 4.0, 22.0, 9.0, 1.0)
    m.box("reb_wall", 0, 0, 5.1, 22.0, 5.0, 1.2)
    m.box("reb_dark", 11.0, 0, 3.0, 0.25, 12.0, 6.0)  # back wall
    m.box("reb_red", -11.15, 0, 2.5, 0.25, 8.0, 5.0)  # red door face
    return m


def build_reb_tower():
    m = Model("reb_tower")
    m.box("reb_sand", 0, 0, 1.5, 5.0, 5.0, 3.0)
    m.box("reb_wall", 0, 0, 5.5, 3.4, 3.4, 5.0)
    m.box("reb_dark", 0, 0, 8.3, 4.6, 4.6, 0.6)
    m.box("reb_red", 0, 0, 9.2, 3.8, 3.8, 1.2)
    m.box("reb_dark", 0, 0, 11.2, 0.3, 0.3, 2.8)
    return m


def build_reb_tanks():
    m = Model("reb_tanks")
    for fx in (-4.5, 4.5):
        m.prism_f("reb_wall", fx, 0, 3.2, 1.2, 3.2, seg=8)  # horizontal tank
        m.box("reb_dark", fx, -1.3, 1.2, 0.5, 2.4, 2.4)     # saddles
        m.box("reb_dark", fx, 1.3, 1.2, 0.5, 2.4, 2.4)
    return m


def build_reb_masts():
    m = Model("reb_masts")
    for fx, ly in ((5, -6), (-3, 3), (7, 8)):
        m.box("reb_dark", fx, ly, 5.0, 0.35, 0.35, 10.0)
        m.box("reb_dark", fx, ly, 9.7, 2.4, 0.25, 0.25)
        m.box("reb_red", fx, ly, 10.3, 0.5, 0.5, 0.5)
    return m


def build_emp_deck():
    m = Model("emp_deck")
    m.box("emp_grey", 0, 0, 4.7, 26, 26, 0.6)         # deck slab, top +5.0
    for sf in (-11, 0, 11):
        for sl in (-11, 11):
            m.box("emp_dark", sf, sl, 2.35, 1.1, 1.1, 4.7)   # legs
    m.box("emp_dark", 0, 0, 5.175, 7.0, 7.0, 0.35)    # pad center plate
    for sf in (-1, 1):
        for sl in (-1, 1):
            m.box("emp_green", sf * 12.2, sl * 12.2, 5.15, 0.8, 0.8, 0.3)
    return m


def build_emp_frame():
    m = Model("emp_frame")
    for ly in (-15, 15):                               # pylon walls
        m.box("emp_grey", 10, ly, 15.0, 5.0, 5.0, 30.0)
    m.box("emp_grey", 10, 0, 31.5, 5.0, 35.0, 3.0)    # top beam
    m.box("emp_dark", 14.5, 0, 15.0, 1.0, 35.0, 30.0)  # back wall
    # Roof plates sit clear above the carrier hull (top +11.5) and the
    # bridge (top +14.1, and the bridge is forward of the plates' f range):
    # no static penetration of the hull fragment standing inside.
    for cz in (14.0, 18, 26):                           # roof plates
        m.box("emp_grey", 2, 0, cz, 20.0, 34.0, 1.5)
    return m


def build_emp_carrier():
    m = Model("emp_carrier")
    # Static carrier bow fragment; nose toward the model front (-f).  The
    # stern is trimmed to f=+13.5 so the hull stays inside the frame's
    # interior (back wall inner face at f=+14) with clearance.
    m.wedge("emp_grey", -25.0, 13.5, 2.0, 6.6, 5.1, 11.5)  # hull, bow at -f
    m.box("emp_dark", -18, 0, 12.8, 12.0, 8.0, 2.6)    # bridge above bow
    m.box("emp_dark", -23, 0, 15.1, 4.0, 4.0, 2.0)     # tower
    m.box("emp_green", -2, 0, 11.65, 10.0, 0.5, 0.3)   # green spine stripe
    for fx in (-18, 8):                                # support pylons
        for ly in (-5, 5):
            m.box("emp_dark", fx, ly, 2.55, 1.4, 1.4, 5.1)
    return m


def build_emp_rack():
    m = Model("emp_rack")
    m.box("emp_dark", 0, -6, 4.0, 0.8, 0.8, 8.0)
    m.box("emp_dark", 0, 6, 4.0, 0.8, 0.8, 8.0)
    m.box("emp_dark", 0, 0, 8.3, 1.2, 13.0, 0.6)       # top beam
    for ly, lz in ((-4.0, 5.2), (0.0, 4.4), (4.0, 5.2)):
        m.box("emp_dark", 0, ly, 6.8, 0.2, 0.2, 2.6)   # cable
        m.octa("emp_green", 0, ly, lz, 0.9, 0.9, 1.3)  # hanging pod
    return m


MODELS = {
    "reb_platform": build_reb_platform,
    "reb_hangar": build_reb_hangar,
    "reb_tower": build_reb_tower,
    "reb_tanks": build_reb_tanks,
    "reb_masts": build_reb_masts,
    "emp_deck": build_emp_deck,
    "emp_frame": build_emp_frame,
    "emp_carrier": build_emp_carrier,
    "emp_rack": build_emp_rack,
}

# ---------------------------------------------------------------------------
# Placements: east/north meters from the faction anchor, intended true
# heading (degrees clockwise from north), roles and notes.

def _place(pid, faction, model, east, north, heading_cw, role, deck_top=None,
           note=""):
    return {"id": pid, "faction": faction, "model": model, "role": role,
            "offset_east_m": float(east), "offset_north_m": float(north),
            "heading_deg": float(heading_cw), "deck_top_above_anchor_m":
            deck_top, "note": note}


PLACEMENTS = [
    _place("reb_platform", "rebels", "reb_platform", 0, 0, 250,
           "ground_pad", deck_top=6.0,
           note="rebels ground pad; deck top +6.0 m design AGL above the "
                "anchor; experimental support surface"),
    _place("reb_hangar", "rebels", "reb_hangar", 60, 0, 70,
           "decor_structure",
           note="low sand/white hangar; door face points at the pad"),
    _place("reb_tower", "rebels", "reb_tower", 25, -45, 250,
           "decor_structure", note="control tower silhouette"),
    _place("reb_tanks", "rebels", "reb_tanks", 18, 40, 250, "decor",
           note="fuel tank group"),
    _place("reb_masts", "rebels", "reb_masts", 40, -70, 250, "decor",
           note="light masts, behind the pad (outside the exit corridor)"),
    _place("emp_deck", "empire", "emp_deck", 0, 0, 250, "ground_pad",
           deck_top=5.0,
           note="empire foredeck; deck top +5.0 m design AGL above the "
                "anchor; experimental support surface"),
    _place("emp_frame", "empire", "emp_frame", 70, 0, 250,
           "decor_structure",
           note="static hangar frame east of the deck, opening faces west "
                "toward the deck; never moves (static model)"),
    _place("emp_carrier", "empire", "emp_carrier", 70, 0, 250,
           "decor_static_carrier",
           note="immobile carrier hull fragment standing inside the frame "
                "on its own ground pylons; bow points west at the deck; "
                "first TASK4 test keeps the carrier static/decorative"),
    _place("emp_rack_n", "empire", "emp_rack", 35, 55, 250, "decor",
           note="decorative pylon rack with hanging pods"),
    _place("emp_rack_s", "empire", "emp_rack", 35, -55, 70, "decor",
           note="decorative pylon rack with hanging pods"),
]

# The only pair whose 3D footprints may intersect: the carrier stands
# inside the hangar frame envelope.  Their AABBs nest by design, but the
# geometry is kept clearance-clean (hull stern f<=13.5 vs back wall f>=14;
# roof plates above hull/bridge tops), and both models are static
# decoration; nothing here claims structural support or FG collision.
STACK_PAIRS = [
    {"lower": "emp_frame", "upper": "emp_carrier",
     "reason": "the static carrier fragment stands inside the static "
               "hangar frame envelope on its own ground pylons; the AABB "
               "nesting is intentional and the meshes keep clearance (bow "
               "points west out of the open front); both are decoration, "
               "support/collision was never tested against FG"},
]

# ---------------------------------------------------------------------------
# Poses: exactly one primary per faction + safe c172p fallbacks.

POSES = [
    {"id": "pose_rebels_primary", "faction": "rebels", "primary": True,
     "kind": "ground_deck", "on_placement": "reb_platform",
     "lon": REBELS_ANCHOR[0], "lat": REBELS_ANCHOR[1], "heading_deg": 250.0,
     "alt": {"value_m": round(6.0 + GEAR_ALLOWANCE_M, 2),
             "datum": "design_agl_above_anchor_m",
             "meaning": "meters above the design terrain plane at the rebel "
                        "anchor (deck top +6.0 m + CG allowance); FG spawn "
                        "altitude in MSL = unknown terrain height + this"},
     "experimental": True,
     "note": "future sa_xwing spawn over the rebel pad; deck support NOT "
             "verified - until a physical FG test use the fallback poses"},
    {"id": "pose_empire_primary", "faction": "empire", "primary": True,
     "kind": "ground_deck", "on_placement": "emp_deck",
     "lon": EMPIRE_ANCHOR[0], "lat": EMPIRE_ANCHOR[1], "heading_deg": 250.0,
     "alt": {"value_m": round(5.0 + GEAR_ALLOWANCE_M, 2),
             "datum": "design_agl_above_anchor_m",
             "meaning": "same datum as pose_rebels_primary"},
     "experimental": True,
     "note": "future sa_tie spawn over the empire pad, carrier bow ahead at "
             "250 deg; deck support NOT verified"},
    {"id": "fb_ksfo_parking", "faction": None, "primary": False,
     "kind": "airport_parking", "airport": "KSFO", "parking_id": None,
     "heading_deg": None, "experimental": False,
     "note": "fallback 1: stock KSFO ramp start; identical to the unchanged "
             "tools/launch.py default; works with or without the arena"},
    {"id": "fb_ksfo_rwy28r", "faction": None, "primary": False,
     "kind": "airport_runway", "airport": "KSFO", "runway": "28R",
     "heading_deg": None, "experimental": False,
     "note": "fallback 2: KSFO runway 28R (launcher default runway); the "
             "runway heading comes from FGData, not restated here"},
    {"id": "fb_airstart_arena", "faction": None, "primary": False,
     "kind": "aircraft_position",
     "lon": -122.5200, "lat": 37.4200, "heading_deg": 70.0,
     "alt": {"value_ft": 2500, "datum": "msl_feet"},
     "speed_kt_ias": 90, "experimental": False,
     "note": "fallback 3: external air-start over the bay for the c172p "
             "visual/scale check, 2500 ft MSL (FG --altitude datum); keeps "
             "terrain clearance while FG terrain is below ~2000 ft AGL "
             "(terrain NOT verified here); fly east toward the bases, then "
             "land back at KSFO"},
]

ROUTES = [
    {"id": "route_rebels_exit", "faction": "rebels",
     "from_pose": "pose_rebels_primary",
     "origin": [REBELS_ANCHOR[0], REBELS_ANCHOR[1]], "heading_deg": 250.0,
     "half_width_m": ROUTE_HALF_WIDTH_M, "length_m": ROUTE_LENGTH_M,
     "floor_above_deck_m": ROUTE_FLOOR_ABOVE_DECK_M,
     "note": "straight exit west-southwest over the open coastal plain to "
             "the Pacific; clear of the empire base and of KSFO"},
    {"id": "route_empire_exit", "faction": "empire",
     "from_pose": "pose_empire_primary",
     "origin": [EMPIRE_ANCHOR[0], EMPIRE_ANCHOR[1]], "heading_deg": 250.0,
     "half_width_m": ROUTE_HALF_WIDTH_M, "length_m": ROUTE_LENGTH_M,
     "floor_above_deck_m": ROUTE_FLOOR_ABOVE_DECK_M,
     "note": "parallel exit west-southwest ~1.7 km south of the rebel "
             "route; static separation of the two corridors is checked by "
             "tools/test_space_arena.py"},
]

SURFACES = [
    {"placement_id": "reb_platform", "surface": "deck_top",
     "height_above_anchor_m": 6.0,
     "support": "unverified", "collision": "unverified",
     "status": "experimental_deck",
     "note": "FG gives static scenery models no landing support by default; "
             "no scenery material override is configured; do NOT treat "
             "this as a supported surface until a physical FG test"},
    {"placement_id": "emp_deck", "surface": "deck_top",
     "height_above_anchor_m": 5.0,
     "support": "unverified", "collision": "unverified",
     "status": "experimental_deck", "note": "same caveat as reb_platform"},
    {"placement_id": "emp_carrier", "surface": "hull_and_pylons",
     "support": "decorative_only", "collision": "unverified",
     "status": "decoration_not_a_surface"},
    {"placement_id": None, "surface": "stock_terrain_at_anchors",
     "height_above_anchor_m": 0.0,
     "support": "depends_on_fg_terrain_mesh", "collision": "depends_on_fg",
     "status": "outside_this_package",
     "note": "the natural terrain under the pads is the surface to verify "
             "first in FG before any deck test"},
    {"placement_id": None, "surface": "bay_water_west",
     "height_above_anchor_m": 0.0,
     "support": "not_supporting_for_wheels", "collision": "unverified",
     "status": "outside_this_package"},
]


# ---------------------------------------------------------------------------
def world_enu_box(local_aabb, heading_deg, east_m, north_m):
    """Axis-aligned ENU box (meters, relative to the faction anchor).

    Design (f,l) corners rotated by the clockwise heading; headings here
    are multiples of 10 deg, but the corner transform is general.
    """
    a = math.radians(heading_deg)
    sin_h, cos_h = math.sin(a), math.cos(a)
    es, ns = [], []
    for f in local_aabb["f"]:
        for l in local_aabb["l"]:
            es.append(f * sin_h - l * cos_h + east_m)
            ns.append(f * cos_h + l * sin_h + north_m)
    return {"east_m": [round(min(es), 3), round(max(es), 3)],
            "north_m": [round(min(ns), 3), round(max(ns), 3)],
            "up_m": local_aabb["u"]}


def anchor_of(faction):
    return REBELS_ANCHOR if faction == "rebels" else EMPIRE_ANCHOR


def xml_wrapper(name):
    return ('<?xml version="1.0"?>\n'
            "<!-- space_arena greybox model wrapper (TASK4; own geometry;\n"
            "     GPL-2.0-or-later). Direct <path> form per the FGData\n"
            "     shared-model dialect: loads the sibling .ac with identity\n"
            "     offsets so the AC3D axes stay the documented source.\n"
            "     NOT runtime-verified: FlightGear is absent on this host. -->\n"
            "<PropertyList>\n"
            "  <path>%s.ac</path>\n"
            "</PropertyList>\n" % name)


def stg_text(entries):
    lines = ["# space_arena greybox placements (TASK4, own geometry).",
             "# OBJECT_STATIC_AGL: offset field = meters above ground level",
             "# at the anchor, written 0.0 for every placement (terrain MSL",
             "# was never measured here; see manifest.json/VALIDATION.md).",
             "# heading = counter-clockwise degrees from north",
             "# (Docs/README.scenery); intended true bearings are in the",
             "# manifest. NOT verified against a running FlightGear 2024.1."]
    for p in entries:
        lines.append("OBJECT_STATIC_AGL Models/space_arena/%s.xml %.6f "
                     "%.6f %.1f %.2f"
                     % (p["model"], p["lon"], p["lat"], p["agl_offset_m"],
                        p["stg_heading_deg"]))
    return "\n".join(lines) + "\n"


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def build_placements(meshes):
    out = []
    for p in PLACEMENTS:
        anchor = anchor_of(p["faction"])
        lon, lat = enu_to_lonlat(anchor, p["offset_east_m"],
                                 p["offset_north_m"])
        mesh = meshes[p["model"]]
        item = dict(p)
        item.pop("note", None)
        item["lon"] = lon
        item["lat"] = lat
        item["agl_offset_m"] = STG_AGL_OFFSET_M
        item["agl_semantics"] = (
            "STG entry is OBJECT_STATIC_AGL: the offset field carries "
            "meters ABOVE GROUND LEVEL at the anchor and is written "
            "%.1f for every placement, i.e. the model origin sits on the "
            "terrain/water surface. This is an AGL offset, NOT an MSL "
            "elevation: the terrain MSL at the anchors was never measured "
            "here (band %s..%s m design, UNVERIFIED); the FG runtime "
            "semantics of the field are also unverified"
            % (STG_AGL_OFFSET_M, TERRAIN_DESIGN_BAND_M[0],
               TERRAIN_DESIGN_BAND_M[1]))
        item["stg_heading_deg"] = round(
            heading_cw_to_stg_ccw(p["heading_deg"]), 2)
        item["heading_semantics"] = (
            "heading_deg = intended true bearing clockwise from north; "
            "stg_heading_deg = value written to the STG, counter-clockwise "
            "from north per Docs/README.scenery; runtime sign NOT verified")
        item["local_size_m"] = mesh.design_aabb()
        item["world_enu_box_m"] = world_enu_box(mesh.design_aabb(),
                                                p["heading_deg"],
                                                p["offset_east_m"],
                                                p["offset_north_m"])
        item["note"] = p["note"]
        out.append(item)
    return out


def build_manifest(placements, meshes, stg_rel, artifact_sha):
    per_model = {name: meshes[name].triangles() for name in MODELS}
    total = sum(per_model.values())
    return {
        "schema": "bombable.space_arena.manifest",
        "schema_version": 1,
        "package": "scenery/space_arena",
        "task": "TASK4",
        "fg_target_version": FG_TARGET,
        "status_banner": "GREYBOX FIRST INCREMENT - NOT runtime-verified; "
                         "FlightGear 2024.1.7 is absent on this host. "
                         "See README.md and VALIDATION.md.",
        "units": "meters for all lengths; WGS84 decimal degrees for lon/lat "
                 "(west/south negative); degrees for angles; knots for "
                 "airspeeds",
        "axes": {
            "model_file_axes": "AC3Db as stored: +x aft, +y up, +z left; "
                               "model front = -x",
            "design_axes": "f forward (= -x), l left (= +z), u up (= +y)",
            "fg_axes": "FG model frame: x aft(+), y right(+), z up(+); "
                       "AC3D->FG: x->x, y->z, z->-y (wiki Howto:3D "
                       "Aircraft Models)",
            "placement_heading": "manifest heading_deg = intended true "
                                 "bearing clockwise from north; "
                                 "stg_heading_deg = value written to the "
                                 "STG (counter-clockwise from north per "
                                 "Docs/README.scenery); runtime sign NOT "
                                 "verified",
        },
        "region": {
            "district": "Half Moon Bay coastal plain, one tile district "
                        "~20 km south of KSFO, entirely inside the default "
                        "scenery tile w123n37; no stock airport inside the "
                        "site box",
            "tile": {"bucket": BUCKET, "tile_1x1": TILE,
                     "lon_range_deg": list(TILE_LON_RANGE),
                     "lat_range_deg": list(TILE_LAT_RANGE),
                     "bucket_note": "for negative longitudes SimGear floors "
                                    "lon/10 downward, so tile w123n37 lives "
                                    "in bucket w130n30 (FGData release/"
                                    "2024.1 Docs/README.scenery shows the "
                                    "same bucket for KSFO)"},
            "site_bbox_deg": {"lon_min": -122.4700, "lon_max": -122.4150,
                              "lat_min": 37.4050, "lat_max": 37.4400},
            "anchors": {"rebels": {"lon": REBELS_ANCHOR[0],
                                   "lat": REBELS_ANCHOR[1]},
                        "empire": {"lon": EMPIRE_ANCHOR[0],
                                   "lat": EMPIRE_ANCHOR[1]}},
            "reference_points_not_from_fgdata": {
                "KSFO_arp": {"lon": KSFO_ARP[0], "lat": KSFO_ARP[1]},
                "KHAF_arp": {"lon": KHAF_ARP[0], "lat": KHAF_ARP[1]},
                "note": "approximate ARPs from general knowledge; used only "
                        "for conservative separation reasoning"},
        },
        "elevation_definition": {
            "stg_elevation_field": "STG entries are OBJECT_STATIC_AGL: the "
                                   "offset field carries meters ABOVE GROUND "
                                   "LEVEL at the anchor and is written %.1f "
                                   "for every placement, so each model "
                                   "origin sits on the terrain/water "
                                   "surface. 0.0 here is an AGL offset "
                                   "choice, NOT an MSL elevation: terrain "
                                   "MSL at the anchors was never measured "
                                   "in this environment; after a FG/UFO "
                                   "measurement the package can switch to "
                                   "exact-MSL OBJECT_STATIC entries"
                                   % STG_AGL_OFFSET_M,
            "agl_offset_m": STG_AGL_OFFSET_M,
            "terrain_height_band_m": {"min": TERRAIN_DESIGN_BAND_M[0],
                                      "max": TERRAIN_DESIGN_BAND_M[1],
                                      "verified": False,
                                      "source": "coastal-terrace design "
                                                "band; no FG install and "
                                                "no elevation service "
                                                "reachable in this build "
                                                "environment"},
            "pose_datums": "every pose altitude carries an explicit datum: "
                           "design_agl_above_anchor_m (meters) or msl_feet "
                           "(FG --altitude); AGL and MSL are never mixed",
        },
        "placements": placements,
        "stack_pairs": STACK_PAIRS,
        "keepouts": [
            {"faction": "rebels",
             "center": {"lon": REBELS_ANCHOR[0], "lat": REBELS_ANCHOR[1]},
             "radius_m": REBEL_KEEPOUT_M,
             "note": "other-faction exclusion zone (design value)"},
            {"faction": "empire",
             "center": {"lon": EMPIRE_ANCHOR[0], "lat": EMPIRE_ANCHOR[1]},
             "radius_m": EMPIRE_KEEPOUT_M,
             "note": "larger zone: the hangar frame + carrier group"},
        ],
        "poses": POSES,
        "routes": ROUTES,
        "surfaces": SURFACES,
        "stg": {"path": stg_rel,
                "entry_kind": "OBJECT_STATIC_AGL",
                "entry_fields": "OBJECT_STATIC_AGL <model.xml> <lon> <lat> "
                                "<agl_offset_m> <heading_deg>",
                "agl_offset_m": STG_AGL_OFFSET_M,
                "agl_note": "the offset is meters above ground level at the "
                            "anchor, written 0.0 because terrain MSL at the "
                            "anchors is unmeasured; NOT runtime-verified",
                "heading_note": "written heading is counter-clockwise "
                                "degrees from north per Docs/"
                                "README.scenery",
                "path_base": "model paths are relative to the tile "
                             "directory containing the STG (per $FG_ROOT/"
                             "Docs/README.scenery)"},
        "models": {
            name: {
                "ac": "Objects/%s/%s/Models/space_arena/%s.ac"
                      % (BUCKET, TILE, name),
                "xml": "Objects/%s/%s/Models/space_arena/%s.xml"
                       % (BUCKET, TILE, name),
                "triangles": per_model[name],
                "local_aabb_m": meshes[name].design_aabb(),
                "author": "TASK4 subagent (Bombable team)",
                "license": "GPL-2.0-or-later",
                "provenance": "own geometry authored in "
                              "src/generate_package.py; the .ac is its "
                              "deterministic build artifact",
                "textures": "none (solid AC3Db materials only)",
            } for name in MODELS},
        "budget": {"triangle_limit": TRI_LIMIT, "triangles_total": total,
                   "per_model": per_model,
                   "note": "arena-only working budget (no cockpit); large "
                           "margin kept below the limit"},
        "provenance": {
            "author": "TASK4 subagent (Bombable team)",
            "license": "GPL-2.0-or-later",
            "original_geometry_source": "src/generate_package.py (the "
                                        "script is the authored original; "
                                        ".ac/.xml/.stg files are committed "
                                        "deterministic artifacts and are "
                                        "never hand-edited)",
            "external_assets": "none - no textures, no binaries, no "
                               "extracted or third-party game content",
            "generator_sha256": None,      # filled below
            "artifact_sha256": artifact_sha,
        },
        "verification": {
            "static_package": "python3 -m unittest tools/test_space_arena.py "
                              "(JSON/XML/AC3Db structure, ids, WGS84 and "
                              "elevation/heading conventions, bucket, "
                              "triangle budget, clearance invariants, "
                              "sha256 + determinism)",
            "fg_runtime_load": "NOT VERIFIED - FlightGear 2024.1.7 is not "
                               "installed on this host",
            "visual": "NOT VERIFIED - nothing was ever rendered; no "
                      "screenshots exist",
            "support_surfaces": "NOT VERIFIED - both decks are marked "
                                "experimental",
            "collision": "NOT VERIFIED - no physical test was possible",
            "collisions_globally_disabled": False,
            "launcher_compatibility": "the stock tools/launch.py defaults "
                                      "(c172p, KSFO, 28R) are untouched; "
                                      "the arena is an ADDITIONAL "
                                      "--fg-scenery path only (design "
                                      "compatibility, not runtime-proven)",
        },
        "launch": {
            "default_unchanged": "python3 tools/launch.py",
            "with_arena_scenery": (
                "fgfs --aircraft=c172p --airport=KSFO --runway=28R "
                "--fg-scenery=<repo>/scenery/space_arena "
                "--fg-scenery=<FGDATA>/data/Scenery"),
            "note": "list the arena directory before the stock Scenery "
                    "directory; FG loads terrain from the first path that "
                    "has it and merges object tiles visited on the way "
                    "(README.scenery); semantics NOT re-verified on "
                    "2024.1.7 here",
        },
        "screenshots": "scenery/space_arena/screenshots/ (directory is "
                       "created by the first host-side FG acceptance run; no "
                       "images are committed because nothing has been "
                       "rendered yet)",
    }


def generate(root=None):
    """Write all artifacts below the package root; return the manifest."""
    root = os.path.abspath(root or PKG_ROOT)
    tile_dir = os.path.join(root, "Objects", BUCKET, TILE)
    models_dir = os.path.join(tile_dir, "Models", "space_arena")
    os.makedirs(models_dir, exist_ok=True)

    meshes = {name: build() for name, build in MODELS.items()}
    artifact_sha = {}
    for name in MODELS:
        ac_rel = "Objects/%s/%s/Models/space_arena/%s.ac" % (BUCKET, TILE,
                                                             name)
        xml_rel = ac_rel[:-3] + ".xml"
        with open(os.path.join(root, ac_rel), "w", encoding="ascii",
                  newline="\n") as fh:
            fh.write(meshes[name].ac_text())
        with open(os.path.join(root, xml_rel), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(xml_wrapper(name))
        artifact_sha[ac_rel] = file_sha(os.path.join(root, ac_rel))
        artifact_sha[xml_rel] = file_sha(os.path.join(root, xml_rel))

    placements = build_placements(meshes)
    # Group placements by their official SimGear sub-tile id (all land in
    # 942042 for this site, but the grouping keeps the generator correct if
    # anchors ever move).
    by_stg = {}
    for p in placements:
        by_stg.setdefault(subtile_id(p["lon"], p["lat"]), []).append(p)
    if len(by_stg) != 1:
        raise RuntimeError("unexpected multi-tile site: %s" % sorted(by_stg))
    stg_id = next(iter(by_stg))
    stg_rel = "Objects/%s/%s/%d.stg" % (BUCKET, TILE, stg_id)
    with open(os.path.join(root, stg_rel), "w", encoding="ascii",
              newline="\n") as fh:
        fh.write(stg_text(by_stg[stg_id]))
    artifact_sha[stg_rel] = file_sha(os.path.join(root, stg_rel))

    manifest = build_manifest(placements, meshes, stg_rel,
                              {k: artifact_sha[k] for k in sorted(
                                  artifact_sha)})
    manifest["provenance"]["generator_sha256"] = file_sha(__file__)

    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return manifest


def main():
    manifest = generate()
    print("generated %d models, %d triangles (limit %d), stg %s"
          % (len(MODELS), manifest["budget"]["triangles_total"], TRI_LIMIT,
             manifest["stg"]["path"]))


if __name__ == "__main__":
    main()
