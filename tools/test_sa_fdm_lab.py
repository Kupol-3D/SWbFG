#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Resources/configuration only. Does NOT prove flight stability or run FlightGear."""
import hashlib
import json
import math
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from .test_sa_tie import read_ac, triangles, point_triangle_distance, ray_hit, sub
    from .run_sa_fdm_lab import samples, analyze
except ImportError:
    from test_sa_tie import read_ac, triangles, point_triangle_distance, ray_hit, sub
    from run_sa_fdm_lab import samples, analyze

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'Aircraft/sa_fdm_lab'


class TestLabResources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = ET.parse(PACKAGE / 'sa_fdm_lab-set.xml').getroot()
        cls.fdm = ET.parse(PACKAGE / 'sa_fdm_lab.xml').getroot()
        cls.materials, cls.objects = read_ac(PACKAGE / 'Models/lab.ac')
        cls.geometry = list(triangles(cls.objects))

    def test_single_backend_and_native_config(self):
        self.assertEqual(self.profile.findtext('sim/flight-model'), 'jsb')
        self.assertEqual(self.profile.findtext('sim/aero'), 'sa_fdm_lab')
        self.assertEqual(self.fdm.tag, 'fdm_config')
        self.assertEqual(self.fdm.get('version'), '2.0')
        self.assertEqual(self.profile.findtext('sim/presets/onground'), 'false')
        self.assertEqual(self.profile.findtext('sim/presets/trim'), 'false')
        self.assertEqual(self.fdm.find('mass_balance/emptywt').get('unit'), 'KG')
        self.assertEqual(float(self.fdm.findtext('mass_balance/emptywt')), 1000)
        for axis in ('ixx', 'iyy', 'izz'):
            element = self.fdm.find('mass_balance/' + axis)
            self.assertEqual(element.get('unit'), 'KG*M2')
            self.assertGreater(float(element.text), 0)
        self.assertGreaterEqual(len(self.fdm.findall('ground_reactions/contact')), 3)
        self.assertEqual(len(self.fdm.find('aerodynamics')), 0)

    def test_all_resources_are_local_and_present(self):
        for path in PACKAGE.rglob('*.xml'):
            root = ET.parse(path).getroot()
            for element in root.iter():
                self.assertNotIn('include', element.attrib)
                if element.tag in ('path', 'file'):
                    value = element.text.strip()
                    self.assertFalse(Path(value).is_absolute())
                    target = ((ROOT / value) if value.startswith('Aircraft/')
                              else path.parent / value).resolve()
                    self.assertTrue(target.is_relative_to(PACKAGE))
                    self.assertTrue(target.is_file(), value)
        self.assertEqual(self.profile.findtext('sim/model/path'), 'Aircraft/sa_fdm_lab/Models/lab.xml')
        self.assertIsNone(self.profile.find('sim/rendering'))

    def test_explicit_support_frame_and_bounded_forces(self):
        force = self.fdm.find("external_reactions/force[@name='repulsor']")
        self.assertEqual(force.get('frame'), 'LOCAL')
        self.assertEqual(tuple(float(force.findtext('direction/' + a)) for a in 'xyz'), (0, 0, -1))
        for force in self.fdm.findall('external_reactions/force'):
            self.assertEqual(tuple(float(force.findtext('location/' + a)) for a in 'xyz'), (0, 0, 0))
        for target in ('lab/support-accel', 'lab/drive-lbs', 'lab/lateral-lbs',
                       'lab/roll-lbsft', 'lab/pitch-lbsft', 'lab/yaw-lbsft'):
            component = self.fdm.find(f".//fcs_function[@name='{target}']")
            lower = float(component.findtext('clipto/min'))
            upper = float(component.findtext('clipto/max'))
            self.assertTrue(math.isfinite(lower) and math.isfinite(upper) and upper > lower)
        properties = {p.text: p.get('value') for p in self.fdm.findall('flight_control/property')}
        self.assertEqual(properties['lab/support'], '1')
        self.assertEqual(properties['lab/stabilize'], '1')
        self.assertAlmostEqual(float(properties['lab/altitude-ft']) * 0.3048, 1000)

    def test_geometry_budget_nodes_normals_and_clear_pilot_view(self):
        self.assertTrue({'hull', 'cockpit', 'engine-1', 'engine-2', 'muzzle-1', 'muzzle-2'} <= self.objects.keys())
        self.assertEqual(len(self.geometry), 96)
        self.assertLess(len(self.geometry), 25000)
        view = self.profile.find("sim/view[@n='0']/config")
        eye = tuple(float(view.findtext(a)) for a in ('z-offset-m', 'x-offset-m', 'y-offset-m'))
        self.assertGreater(min(point_triangle_distance(eye, tri) for _, tri in self.geometry), 0.3)
        for y in (-0.15, 0, 0.15):
            for z in (-0.10, 0, 0.10):
                direction = sub((-3, y, 0.8 + z), eye)
                self.assertFalse(any(ray_hit(eye, direction, tri) is not None for _, tri in self.geometry))
        for obj in self.objects.values():
            if not obj['faces']:
                continue
            center = obj['origin']
            for indices, _, _ in obj['faces']:
                a,b,c = [obj['vertices'][i] for i in indices]
                ab, ac = sub(b,a), sub(c,a)
                normal = (ab[1]*ac[2]-ab[2]*ac[1], ab[2]*ac[0]-ab[0]*ac[2], ab[0]*ac[1]-ab[1]*ac[0])
                self.assertGreater(sum(n*v for n,v in zip(normal, sub(a, center))), 0)

    def test_provenance_and_reproducible_geometry(self):
        for line in (PACKAGE / 'Sources/SHA256SUMS').read_text().splitlines():
            checksum, relative = line.split('  ', 1)
            path = (PACKAGE / relative).resolve()
            self.assertTrue(path.is_relative_to(PACKAGE))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), checksum)
        result = subprocess.run([sys.executable, str(PACKAGE / 'Sources/build_lab.py'), '--check'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((PACKAGE / 'LICENSE').is_file())
        self.assertTrue((PACKAGE / 'README.md').is_file())
        # Integrity of the recorded experiment, not a claim of aerodynamic proof.
        recorded = json.loads((ROOT / 'docs/fdm_lab_evidence/environment.json').read_text())
        for relative, checksum in recorded['package_sha256'].items():
            path = (PACKAGE / relative).resolve()
            self.assertTrue(path.is_relative_to(PACKAGE))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), checksum,
                             'Recorded run belongs to a different package: ' + relative)

    def test_experiment_is_opt_in_and_controls_do_not_set_pose(self):
        text = (PACKAGE / 'Nasal/experiment.nas').read_text()
        self.assertIn('getprop("/sim/space-arena/fdm-lab/run-test")', text)
        tick = text.split('var tick = func {', 1)[1].split('var start = func {', 1)[0]
        for line in tick.splitlines():
            if 'setprop(' in line:
                self.assertIn('setprop(root ~ ', line)
        for element in self.profile.findall('.//binding/property'):
            self.assertTrue(element.text.startswith('/fdm/jsbsim/lab/'))
        self.assertEqual(len(self.profile.findall('sim/view')), 2)


class TestTelemetryIntegrity(unittest.TestCase):
    def test_missing_telemetry_cannot_pass(self):
        self.assertFalse(analyze('SA_FDM_COMPLETE', 0)['passed'])

    def test_incomplete_and_nonfinite_rows_fail(self):
        for text in ('SA_FDM_SAMPLE,0,hover',
                     'SA_FDM_SAMPLE,0,hover,nan,' + ','.join(['0'] * 13)):
            with self.assertRaises(ValueError):
                samples(text)

    def test_fg_logger_prefix_is_accepted(self):
        row = samples('   12.0 [INFO]:nasal SA_FDM_SAMPLE,0,hover,' + ','.join(['0'] * 14))
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]['phase'], 'hover')
        self.assertFalse(analyze('SA_FDM_SAMPLE,0,hover,' + ','.join(['0'] * 14), 0)['passed'])


if __name__ == '__main__':
    unittest.main()
