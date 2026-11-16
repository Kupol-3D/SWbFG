#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Acceptance guards for tools/tie_ac_export.py (bombable.ac-scene/v1 -> AC3Db).

These exercise the exporter contract statically: axis mapping, stable
grouping, names, material/SURF flags, UV handling, error messages, CLI
--check/stale behaviour and byte reproducibility. Geometry is re-read with an
independent strict AC3D parser below; nothing here launches FlightGear.
"""

import importlib.util
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "tools" / "tie_ac_export.py"

_spec = importlib.util.spec_from_file_location("tie_ac_export", EXPORTER)
tie_ac_export = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tie_ac_export)
ExportError = tie_ac_export.ExportError

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def material(name="body", **overrides):
    entry = {"name": name, "diffuse": [0.5, 0.5, 0.5]}
    entry.update(overrides)
    return entry


def obj(name="hull", vertices=None, triangles=None, materials=1, **overrides):
    if vertices is None:
        vertices = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
    if triangles is None:
        triangles = [[0, 1, 2]]
    entry = {"name": name, "matrix_world": list(IDENTITY),
             "vertices": vertices, "triangles": triangles,
             "material_indices": [0] * len(triangles)}
    if materials > 1:
        entry["material_indices"] = [i % materials for i in
                                     range(len(triangles))]
    entry.update(overrides)
    return entry


def scene(**overrides):
    entry = {"schema": tie_ac_export.SCHEMA, "asset_id": "Game_Tie",
             "materials": [material()], "objects": [obj()]}
    entry.update(overrides)
    return entry


def run_cli(*args):
    return subprocess.run([sys.executable, str(EXPORTER),
                           *[str(a) for a in args]],
                          capture_output=True, text=True, cwd=ROOT)


def read_ac(text):
    """Strict independent parser for the dialect this exporter must emit.

    Returns (materials, root_name, children).  AC frame points are kept as
    stored; conversions to other frames happen in the tests.
    """
    tokens = iter(shlex.split(text))

    def expect(value):
        actual = next(tokens)
        if actual != value:
            raise ValueError(f"Expected {value!r}, got {actual!r}")

    def number():
        value = float(next(tokens))
        if not math.isfinite(value):
            raise ValueError("Non-finite number in AC output")
        return value

    def point():
        return (number(), number(), number())

    expect("AC3Db")
    materials = []
    token = next(tokens)
    while token == "MATERIAL":
        mat = {"name": next(tokens), "texture": None}
        for field, count in (("rgb", 3), ("amb", 3), ("emis", 3),
                             ("spec", 3), ("shi", 1), ("trans", 1)):
            expect(field)
            mat[field] = tuple(number() for _ in range(count))
        token = next(tokens)
        if token == "texture":
            mat["texture"] = next(tokens)
            token = next(tokens)
        materials.append(mat)

    objects = []

    def read_object(kind):
        if kind not in ("world", "poly"):
            raise ValueError(f"Unsupported object type {kind!r}")
        node = {"name": None, "kind": kind, "loc": None, "texture": None,
                "crease": None, "vertices": [], "surfaces": [], "kids": 0}
        key = next(tokens)
        if key == "name":
            node["name"] = next(tokens)
            key = next(tokens)
        while key in ("loc", "texture", "crease"):
            if key == "loc":
                node["loc"] = point()
            elif key == "crease":
                node["crease"] = number()
            else:
                value = next(tokens)
                node["texture"] = None if value == "-1" else value
            key = next(tokens)
        if kind == "poly":
            if key != "numvert":
                raise ValueError("Missing numvert")
            for _ in range(int(next(tokens))):
                node["vertices"].append(point())
            expect("numsurf")
            for surface_index in range(int(next(tokens))):
                expect("SURF")
                flags = int(next(tokens), 0)
                if flags & 0xF:
                    raise ValueError("Not a polygon")
                expect("mat")
                mat = int(next(tokens))
                if not 0 <= mat < len(materials):
                    raise ValueError("Invalid material reference")
                expect("refs")
                if int(next(tokens)) != 3:
                    raise ValueError("Runtime faces must be triangles")
                refs = []
                for _ in range(3):
                    vertex = int(next(tokens))
                    if not 0 <= vertex < len(node["vertices"]):
                        raise ValueError("Invalid vertex reference")
                    refs.append((vertex, number(), number()))
                node["surfaces"].append((flags, mat, refs))
            key = next(tokens)
        if key != "kids":
            raise ValueError("Missing children count")
        node["kids"] = int(next(tokens))
        objects.append(node)
        return node

    if token != "OBJECT":
        raise ValueError(f"Expected OBJECT world, got {token!r}")
    root = read_object(next(tokens))
    for _ in range(root["kids"]):
        expect("OBJECT")
        read_object(next(tokens))
    if next(tokens, None) is not None:
        raise ValueError("Trailing data after kids 0")
    if len(objects) != root["kids"] + 1:
        raise ValueError("Kids count does not match parsed objects")
    return materials, root["name"], [o for o in objects if o is not root]


def ac_to_model(p):
    """SimGear ACProcessPolicy: AC (x, y, z) -> FG model (x, -z, y)."""
    return (p[0], -p[2], p[1])


class TestAxisMapping(unittest.TestCase):
    def test_basis_swap_scale_and_translation_bake_into_ac_axes(self):
        # Blender working frame: X right, Y forward, Z up.  Uniform scale 2,
        # yaw +90 deg, then translation (10, 20, 30); row-major matrix.
        matrix = [0, -2, 0, 10,
                  2, 0, 0, 20,
                  0, 0, 2, 30,
                  0, 0, 0, 1]
        payload = scene(objects=[obj(
            vertices=[[1, 2, 3], [2, 2, 3], [1, 3, 3]], matrix_world=matrix)])
        _, _, children = read_ac(tie_ac_export.export_scene(payload))
        self.assertEqual(len(children), 1)
        child = children[0]
        self.assertEqual(child["loc"], (0.0, 0, 0))  # baked, loc stays zero
        expected_ac = [(-22, 36, -6), (-24, 36, -6), (-22, 36, -4)]
        for actual, want in zip(child["vertices"], expected_ac):
            self.assertAlmostEqual(actual[0], want[0], places=5)
            self.assertAlmostEqual(actual[1], want[1], places=5)
            self.assertAlmostEqual(actual[2], want[2], places=5)
        # Blender (right, forward, up) must land as FG model (aft, right, up).
        model = [ac_to_model(p) for p in child["vertices"]]
        self.assertAlmostEqual(model[0][0], -22, places=5)  # forward -> -X
        self.assertAlmostEqual(model[0][1], 6, places=5)    # right -> +Y
        self.assertAlmostEqual(model[0][2], 36, places=5)   # up -> +Z

    def test_winding_survives_the_positive_det_mapping(self):
        matrix = [0, -2, 0, 10, 2, 0, 0, 20, 0, 0, 2, 30, 0, 0, 0, 1]
        payload = scene(objects=[obj(
            vertices=[[1, 2, 3], [2, 2, 3], [1, 3, 3]], matrix_world=matrix)])
        _, _, children = read_ac(tie_ac_export.export_scene(payload))
        flags, _, refs = children[0]["surfaces"][0]
        model = [ac_to_model(children[0]["vertices"][i]) for i, _, _ in refs]
        a, b, c = model
        # Blender normal +Z (up); with det +1 the same winding keeps a
        # positive model Z (up) component in FlightGear.
        self.assertGreater(dot(cross(sub(b, a), sub(c, a)), (0, 0, 1)), 0)


class TestGroupingAndNames(unittest.TestCase):
    def test_stable_groups_by_object_material_and_smooth(self):
        triangles = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11]]
        vertices = []
        for i in range(12):
            vertices.append([i, (i % 3) * 0.5, 0])
        payload = scene(
            materials=[material("a"), material("b")],
            objects=[obj("Wing", vertices=vertices, triangles=triangles,
                         material_indices=[0, 1, 0, 0],
                         smooth=[False, False, False, True])])
        _, _, children = read_ac(tie_ac_export.export_scene(payload))
        # First-face order: (mat0, flat), (mat1, flat), (mat0, smooth).
        self.assertEqual([c["name"] for c in children],
                         ["Wing", "Wing_2", "Wing_3"])
        self.assertEqual([len(c["surfaces"]) for c in children], [2, 1, 1])
        self.assertEqual([s[1] for s in children[0]["surfaces"]], [0, 0])
        self.assertEqual(children[1]["surfaces"][0][1], 1)
        self.assertEqual(children[0]["surfaces"][0][0], 0x00)
        self.assertEqual(children[1]["surfaces"][0][0], 0x00)
        self.assertEqual(children[2]["surfaces"][0][0], 0x10)

    def test_names_are_sanitized_unique_and_keep_game_cockpit_prefixes(self):
        payload = scene(
            asset_id="Game_Tie",
            materials=[material()],
            objects=[
                obj("Game_Tie"),                      # collides with root
                obj("Cockpit_panel"),
                obj("Cockpit panel"),                 # same after sanitize
                obj("Cockpit/panel"),
                obj('bad"name'),
                obj("люк"),                           # non-ASCII -> safe
            ])
        _, root_name, children = read_ac(tie_ac_export.export_scene(payload))
        self.assertEqual(root_name, "Game_Tie")
        names = [c["name"] for c in children]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.isascii() for name in names))
        self.assertEqual(names[:5], ["Game_Tie_2", "Cockpit_panel",
                                     "Cockpit_panel_2", "Cockpit_panel_3",
                                     "bad_name"])
        self.assertTrue(all(name.startswith("Cockpit_")
                            for name in names[1:4]))


class TestMaterials(unittest.TestCase):
    def test_material_flags_texture_and_transparency(self):
        payload = scene(
            materials=[
                material("body", diffuse=[0.8, 0.1, 0.2],
                         emission=[1, 0, 0], specular=[0.5, 0.5, 0.5],
                         shininess=64, alpha=0.25, double_sided=True,
                         texture="textures/tie_body.png"),
                material("plain"),
            ],
            objects=[obj("Shell", material_indices=[0]),
                     obj("Inner", material_indices=[0]),
                     obj("Trim", material_indices=[1])])
        text = tie_ac_export.export_scene(payload)
        materials, _, children = read_ac(text)
        self.assertEqual(len(materials), 2)
        body, plain = materials
        self.assertEqual(body["rgb"], (0.8, 0.1, 0.2))
        self.assertEqual(body["amb"], body["rgb"])
        self.assertEqual(body["emis"], (1.0, 0.0, 0.0))
        self.assertEqual(body["spec"], (0.5, 0.5, 0.5))
        self.assertEqual(body["shi"], (64.0,))
        self.assertAlmostEqual(body["trans"][0], 0.75, places=6)  # 1 - alpha
        self.assertEqual(body["texture"], "textures/tie_body.png")
        self.assertEqual(plain["emis"], (0.0, 0.0, 0.0))
        self.assertEqual(plain["spec"], (0.0, 0.0, 0.0))
        self.assertEqual(plain["shi"], (0.0,))
        self.assertAlmostEqual(plain["trans"][0], 0.0, places=9)
        self.assertIsNone(plain["texture"])
        by_name = {c["name"]: c for c in children}
        self.assertEqual(by_name["Shell"]["texture"],
                         "textures/tie_body.png")
        self.assertEqual(by_name["Trim"]["texture"], None)  # texture -1
        # double_sided adds 0x20 to every surface using that material.
        self.assertEqual(by_name["Shell"]["surfaces"][0][0], 0x20)
        self.assertEqual(by_name["Trim"]["surfaces"][0][0], 0x00)

    def test_smooth_flag_and_double_sided_combine(self):
        payload = scene(
            materials=[material("glass", double_sided=True)],
            objects=[obj("Canopy", smooth=True)])
        _, _, children = read_ac(tie_ac_export.export_scene(payload))
        self.assertEqual(children[0]["surfaces"][0][0], 0x10 | 0x20)


class TestUv(unittest.TestCase):
    QUAD = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]]
    FACES = [[0, 1, 2], [0, 2, 3]]

    def exported_quad(self, uv):
        payload = scene(objects=[obj("Skin", vertices=self.QUAD,
                                     triangles=self.FACES, uv=uv)])
        _, _, children = read_ac(tie_ac_export.export_scene(payload))
        self.assertEqual(len(children), 1)
        return children[0]

    def test_blender_uv_flips_and_seam_splits_the_shared_corner(self):
        uv = [[[0, 0], [1, 0], [1, 1]],
              [[0, 0.5], [1, 1], [0, 1]]]
        child = self.exported_quad(uv)
        # v0 appears with two different UVs (seam) -> 5 unique (pos, uv).
        self.assertEqual(len(child["vertices"]), 5)
        refs0 = child["surfaces"][0][2]
        refs1 = child["surfaces"][1][2]
        flipped = [[(u, 1.0 - v) for u, v in face] for face in uv]
        for refs, want in ((refs0, flipped[0]), (refs1, flipped[1])):
            for (_, u, v), (wu, wv) in zip(refs, want):
                self.assertAlmostEqual(u, wu, places=6)
                self.assertAlmostEqual(v, wv, places=6)
        # The two faces share exactly the merged corner (p2, uv 1,1) and
        # never confuse loop order with vertex order.
        self.assertEqual(refs0[2][0], refs1[1][0])
        self.assertNotEqual(refs0[0][0], refs1[0][0])
        split_a = child["vertices"][refs0[0][0]]
        split_b = child["vertices"][refs1[0][0]]
        self.assertEqual(split_a, split_b)  # same position, different UV
        # ...and a coincident UV at another position stays a separate vertex.
        self.assertNotEqual(child["vertices"][refs1[2][0]], split_a)

    def test_missing_uv_is_literal_zero_and_merges_vertices(self):
        child = self.exported_quad(None)
        self.assertEqual(len(child["vertices"]), 4)
        for surface in child["surfaces"]:
            for _, u, v in surface[2]:
                self.assertEqual((u, v), (0.0, 0.0))

    def test_per_face_null_uv_falls_back_to_zero(self):
        uv = [None, [[0.25, 0.75], [0.5, 0.5], [0.75, 0.25]]]
        child = self.exported_quad(uv)
        for _, u, v in child["surfaces"][0][2]:
            self.assertEqual((u, v), (0.0, 0.0))
        uvs = [(u, v) for _, u, v in child["surfaces"][1][2]]
        self.assertAlmostEqual(uvs[0], (0.25, 0.25), places=6)
        self.assertAlmostEqual(uvs[2], (0.75, 0.75), places=6)


class TestValidation(unittest.TestCase):
    def assert_rejects(self, mutate, *fragments):
        payload = scene()
        mutate(payload)
        with self.assertRaises(ExportError) as caught:
            tie_ac_export.export_scene(payload)
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)

    def test_rejects_bad_schema_and_unknown_keys(self):
        self.assert_rejects(lambda s: s.update(schema="bombable.ac-scene/v2"),
                            "scene", "schema")
        self.assert_rejects(lambda s: s.update(extra=1), "scene",
                            "unknown key")
        self.assert_rejects(lambda s: s.update(asset_id="  "), "asset_id")

    def test_rejects_bad_materials(self):
        self.assert_rejects(
            lambda s: s["materials"][0].update(diffuse=[1.2, 0, 0]),
            "scene.materials[0].diffuse[0]", "outside")
        self.assert_rejects(
            lambda s: s["materials"][0].update(diffuse=[0, 0]),
            "scene.materials[0].diffuse", "expected [x, y, z]")
        self.assert_rejects(
            lambda s: s["materials"][0].update(shininess=128.5),
            "shininess", "outside")
        self.assert_rejects(
            lambda s: s["materials"][0].update(alpha=2), "alpha", "outside")
        self.assert_rejects(
            lambda s: s["materials"][0].update(double_sided=1),
            "double_sided", "true or false")
        self.assert_rejects(
            lambda s: s["materials"][0].update(emission="red"),
            "emission", "expected [x, y, z]")

    def test_rejects_bad_texture_paths(self):
        for bad, fragment in (("/abs/x.png", "absolute"),
                              ("../x.png", "relative"),
                              ("a/../b.png", "relative"),
                              ("~/x.png", "absolute"),
                              ("C:/x.png", "absolute"),
                              ("C:\\x.png", "POSIX"),
                              ("dir\\x.png", "POSIX"),
                              ("", "non-empty"),
                              ("a//b.png", "relative"),
                              ('a"b.png', "quotes")):
            with self.subTest(bad=bad):
                self.assert_rejects(
                    lambda s, bad=bad: s["materials"][0].update(texture=bad),
                    "scene.materials[0].texture", fragment)

    def test_accepts_reasonable_relative_texture_paths(self):
        for good in ("x.png", "textures/sub dir/x.png", "a.b.c/d.png"):
            payload = scene()
            payload["materials"][0]["texture"] = good
            tie_ac_export.export_scene(payload)  # must not raise

    def test_rejects_bad_geometry_and_matrices(self):
        self.assert_rejects(
            lambda s: s["objects"][0].update(
                vertices=[[0, 0, float("nan")], [1, 0, 0], [0, 1, 0]]),
            "scene.objects[0].vertices[0][2]", "finite")
        self.assert_rejects(
            lambda s: s["objects"][0].update(triangles=[[0, 1]]),
            "scene.objects[0].triangles[0]", "expected [a, b, c]")
        self.assert_rejects(
            lambda s: s["objects"][0].update(triangles=[[0, 1, 3]]),
            "scene.objects[0].triangles[0][2]", "out of range")
        self.assert_rejects(
            lambda s: s["objects"][0].update(material_indices=[0, 0]),
            "material_indices", "expected 1 indices")
        self.assert_rejects(
            lambda s: s["objects"][0].update(material_indices=[2]),
            "material_indices[0]", "out of range")
        self.assert_rejects(
            lambda s: s["objects"][0].update(matrix_world=[1] * 15),
            "matrix_world", "16 row-major")
        self.assert_rejects(
            lambda s: s["objects"][0].update(
                matrix_world=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0,
                              0.001, 0, 0, 1]),
            "matrix_world", "affine")
        self.assert_rejects(
            lambda s: s["objects"][0].update(name=17),
            "scene.objects[0].name", "non-empty string")
        self.assert_rejects(
            lambda s: s["objects"].append(["not", "an", "object"]),
            "scene.objects[1]", "expected an object")

    def test_rejects_bad_uv_and_smooth(self):
        self.assert_rejects(
            lambda s: s["objects"][0].update(uv=[[[0, 0], [1, 0]]]),
            "scene.objects[0].uv[0]", "three [u, v] pairs")
        self.assert_rejects(
            lambda s: s["objects"][0].update(
                uv=[[[0, 0], [1, 0], [1, 1]], [[0, 0], [1, 0], [1, 1]]]),
            "scene.objects[0].uv", "loop-UV")
        self.assert_rejects(
            lambda s: s["objects"][0].update(
                uv=[[[0, 0], [1, 0], [1, "x"]]]),
            "scene.objects[0].uv[0][2]", "expected a number")
        self.assert_rejects(
            lambda s: s["objects"][0].update(smooth="yes"),
            "scene.objects[0].smooth", "expected a bool")
        self.assert_rejects(
            lambda s: s["objects"][0].update(smooth=[True, False]),
            "scene.objects[0].smooth", "bools")
        self.assert_rejects(
            lambda s: s["objects"][0].update(smooth=[1]),
            "scene.objects[0].smooth[0]", "true or false")


class TestEmptyAndFormat(unittest.TestCase):
    def test_empty_objects_are_skipped(self):
        payload = scene(objects=[obj("Ghost", vertices=[[0, 0, 0]],
                                     triangles=[]),
                                 obj("Dust", vertices=[], triangles=[]),
                                 obj("Shell")])
        payload["objects"][0]["material_indices"] = []
        payload["objects"][1]["material_indices"] = []
        _, root_name, children = read_ac(tie_ac_export.export_scene(payload))
        self.assertEqual([c["name"] for c in children], ["Shell"])
        self.assertEqual(root_name, "Game_Tie")  # kids==children enforced by read_ac

    def test_all_empty_scene_is_still_a_valid_minimal_ac(self):
        payload = scene(objects=[])
        text = tie_ac_export.export_scene(payload)
        self.assertTrue(text.startswith("AC3Db\n"))
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n"), len(text.splitlines()))
        _, _, children = read_ac(text)
        self.assertEqual(children, [])

    def test_output_is_ascii_with_7_to_9_significant_digits(self):
        payload = scene(
            materials=[material("m", alpha=1 / 3)],
            objects=[obj("v", vertices=[[1 / 3, 0, 0], [1 / 3, 1, 0],
                                         [0, 0, 123456.789]])])
        text = tie_ac_export.export_scene(payload)
        text.encode("ascii")
        self.assertIn("0.33333333", text)
        self.assertIn("123456.79", text)
        self.assertIn("0.66666667", text)  # trans = 1 - 1/3
        self.assertNotIn("-0 \n", text)


class TestCliAndReproducibility(unittest.TestCase):
    def sample(self):
        return scene(
            materials=[material("body", texture="textures/tie_body.png"),
                       material("glass", alpha=0.4, double_sided=True)],
            objects=[obj("Hull", material_indices=[0]),
                     obj("Canopy", smooth=True, material_indices=[1],
                         uv=[[[0, 0], [1, 0], [1, 1]]])])

    def test_export_bytes_are_deterministic(self):
        first = tie_ac_export.export_bytes(self.sample())
        second = tie_ac_export.export_bytes(json.loads(json.dumps(
            self.sample())))
        self.assertEqual(first, second)

    def test_cli_write_check_stale_and_missing(self):
        payload = self.sample()
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "scene.json"
            out = Path(tmp) / "out" / "tie.ac"
            src.write_text(json.dumps(payload), encoding="utf-8")

            proc = run_cli(src, out)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            written = out.read_bytes()
            self.assertEqual(written, tie_ac_export.export_bytes(payload))

            proc = run_cli(src, out, "--check")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

            out.write_bytes(written + b"stale\n")
            proc = run_cli(src, out, "--check")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("stale or missing", proc.stderr)
            # --check must not repair or rewrite anything.
            self.assertTrue(out.read_bytes().endswith(b"stale\n"))

            out.unlink()
            proc = run_cli(src, out, "--check")
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(out.exists())

            # Writing the same scene twice is byte-identical.
            other = Path(tmp) / "second.ac"
            self.assertEqual(run_cli(src, other).returncode, 0)
            self.assertEqual(other.read_bytes(), written)

    def test_cli_reports_bad_input_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            broken = tmp_path / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            out = tmp_path / "out.ac"
            proc = run_cli(broken, out)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("invalid JSON", proc.stderr)
            self.assertFalse(out.exists())

            bad_scene = tmp_path / "bad.json"
            payload = scene()
            payload["objects"][0]["triangles"] = [[0, 1, 9]]
            bad_scene.write_text(json.dumps(payload), encoding="utf-8")
            proc = run_cli(bad_scene, out)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("scene.objects[0].triangles[0][2]", proc.stderr)
            self.assertFalse(out.exists())

            proc = run_cli(tmp_path / "absent.json", out)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("cannot read", proc.stderr)


class TestReverseParse(unittest.TestCase):
    def test_counts_bounds_and_uv_ranges_survive_a_round_trip(self):
        scale = [3, 0, 0, 0, 0, 3, 0, 0, 0, 0, 3, 0, 0, 0, 0, 1]
        shift = [1, 0, 0, -2.5, 0, 1, 0, 1.25, 0, 0, 1, 0, 0, 0, 0, 1]
        payload = scene(
            materials=[material("body", texture="textures/tie_body.png"),
                       material("trim")],
            objects=[
                obj("Hull",
                    vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    triangles=[[0, 1, 2], [0, 3, 1], [1, 3, 2], [2, 3, 0]],
                    material_indices=[0, 0, 1, 1],
                    smooth=[False, True, True, False],
                    uv=[[[0, 0], [1, 0], [1, 1]]] * 4,
                    matrix_world=scale),
                obj("Antenna",
                    vertices=[[0, 0, 0], [0.5, 0, 0.25], [0, 0.5, 0.25]],
                    triangles=[[0, 1, 2]], uv=None, smooth=True,
                    matrix_world=shift),
                obj("Ghost", vertices=[[0, 0, 0]], triangles=[]),
            ])
        payload["objects"][2]["material_indices"] = []
        materials, root_name, children = read_ac(
            tie_ac_export.export_scene(payload))

        parsed_faces = sum(len(c["surfaces"]) for c in children)
        input_faces = sum(len(o["triangles"]) for o in payload["objects"])
        self.assertEqual(parsed_faces, input_faces)          # 4 + 1, empty skipped
        self.assertEqual(root_name, "Game_Tie")
        self.assertEqual(len(materials), 2)
        self.assertEqual([c["name"] for c in children],
                         ["Hull", "Hull_2", "Hull_3", "Hull_4", "Antenna"])

        points = [p for c in children for p in c["vertices"]]
        expected = []
        for matrix, verts in ((scale, payload["objects"][0]["vertices"]),
                              (shift, payload["objects"][1]["vertices"])):
            for x, y, z in verts:
                bx = matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3]
                by = matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7]
                bz = matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11]
                expected.append((-by, bz, -bx))
        for axis in range(3):
            self.assertAlmostEqual(min(p[axis] for p in points),
                                   min(p[axis] for p in expected), places=5)
            self.assertAlmostEqual(max(p[axis] for p in points),
                                   max(p[axis] for p in expected), places=5)
        for child in children:
            for flags, mat, refs in child["surfaces"]:
                self.assertLess(mat, len(materials))
                for _, u, v in refs:
                    self.assertGreaterEqual(u, 0.0)
                    self.assertLessEqual(u, 1.0)
                    self.assertGreaterEqual(v, 0.0)
                    self.assertLessEqual(v, 1.0)


if __name__ == "__main__":
    unittest.main()
