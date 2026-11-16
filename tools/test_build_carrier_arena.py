#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for tools/build_carrier_arena.py.

The happy path uses the real committed space_arena plus a tiny synthetic
carrier pack.  Invalid-input tests mutate that tiny pack or a temporary copy
of the arena; the committed arena, the source generator and the source
manifest are never modified.
"""

import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
ARENA = ROOT / "scenery" / "space_arena"
TOOL = Path(__file__).resolve().parent / "build_carrier_arena.py"

SPEC = importlib.util.spec_from_file_location(
    "build_carrier_arena_for_test", TOOL)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)

RETIRED_XML = {
    "Models/space_arena/%s.xml" % name for name in builder.RETIRED_MODELS
}
RETIRED_IDS = {
    "emp_carrier",
    "emp_deck",
    "emp_frame",
    "emp_rack_n",
    "emp_rack_s",
}
CARRIER_DIR = Path("Objects/w130n30/w123n37/Models/venator")
ARENA_STG = Path("Objects/w130n30/w123n37/942042.stg")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hashes(root):
    root = Path(root)
    hashes = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.endswith(".pyc") or "__pycache__" in relative.split("/"):
            continue
        hashes[relative] = sha256(path)
    return hashes


def stg_entries(path):
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        entries.append(fields)
    return entries


def material_line():
    return ('MATERIAL "fixture" rgb 0.500 0.500 0.500  amb 0.500 0.500 '
            '0.500  emis 0.000 0.000 0.000  spec 0.200 0.200 0.200  '
            'shi 32  trans 0')


def ac_text(name, triangles, texture=None):
    """A tiny valid AC3Db polygon with exactly ``triangles`` triangles."""
    vertices = triangles + 2
    lines = ["AC3Db", material_line(), "OBJECT world", "kids 1",
             "OBJECT poly", 'name "%s"' % name, "loc 0 0 0"]
    lines.append("texture %s" % ('"%s"' % texture if texture else "-1"))
    lines.append("numvert %d" % vertices)
    for index in range(vertices):
        angle = 2.0 * math.pi * index / vertices
        lines.append("%.3f %.3f %.3f"
                     % (10.0 * math.cos(angle), 0.0,
                        10.0 * math.sin(angle)))
    lines += ["numsurf 1", "SURF 0x30", "mat 0", "refs %d" % vertices]
    lines += ["%d 0 0" % index for index in range(vertices)]
    lines.append("kids 0")
    return "\n".join(lines) + "\n"


AC_OBJECT_NAMES = {
    "carrier_lod0.ac": "Venator_LOD0",
    "carrier_lod1.ac": "Venator_LOD1",
    "carrier_lod2.ac": "Venator_LOD2",
    "hangar.ac": "Venator_Hangar",
}
ALL_CARRIER_MODELS = tuple(AC_OBJECT_NAMES)


def carrier_xml(models=None, texture=True, extra_lines=None):
    """A valid carrier wrapper: one top-level <model><path> per AC."""
    if models is None:
        models = ALL_CARRIER_MODELS
    lines = ['<?xml version="1.0"?>', "<PropertyList>"]
    for name in models:
        lines += [
            "  <model>",
            "    <name>%s_container</name>" % AC_OBJECT_NAMES[name],
            "    <path>%s</path>" % name,
            "  </model>",
        ]
    if texture:
        lines.append("  <texture>textures/hull.png</texture>")
    lines += list(extra_lines or ())
    lines += ["</PropertyList>", ""]
    return "\n".join(lines)


def base_layout():
    return {
        "schema": builder.LAYOUT_SCHEMA,
        "axes": builder.LAYOUT_AXES,
        "bounds_m": [[-12.0, 12.0], [-4.0, 18.0], [0.0, 9.0]],
        "triangles": {"lod0": 7, "lod1": 5, "lod2": 3, "hangar": 2},
        "spawn_slots": [
            {"id": "slot_a", "position_m": [0.0, 2.0, 1.5],
             "heading_deg": 0, "ship_ids": ["sa_tie"],
             "fixture_note": "preserved"},
            {"id": "slot_b", "position_m": [-6.0, 12.0, 1.5],
             "heading_deg": 180,
             "ship_ids": ["sa_tie", "sa_interceptor"]},
        ],
        "runtime_verified": False,
        "provenance": {"license_status": "unverified",
                       "source": "unit-test fixture"},
        "fixture_extra": {"preserved": True},
    }


def write_layout(pack, layout):
    (Path(pack) / "layout.json").write_text(
        json.dumps(layout, indent=2, allow_nan=True) + "\n",
        encoding="utf-8")


def make_carrier_pack(root, layout=None, xml_text=None, texture=True):
    """Build a tiny carrier pack; returns its root Path."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    counts = {"carrier_lod0.ac": 7, "carrier_lod1.ac": 5,
              "carrier_lod2.ac": 3, "hangar.ac": 2}
    for index, (name, triangles) in enumerate(counts.items()):
        tex = "textures/hull.png" if name == "carrier_lod0.ac" and texture \
            else None
        (root / name).write_text(
            ac_text(AC_OBJECT_NAMES[name], triangles, texture=tex),
            encoding="utf-8")
    if xml_text is None:
        xml_text = carrier_xml(texture=texture)
    (root / "carrier.xml").write_text(xml_text, encoding="utf-8")
    write_layout(root, layout if layout is not None else base_layout())
    if texture:
        textures = root / "textures"
        textures.mkdir()
        (textures / "hull.png").write_bytes(b"fixture texture bytes\n")
    # Deliberately outside the whitelist; it must never be copied.
    (root / "README.external.txt").write_text("not copied\n", encoding="utf-8")
    (root / "private").mkdir()
    (root / "private" / "secret.ac").write_text("not copied\n",
                                                encoding="utf-8")
    return root


def copy_arena(destination):
    destination = Path(destination)
    shutil.copytree(
        ARENA, destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return destination


def rewrite_arena_stg(arena, transform):
    path = Path(arena) / ARENA_STG
    original = path.read_text(encoding="utf-8")
    updated = transform(original)
    if updated == original:
        raise AssertionError("STG fixture mutation did not change the file")
    path.write_text(updated, encoding="utf-8", newline="\n")


class CarrierArenaBuildTest(unittest.TestCase):
    def _unified_pack(self, root):
        layout = base_layout()
        layout["geometry_mode"] = "unified"
        layout["triangles"]["hangar"] = 0
        xml = carrier_xml(models=("carrier_lod0.ac", "carrier_lod1.ac",
                                  "carrier_lod2.ac"))
        pack = make_carrier_pack(root, layout=layout, xml_text=xml)
        (pack / "hangar.ac").unlink()
        return pack

    def test_unified_carrier_has_no_second_hangar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = self._unified_pack(root / "pack")
            manifest = builder.build(ARENA, pack, root / "out")
            integration = manifest["carrier_integration"]
            self.assertEqual(integration["geometry_mode"], "unified")
            self.assertNotIn("hangar", integration["models"])
            self.assertEqual(integration["parsed_triangles"]["hangar"], 0)
            self.assertEqual(manifest["budget"]["carrier"]["runtime_lod0_plus_hangar"], 7)
            self.assertFalse(list((root / "out").rglob("hangar.ac")))

    def test_unified_rejects_duplicate_hangar_mesh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = self._unified_pack(root / "pack")
            (pack / "hangar.ac").write_text(ac_text("duplicate", 2))
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, root / "out")
            self.assertFalse((root / "out").exists())

    def test_unified_rejects_unloaded_lods(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = self._unified_pack(root / "pack")
            (pack / "carrier.xml").write_text(
                carrier_xml(models=("carrier_lod0.ac",)))
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, root / "out")
            self.assertFalse((root / "out").exists())

    def test_invalid_geometry_mode_and_unified_hangar_count(self):
        for mode, count in (("unknown", 0), ("unified", 1), ("unified", False)):
            with self.subTest(mode=mode, count=count):
                layout = base_layout()
                layout["geometry_mode"] = mode
                layout["triangles"]["hangar"] = count
                with self.assertRaises(builder.BuildError):
                    builder.validate_layout(layout)

    def test_declared_triangle_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = self._unified_pack(root / "pack")
            layout = json.loads((pack / "layout.json").read_text())
            layout["triangles"]["lod0"] += 1
            write_layout(pack, layout)
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, root / "out")
            self.assertFalse((root / "out").exists())

    def test_build_preserves_rebels_removes_empire_adds_carrier(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            before = tree_hashes(ARENA)
            manifest = builder.build(ARENA, pack, output, 450.0)
            self.assertEqual(tree_hashes(ARENA), before,
                             "source arena must be read-only")

            self.assertTrue((output / "manifest.json").is_file())
            self.assertEqual(
                (output / "source" / "manifest.json").read_bytes(),
                (ARENA / "manifest.json").read_bytes())
            self.assertEqual(
                (output / "source" / "942042.stg.original").read_bytes(),
                (ARENA / "Objects/w130n30/w123n37/942042.stg").read_bytes())
            for legacy in ("README.md", "VALIDATION.md",
                           "src/generate_package.py"):
                self.assertTrue((output / "source" / legacy).is_file(),
                                legacy)

            original_text = (ARENA /
                             "Objects/w130n30/w123n37/942042.stg").read_text(
                                 encoding="utf-8")
            original_rebels = [
                line for line in original_text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
                and not any(model in line for model in RETIRED_XML)
            ]
            output_text = (output /
                           "Objects/w130n30/w123n37/942042.stg").read_text(
                               encoding="utf-8")
            output_rebels = [
                line for line in output_text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
                and not any(model in line for model in RETIRED_XML)
                and "Models/venator/carrier.xml" not in line
            ]
            self.assertEqual(output_rebels, original_rebels)
            entries = stg_entries(
                output / "Objects/w130n30/w123n37/942042.stg")
            self.assertEqual(len(entries), len(original_rebels) + 1)
            for fields in entries:
                self.assertFalse(any(model in fields[1]
                                     for model in RETIRED_XML), fields)
            carrier = [fields for fields in entries
                       if fields[1] == "Models/venator/carrier.xml"]
            self.assertEqual(len(carrier), 1)
            fields = carrier[0]
            self.assertEqual(fields[0], "OBJECT_STATIC_AGL")
            self.assertAlmostEqual(float(fields[2]), -122.455000)
            self.assertAlmostEqual(float(fields[3]), 37.415000)
            self.assertAlmostEqual(float(fields[4]), 450.0)
            self.assertAlmostEqual(float(fields[5]), 110.0)
            self.assertIn(" 450.0 110.00", (output /
                          "Objects/w130n30/w123n37/942042.stg").read_text(
                              encoding="utf-8"))

            for name in builder.REQUIRED_CARRIER_FILES:
                emitted = output / CARRIER_DIR / name
                self.assertTrue(emitted.is_file(), name)
                self.assertEqual(emitted.read_bytes(),
                                 (pack / name).read_bytes(), name)
            self.assertEqual(
                (output / CARRIER_DIR / "textures" / "hull.png").read_bytes(),
                (pack / "textures" / "hull.png").read_bytes())
            self.assertFalse((output / CARRIER_DIR /
                              "README.external.txt").exists())
            self.assertFalse((output / CARRIER_DIR / "private").exists())

            self.assertTrue((output / "Objects/w130n30/w123n37/Models/"
                             "space_arena/reb_platform.ac").is_file())
            # Unreferenced legacy Empire model files are retained, but the
            # STG no longer places them.
            self.assertTrue((output / "Objects/w130n30/w123n37/Models/"
                             "space_arena/emp_carrier.ac").is_file())

            self.assertEqual(manifest["schema"], builder.OVERLAY_SCHEMA)
            self.assertEqual(manifest["schema_version"], 1)
            self.assertIs(manifest["runtime_verified"], False)
            self.assertEqual(manifest["launch"]["commands"], [])
            self.assertNotIn("triangles_total", manifest["budget"])
            self.assertNotIn("triangle_limit", manifest["budget"])
            self.assertIn("legacy_triangle_limit", manifest["budget"])
            self.assertEqual(manifest["budget"]["active_total"], 492)
            self.assertEqual(
                manifest["budget"]["carrier"]["parsed"],
                {"lod0": 7, "lod1": 5, "lod2": 3, "hangar": 2})
            self.assertEqual(
                manifest["budget"]["overlay_runtime_total"], 492 + 9)
            self.assertEqual(
                set(manifest["budget"]["legacy_empire_retained_unused"]),
                set(builder.RETIRED_MODELS))

            active = {placement["id"] for placement in
                      manifest["placements"]}
            self.assertEqual(active,
                             {"reb_platform", "reb_hangar", "reb_tower",
                              "reb_tanks", "reb_masts"})
            self.assertEqual(
                {placement["id"] for placement in
                 manifest["retired_empire_placements"]}, RETIRED_IDS)
            for placement in manifest["placements"]:
                self.assertEqual(placement["faction"], "rebels")
                self.assertEqual(placement["overlay_status"],
                                 "preserved_from_source")

            pose_ids = {pose["id"] for pose in manifest["poses"]}
            self.assertIn("pose_rebels_primary", pose_ids)
            self.assertNotIn("pose_empire_primary", pose_ids)
            obsolete_poses = manifest["obsolete"]["poses"]
            self.assertEqual([pose["id"] for pose in obsolete_poses],
                             ["pose_empire_primary"])
            self.assertEqual(obsolete_poses[0]["status"], "obsolete")
            self.assertIs(obsolete_poses[0]["primary"], False)
            self.assertNotIn("route_empire_exit",
                             {route["id"] for route in manifest["routes"]})
            self.assertEqual(
                [route["id"] for route in manifest["obsolete"]["routes"]],
                ["route_empire_exit"])
            self.assertNotIn(
                "emp_deck",
                {surface.get("placement_id")
                 for surface in manifest["surfaces"]})
            self.assertNotIn(
                "emp_carrier",
                {surface.get("placement_id")
                 for surface in manifest["surfaces"]})
            self.assertEqual(manifest["stack_pairs"], [])
            self.assertEqual(len(manifest["obsolete"]["stack_pairs"]), 1)

            integration = manifest["carrier_integration"]
            self.assertIs(integration["runtime_verified"], False)
            self.assertEqual(integration["schema"], builder.LAYOUT_SCHEMA)
            self.assertEqual(integration["layout_axes"], builder.LAYOUT_AXES)
            self.assertAlmostEqual(integration["anchor"]["agl_m"], 450.0)
            self.assertAlmostEqual(integration["agl_m"], 450.0)
            self.assertIsNone(integration["anchor"]["msl_m"])
            self.assertAlmostEqual(integration["anchor"]["lon"], -122.455)
            self.assertAlmostEqual(integration["anchor"]["lat"], 37.415)
            self.assertEqual(integration["stg"]["stg_heading_deg"], 110.0)
            self.assertEqual(integration["stg"]["heading_deg"], 250.0)
            self.assertEqual(integration["transforms"]["ac"],
                             ["-Y", "Z", "-X"])
            self.assertEqual(
                integration["transforms"]["layout_to_ac"],
                {"x": "-Y", "y": "Z", "z": "-X"})
            slots = integration["spawn_slots"]
            self.assertEqual([slot["id"] for slot in slots],
                             ["slot_a", "slot_b"])
            self.assertEqual(slots[0]["position_ac_m"],
                             [-2.0, 1.5, 0.0])
            self.assertEqual(slots[0]["fixture_note"], "preserved")
            self.assertEqual(integration["external_pack"]["license_status"],
                             "unverified")
            self.assertEqual(
                integration["external_pack"]["license_status_source"],
                "unverified")
            self.assertFalse(
                integration["external_pack"]["redistributed_in_repo_git"])

            self.assertEqual(manifest["source"]["manifest_sha256"],
                             sha256(ARENA / "manifest.json"))
            self.assertEqual(
                manifest["elevation_definition"]["carrier_agl_m"], 450.0)

            hashes = manifest["provenance"]["artifact_sha256"]
            emitted = {
                path.relative_to(output).as_posix()
                for path in output.rglob("*") if path.is_file()
            }
            self.assertEqual(set(hashes), emitted - {"manifest.json"})
            for relative, expected in hashes.items():
                self.assertEqual(
                    sha256(output / relative), expected, relative)
            self.assertEqual(manifest["provenance"]["builder_sha256"],
                             sha256(TOOL))

            leftovers = [entry.name for entry in temporary.iterdir()
                         if entry.name.startswith(".out.tmp-")]
            self.assertEqual(leftovers, [])

    def test_existing_empty_output_is_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            output.mkdir()
            builder.build(ARENA, pack, output)
            self.assertTrue((output / "manifest.json").is_file())

    def test_nonempty_output_and_overlap_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            nonempty = temporary / "nonempty"
            nonempty.mkdir()
            (nonempty / "keep.txt").write_text("keep\n", encoding="utf-8")
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, nonempty)
            self.assertEqual((nonempty / "keep.txt").read_text(),
                             "keep\n")

            inside_arena = ARENA / "carrier-overlay-must-not-exist"
            self.assertFalse(inside_arena.exists())
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, inside_arena)
            self.assertFalse(inside_arena.exists())

            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, pack)
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, pack / "out")
            self.assertFalse((pack / "out").exists())

            output_file = temporary / "file"
            output_file.write_text("x", encoding="utf-8")
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, output_file)

    def test_failed_build_leaves_no_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            layout = base_layout()
            layout["runtime_verified"] = True
            write_layout(pack, layout)

            missing = temporary / "missing"
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, missing)
            self.assertFalse(missing.exists())
            self.assertEqual(
                [entry.name for entry in temporary.iterdir()
                 if entry.name.startswith(".missing.tmp-")], [])

            empty = temporary / "empty"
            empty.mkdir()
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, empty)
            self.assertEqual(list(empty.iterdir()), [])
            self.assertEqual(
                [entry.name for entry in temporary.iterdir()
                 if entry.name.startswith(".empty.tmp-")], [])

    def test_layout_validation_rejects_bad_vectors_and_claims(self):
        def set_nan(layout):
            layout["bounds_m"][0][1] = float("nan")

        def bad_triangle_order(layout):
            layout["triangles"]["lod1"] = layout["triangles"]["lod0"]

        def bad_triangle_type(layout):
            layout["triangles"]["hangar"] = 2.0

        def bad_triangle_zero(layout):
            layout["triangles"]["hangar"] = 0

        def duplicate_slot(layout):
            layout["spawn_slots"][1]["id"] = layout["spawn_slots"][0]["id"]

        def outside_bounds(layout):
            layout["spawn_slots"][0]["position_m"][0] = 99.0

        def nan_position(layout):
            layout["spawn_slots"][0]["position_m"][1] = float("inf")

        def runtime_verified(layout):
            layout["runtime_verified"] = True

        def license_unknown(layout):
            layout["provenance"]["license_status"] = "unknown"

        def missing_slots(layout):
            layout["spawn_slots"] = []

        def duplicate_ship(layout):
            layout["spawn_slots"][0]["ship_ids"] = ["sa_tie", "sa_tie"]

        def bad_axes(layout):
            layout["axes"] = "X right, Y up, Z forward"

        def bad_schema(layout):
            layout["schema"] = "bombable.carrier-layout/v2"

        def inverted_bounds(layout):
            layout["bounds_m"][0] = [12.0, -12.0]

        def extra_nan(layout):
            layout["fixture_extra"]["bad"] = float("nan")

        mutations = [
            ("nan bound", set_nan),
            ("triangle order", bad_triangle_order),
            ("triangle type", bad_triangle_type),
            ("triangle zero", bad_triangle_zero),
            ("duplicate slot", duplicate_slot),
            ("slot outside bounds", outside_bounds),
            ("nan position", nan_position),
            ("runtime verified", runtime_verified),
            ("license unknown", license_unknown),
            ("missing slots", missing_slots),
            ("duplicate ship", duplicate_ship),
            ("bad axes", bad_axes),
            ("bad schema", bad_schema),
            ("inverted bounds", inverted_bounds),
            ("extra nan", extra_nan),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            for label, mutation in mutations:
                layout = copy.deepcopy(base_layout())
                mutation(layout)
                write_layout(pack, layout)
                output = temporary / ("out_" + label.replace(" ", "_"))
                with self.subTest(label=label):
                    with self.assertRaises(builder.BuildError):
                        builder.build(ARENA, pack, output)
                    self.assertFalse(output.exists())

    def test_xml_traversal_absolute_missing_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            cases = {
                "traversal": ('<?xml version="1.0"?><PropertyList>'
                              '<path>../outside.ac</path></PropertyList>'),
                "absolute": ('<?xml version="1.0"?><PropertyList>'
                             '<path>/tmp/outside.ac</path></PropertyList>'),
                "backslash": ('<?xml version="1.0"?><PropertyList>'
                              '<path>..\\\\outside.ac</path></PropertyList>'),
                "missing": ('<?xml version="1.0"?><PropertyList>'
                            '<path>carrier_lod9.ac</path></PropertyList>'),
                "texture_traversal": (
                    '<?xml version="1.0"?><PropertyList>'
                    '<path>carrier_lod0.ac</path>'
                    '<texture>textures/../../outside.png</texture>'
                    '</PropertyList>'),
            }
            for label, xml_text in cases.items():
                pack = make_carrier_pack(temporary / ("pack_" + label),
                                         xml_text=xml_text)
                output = temporary / ("out_" + label)
                with self.subTest(label=label):
                    with self.assertRaises(builder.BuildError):
                        builder.build(ARENA, pack, output)
                    self.assertFalse(output.exists())

            # Symlinked required file.
            pack = make_carrier_pack(temporary / "pack_symlink_file")
            outside = temporary / "outside.ac"
            outside.write_text(ac_text("outside", 4), encoding="utf-8")
            (pack / "carrier_lod1.ac").unlink()
            os.symlink(outside, pack / "carrier_lod1.ac")
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, temporary / "out_symlink_file")

            # Symlinked texture inside textures/.
            pack = make_carrier_pack(temporary / "pack_symlink_texture")
            os.symlink(outside, pack / "textures" / "linked.png")
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, pack, temporary / "out_symlink_texture")

            # Symlinked carrier root.
            target = make_carrier_pack(temporary / "pack_real")
            root_link = temporary / "pack_link"
            os.symlink(target, root_link)
            with self.assertRaises(builder.BuildError):
                builder.build(ARENA, root_link, temporary / "out_root_link")

    def test_missing_required_carrier_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            for index, name in enumerate(builder.REQUIRED_CARRIER_FILES):
                pack = make_carrier_pack(temporary / ("pack_%d" % index))
                (pack / name).unlink()
                output = temporary / ("out_%d" % index)
                with self.subTest(missing=name):
                    with self.assertRaises(builder.BuildError):
                        builder.build(ARENA, pack, output)
                    self.assertFalse(output.exists())

    def test_invalid_arena_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            arena = copy_arena(temporary / "arena")
            manifest = json.loads(
                (arena / "manifest.json").read_text(encoding="utf-8"))
            manifest["schema"] = "wrong.schema"
            (arena / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            with self.assertRaises(builder.BuildError):
                builder.build(arena, pack, output)
            self.assertFalse(output.exists())

    def test_cli_success_and_failure_exit_codes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "cli_out"
            code = builder.main([
                "--arena-root", str(ARENA),
                "--carrier-root", str(pack),
                "--output-root", str(output),
            ])
            self.assertEqual(code, 0)
            self.assertTrue((output / "manifest.json").is_file())

            layout = base_layout()
            layout["runtime_verified"] = True
            write_layout(pack, layout)
            failed_output = temporary / "cli_failed"
            code = builder.main([
                "--arena-root", str(ARENA),
                "--carrier-root", str(pack),
                "--output-root", str(failed_output),
            ])
            self.assertEqual(code, 2)
            self.assertFalse(failed_output.exists())

    def test_pending_license_status_is_normalised_to_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            layout = base_layout()
            layout["provenance"]["license_status"] = (
                "source_urls_recorded_terms_pending")
            pack = make_carrier_pack(temporary / "pack", layout=layout)
            output = temporary / "out"
            manifest = builder.build(ARENA, pack, output)
            external = manifest["carrier_integration"]["external_pack"]
            self.assertEqual(external["license_status"], "unverified")
            self.assertEqual(external["license_status_source"],
                             "source_urls_recorded_terms_pending")

    def test_multi_model_lod_xml_is_accepted(self):
        extra = []
        for target, low, high in (
                ("Venator_LOD0", "0", "1500"),
                ("Venator_LOD1", "1500", "6000"),
                ("Venator_LOD2", "6000", None),
                ("Venator_Hangar", "0", "1200")):
            extra += [
                "  <animation>",
                "    <type>range</type>",
                "    <object-name>%s</object-name>" % target,
                "    <min-m>%s</min-m>" % low,
            ]
            if high is not None:
                extra.append("    <max-m>%s</max-m>" % high)
            extra.append("  </animation>")
        xml_text = carrier_xml(extra_lines=extra)
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack", xml_text=xml_text)
            output = temporary / "out"
            builder.build(ARENA, pack, output)
            self.assertEqual(
                (output / CARRIER_DIR / "carrier.xml").read_text(
                    encoding="utf-8"),
                xml_text)

    def test_agl_is_an_explicit_design_parameter(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            manifest = builder.build(ARENA, pack, output, 500.0)
            entries = stg_entries(
                output / "Objects/w130n30/w123n37/942042.stg")
            carrier = [fields for fields in entries
                       if fields[1] == "Models/venator/carrier.xml"][0]
            self.assertAlmostEqual(float(carrier[4]), 500.0)
            self.assertEqual(
                manifest["carrier_integration"]["anchor"]["agl_m"], 500.0)
            self.assertIsNone(
                manifest["carrier_integration"]["anchor"]["msl_m"])
            self.assertIn(
                "NOT a measured MSL",
                (output / "README.md").read_text(encoding="utf-8"))

    def test_carrier_xml_requires_one_model_wrapper_per_ac(self):
        all_paths = "".join("<path>%s</path>" % name
                            for name in ALL_CARRIER_MODELS)
        cases = {
            "many_paths_in_one_model": (
                "<PropertyList><model><name>Venator_all</name>%s</model>"
                "</PropertyList>" % all_paths),
            "root_path_list": (
                "<PropertyList>%s</PropertyList>" % "".join(
                    "<path>%s</path>" % name
                    for name in ALL_CARRIER_MODELS)),
            "path_to_texture": carrier_xml(
                models=("carrier_lod1.ac", "carrier_lod2.ac", "hangar.ac"),
                texture=True).replace(
                    "</PropertyList>",
                    "<model><name>Venator_texture</name>"
                    "<path>textures/hull.png</path></model></PropertyList>"),
            "duplicate_model": carrier_xml(
                models=("carrier_lod0.ac", "carrier_lod0.ac",
                        "carrier_lod1.ac", "carrier_lod2.ac", "hangar.ac")),
            "missing_hangar": carrier_xml(
                models=("carrier_lod0.ac", "carrier_lod1.ac",
                        "carrier_lod2.ac")),
            "nested_model": (
                "<PropertyList><model><name>Venator_outer</name>"
                "<path>carrier_lod0.ac</path>"
                "<model><name>Venator_inner</name>"
                "<path>carrier_lod1.ac</path></model></model>"
                "</PropertyList>"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            for label, xml_text in cases.items():
                pack = make_carrier_pack(temporary / ("pack_" + label),
                                         xml_text=xml_text)
                output = temporary / ("out_" + label)
                with self.subTest(label=label):
                    with self.assertRaises(builder.BuildError) as caught:
                        builder.build(ARENA, pack, output)
                    self.assertIn("XML", str(caught.exception))
                    self.assertFalse(output.exists())

    def test_range_animations_must_target_real_ac_groups(self):
        def range_animation(target, min_m="0", max_m="1500"):
            lines = ["  <animation>", "    <type>range</type>"]
            if target is not None:
                lines.append("    <object-name>%s</object-name>" % target)
            lines += ["    <min-m>%s</min-m>" % min_m,
                      "    <max-m>%s</max-m>" % max_m,
                      "  </animation>"]
            return lines

        duplicate = (range_animation("Venator_LOD0")
                     + range_animation("Venator_LOD0"))
        cases = {
            "unknown_target": range_animation("Missing_Group"),
            "empty_target": range_animation(None),
            "duplicate_target": duplicate,
            "non_numeric_bound": range_animation(
                "Venator_LOD0", min_m="near"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            for label, extra in cases.items():
                pack = make_carrier_pack(
                    temporary / ("pack_" + label),
                    xml_text=carrier_xml(extra_lines=extra))
                output = temporary / ("out_" + label)
                with self.subTest(label=label):
                    with self.assertRaises(builder.BuildError) as caught:
                        builder.build(ARENA, pack, output)
                    self.assertIn("range animation", str(caught.exception))
                    self.assertFalse(output.exists())

    def test_arena_and_carrier_roots_must_not_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)

            # Carrier pack nested inside the arena would be copied by the
            # arena tree copy (private/secret.ac, README, ...).
            arena = copy_arena(temporary / "arena")
            pack = make_carrier_pack(arena / "external_carrier_pack")
            output = temporary / "out_carrier_inside_arena"
            with self.assertRaises(builder.BuildError) as caught:
                builder.build(arena, pack, output)
            self.assertIn("must not overlap", str(caught.exception))
            self.assertFalse(output.exists())

            # Arena nested inside the carrier pack has the same hazard.
            carrier = temporary / "carrier_root"
            carrier.mkdir()
            nested_arena = copy_arena(carrier / "arena")
            nested_pack = make_carrier_pack(carrier)
            output = temporary / "out_arena_inside_carrier"
            with self.assertRaises(builder.BuildError) as caught:
                builder.build(nested_arena, nested_pack, output)
            self.assertIn("must not overlap", str(caught.exception))
            self.assertFalse(output.exists())

            # Identical roots are rejected as well.
            with self.assertRaises(builder.BuildError):
                builder.build(arena, arena, temporary / "out_same_root")

    def test_output_inside_repository_is_rejected_without_writing(self):
        candidate = builder.REPO_ROOT / "out" / "carrier_overlay"
        self.assertFalse(candidate.exists())
        with tempfile.TemporaryDirectory() as temporary:
            pack = make_carrier_pack(Path(temporary) / "pack")
            with self.assertRaises(builder.BuildError) as caught:
                builder._check_output_root(candidate, ARENA, pack)
            self.assertIn("outside the repository", str(caught.exception))
            with self.assertRaises(builder.BuildError):
                builder._check_output_root(builder.REPO_ROOT, ARENA, pack)
        self.assertFalse(candidate.exists())


    def test_empire_anchor_must_belong_to_the_stg_cell(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            arena = copy_arena(temporary / "arena")
            manifest = json.loads(
                (arena / "manifest.json").read_text(encoding="utf-8"))
            manifest["region"]["anchors"]["empire"] = {
                "lon": -121.5,
                "lat": 38.5,
            }
            (arena / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            with self.assertRaises(builder.BuildError) as caught:
                builder.build(arena, pack, output)
            self.assertIn("STG cell", str(caught.exception))
            self.assertFalse(output.exists())

    def test_nonzero_agl_offsets_are_preserved_from_the_stg(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            arena = copy_arena(temporary / "arena")
            rewrite_arena_stg(
                arena, lambda text: text.replace(" 0.0 ", " 5.0 "))
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            manifest = builder.build(arena, pack, output)

            for placement in manifest["placements"]:
                record = placement["source_stg"]
                self.assertEqual(record["kind"], "OBJECT_STATIC_AGL")
                self.assertEqual(record["offset_m"], 5.0)
                self.assertEqual(record["offset_datum"], "agl_offset_m")
                self.assertIn("above ground level",
                              placement["elevation_semantics"])
            elevation = manifest["elevation_definition"]
            self.assertEqual(elevation["active_agl_offset_m"], 5.0)
            self.assertEqual(elevation["active_offset_summary"],
                             "uniform_agl")
            self.assertEqual(
                elevation["per_placement"]["reb_platform"]["offset_m"], 5.0)
            output_stg = (output / ARENA_STG).read_text(encoding="utf-8")
            self.assertIn("reb_platform.xml -122.430000 37.430000 5.0 "
                          "110.00", output_stg)
            self.assertNotIn("Rebel entries keep 0.0 AGL", output_stg)

    def test_mixed_agl_offsets_are_null_summary_and_per_placement(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            arena = copy_arena(temporary / "arena")
            rewrite_arena_stg(
                arena,
                lambda text: text.replace(
                    "Models/space_arena/reb_platform.xml "
                    "-122.430000 37.430000 0.0 110.00",
                    "Models/space_arena/reb_platform.xml "
                    "-122.430000 37.430000 7.5 110.00"))
            pack = make_carrier_pack(temporary / "pack")
            manifest = builder.build(arena, pack, temporary / "out")
            elevation = manifest["elevation_definition"]
            self.assertIsNone(elevation["active_agl_offset_m"])
            self.assertEqual(elevation["active_offset_summary"],
                             "mixed_or_empty")
            self.assertEqual(
                elevation["per_placement"]["reb_platform"]["offset_m"], 7.5)
            self.assertEqual(
                elevation["per_placement"]["reb_hangar"]["offset_m"], 0.0)

    def test_object_static_source_entry_keeps_its_msl_datum(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            arena = copy_arena(temporary / "arena")
            rewrite_arena_stg(
                arena,
                lambda text: text.replace(
                    "OBJECT_STATIC_AGL Models/space_arena/reb_platform.xml "
                    "-122.430000 37.430000 0.0 110.00",
                    "OBJECT_STATIC Models/space_arena/reb_platform.xml "
                    "-122.430000 37.430000 123.5 110.00"))
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            manifest = builder.build(arena, pack, output)

            placement = next(item for item in manifest["placements"]
                             if item["id"] == "reb_platform")
            self.assertEqual(placement["source_stg"]["kind"],
                             "OBJECT_STATIC")
            self.assertEqual(placement["source_stg"]["offset_m"], 123.5)
            self.assertEqual(placement["source_stg"]["offset_datum"],
                             "elevation_msl_m")
            self.assertIn("not an AGL offset",
                          placement["elevation_semantics"])
            self.assertIsNone(
                manifest["elevation_definition"]["active_agl_offset_m"])
            output_stg = (output / ARENA_STG).read_text(encoding="utf-8")
            self.assertIn(
                "OBJECT_STATIC Models/space_arena/reb_platform.xml "
                "-122.430000 37.430000 123.5 110.00", output_stg)

    def test_budget_note_uses_the_actual_source_budget(self):
        source_budget = json.loads(
            (ARENA / "manifest.json").read_text(
                encoding="utf-8"))["budget"]
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            manifest = builder.build(ARENA, pack, temporary / "out")
            note = manifest["budget"]["note"]
            self.assertIn(str(source_budget["triangles_total"]), note)
            self.assertIn(str(source_budget["triangle_limit"]), note)

    def test_artifact_set_rejects_symlinks_and_extra_carrier_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            pack = make_carrier_pack(temporary / "pack")
            output = temporary / "out"
            builder.build(ARENA, pack, output)
            for path in output.rglob("*"):
                self.assertFalse(path.is_symlink(), path)
            # Only the whitelist, not the ignored source entries.
            emitted = {
                path.relative_to(output / CARRIER_DIR).as_posix()
                for path in (output / CARRIER_DIR).rglob("*")
                if path.is_file()
            }
            self.assertEqual(
                emitted,
                set(builder.REQUIRED_CARRIER_FILES) | {"textures/hull.png"})


if __name__ == "__main__":
    unittest.main()
