#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Independent FlightGear scenery-contract checks for TASK4.

The references are FGData release/2024.1 Docs/README.scenery and SimGear
release/2024.1 simgear/bucket/newbucket.cxx.  These checks deliberately do
not import the package generator: a generator and its self-test must not be
the only authorities for the emitted scenery contract.

No FlightGear runtime is required.  Passing these tests does not prove visual
loading, terrain clearance, collision or support by a deck.
"""

import json
import math
from pathlib import Path, PurePosixPath
import shlex
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "scenery" / "space_arena"
OBJECTS = PACKAGE / "Objects"


def longitude_span(latitude):
    """SGBucket::sg_bucket_span for release/2024.1."""
    limits = (
        (89.0, 12.0), (86.0, 4.0), (83.0, 2.0), (76.0, 1.0),
        (62.0, 0.5), (22.0, 0.25), (-22.0, 0.125),
        (-62.0, 0.25), (-76.0, 0.5), (-83.0, 1.0),
        (-86.0, 2.0), (-89.0, 4.0),
    )
    for lower, span in limits:
        if latitude >= lower:
            return span
    return 12.0


def bucket_path(lon, lat):
    """Return Objects-relative STG path using the SimGear bucket algorithm."""
    if not -180.0 <= lon < 180.0 or not -90.0 <= lat <= 90.0:
        raise ValueError("coordinate outside the SimGear bucket domain")
    span = longitude_span(lat)
    base_lon = math.floor(lon + 1e-12)
    if span <= 1.0:
        x = math.floor((lon - base_lon) / span + 1e-12)
    else:
        base_lon = math.floor(base_lon / span) * span
        x = 0
    base_lat = math.floor(lat + 1e-12)
    if base_lat == 90:
        base_lat, y = 89, 7
    else:
        y = math.floor((lat - base_lat) * 8 + 1e-12)

    top_lon = math.floor(base_lon / 10) * 10
    top_lat = math.floor(base_lat / 10) * 10
    lon_hemi = "e" if top_lon >= 0 else "w"
    lat_hemi = "n" if top_lat >= 0 else "s"
    main_hemi = "e" if base_lon >= 0 else "w"
    main_lat_hemi = "n" if base_lat >= 0 else "s"
    index = (((base_lon + 180) << 14) + ((base_lat + 90) << 6)
             + (y << 3) + x)
    return PurePosixPath(
        "%s%03d%s%02d" % (lon_hemi, abs(top_lon), lat_hemi, abs(top_lat)),
        "%s%03d%s%02d" % (main_hemi, abs(base_lon),
                          main_lat_hemi, abs(base_lat)),
        "%d.stg" % index,
    )


def safe_relative_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or ".." in path.parts:
        raise ValueError("unsafe or non-portable relative path: " + value)
    return path


class TestOfficialBucketExamples(unittest.TestCase):
    def test_ksfo_example_from_fgdata(self):
        self.assertEqual(
            bucket_path(-122.375, 37.619),
            PurePosixPath("w130n30/w123n37/942050.stg"),
        )

    def test_eastern_example_from_fgdata(self):
        self.assertEqual(
            bucket_path(16.1234, 48.5678),
            PurePosixPath("e010n40/e016n48/3220128.stg"),
        )

    def test_negative_longitude_uses_western_ten_degree_bucket(self):
        path = bucket_path(-122.43, 37.43)
        self.assertEqual(path.parts[:2], ("w130n30", "w123n37"))


class TestEmittedSceneryContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stg_files = sorted(OBJECTS.rglob("*.stg"))
        if not cls.stg_files:
            raise AssertionError("space_arena has no STG placements")
        json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))

    def entries(self):
        for stg in self.stg_files:
            for number, line in enumerate(
                    stg.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                fields = shlex.split(stripped, posix=True)
                if fields[0] in ("OBJECT_STATIC", "OBJECT_STATIC_AGL"):
                    self.assertGreaterEqual(
                        len(fields), 6,
                        "%s:%d: incomplete static placement" % (stg, number))
                    self.assertLessEqual(
                        len(fields), 9,
                        "%s:%d: too many static placement fields" % (stg, number))
                    yield stg, number, fields

    def test_every_static_entry_is_in_its_official_bucket(self):
        entries = list(self.entries())
        self.assertTrue(entries)
        for stg, number, fields in entries:
            lon, lat = float(fields[2]), float(fields[3])
            expected = bucket_path(lon, lat)
            actual = PurePosixPath(stg.relative_to(OBJECTS).as_posix())
            self.assertEqual(
                actual, expected,
                "%s:%d: placement is in the wrong SimGear bucket" % (stg, number),
            )
            self.assertEqual(actual.parts[:2], ("w130n30", "w123n37"))

    def test_static_model_paths_resolve_inside_the_package(self):
        for stg, number, fields in self.entries():
            relative = safe_relative_path(fields[1])
            model = (stg.parent / Path(*relative.parts)).resolve()
            self.assertTrue(model.is_relative_to(PACKAGE.resolve()))
            self.assertTrue(
                model.is_file(),
                "%s:%d: missing static model %s" % (stg, number, fields[1]),
            )
            self.assertIn(model.suffix, (".ac", ".xml"))

    def test_xml_wrappers_use_the_official_direct_path_dialect(self):
        wrappers = set()
        for stg, _number, fields in self.entries():
            relative = safe_relative_path(fields[1])
            model = stg.parent / Path(*relative.parts)
            if model.suffix == ".xml":
                wrappers.add(model)
        self.assertTrue(wrappers, "no XML model wrappers referenced by STG")
        for wrapper in sorted(wrappers):
            root = ET.parse(wrapper).getroot()
            self.assertEqual(root.tag, "PropertyList")
            paths = root.findall("path")
            self.assertEqual(
                len(paths), 1,
                wrapper.as_posix() + ": expected one direct <path> child",
            )
            relative = safe_relative_path(paths[0].text.strip())
            self.assertEqual(relative.suffix, ".ac")
            ac_model = (wrapper.parent / Path(*relative.parts)).resolve()
            self.assertTrue(ac_model.is_relative_to(PACKAGE.resolve()))
            self.assertTrue(ac_model.is_file(), ac_model.as_posix())

    def test_ac_models_use_standard_ac3db_structure(self):
        models = sorted(OBJECTS.rglob("*.ac"))
        self.assertTrue(models, "no AC3D models in space_arena")
        for model in models:
            lines = model.read_text(encoding="ascii").splitlines()
            self.assertTrue(lines and lines[0].strip() == "AC3Db", model)
            text = "\n".join(lines)
            self.assertIn("MATERIAL ", text, model)
            self.assertIn("OBJECT world", text, model)
            self.assertIn("OBJECT poly", text, model)
            cursor = 0
            vertex_count = 0
            surface_count = 0
            while cursor < len(lines):
                stripped = lines[cursor].strip()
                if stripped.startswith("numvert "):
                    count = int(stripped.split()[1])
                    self.assertGreater(count, 0, model)
                    vertex_count += count
                    self.assertLessEqual(cursor + count, len(lines), model)
                    for vertex in lines[cursor + 1:cursor + 1 + count]:
                        values = [float(value) for value in vertex.split()]
                        self.assertEqual(len(values), 3, model)
                        self.assertTrue(all(math.isfinite(v) for v in values))
                    cursor += count + 1
                    continue
                if stripped.startswith("numsurf "):
                    declared = int(stripped.split()[1])
                    self.assertGreater(declared, 0, model)
                    cursor += 1
                    for _surface in range(declared):
                        self.assertTrue(lines[cursor].strip().startswith("SURF "))
                        cursor += 1
                        while cursor < len(lines) and not lines[cursor].strip().startswith("refs "):
                            cursor += 1
                        self.assertLess(cursor, len(lines), model)
                        refs = int(lines[cursor].strip().split()[1])
                        self.assertGreaterEqual(refs, 3, model)
                        surface_count += refs - 2
                        cursor += 1
                        for _ref in range(refs):
                            index = int(lines[cursor].split()[0])
                            self.assertGreaterEqual(index, 0, model)
                            self.assertLess(index, count, model)
                            cursor += 1
                    continue
                cursor += 1
            self.assertGreater(vertex_count, 0, model)
            self.assertGreater(surface_count, 0, model)

    def test_stg_numeric_fields_are_finite(self):
        for stg, number, fields in self.entries():
            for value in fields[2:]:
                self.assertTrue(
                    math.isfinite(float(value)),
                    "%s:%d: non-finite numeric field" % (stg, number),
                )


if __name__ == "__main__":
    unittest.main()
