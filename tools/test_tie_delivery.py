#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Static delivery checks for the TIE FlightGear packages."""
import json
import unittest
import xml.etree.ElementTree as ET
import re
from pathlib import Path
from tools.test_sa_tie import ray_hit

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT.parent / "TIE_Game_Asset_Pack_v1"
PACKAGES = ("sa_tie_royal_guard", "sa_tie_m1", "sa_tie_boarding")
SHIPS = ("royal_guard", "m1_bizarro", "boarding_craft")


class TestTieDelivery(unittest.TestCase):
    def test_packages_exist_with_required_files(self):
        for pkg in PACKAGES:
            base = ROOT / "Aircraft" / pkg
            self.assertTrue(base.is_dir(), pkg)
            self.assertTrue((base / f"{pkg}-set.xml").is_file())
            self.assertTrue((base / "Effects" / "visual.eff").is_file())
            self.assertTrue((base / "Config" / "flight_profile.json").is_file())
            self.assertTrue(any((base / "Models").glob("*.ac")))
            self.assertTrue(any((base / "Models").glob("*-collision.glb")))

    def test_set_xml_keeps_null_fdm_by_default(self):
        for pkg in PACKAGES:
            root = ET.parse(ROOT / "Aircraft" / pkg / f"{pkg}-set.xml").getroot()
            self.assertEqual(root.findtext("sim/flight-model"), "null")

    def test_boarding_integration_is_actual_cockpit(self):
        integration = json.loads((PACK / "ships" / "boarding_craft" / "export" / "integration.json").read_text())
        cam = integration["cameras"][0]
        eye = cam["eye_asset_xyz_m"]
        self.assertGreater(eye[0], 1.0)  # right pilot module, not left docking pod
        self.assertLess(eye[0], 2.3)
        self.assertGreater(eye[1], 1.0)
        self.assertLess(eye[1], 3.0)  # physically behind the outer window, not outboard
        self.assertEqual(integration["cockpit"]["mode"], "canon")
        self.assertFalse(integration["cockpit"]["interior_mvp"]["authored_hollow_interior"])
        self.assertEqual(integration["cockpit"]["interior_mvp"]["mode"],
                         "inside_module_exact_plane_window")
        report = json.loads((PACK / "ships" / "boarding_craft" / "working" / "06_world_rays.json").read_text())
        probes = {r["label"]: r for r in report["rays"]}
        for label in ("center", "upper_pane", "left_pane"):
            self.assertIsNone(probes[label]["first_hit"])
        for label in ("indicator", "panel", "radial"):
            self.assertIsNotNone(probes[label]["first_hit"])

    def test_source_hashes_recorded(self):
        for ship in SHIPS:
            sha = json.loads((PACK / "ships" / ship / "working" / "scale_and_axes.json").read_text())["source_sha256"]
            self.assertTrue(len(sha) == 64)

    def test_hulls_and_cockpits_have_no_unintended_alpha(self):
        for ship in SHIPS:
            for name in ("game_lod0", "cockpit"):
                path = PACK / "ships" / ship / "export" / "ac_scenes" / (name + ".json")
                data = json.loads(path.read_text())
                self.assertTrue(data["materials"])
                for material in data["materials"]:
                    self.assertEqual(material.get("alpha", 1), 1, (ship, material["name"]))
                ac = (path.parents[1] / (name + ".ac")).read_text()
                for transparency in re.findall(r"\btrans ([0-9.eE+-]+)", ac):
                    self.assertEqual(float(transparency), 0)

    def test_window_is_open_octagon_not_solid_hub_or_rectangular_sheet(self):
        """Independent geometric check of export input, not an escape-counter."""
        for ship in SHIPS:
            root = PACK / "ships" / ship / "export"
            data = json.loads((root / "ac_scenes/cockpit.json").read_text())
            objects = {o["name"]: o for o in data["objects"]}
            self.assertIn("WindowCentralOctagon", objects)
            self.assertIn("WindowOuterRim", objects)
            self.assertEqual(sum(n.startswith("WindowRadial_") for n in objects), 8)
            self.assertFalse(any("Glass" in n or "WindowPane" in n for n in objects))
            eye = json.loads((root / "integration.json").read_text())["cameras"][0]["eye_asset_xyz_m"]
            for o in objects.values():
                m = o["matrix_world"]
                vertices = [(m[0]*p[0]+m[1]*p[1]+m[2]*p[2]+m[3],
                             m[4]*p[0]+m[5]*p[1]+m[6]*p[2]+m[7],
                             m[8]*p[0]+m[9]*p[1]+m[10]*p[2]+m[11])
                            for p in o["vertices"]]
                for face in o["triangles"]:
                    hit = ray_hit(eye, (0, 1, 0), tuple(vertices[i] for i in face))
                    self.assertIsNone(hit, (ship, o["name"], "central view is blocked"))


if __name__ == "__main__":
    unittest.main()
