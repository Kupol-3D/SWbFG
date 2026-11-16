#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Static delivery checks for the committed TIE FlightGear packages."""
import re
import math
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from test_sa_tie import read_ac as _read_ac_raw  # noqa: E402

_AC_CACHE = {}
def read_ac(path):
    path = Path(path)
    if path in _AC_CACHE:
        return _AC_CACHE[path]
    res = _read_ac_raw(path)
    _AC_CACHE[path] = res
    return res

PACKAGES = {
    "sa_tie_royal_guard": {
        "asset": "royal_guard",
        "tris": 106559,
        "dims": (9.6, 10.237489700317383, 6.1793437004089355),
    },
    "sa_tie_m1": {
        "asset": "m1_bizarro",
        "tris": 86161,
        "dims": (6.299999952316284, 7.3850932121276855, 6.976900577545166),
    },
    "sa_tie_boarding": {
        "asset": "boarding_craft",
        "tris": 180275,
        "dims": (7.600000858306885, 9.27566146850586, 4.856714248657227),
    },
}

PERSONAL_PATH_RE = re.compile(r"/home/[A-Za-z]|/Users/[A-Za-z]")

_LOD_CACHE = {}


def _lod_files(package, asset):
    base = ROOT / "Aircraft" / package / "Models"
    return [base / f"{asset}.ac",
            base / f"{asset}_lod1.ac",
            base / f"{asset}_lod2.ac"]


def _parse_lod(path):
    path = Path(path)
    if path in _LOD_CACHE:
        return _LOD_CACHE[path]
    materials, objects = read_ac(path)
    tris = 0
    area = 0.0
    pts = []
    for obj in objects.values():
        for indices, _, _ in obj["faces"]:
            tris += 1
            a, b, c = (obj["vertices"][i] for i in indices)
            ab = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
            ac = (c[0]-a[0], c[1]-a[1], c[2]-a[2])
            cr = (ab[1]*ac[2]-ab[2]*ac[1],
                  ab[2]*ac[0]-ab[0]*ac[2],
                  ab[0]*ac[1]-ab[1]*ac[0])
            tri_area = 0.5 * math.sqrt(cr[0]*cr[0]+cr[1]*cr[1]+cr[2]*cr[2])
            area += tri_area
            # degenerate check
            assert tri_area > 0, (path, "degenerate triangle")
            for pt in (a, b, c):
                pts.append(pt)
                for v in pt:
                    assert math.isfinite(v), (path, "NaN/inf")
    for m in materials:
        t = m["trans"][0]
        assert t == 0.0, (path, m["name"], t)
    bbox = [(min(p[i] for p in pts), max(p[i] for p in pts)) for i in range(3)]
    dims = tuple(mx - mn for mn, mx in bbox)
    center = tuple((mn + mx) / 2.0 for mn, mx in bbox)
    result = {"tris": tris, "area": area, "dims": dims, "center": center,
            "materials": materials, "objects": objects}
    _LOD_CACHE[path] = result
    return result


class TestTieDelivery(unittest.TestCase):
    def _ac(self, package):
        info = PACKAGES[package]
        return read_ac(ROOT / "Aircraft" / package / "Models" / (info["asset"] + ".ac"))

    def test_packages_exist_with_required_files(self):
        for package in PACKAGES:
            base = ROOT / "Aircraft" / package
            self.assertTrue(base.is_dir(), package)
            self.assertTrue((base / f"{package}-set.xml").is_file())
            self.assertTrue((base / "Effects" / "visual.eff").is_file())
            self.assertTrue((base / "Config" / "flight_profile.json").is_file())
            self.assertTrue(any((base / "Models").glob("*.ac")))
            info = PACKAGES[package]
            self.assertTrue((base / "Models" / (info["asset"] + ".ac")).is_file())

    def test_ac_and_cockpit_objects_present(self):
        for package in PACKAGES:
            materials, objects = self._ac(package)
            self.assertIn("Cockpit", objects, package)
            # main .ac is the parsed file itself; cockpit sub-objects exist
            radial = [n for n in objects if "WindowRadial_" in n]
            self.assertEqual(len(radial), 8, (package, len(radial)))
            self.assertTrue(any("WindowOuterRim" in n for n in objects), package)
            self.assertTrue(any("WindowCentralOctagon" in n for n in objects), package)
            self.assertFalse(any("WindowPane" in n or "Glass" in n for n in objects), package)

    def test_materials_have_no_transparency(self):
        for package in PACKAGES:
            materials, objects = self._ac(package)
            self.assertTrue(materials, package)
            for material in materials:
                self.assertEqual(material["trans"][0], 0.0,
                                 (package, material["name"]))

    def test_triangle_budget_matches_metrics(self):
        for package, info in PACKAGES.items():
            materials, objects = self._ac(package)
            total = sum(len(obj["faces"]) for obj in objects.values())
            self.assertEqual(total, info["tris"], (package, total))

    def test_dimensions_match_claimed_lwh(self):
        for package, info in PACKAGES.items():
            materials, objects = self._ac(package)
            points = [p for obj in objects.values() for p in obj["vertices"]]
            dims = tuple(max(p[i] for p in points) - min(p[i] for p in points)
                         for i in range(3))
            for actual, target in zip(dims, info["dims"]):
                self.assertLessEqual(abs(actual - target), 1e-4,
                                     (package, actual, target))

    def test_lod_triangle_counts_monotonic_and_floor(self):
        for package, info in PACKAGES.items():
            asset = info["asset"]
            lods = [_parse_lod(p) for p in _lod_files(package, asset)]
            self.assertGreater(lods[0]["tris"], lods[1]["tris"], package)
            self.assertGreater(lods[1]["tris"], lods[2]["tris"], package)
            self.assertGreaterEqual(lods[2]["tris"], 1000, package)

    def test_lod_surface_area_within_25_percent(self):
        for package, info in PACKAGES.items():
            asset = info["asset"]
            lods = [_parse_lod(p) for p in _lod_files(package, asset)]
            areas = [l["area"] for l in lods]
            for a, b in ((areas[0], areas[1]), (areas[1], areas[2])):
                diff = abs(a - b) / max(a, b)
                self.assertLessEqual(diff, 0.25, (package, diff))

    def test_lod_bounding_boxes_match(self):
        for package, info in PACKAGES.items():
            asset = info["asset"]
            lods = [_parse_lod(p) for p in _lod_files(package, asset)]
            for l in lods[1:]:
                for i in range(3):
                    self.assertLessEqual(abs(l["dims"][i] - lods[0]["dims"][i]),
                                         1e-2, (package, i))

    def test_lod_centers_not_shifted(self):
        for package, info in PACKAGES.items():
            asset = info["asset"]
            lods = [_parse_lod(p) for p in _lod_files(package, asset)]
            for l in lods[1:]:
                for i in range(3):
                    self.assertLessEqual(abs(l["center"][i] - lods[0]["center"][i]),
                                         0.01, (package, i))

    def test_no_degenerate_or_nan_triangles(self):
        for package, info in PACKAGES.items():
            asset = info["asset"]
            for path in _lod_files(package, asset):
                lod = _parse_lod(path)  # raises on degenerate/NaN
                self.assertGreater(lod["tris"], 0)

    def test_textures_referenced_exist(self):
        for package in PACKAGES:
            base = ROOT / "Aircraft" / package / "Models"
            materials, objects = read_ac(base / (PACKAGES[package]["asset"] + ".ac"))
            for m in materials:
                tex = m.get("texture")
                if tex and tex != "-1":
                    self.assertTrue((base / tex).is_file(), (package, tex))

    def test_set_xml_viewer_contract(self):
        for package in PACKAGES:
            root = ET.parse(ROOT / "Aircraft" / package / f"{package}-set.xml").getroot()
            self.assertEqual(root.findtext("sim/flight-model"), "null", package)
            views = root.findall("sim/view")
            self.assertGreaterEqual(len(views), 2, package)
            v0 = views[0].find("config")
            self.assertEqual(views[0].findtext("internal"), "true", package)
            self.assertEqual(v0.findtext("default-field-of-view-deg") is not None, True)
            fov0 = float(v0.findtext("default-field-of-view-deg"))
            near0 = float(v0.findtext("ground-level-nearplane-m"))
            self.assertGreaterEqual(fov0, 75.0)
            self.assertLessEqual(fov0, 80.0)
            self.assertGreaterEqual(near0, 0.03)
            self.assertLessEqual(near0, 0.05)
            self.assertEqual(views[1].findtext("type"), "lookat", package)

    def test_m1_remote_noncanon_and_boarding_right_module(self):
        m1_readme = (ROOT / "Aircraft" / "sa_tie_m1" / "README.md").read_text()
        m1_set = (ROOT / "Aircraft" / "sa_tie_m1" / "sa_tie_m1-set.xml").read_text()
        combined = m1_readme + "\n" + m1_set
        self.assertIn("remote", combined.lower())
        self.assertIn("NON_CANON", combined)
        # boarding: pilot camera in right module
        boarding_set = ET.parse(ROOT / "Aircraft" / "sa_tie_boarding" /
                                "sa_tie_boarding-set.xml").getroot()
        v0 = boarding_set.findall("sim/view")[0].find("config")
        xoff = float(v0.findtext("x-offset-m"))
        self.assertGreater(xoff, 1.0)
        self.assertLess(xoff, 2.3)

    def test_package_text_files_have_no_personal_paths(self):
        for package in PACKAGES:
            base = ROOT / "Aircraft" / package
            candidates = [base / "README.md", base / "LICENSE_ATTRIBUTION.md",
                          base / "Sources" / "SHA256SUMS",
                          base / "Sources" / "BUILD_MANIFEST.json"]
            candidates += sorted((base / "Config").glob("*.xml")) if (base / "Config").is_dir() else []
            for path in candidates:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertIsNone(PERSONAL_PATH_RE.search(text), str(path))

if __name__ == "__main__":
    unittest.main()
