#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the assisted FDM builder.  Resources/configuration only."""
import hashlib
import json
import math
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools" / "build_assisted_fdm.py"
PACK = ROOT.parent / "TIE_Game_Asset_Pack_v1"
SLUGS_PER_KG = 1.0 / 14.5939029372064
FT_PER_M = 1.0 / 0.3048
LBF_PER_N = 1.0 / 4.4482216152605

SHIPS = ("m1_bizarro", "royal_guard", "boarding_craft")


def ship_dir(name):
    return PACK / "ships" / name


class TestBuildAssistedFdm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PACK.is_dir():
            raise unittest.SkipTest("TIE asset pack not available")
        for name in SHIPS:
            if not (ship_dir(name) / "flight_profile.json").is_file():
                raise unittest.SkipTest(f"missing profile for {name}")

    def _build(self, name, tmp, integration=None):
        out = tmp / f"{name}.xml"
        cmd = [sys.executable, str(BUILDER), "--ship-dir", str(ship_dir(name)), "--output", str(out)]
        if integration is not None:
            cmd[3:3] = []  # noop
            cmd += ["--integration", str(integration)]
        # simpler: rebuild cmd cleanly
        cmd = [sys.executable, str(BUILDER), str(ship_dir(name))]
        if integration is not None:
            cmd.append(str(integration))
        cmd += ["--output", str(out)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return out

    def test_actual_profiles_produce_deterministic_xml(self):
        import tempfile
        for name in SHIPS:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                out1 = self._build(name, tmp)
                xml1 = out1.read_text()
                out2 = tmp / f"{name}-2.xml"
                subprocess.run([sys.executable, str(BUILDER), str(ship_dir(name)), "--output", str(out2)],
                               check=True, capture_output=True, text=True)
                self.assertEqual(xml1, out2.read_text())
                ET.fromstring(xml1)  # parse
                self.assertIn("GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS", xml1)
                self.assertNotIn("4240", xml1)

    def test_transform_and_accel_no_lore_constants(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out = self._build("m1_bizarro", tmp)
            xml = out.read_text()
            self.assertIn("[Y, X, -Z]", xml)
            # mass and accel conversion present
            profile = json.loads((ship_dir("m1_bizarro") / "flight_profile.json").read_text())
            mass = profile["mass_gameplay_kg"]
            accel = profile["assisted_flight"]["forward_accel_m_s2"]
            thrust = mass * accel * LBF_PER_N
            self.assertIn(repr(thrust), xml)

    def test_axes_and_clips(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out = self._build("royal_guard", tmp)
            root = ET.parse(out).getroot()
            repulsor = root.find("external_reactions/force[@name='repulsor']")
            self.assertEqual(repulsor.get("frame"), "LOCAL")
            self.assertEqual(tuple(float(repulsor.findtext(f"direction/{a}")) for a in "xyz"), (0, 0, -1))
            drive = root.find("external_reactions/force[@name='drive']")
            self.assertEqual(tuple(float(drive.findtext(f"direction/{a}")) for a in "xyz"), (1, 0, 0))
            lateral = root.find("external_reactions/force[@name='lateral-assist']")
            self.assertEqual(tuple(float(lateral.findtext(f"direction/{a}")) for a in "xyz"), (0, 1, 0))
            # clips
            for name in ("lab/drive-lbs",):
                comp = root.find(f".//fcs_function[@name='{name}']")
                lo = float(comp.findtext("clipto/min"))
                hi = float(comp.findtext("clipto/max"))
                self.assertLess(lo, 0)
                self.assertGreater(hi, 0)

    def test_rate_tau_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out = self._build("boarding_craft", tmp)
            xml = out.read_text()
            profile = json.loads((ship_dir("boarding_craft") / "flight_profile.json").read_text())
            tau = profile["assisted_flight"]["rate_response_time_s"]
            self.assertIn(repr(tau), xml)

    def test_no_fake_enabler(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out = self._build("m1_bizarro", tmp)
            xml = out.read_text()
            self.assertNotIn("<flight-model>", xml)
            self.assertNotIn("flight-model>jsb", xml)
            self.assertIn("pending_engine", xml)

    def test_check_stale_and_correct(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out = self._build("m1_bizarro", tmp)
            # correct
            r = subprocess.run([sys.executable, str(BUILDER), str(ship_dir("m1_bizarro")), "--output", str(out), "--check"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            # stale
            out.write_text(out.read_text() + "\n")
            r2 = subprocess.run([sys.executable, str(BUILDER), str(ship_dir("m1_bizarro")), "--output", str(out), "--check"],
                                capture_output=True, text=True)
            self.assertNotEqual(r2.returncode, 0)

    def test_original_profiles_hashes_unchanged(self):
        import tempfile
        hashes_before = {n: hashlib.sha256((ship_dir(n) / "flight_profile.json").read_bytes()).hexdigest() for n in SHIPS}
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for n in SHIPS:
                self._build(n, tmp)
        hashes_after = {n: hashlib.sha256((ship_dir(n) / "flight_profile.json").read_bytes()).hexdigest() for n in SHIPS}
        self.assertEqual(hashes_before, hashes_after)

    def test_extract_values_compare_source(self):
        import tempfile
        name = "royal_guard"
        profile = json.loads((ship_dir(name) / "flight_profile.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            out = self._build(name, tmp)
            root = ET.parse(out).getroot()
            mass = float(root.findtext("mass_balance/emptywt"))
            self.assertEqual(mass, profile["mass_gameplay_kg"])
            # inertia proxy positive
            for axis in ("ixx", "iyy", "izz"):
                self.assertGreater(float(root.findtext(f"mass_balance/{axis}")), 0)
            # speed limit property
            props = {p.text: p.get("value") for p in root.findall("flight_control/property")}
            self.assertEqual(float(props["lab/speed-limit-m-s"]), profile["assisted_flight"]["speed_limit_m_s"])
            tau = float(props["lab/rate-tau-s"])
            self.assertEqual(tau, profile["assisted_flight"]["rate_response_time_s"])

    def test_invalid_json_trapped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sdir = tmp / "bad_ship"
            sdir.mkdir()
            (sdir / "flight_profile.json").write_text("{not json")
            out = tmp / "out.xml"
            r = subprocess.run([sys.executable, str(BUILDER), str(sdir), "--output", str(out)],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)

    def test_missing_units_or_meter_data_trapped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sdir = tmp / "bad_ship"
            sdir.mkdir()
            profile = json.loads((ship_dir("m1_bizarro") / "flight_profile.json").read_text())
            profile["scale_reference_length_m"] = 0
            (sdir / "flight_profile.json").write_text(json.dumps(profile))
            # ship_id mismatch directory
            out = tmp / "out.xml"
            r = subprocess.run([sys.executable, str(BUILDER), str(sdir), "--output", str(out)],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)

    def test_integration_proxy_override(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            integration = tmp / "integration.json"
            integration.write_text(json.dumps({
                "units": "m",
                "measured_dimensions_m": {"x": 2.0, "y": 3.0, "z": 4.0},
                "inertia_kg_m2": {"ixx": 10.0, "iyy": 20.0, "izz": 30.0},
            }))
            out = tmp / "out.xml"
            r = subprocess.run([sys.executable, str(BUILDER), str(ship_dir("m1_bizarro")), str(integration), "--output", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            root = ET.parse(out).getroot()
            self.assertEqual(float(root.findtext("mass_balance/ixx")), 10.0)
            self.assertIn("measured integration.json", out.read_text())


if __name__ == "__main__":
    unittest.main()
