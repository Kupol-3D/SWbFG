#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Static tests for tools/build_tie_packages.py.

The model fixtures are deliberately tiny (one AC triangle each): they exercise
metadata/package assembly, not aircraft completion.  No test launches
FlightGear and no runtime/visual/collision/FDM acceptance is claimed.
"""

import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools" / "build_tie_packages.py"
sys.path.insert(0, str(ROOT / "tools"))
import build_tie_packages as btp  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def ac_text(name, material="fixture", texture=None):
    line = (f'MATERIAL "{material}" rgb 0.6 0.6 0.6 amb 0.6 0.6 0.6 '
            'emis 0 0 0 spec 0.2 0.2 0.2 shi 8 trans 0')
    if texture:
        line += f' texture "{texture}"'
    return (
        "AC3Db\n"
        f"{line}\n"
        "OBJECT world\n"
        'name "world"\n'
        "kids 1\n"
        "OBJECT poly\n"
        f'name "{name}"\n'
        "loc 0 0 0\n"
        "texture -1\n"
        "crease 45\n"
        "numvert 3\n"
        "0 0 0\n"
        "1 0 0\n"
        "0 1 0\n"
        "numsurf 1\n"
        "SURF 0x20\n"
        "mat 0\n"
        "refs 3\n"
        "0 0 0\n"
        "1 0 0\n"
        "2 0 0\n"
        "kids 0\n"
    )


def default_integration(asset_id, mode=None):
    if mode is None:
        mode = "remote" if asset_id == "m1_bizarro" else "canon"
    return {
        "schema": btp.INTEGRATION_SCHEMA,
        "asset_id": asset_id,
        "frame": {"x": "right", "y": "forward", "z": "up"},
        "cameras": [
            {"id": "pilot", "role": "internal", "type": "lookfrom",
             "eye_asset_xyz_m": [1.0, 2.0, 3.0], "fov_deg": 80,
             "nearplane_m": 0.05},
            {"id": "external", "role": "external", "type": "lookat",
             "eye_asset_xyz_m": [-4.0, 6.0, 2.0], "fov_deg": 45,
             "nearplane_m": 0.1, "heading_offset_deg": 180},
        ],
        "cockpit": {"mode": mode},
    }


def minimal_profile(asset_id):
    return {
        "schema_version": 1,
        "ship_id": asset_id,
        "status": "GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS",
        "frame": {"x": "right", "y": "forward", "z": "up"},
        "scale_reference_length_m": 9.6,
        "mass_gameplay_kg": 8000,
        "mass_status": "fixture estimate, not measured",
        "assisted_flight": {"speed_limit_m_s": 347.2,
                            "forward_accel_m_s2": 60},
    }


def make_assets(root, ships=("royal_guard", "m1_bizarro", "boarding_craft")):
    ships_root = root / "ships"
    ships_root.mkdir(parents=True, exist_ok=True)
    for asset_id in ships:
        ship_dir = ships_root / asset_id
        (ship_dir / "export").mkdir(parents=True, exist_ok=True)
        (ship_dir / "export" / "game_lod0.ac").write_text(
            ac_text(f"{asset_id}_hull"), encoding="utf-8")
        (ship_dir / "export" / "cockpit.ac").write_text(
            ac_text(f"{asset_id}_cockpit"), encoding="utf-8")
        (ship_dir / "export" / "integration.json").write_text(
            json.dumps(default_integration(asset_id)), encoding="utf-8")
        (ship_dir / "flight_profile.json").write_text(
            json.dumps(minimal_profile(asset_id)), encoding="utf-8")
    return ships_root


def run_builder(assets, output, *extra):
    command = [sys.executable, str(BUILDER),
               "--assets-root", str(assets),
               "--output-root", str(output), *map(str, extra)]
    return subprocess.run(command, capture_output=True, text=True,
                          cwd=str(ROOT))


def tree_hashes(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
# Independent AC reader for generated files
# ---------------------------------------------------------------------------


def read_generated_ac(path):
    """Small independent AC3Db reader (groups/nesting, counts and textures)."""
    tokens = shlex.split(path.read_text(encoding="utf-8"))
    index = 0
    assert tokens[index] == "AC3Db"
    index += 1
    materials = []
    while index < len(tokens) and tokens[index] == "MATERIAL":
        index += 1
        name = tokens[index]
        index += 1
        texture = None
        while index < len(tokens) and tokens[index] not in ("MATERIAL",
                                                            "OBJECT"):
            key = tokens[index]
            if key == "texture":
                index += 1
                texture = tokens[index]
                index += 1
            elif key in ("rgb", "amb", "emis", "spec"):
                index += 4
            elif key in ("shi", "trans"):
                index += 2
            else:
                raise AssertionError(f"unexpected material field {key}")
        materials.append({"name": name, "texture": texture})

    objects = {}

    def parse_object(ancestry):
        nonlocal index
        assert tokens[index] == "OBJECT"
        index += 1
        kind = tokens[index]
        index += 1
        assert tokens[index] == "name"
        index += 1
        name = tokens[index]
        index += 1
        vertices = 0
        faces = 0
        object_textures = []
        while index < len(tokens) and tokens[index] != "OBJECT":
            key = tokens[index]
            index += 1
            if key == "loc":
                index += 3
            elif key == "texture":
                object_textures.append(tokens[index])
                index += 1
            elif key == "crease":
                index += 1
            elif key == "numvert":
                count = int(tokens[index])
                index += 1
                vertices += count
                index += 3 * count
            elif key == "numsurf":
                count = int(tokens[index])
                index += 1
                for _ in range(count):
                    assert tokens[index] == "SURF"
                    index += 1
                    index += 1  # flags
                    assert tokens[index] == "mat"
                    index += 2
                    assert tokens[index] == "refs"
                    index += 1
                    refs = int(tokens[index])
                    index += 1
                    index += 3 * refs
                faces += count
            elif key == "kids":
                count = int(tokens[index])
                index += 1
                for _ in range(count):
                    parse_object(ancestry + (name,))
            else:
                raise AssertionError(f"unexpected object field {key}")
        objects[name] = {"kind": kind, "vertices": vertices, "faces": faces,
                         "ancestry": ancestry, "textures": object_textures,
                         "materials": materials}
        return name

    parse_object(())
    assert index == len(tokens), tokens[index:]
    return materials, objects


# ---------------------------------------------------------------------------
# Camera maths
# ---------------------------------------------------------------------------


class TestCameraMath(unittest.TestCase):
    def test_asset_point_maps_to_fg_model_axes(self):
        self.assertEqual(btp.asset_to_fg_model((1.0, 2.0, 3.0)),
                         (-2.0, 1.0, 3.0))

    def test_asset_eye_maps_to_right_up_aft_offsets(self):
        self.assertEqual(btp.asset_eye_to_view_offsets((1.0, 2.0, 3.0)),
                         (1.0, 3.0, -2.0))

    def test_explicit_fg_offsets_are_not_guessed_or_converted(self):
        camera = btp._normalise_camera(
            {"id": "pilot", "role": "internal", "type": "lookfrom",
             "fg_offsets_right_up_aft_m": [9.0, 8.0, 7.0],
             "fov_deg": 75, "nearplane_m": 0.03}, 0, "fixture")
        self.assertEqual(camera.offsets, (9.0, 8.0, 7.0))
        self.assertEqual(camera.source, "explicit-fg-offsets")

    def test_missing_eye_data_is_rejected(self):
        with self.assertRaises(btp.BuildError):
            btp._normalise_camera(
                {"id": "pilot", "role": "internal", "type": "lookfrom",
                 "fov_deg": 80, "nearplane_m": 0.05}, 0, "fixture")

    def test_internal_fov_and_nearplane_bounds_are_enforced(self):
        base = {"id": "pilot", "role": "internal", "type": "lookfrom",
                "eye_asset_xyz_m": [0, 0, 0], "nearplane_m": 0.05}
        with self.assertRaises(btp.BuildError):
            btp._normalise_camera({**base, "fov_deg": 70}, 0, "fixture")
        with self.assertRaises(btp.BuildError):
            btp._normalise_camera({**base, "fov_deg": 80,
                                   "nearplane_m": 0.1}, 0, "fixture")

    def test_external_camera_must_be_lookat(self):
        with self.assertRaises(btp.BuildError):
            btp._normalise_camera(
                {"id": "outside", "role": "external", "type": "lookfrom",
                 "eye_asset_xyz_m": [0, 0, 0], "fov_deg": 45,
                 "nearplane_m": 0.1}, 0, "fixture")


# ---------------------------------------------------------------------------
# Built packages
# ---------------------------------------------------------------------------


class TestBuiltPackages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.assets = make_assets(root / "fixtures")
        cls.out = root / "Aircraft"
        cls.result = run_builder(cls.assets, cls.out)
        if cls.result.returncode != 0:
            raise RuntimeError("fixture build failed: " + cls.result.stderr)
        cls.packages = {spec.package: cls.out / spec.package
                        for spec in btp.SHIPS.values()}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_expected_files_exist(self):
        for package, pkg in self.packages.items():
            with self.subTest(package=package):
                asset = next(spec.asset_id for spec in btp.SHIPS.values()
                             if spec.package == package)
                expected = [
                    f"Models/{asset}.ac",
                    f"Models/{asset}.xml",
                    "Effects/visual.eff",
                    f"{package}-set.xml",
                    "Config/flight_profile.json",
                    "LICENSE_ATTRIBUTION.md",
                    "README.md",
                    "Sources/SHA256SUMS",
                    "Sources/BUILD_MANIFEST.json",
                ]
                for relative in expected:
                    self.assertTrue((pkg / relative).is_file(), relative)

    def test_xml_files_parse_and_paths_stay_inside_package(self):
        fg_root = self.out.parent
        for package, pkg in self.packages.items():
            for path in sorted(pkg.rglob("*.xml")) + sorted(
                    pkg.rglob("*.eff")):
                root = ET.parse(path).getroot()
                self.assertEqual(root.tag, "PropertyList", path)
                for element in root.iter():
                    if element.tag not in ("path", "inherits-from", "file",
                                           "aero"):
                        continue
                    value = (element.text or "").strip()
                    if not value:
                        continue
                    if element.tag == "inherits-from" and not value.startswith(
                            "Aircraft/"):
                        # Stock FGData effects (model-combined) are external
                        # by contract; they must still be safe references.
                        self.assertFalse(Path(value).is_absolute(), value)
                        self.assertNotIn("..", value.split("/"), value)
                        continue
                    if value.startswith("Aircraft/"):
                        resolved = fg_root / value
                    else:
                        resolved = path.parent / value
                    if element.tag == "inherits-from":
                        resolved = Path(str(resolved) + ".eff")
                    self.assertFalse(Path(value).is_absolute(), value)
                    self.assertTrue(resolved.is_file(),
                                    f"{path}: {value} -> {resolved}")
                    self.assertTrue(resolved.resolve().is_relative_to(
                        pkg.resolve()), value)

    def test_effect_uses_task2_native_als_pattern(self):
        for package, pkg in self.packages.items():
            wrapper = ET.parse(pkg / "Models" /
                               f"{next(spec.asset_id for spec in btp.SHIPS.values() if spec.package == package)}.xml").getroot()
            effects = wrapper.findall("effect")
            self.assertEqual(len(effects), 1)
            self.assertEqual(effects[0].findtext("default"), "true")
            inherits = effects[0].findtext("inherits-from")
            self.assertEqual(inherits,
                             f"Aircraft/{package}/Effects/visual")
            effect = ET.parse(pkg / "Effects/visual.eff").getroot()
            self.assertEqual(effect.findtext("inherits-from"),
                             "Effects/model-combined")
            self.assertIsNone(effect.find(".//technique"))
            self.assertIsNone(effect.find(".//predicate"))
            self.assertEqual(effect.findtext("name"),
                             f"Aircraft/{package}/Effects/visual")

    def test_camera_conversion_in_set_xml(self):
        profile = ET.parse(self.packages["sa_tie_royal_guard"] /
                           "sa_tie_royal_guard-set.xml").getroot()
        views = profile.findall("sim/view")
        self.assertGreaterEqual(len(views), 2)
        first = views[0].find("config")
        self.assertEqual(views[0].findtext("type"), "lookfrom")
        self.assertEqual(views[0].findtext("internal"), "true")
        self.assertEqual(tuple(float(first.findtext(tag)) for tag in
                               ("x-offset-m", "y-offset-m", "z-offset-m")),
                         (1.0, 3.0, -2.0))
        self.assertEqual(float(first.findtext(
            "default-field-of-view-deg")), 80.0)
        self.assertEqual(float(first.findtext(
            "ground-level-nearplane-m")), 0.05)
        second = views[1].find("config")
        self.assertEqual(views[1].findtext("type"), "lookat")
        self.assertEqual(views[1].findtext("internal"), "false")
        self.assertEqual(tuple(float(second.findtext(tag)) for tag in
                               ("x-offset-m", "y-offset-m", "z-offset-m")),
                         (-4.0, 2.0, -6.0))

    def test_set_xml_is_visual_viewer_with_null_fdm(self):
        for package, pkg in self.packages.items():
            profile = ET.parse(pkg / f"{package}-set.xml").getroot()
            self.assertEqual(profile.findtext("sim/flight-model"), "null")
            self.assertEqual(profile.findtext("sim/space-arena/ship-id"),
                             next(spec.asset_id for spec in btp.SHIPS.values()
                                  if spec.package == package))
            self.assertEqual(profile.findtext("sim/space-arena/team-id"),
                             "empire")
            model_path = profile.findtext("sim/model/path")
            self.assertTrue(model_path.startswith(f"Aircraft/{package}/"))
            self.assertTrue((self.out.parent / model_path).is_file())

    def test_profile_is_copied_without_mutation(self):
        for asset_id, spec in btp.SHIPS.items():
            source = self.assets / asset_id / "flight_profile.json"
            copied = self.packages[spec.package] / "Config" / \
                "flight_profile.json"
            self.assertEqual(source.read_bytes(), copied.read_bytes())
            self.assertEqual(json.loads(copied.read_text())["status"],
                             "GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS")

    def test_merged_ac_structure_and_texture_containment(self):
        for asset_id, spec in btp.SHIPS.items():
            pkg = self.packages[spec.package]
            materials, objects = read_generated_ac(pkg / "Models" /
                                                   f"{asset_id}.ac")
            self.assertIn("Hull", objects)
            self.assertIn("Cockpit", objects)
            # Minimal fixture: one triangle in each input => exactly two.
            total_faces = sum(node["faces"] for node in objects.values())
            self.assertEqual(total_faces, 2, asset_id)
            poly_nodes = [node for node in objects.values()
                          if node["kind"] == "poly"]
            self.assertEqual(len(poly_nodes), 2, asset_id)
            for node in poly_nodes:
                self.assertEqual(node["vertices"], 3)
            for material in materials:
                self.assertIsNone(material["texture"])
            for name, node in objects.items():
                for token in node["textures"]:
                    if token == "-1":
                        continue
                    self.assertFalse(token.startswith("/"), token)
                    self.assertNotIn("..", token.split("/"), token)
                    resolved = (pkg / "Models" / token).resolve()
                    self.assertTrue(resolved.is_file(),
                                    f"{name}: {token}")

    def test_license_attribution_records_author_url_license_and_changes(self):
        urls = {spec.package: spec.url for spec in btp.SHIPS.values()}
        for package, pkg in self.packages.items():
            text = (pkg / "LICENSE_ATTRIBUTION.md").read_text()
            self.assertIn("KonbayanSW", text)
            self.assertIn(urls[package], text)
            self.assertIn("CC BY", text)
            self.assertIn("version", text.lower())
            self.assertIn("## Changes applied", text)
            self.assertIn("GPL-2.0-or-later", text)
        boarding = (self.packages["sa_tie_boarding"] /
                    "LICENSE_ATTRIBUTION.md").read_text()
        self.assertIn("https://www.thingiverse.com/thing:3178215", boarding)
        self.assertIn("pending", boarding.lower())

    def test_readme_states_pending_collision_and_no_runtime_claim(self):
        for package, pkg in self.packages.items():
            text = (pkg / "README.md").read_text()
            self.assertIn("pending_collision_backend", text)
            self.assertIn("No FlightGear launch", text)
            self.assertIn("<flight-model>null</flight-model>", text)
            self.assertIn("GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS",
                          text)
            self.assertIn("flight_config", text)

    def test_m1_has_remote_noncanonical_warning(self):
        pkg = self.packages["sa_tie_m1"]
        readme = (pkg / "README.md").read_text()
        self.assertIn("NON_CANON_Gameplay", readme)
        self.assertIn("remote", readme.lower())
        profile = ET.parse(pkg / "sa_tie_m1-set.xml").getroot()
        help_text = " ".join(line.text or "" for line in
                             profile.findall("sim/help/line"))
        self.assertIn("NON_CANON_Gameplay", help_text)
        license_text = (pkg / "LICENSE_ATTRIBUTION.md").read_text()
        self.assertIn("noncanonical", license_text.lower())

    def test_no_working_collision_or_fdm_is_invented(self):
        for package, pkg in self.packages.items():
            for path in pkg.rglob("*.xml"):
                root = ET.parse(path).getroot()
                for element in root.iter():
                    self.assertNotIn("collision", element.tag.lower())
                    self.assertNotIn("aerodynamics", element.tag.lower())
                    self.assertNotIn("flight-control", element.tag.lower())
            for path in pkg.rglob("*"):
                if path.is_file():
                    self.assertNotIn("collision", path.name.lower())
                    self.assertNotIn("fdm", path.name.lower())


# ---------------------------------------------------------------------------
# Determinism, checking, options and diagnostics
# ---------------------------------------------------------------------------


class TestDeterminismAndOptions(unittest.TestCase):
    def test_two_builds_are_byte_identical_and_check_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            out_a = root / "a" / "Aircraft"
            out_b = root / "b" / "Aircraft"
            self.assertEqual(run_builder(assets, out_a).returncode, 0)
            self.assertEqual(run_builder(assets, out_b).returncode, 0)
            self.assertEqual(tree_hashes(out_a), tree_hashes(out_b))

            check = run_builder(assets, out_a, "--check")
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

            victim = out_a / "sa_tie_royal_guard" / "README.md"
            victim.write_text(victim.read_text() + "\nstaleness\n")
            stale = run_builder(assets, out_a, "--check")
            self.assertEqual(stale.returncode, 1)
            self.assertIn("STALE", stale.stdout + stale.stderr)
            # --check must never rewrite.
            self.assertTrue(victim.read_text().endswith("staleness\n"))

            self.assertEqual(run_builder(assets, out_a).returncode, 0)
            self.assertEqual(run_builder(assets, out_a, "--check").returncode,
                             0)

    def test_second_build_leaves_tree_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            out = root / "Aircraft"
            self.assertEqual(run_builder(assets, out).returncode, 0)
            before = tree_hashes(out)
            result = run_builder(assets, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, tree_hashes(out))

    def test_source_inputs_are_never_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            before = tree_hashes(assets)
            self.assertEqual(run_builder(assets, root / "Aircraft").returncode,
                             0)
            self.assertEqual(before, tree_hashes(assets))

    def test_unrelated_output_root_content_and_beam_flight_config_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            out = root / "Aircraft"
            (out / "sa_tie").mkdir(parents=True)
            marker = out / "sa_tie" / "TASK2-marker.txt"
            marker.write_text("do not touch")
            beam = out / "sa_tie_m1" / "Config" / "flight_config.json"
            beam.parent.mkdir(parents=True)
            beam.write_text('{"generator": "beam", "untouched": true}')

            self.assertEqual(run_builder(assets, out).returncode, 0)
            self.assertEqual(marker.read_text(), "do not touch")
            self.assertEqual(beam.read_text(),
                             '{"generator": "beam", "untouched": true}')

    def test_optional_lods_and_collision_are_copied_but_never_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            ship = assets / "royal_guard"
            (ship / "export" / "lod1.ac").write_text(ac_text("lod1"),
                                                     encoding="utf-8")
            (ship / "export" / "game_lod2.ac").write_text(ac_text("lod2"),
                                                          encoding="utf-8")
            collision = ship / "export" / "collision.glb"
            collision.write_bytes(b"glTF-fixture-bytes")
            integration = default_integration("royal_guard")
            integration["collision"] = {
                "file": "collision.glb",
                "status": "pending_collision_backend",
            }
            (ship / "export" / "integration.json").write_text(
                json.dumps(integration), encoding="utf-8")

            out = root / "Aircraft"
            self.assertEqual(run_builder(assets, out).returncode, 0)
            pkg = out / "sa_tie_royal_guard"
            self.assertEqual((pkg / "Models/royal_guard_lod1.ac").read_text(),
                             ac_text("lod1"))
            self.assertEqual((pkg / "Models/royal_guard_lod2.ac").read_text(),
                             ac_text("lod2"))
            self.assertEqual(
                (pkg / "Models/royal_guard-collision.glb").read_bytes(),
                b"glTF-fixture-bytes")
            for path in pkg.rglob("*.xml"):
                self.assertNotIn(b"<collision", path.read_bytes())
            readme = (pkg / "README.md").read_text()
            self.assertIn("pending_collision_backend", readme)
            self.assertIn("runtime LOD selection is **not** wired",
                          readme)

    def test_declared_texture_is_bundled_and_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            ship = assets / "royal_guard"
            texture = ship / "export" / "Textures" / "hull.png"
            texture.parent.mkdir(parents=True)
            texture.write_bytes(b"PNG-fixture")
            (ship / "export" / "game_lod0.ac").write_text(
                ac_text("hull", texture="Textures/hull.png"), encoding="utf-8")
            integration = default_integration("royal_guard")
            integration["textures"] = [{
                "source": "export/Textures/hull.png",
                "target": "Textures/hull.png",
            }]
            (ship / "export" / "integration.json").write_text(
                json.dumps(integration), encoding="utf-8")

            out = root / "Aircraft"
            self.assertEqual(run_builder(assets, out).returncode, 0)
            pkg = out / "sa_tie_royal_guard"
            bundled = pkg / "Models" / "Textures" / "hull.png"
            self.assertEqual(bundled.read_bytes(), b"PNG-fixture")
            ac = (pkg / "Models/royal_guard.ac").read_text()
            self.assertIn('texture "Textures/hull.png"', ac)
            # No reference/collage imagery leaks into the package.
            images = [path.relative_to(pkg).as_posix()
                      for path in pkg.rglob("*")
                      if path.suffix.lower() in (".jpg", ".jpeg", ".png")]
            self.assertTrue(all(name.startswith("Models/Textures/")
                                for name in images), images)
            self.assertNotIn("..", ac)

    def test_explicit_flight_model_switch_requires_supplied_aero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            out = root / "Aircraft"
            for package in btp.SHIPS.values():
                models = out / package.package / "Models"
                models.mkdir(parents=True)
                (models / "supplied_assisted.xml").write_text(
                    "<fdm_config>fixture</fdm_config>\n")

            result = run_builder(assets, out, "--flight-model", "jsb",
                                 "--aero", "supplied_assisted.xml")
            self.assertEqual(result.returncode, 0, result.stderr)
            profile = ET.parse(out / "sa_tie_m1" / "sa_tie_m1-set.xml").getroot()
            self.assertEqual(profile.findtext("sim/flight-model"), "jsb")
            self.assertEqual(profile.findtext("sim/aero"),
                             "supplied_assisted.xml")
            readme = (out / "sa_tie_m1" / "README.md").read_text()
            self.assertIn("runtime-accepted", readme)

            # Without a supplied file the selection is refused.
            missing = run_builder(assets, root / "empty", "--flight-model",
                                  "jsb", "--aero", "absent.xml")
            self.assertEqual(missing.returncode, 2)
            self.assertIn("absent.xml", missing.stderr)

    def test_missing_required_inputs_diagnose_clearly_without_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ships = root / "pack" / "ships"
            ships.mkdir(parents=True)
            out = root / "Aircraft"
            result = run_builder(root / "pack", out)
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing ship directory", result.stderr)
            self.assertFalse(out.exists())

    def test_missing_single_artifact_names_the_expected_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures",
                                 ships=("royal_guard",))
            ship = assets / "royal_guard"
            (ship / "export" / "integration.json").unlink()
            out = root / "Aircraft"
            try:
                btp.build_package(ship, assets, btp.SHIPS["royal_guard"],
                                  "null", None, out)
            except btp.InputError as exc:
                self.assertIn("integration.json", str(exc))
            else:
                self.fail("missing integration.json was not diagnosed")
            self.assertFalse(out.exists())

    def test_missing_cockpit_and_profile_are_diagnosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures",
                                 ships=("royal_guard",))
            ship = assets / "royal_guard"
            spec = btp.SHIPS["royal_guard"]
            (ship / "export" / "cockpit.ac").unlink()
            with self.assertRaises(btp.InputError) as caught:
                btp.build_package(ship, assets, spec, "null", None,
                                  root / "out")
            self.assertIn("cockpit.ac", str(caught.exception))
            (ship / "export" / "cockpit.ac").write_text(
                ac_text("cockpit"), encoding="utf-8")
            (ship / "flight_profile.json").unlink()
            with self.assertRaises(btp.InputError) as caught:
                btp.build_package(ship, assets, spec, "null", None,
                                  root / "out")
            self.assertIn("flight_profile.json", str(caught.exception))

    def test_unresolved_texture_reference_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures",
                                 ships=("royal_guard",))
            ship = assets / "royal_guard"
            (ship / "export" / "game_lod0.ac").write_text(
                ac_text("hull", texture="Textures/missing.png"),
                encoding="utf-8")
            with self.assertRaises(btp.BuildError) as caught:
                btp.build_package(ship, assets, btp.SHIPS["royal_guard"],
                                  "null", None, root / "out")
            self.assertIn("missing.png", str(caught.exception))
            self.assertFalse((root / "out").exists())

    def test_non_pending_collision_status_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures",
                                 ships=("royal_guard",))
            ship = assets / "royal_guard"
            (ship / "export" / "collision.glb").write_bytes(b"x")
            integration = default_integration("royal_guard")
            integration["collision"] = {"file": "collision.glb",
                                        "status": "working_backend"}
            (ship / "export" / "integration.json").write_text(
                json.dumps(integration), encoding="utf-8")
            with self.assertRaises(btp.BuildError) as caught:
                btp.build_package(ship, assets, btp.SHIPS["royal_guard"],
                                  "null", None, root / "out")
            self.assertIn("pending", str(caught.exception))

    def test_documented_alias_game_ac_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = make_assets(root / "fixtures")
            ship = assets / "boarding_craft"
            (ship / "export" / "game_lod0.ac").unlink()
            (ship / "export" / "cockpit.ac").unlink()
            (ship / "export" / "game.ac").write_text(ac_text("hull"),
                                                     encoding="utf-8")
            (ship / "export" / "game_cockpit.ac").write_text(
                ac_text("cockpit"), encoding="utf-8")
            out = root / "Aircraft"
            result = run_builder(assets, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / "sa_tie_boarding" /
                             "Models/boarding_craft.ac").is_file())

    def test_empty_assets_root_is_diagnosed_not_crashed(self):
        empty = Path(tempfile.mkdtemp()) / "empty_ships"
        empty.mkdir()
        out = Path(tempfile.mkdtemp()) / "Aircraft"
        result = run_builder(empty, out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no 'ships/' directory", result.stderr)
        self.assertFalse(out.exists())

    def test_real_asset_pack_builds_when_stage2_exports_exist(self):
        pack = ROOT.parent / "TIE_Game_Asset_Pack_v1"
        if not (pack / "ships" / "royal_guard" / "export" / "game_lod0.ac").is_file():
            self.skipTest("stage-2 exports not available yet")
        out = Path(tempfile.mkdtemp()) / "Aircraft"
        result = run_builder(pack / "ships", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        for package in ("sa_tie_royal_guard", "sa_tie_m1", "sa_tie_boarding"):
            self.assertTrue((out / package).is_dir())
            self.assertTrue(any((out / package / "Models").glob("*.ac")))
            self.assertTrue((out / package / (package + "-set.xml")).is_file() or
                            any((out / package).glob("*-set.xml")))


if __name__ == "__main__":
    unittest.main()
