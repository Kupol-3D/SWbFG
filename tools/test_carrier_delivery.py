#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Static delivery checks for the Venator aircraft package and its scene.

The canonical asset package is ``Aircraft/sa_venator``.  The ready scenery only
places it: ``scenery/space_arena_carrier/.../Models/venator/carrier.xml`` is a
small PropertyList binder pointing at ``Aircraft/sa_venator/Models/carrier.xml``.
There is no second copy of the meshes or layout inside the scenery.

These are file checks only.  They do not prove a FlightGear 2024.1.7 visual
load, movement/AI, spawn, collision, platform support or LOD-switch behaviour.
"""
import hashlib
import json
import math
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_carrier_arena import parse_ac  # noqa: E402

AIRCRAFT = ROOT / "Aircraft" / "sa_venator"
AIRCRAFT_MODELS = AIRCRAFT / "Models"
BUILD_MANIFEST = AIRCRAFT / "Sources" / "BUILD_MANIFEST.json"
SET_XML = AIRCRAFT / "sa_venator-set.xml"
LAYOUT = AIRCRAFT_MODELS / "layout.json"
CARRIER_XML = AIRCRAFT_MODELS / "carrier.xml"
AIR_LICENSE = AIRCRAFT / "LICENSE_ATTRIBUTION.md"

SCENE = ROOT / "scenery" / "space_arena_carrier"
LEGACY = ROOT / "scenery" / "space_arena"
SCENE_TILE = SCENE / "Objects" / "w130n30" / "w123n37"
SCENE_STG = SCENE_TILE / "942042.stg"
SCENE_BINDER = SCENE_TILE / "Models" / "venator" / "carrier.xml"
SCENE_MANIFEST = SCENE / "manifest.json"

LODS = ("lod0", "lod1", "lod2")
AC_FILES = tuple("carrier_%s.ac" % lod for lod in LODS)
RETIRED = ("emp_carrier", "emp_deck", "emp_frame", "emp_rack")
ALLOWED_LAYOUT_STATUSES = frozenset((
    "unverified",
    "source_urls_recorded_terms_pending",
))
CANONICAL_AIRCRAFT_MODEL = "Aircraft/sa_venator/Models/carrier.xml"
CANONICAL_AIRCRAFT_FILES = tuple(
    "Aircraft/sa_venator/Models/" + name
    for name in ("carrier.xml", "carrier_lod0.ac", "carrier_lod1.ac",
                 "carrier_lod2.ac", "layout.json")
)

FORBIDDEN_SUFFIXES = (
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".blend", ".blend1", ".blend2", ".bak", ".orig", ".rej", ".pyc",
)
FORBIDDEN_NAMES = frozenset((
    ".DS_Store", "Thumbs.db", "desktop.ini",
))
FORBIDDEN_DIRS = frozenset((
    "__pycache__", ".cache", ".pytest_cache", ".mypy_cache", ".git",
    ".venv", "venv", "node_modules",
))
FOREIGN_DEPENDENCIES = (
    "star_destroyer_pack",
    "starterpack",
    "tie-shared-scenery",
    "work/runtime",
)

# Fragments keep this guard file itself clean under repository hygiene scans.
_PERSONAL_PATH_RES = (
    re.compile(r"/" + "home" + r"/[A-Za-z0-9][A-Za-z0-9._-]*"),
    re.compile(r"/" + "Users" + r"/[A-Za-z0-9][A-Za-z0-9._-]*"),
    re.compile(re.escape(":" + "\\") + "Users"),
)

_AC_CACHE = {}


def parsed_carrier():
    """Parse the three LOD files at most once for the whole test run."""
    if "lods" not in _AC_CACHE:
        _AC_CACHE["lods"] = {
            lod: parse_ac(AIRCRAFT_MODELS / ("carrier_%s.ac" % lod))
            for lod in LODS
        }
    return _AC_CACHE["lods"]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_files(root):
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )


def stg_entries(path):
    entries = []
    for line_number, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 6 or fields[0] not in (
                "OBJECT_STATIC", "OBJECT_STATIC_AGL"):
            raise AssertionError(
                "%s:%d: unexpected STG line %r" % (path, line_number, raw))
        entries.append({
            "kind": fields[0],
            "model": fields[1],
            "lon": float(fields[2]),
            "lat": float(fields[3]),
            "offset": float(fields[4]),
            "heading": float(fields[5]),
            "line": line,
        })
    return entries


def assert_relative_file(case, base, reference, what, containment):
    case.assertIsInstance(reference, str, what)
    reference = reference.strip()
    case.assertTrue(reference, what)
    case.assertNotIn("\\", reference, what)
    case.assertNotIn(":", reference, what)
    case.assertFalse(reference.startswith("/"), what)
    case.assertFalse(reference.startswith("~"), what)
    parts = PurePosixPath(reference).parts
    case.assertTrue(parts, what)
    case.assertNotIn("..", parts, what)
    candidate = base.joinpath(*parts)
    case.assertTrue(candidate.is_file(), (what, reference))
    try:
        candidate.resolve().relative_to(containment.resolve())
    except ValueError:
        case.fail("%s: reference escapes %s: %r"
                  % (what, containment, reference))
    current = candidate
    while current != containment and current != current.parent:
        case.assertFalse(current.is_symlink(),
                         "%s: symlink in reference path: %s" % (what, current))
        current = current.parent
    return candidate


def assert_no_bulk_inputs(case, root):
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_dir():
            case.assertNotIn(path.name, FORBIDDEN_DIRS, relative)
            continue
        case.assertNotIn(path.name, FORBIDDEN_NAMES, relative)
        case.assertNotIn(path.suffix.lower(), FORBIDDEN_SUFFIXES, relative)


class TestVenatorAircraftDelivery(unittest.TestCase):
    def test_required_package_tree(self):
        for path in (CARRIER_XML, LAYOUT, SET_XML, BUILD_MANIFEST,
                     AIR_LICENSE, AIRCRAFT / "README.md"):
            self.assertTrue(path.is_file(), path.relative_to(ROOT))
        for name in AC_FILES:
            self.assertTrue((AIRCRAFT_MODELS / name).is_file(), name)
        self.assertEqual(list(AIRCRAFT.rglob("hangar.ac")), [],
                         "unified geometry must not ship a second hangar mesh")

    def test_no_bulk_archives_backups_or_caches(self):
        assert_no_bulk_inputs(self, AIRCRAFT)

    def test_build_manifest_hashes_cover_every_other_file(self):
        manifest = json.loads(BUILD_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"],
                         "bombable.carrier-aircraft-build/v1")
        self.assertEqual(manifest["geometry_mode"], "unified")
        self.assertEqual(manifest["model_path"], "Models/carrier.xml")
        self.assertIs(manifest["runtime_verified"], False)

        hashes = manifest["files"]
        expected = set(tree_files(AIRCRAFT)) - {
            "Sources/BUILD_MANIFEST.json"}
        self.assertEqual(set(hashes), expected)
        for relative, digest in sorted(hashes.items()):
            self.assertFalse(relative.startswith("/"), relative)
            self.assertNotIn("..", PurePosixPath(relative).parts, relative)
            self.assertRegex(digest, r"^[0-9a-f]{64}$", relative)
            self.assertEqual(sha256(AIRCRAFT / relative), digest, relative)

    def test_set_xml_is_visual_only_and_points_at_carrier_model(self):
        root = ET.parse(SET_XML).getroot()
        self.assertEqual(root.findtext("sim/flight-model"), "null")
        self.assertEqual(root.findtext("sim/model/path"),
                         CANONICAL_AIRCRAFT_MODEL)
        self.assertTrue((ROOT / CANONICAL_AIRCRAFT_MODEL).is_file())

    def test_layout_contract_and_finite_bounds(self):
        layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
        self.assertEqual(layout["schema"], "bombable.carrier-layout/v1")
        self.assertEqual(layout["axes"], "X right, Y forward, Z up")
        self.assertEqual(layout["geometry_mode"], "unified")
        self.assertIs(layout["runtime_verified"], False)

        bounds = layout["bounds_m"]
        self.assertEqual(len(bounds), 3)
        for axis in bounds:
            self.assertEqual(len(axis), 2)
            for value in axis:
                self.assertIsInstance(value, (int, float))
                self.assertTrue(math.isfinite(value))
            self.assertLessEqual(axis[0], axis[1])

        triangles = layout["triangles"]
        self.assertEqual(set(triangles), {"lod0", "lod1", "lod2", "hangar"})
        self.assertEqual(triangles["hangar"], 0)
        self.assertGreater(triangles["lod0"], triangles["lod1"])
        self.assertGreater(triangles["lod1"], triangles["lod2"])
        self.assertGreater(triangles["lod2"], 0)

        slots = layout["spawn_slots"]
        self.assertTrue(slots)
        ids = [slot["id"] for slot in slots]
        self.assertEqual(len(ids), len(set(ids)))
        for slot in slots:
            self.assertTrue(isinstance(slot["id"], str) and slot["id"].strip())
            self.assertTrue(math.isfinite(float(slot["heading_deg"])))
            position = slot["position_m"]
            self.assertEqual(len(position), 3)
            for index, value in enumerate(position):
                self.assertTrue(math.isfinite(value))
                self.assertGreaterEqual(value, bounds[index][0])
                self.assertLessEqual(value, bounds[index][1])
            self.assertTrue(slot["ship_ids"])
            for ship_id in slot["ship_ids"]:
                self.assertTrue(isinstance(ship_id, str) and ship_id.strip())

        status = layout["provenance"]["license_status"]
        self.assertIn(status, ALLOWED_LAYOUT_STATUSES)
        self.assertNotIn("GPL", status.upper())

    def test_actual_ac_counts_match_layout_and_build_manifest(self):
        layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
        manifest = json.loads(BUILD_MANIFEST.read_text(encoding="utf-8"))
        parsed = parsed_carrier()

        actual = {lod: parsed[lod]["triangles"] for lod in LODS}
        with_hangar = dict(actual)
        with_hangar["hangar"] = 0
        self.assertEqual(with_hangar, layout["triangles"])
        self.assertEqual(
            {lod: manifest["triangles"][lod] for lod in LODS}, actual)
        self.assertEqual(manifest["triangles"].get("hangar", 0), 0)

    def test_xml_loads_three_lods_once_with_real_ranges(self):
        parsed = parsed_carrier()
        root = ET.parse(CARRIER_XML).getroot()
        self.assertEqual(root.tag, "PropertyList")
        self.assertEqual(len(root.findall("model")), 3)
        self.assertEqual(root.findall("path"), [])

        expected = set(AC_FILES)
        seen_paths = []
        seen_names = []
        for model in root.findall("model"):
            self.assertEqual(model.findall("model"), [])
            paths = model.findall("path")
            self.assertEqual(len(paths), 1)
            name = model.findtext("name", "").strip()
            self.assertTrue(name)
            seen_names.append(name)
            path = (paths[0].text or "").strip()
            self.assertEqual(PurePosixPath(path).parent.parts, ())
            self.assertIn(path, expected)
            seen_paths.append(path)
            assert_relative_file(
                self, AIRCRAFT_MODELS, path,
                "carrier.xml model reference", AIRCRAFT)
        self.assertEqual(sorted(seen_paths), sorted(expected))
        self.assertEqual(len(seen_names), len(set(seen_names)))

        object_names = set()
        for geometry in parsed.values():
            object_names.update(geometry["object_names"])

        animations = [node for node in root.findall("animation")
                      if node.findtext("type", "").strip() == "range"]
        self.assertEqual(len(animations), 3)
        ranges = []
        targets = []
        for animation in animations:
            target = animation.findtext("object-name", "").strip()
            self.assertIn(target, object_names)
            targets.append(target)
            lower = float(animation.findtext("min-m"))
            upper = float(animation.findtext("max-m"))
            self.assertTrue(math.isfinite(lower))
            self.assertTrue(math.isfinite(upper))
            self.assertLess(lower, upper)
            ranges.append((lower, upper))
        self.assertEqual(len(targets), len(set(targets)))
        ranges.sort()
        self.assertEqual(ranges[0][0], 0.0)
        for previous, following in zip(ranges, ranges[1:]):
            self.assertEqual(previous[1], following[0],
                             "LOD ranges must be contiguous")

    def test_aircraft_references_are_local_and_relative(self):
        references = []
        root = ET.parse(CARRIER_XML).getroot()
        for node in root.iter():
            if node.tag not in ("path", "texture", "texture-path"):
                continue
            reference = (node.text or "").strip()
            if not reference or reference in ("-1", "0", "none", "None"):
                continue
            assert_relative_file(
                self, CARRIER_XML.parent, reference,
                "reference in carrier.xml", AIRCRAFT)
            references.append(reference)

        for lod, geometry in parsed_carrier().items():
            for reference in geometry["textures"]:
                assert_relative_file(
                    self, AIRCRAFT_MODELS, reference,
                    "texture in %s" % lod, AIRCRAFT)
                references.append(reference)

        build = json.loads(BUILD_MANIFEST.read_text(encoding="utf-8"))
        for reference in build["files"]:
            self.assertFalse(reference.startswith("/"), reference)
            self.assertNotIn("..", PurePosixPath(reference).parts)
            self.assertTrue((AIRCRAFT / reference).is_file(), reference)
            references.append(reference)

        for reference in references:
            normalized = reference.lower()
            for token in FOREIGN_DEPENDENCIES:
                self.assertNotIn(token, normalized, reference)

        for path in AIRCRAFT.rglob("*"):
            self.assertFalse(path.is_symlink(),
                             "symlink in delivery tree: %s" % path)

    def test_attribution_is_present_and_not_gpl(self):
        text = AIR_LICENSE.read_text(encoding="utf-8")
        self.assertTrue(text.strip())
        self.assertIn("CGTrader", text)
        build = json.loads(BUILD_MANIFEST.read_text(encoding="utf-8"))
        self.assertIs(build["runtime_verified"], False)


class TestCarrierScenePlacement(unittest.TestCase):
    def test_scene_keeps_only_a_binder_to_the_aircraft_package(self):
        self.assertTrue(SCENE_BINDER.is_file())
        root = ET.parse(SCENE_BINDER).getroot()
        self.assertEqual(root.tag, "PropertyList")
        paths = root.findall("path")
        self.assertEqual(len(paths), 1)
        self.assertEqual((paths[0].text or "").strip(),
                         CANONICAL_AIRCRAFT_MODEL)
        self.assertEqual(root.findall("model"), [])
        self.assertEqual(root.findall("animation"), [])

        self.assertEqual(
            sorted(tree_files(SCENE_BINDER.parent)), ["carrier.xml"],
            "the scene venator directory may contain only the binder")
        self.assertEqual(list(SCENE.rglob("carrier_lod*.ac")), [],
                         "carrier meshes must not be duplicated in scenery")
        self.assertEqual(list(SCENE.rglob("layout.json")), [],
                         "carrier layout must not be duplicated in scenery")
        self.assertEqual(list(SCENE.rglob("hangar.ac")), [])

        reference = (paths[0].text or "").strip()
        self.assertFalse(reference.startswith("/"))
        self.assertNotIn("..", PurePosixPath(reference).parts)
        self.assertTrue((ROOT / reference).is_file(), reference)
        self.assertTrue((ROOT / reference).resolve().is_relative_to(
            AIRCRAFT.resolve()))

    def test_source_manifest_and_original_stg_are_byte_identical(self):
        self.assertEqual(
            (SCENE / "source" / "manifest.json").read_bytes(),
            (LEGACY / "manifest.json").read_bytes())
        self.assertEqual(
            (SCENE / "source" / "942042.stg.original").read_bytes(),
            (LEGACY / "Objects" / "w130n30" / "w123n37" / "942042.stg")
            .read_bytes())
        self.assertEqual(
            (SCENE / "source" / "src" / "generate_package.py").read_bytes(),
            (LEGACY / "src" / "generate_package.py").read_bytes())

    def test_scene_manifest_declares_the_aircraft_package(self):
        manifest = json.loads(SCENE_MANIFEST.read_text(encoding="utf-8"))
        integration = manifest["carrier_integration"]
        self.assertEqual(integration["asset_package"],
                         "Aircraft/sa_venator")
        self.assertEqual(integration["layout"],
                         "Aircraft/sa_venator/Models/layout.json")
        self.assertEqual(integration["path_base"], "repository-root")
        self.assertEqual(integration["geometry_mode"], "unified")
        self.assertIs(integration["runtime_verified"], False)
        self.assertIs(manifest["runtime_verified"], False)

        for lod in LODS:
            self.assertEqual(
                integration["models"][lod]["file"],
                "Aircraft/sa_venator/Models/carrier_%s.ac" % lod)
        declared = integration["declared_triangles"]
        parsed = integration["parsed_triangles"]
        self.assertEqual(declared["hangar"], 0)
        self.assertEqual(parsed["hangar"], 0)
        self.assertGreater(declared["lod0"], declared["lod1"])
        self.assertGreater(declared["lod1"], declared["lod2"])
        self.assertEqual(declared, parsed)

        external = integration["external_pack"]
        self.assertIn(external["license_status"], ALLOWED_LAYOUT_STATUSES)
        self.assertNotIn("GPL", external["license_status"].upper())

    def test_scene_manifest_references_external_models_by_hash(self):
        manifest = json.loads(SCENE_MANIFEST.read_text(encoding="utf-8"))
        references = manifest["carrier_integration"]["files"]
        self.assertTrue(references)
        for canonical, digest in sorted(references.items()):
            self.assertTrue(canonical.startswith("Aircraft/sa_venator/"))
            self.assertFalse(canonical.startswith("/"), canonical)
            self.assertNotIn("..", PurePosixPath(canonical).parts, canonical)
            self.assertRegex(digest, r"^[0-9a-f]{64}$", canonical)
            self.assertEqual(sha256(ROOT / canonical), digest, canonical)
        self.assertEqual(set(CANONICAL_AIRCRAFT_FILES) - set(references), set())

    def test_scene_manifest_pins_the_aircraft_build_manifest(self):
        manifest = json.loads(SCENE_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["repository_dependencies"],
            {"Aircraft/sa_venator/Sources/BUILD_MANIFEST.json":
             sha256(BUILD_MANIFEST)})

    def test_scene_manifest_hashes_only_its_own_files(self):
        manifest = json.loads(SCENE_MANIFEST.read_text(encoding="utf-8"))
        hashes = manifest["provenance"]["artifact_sha256"]
        expected = set(tree_files(SCENE)) - {"manifest.json"}
        self.assertEqual(set(hashes), expected)
        for relative, digest in sorted(hashes.items()):
            self.assertFalse(relative.startswith("/"), relative)
            self.assertNotIn("..", PurePosixPath(relative).parts, relative)
            self.assertEqual(sha256(SCENE / relative), digest, relative)

    def test_stg_keeps_rebels_and_replaces_old_empire(self):
        overlay = stg_entries(SCENE_STG)
        legacy = stg_entries(LEGACY / "Objects" / "w130n30" / "w123n37"
                             / "942042.stg")
        legacy_rebels = [
            entry["line"] for entry in legacy
            if PurePosixPath(entry["model"]).stem not in RETIRED
        ]
        overlay_rebels = [
            entry["line"] for entry in overlay
            if entry["model"] != "Models/venator/carrier.xml"
        ]
        self.assertEqual(overlay_rebels, legacy_rebels)

        carriers = [entry for entry in overlay
                    if entry["model"] == "Models/venator/carrier.xml"]
        self.assertEqual(len(carriers), 1)
        carrier = carriers[0]
        self.assertEqual(carrier["kind"], "OBJECT_STATIC_AGL")
        self.assertAlmostEqual(carrier["lon"], -122.455, places=6)
        self.assertAlmostEqual(carrier["lat"], 37.415, places=6)
        self.assertAlmostEqual(carrier["offset"], 450.0, places=3)
        self.assertAlmostEqual(carrier["heading"], 110.0, places=3)

        for entry in overlay:
            self.assertFalse(
                PurePosixPath(entry["model"]).stem in RETIRED,
                "old Empire placement still active: %s" % entry["line"])
            self.assertNotIn("..", PurePosixPath(entry["model"]).parts)
            self.assertNotIn("Models/space_arena/emp_", entry["model"])
            for token in FOREIGN_DEPENDENCIES:
                self.assertNotIn(token, entry["model"])
            assert_relative_file(
                self, SCENE_TILE, entry["model"], "STG model reference",
                SCENE)

        manifest = json.loads(SCENE_MANIFEST.read_text(encoding="utf-8"))
        active = manifest["placements"]
        self.assertTrue(active)
        self.assertEqual(active, [entry for entry in active
                                  if entry["faction"] != "empire"])
        self.assertFalse([entry for entry in active
                          if str(entry["model"]).startswith("emp_")])
        retired = manifest["retired_empire_placements"]
        self.assertTrue(retired)
        self.assertEqual({entry["status"] for entry in retired}, {"retired"})
        self.assertEqual(
            {entry["model"] for entry in retired}, set(RETIRED))

    def test_scene_runtime_references_are_local_and_plain(self):
        for xml_path in sorted((SCENE / "Objects").rglob("*.xml")):
            root = ET.parse(xml_path).getroot()
            for node in root.iter():
                if node.tag not in ("path", "texture", "texture-path"):
                    continue
                reference = (node.text or "").strip()
                if not reference or reference in ("-1", "0", "none", "None"):
                    continue
                if xml_path == SCENE_BINDER:
                    self.assertEqual(reference, CANONICAL_AIRCRAFT_MODEL)
                    continue
                assert_relative_file(
                    self, xml_path.parent, reference,
                    "reference in %s" % xml_path.relative_to(SCENE), SCENE)

        for path in SCENE.rglob("*"):
            self.assertFalse(path.is_symlink(),
                             "symlink in delivery tree: %s" % path)

    def test_no_bulk_archives_backups_or_caches(self):
        assert_no_bulk_inputs(self, SCENE)

    def test_no_personal_paths_or_hidden_inputs(self):
        for path in SCENE.rglob("*"):
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            if path.suffix.lower() in (".ac", ".zip", ".abc", ".blend"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in _PERSONAL_PATH_RES:
                self.assertIsNone(
                    pattern.search(text),
                    "personal absolute path in %s"
                    % path.relative_to(SCENE))


if __name__ == "__main__":
    unittest.main()
