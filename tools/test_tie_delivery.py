#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Static delivery checks for the committed TIE FlightGear packages."""
import hashlib
import json
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from test_sa_tie import read_ac  # noqa: E402

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

STL_NAMES = {
    "royal_guard": "TIE_Royal_guard.stl",
    "m1_bizarro": "TIE_Bizarro.stl",
    "boarding_craft": "TIE_Boarding_craft.stl",
}

ASSET_PACK = Path(os.environ.get("TIE_ASSET_PACK",
                                ROOT.parent / "TIE_Game_Asset_Pack_v1"))
ASSET_PACK_AVAILABLE = (ASSET_PACK / "ships").is_dir()

PERSONAL_PATH_RE = re.compile(r"/home/[A-Za-z]|/Users/[A-Za-z]")


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

    @unittest.skipUnless(ASSET_PACK_AVAILABLE, "asset pack not available")
    def test_source_stl_hashes_match_external_pack(self):
        for asset, stl_name in STL_NAMES.items():
            # map asset id to package? asset ids are royal_guard etc.
            pkg_dir = None
            for p in PACKAGES:
                if PACKAGES[p]["asset"] == asset:
                    pkg_dir = p
                    break
            stl_path = ASSET_PACK / "ships" / asset / "source" / "files" / stl_name
            self.assertTrue(stl_path.is_file(), str(stl_path))
            sha = hashlib.sha256(stl_path.read_bytes()).hexdigest()
            meta = json.loads((ASSET_PACK / "ships" / asset / "working" /
                               "scale_and_axes.json").read_text())
            self.assertEqual(sha, meta["source_sha256"], asset)
            self.assertEqual(len(sha), 64)


if __name__ == "__main__":
    unittest.main()
