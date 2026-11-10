#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Single FlightGear/SimGear/FGData target version across docs.

docs/SOURCES.json is the machine-readable pin; the listed first-party docs
must state exactly the same version and must not mention the previous one.

This is a documentation-consistency check only.  It never runs fgfs and says
nothing about what is installed on any machine: the actually installed bundle
is verified locally with `fgfs --version` and the local FGData/SimGear
metadata.  Independent pins (FGMS commit/version, Python, websockets,
cryptography) are NOT re-checked here and do not imply runtime compatibility
with the new target bundle.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TARGET = '2024.1.7'
VERSION_RE = re.compile(r'\b20\d{2}\.\d+\.\d+\b')

SOURCES_JSON = ROOT / 'docs' / 'SOURCES.json'
BUNDLE_KEYS = ('version', 'simgear_version', 'fgdata_version')
LISTED_DOCS = (
    'README.md',
    'TASK1.md',
    'TASK2.md',
    'TASK3.md',
    'TASK4.md',
    'docs/TEAM_ENVIRONMENT.md',
    'docs/SOURCES.json',
)


class TestTargetVersions(unittest.TestCase):
    def test_sources_json_pins_one_bundle_version(self):
        block = json.loads(SOURCES_JSON.read_text(encoding='utf-8'))['flightgear_target']
        self.assertEqual(sorted(block), sorted((*BUNDLE_KEYS, 'bundled')))
        for key in BUNDLE_KEYS:
            self.assertEqual(block[key], EXPECTED_TARGET, key)
        self.assertIs(block['bundled'], False)

    def test_listed_docs_state_the_same_target(self):
        for rel in LISTED_DOCS:
            text = (ROOT / rel).read_text(encoding='utf-8')
            self.assertIn(EXPECTED_TARGET, text, rel + ' is missing the target')

    def test_listed_docs_hold_no_other_or_old_version(self):
        offenders = {}
        for rel in LISTED_DOCS:
            text = (ROOT / rel).read_text(encoding='utf-8')
            stale = sorted(set(VERSION_RE.findall(text)) - {EXPECTED_TARGET})
            if stale:
                offenders[rel] = stale
        self.assertEqual(offenders, {})


if __name__ == '__main__':
    unittest.main()
