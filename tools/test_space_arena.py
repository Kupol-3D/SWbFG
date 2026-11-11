#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Package integrity and gameplay-space checks for the TASK4 greybox arena."""

import hashlib
import importlib.util
import json
import math
from pathlib import Path, PurePosixPath
import shlex
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "scenery" / "space_arena"
SOURCE = PACKAGE / "src" / "generate_package.py"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_file(relative):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or "\\" in relative:
        raise ValueError("unsafe package path: " + relative)
    return PACKAGE.joinpath(*rel.parts)


def ac_triangles(path):
    count = 0
    for line in path.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if fields and fields[0] == "refs":
            refs = int(fields[1])
            if refs < 3:
                raise ValueError("surface with fewer than three vertices")
            count += refs - 2
    return count


def local_box_corners(placement):
    aabb = placement["local_size_m"]
    fmin, fmax = aabb["f"]
    lmin, lmax = aabb["l"]
    umin, umax = aabb["u"]
    heading = math.radians(placement["heading_deg"])
    corners = []
    for forward in (fmin, fmax):
        for left in (lmin, lmax):
            east = forward * math.sin(heading) - left * math.cos(heading)
            north = forward * math.cos(heading) + left * math.sin(heading)
            corners.append((east + placement["offset_east_m"],
                            north + placement["offset_north_m"],
                            umin, umax))
    return corners, umin, umax


def world_box(placement):
    """Conservative ENU AABB relative to the placement's own faction anchor."""
    corners, umin, umax = local_box_corners(placement)
    return (min(p[0] for p in corners), max(p[0] for p in corners),
            min(p[1] for p in corners), max(p[1] for p in corners),
            umin, umax)


def global_world_box(placement, origin, anchors):
    """Conservative ENU AABB in one site-wide ENU frame."""
    anchor = anchors[placement["faction"]]
    origin_lon, origin_lat = origin
    anchor_east = ((anchor["lon"] - origin_lon) * 111320.0
                   * math.cos(math.radians(origin_lat)))
    anchor_north = (anchor["lat"] - origin_lat) * 111320.0
    local_corners, umin, umax = local_box_corners(placement)
    corners = [(anchor_east + east, anchor_north + north)
               for east, north, _, _ in local_corners]
    return (min(p[0] for p in corners), max(p[0] for p in corners),
            min(p[1] for p in corners), max(p[1] for p in corners),
            umin, umax)


def boxes_overlap(a, b):
    return all(a[index] < b[index + 1] and b[index] < a[index + 1]
               for index in (0, 2, 4))


def boxes_intersect_route(box, heading_deg, length, half_width):
    heading = math.radians(heading_deg)
    forwards = []
    lefts = []
    for east in (box[0], box[1]):
        for north in (box[2], box[3]):
            forwards.append(east * math.sin(heading) + north * math.cos(heading))
            lefts.append(-east * math.cos(heading) + north * math.sin(heading))
    return (min(forwards) < length and max(forwards) > 0.0
            and min(lefts) < half_width and max(lefts) > -half_width)


def route_separation(first, second):
    """Perpendicular distance between two parallel route centre lines."""
    a = first["origin"]
    b = second["origin"]
    first_point = first["origin"]
    second_point = second["origin"]
    east = (b[0] - a[0]) * 111320.0 * math.cos(math.radians(first_point[1]))
    north = (b[1] - first_point[1]) * 111320.0
    heading = math.radians(first["heading_deg"])
    perpendicular = (-math.cos(heading), math.sin(heading))
    return abs(east * perpendicular[0] + north * perpendicular[1])


class TestSpaceArenaPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (PACKAGE / "manifest.json").read_text(encoding="utf-8"))
        cls.placements = {p["id"]: p for p in cls.manifest["placements"]}
        cls.poses = {p["id"]: p for p in cls.manifest["poses"]}
        cls.models = cls.manifest["models"]

    def test_identity_and_single_canonical_source(self):
        self.assertEqual(self.manifest["schema"], "bombable.space_arena.manifest")
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["task"], "TASK4")
        self.assertEqual(self.manifest["fg_target_version"], "2024.1.7")
        self.assertEqual(self.manifest["package"], "scenery/space_arena")
        self.assertTrue(SOURCE.is_file())
        self.assertFalse((PACKAGE / "src" / "generate_geometry.py").exists())
        self.assertIn("GPL-2.0-or-later", self.manifest["provenance"]["license"])
        self.assertIn("none", self.manifest["provenance"]["external_assets"])

    def test_documented_airstart_matches_manifest(self):
        pose = self.poses['fb_airstart_arena']
        readme = (PACKAGE / 'README.md').read_text(encoding='utf-8')
        self.assertIn(f'--lon={pose["lon"]:.6f}', readme)
        self.assertIn(f'--lat={pose["lat"]:.6f}', readme)
        self.assertIn(f'--heading={pose["heading_deg"]:g} ', readme)
        self.assertIn(f'--altitude={pose["alt"]["value_ft"]:g}', readme)
        self.assertIn(f'--vc={pose["speed_kt_ias"]:g} ', readme)

    def test_models_exist_and_triangle_budget_is_independently_recomputed(self):
        self.assertTrue(self.models)
        actual_total = 0
        for name, metadata in self.models.items():
            self.assertRegex(name, r"^[a-z][a-z0-9_]*$")
            ac = package_file(metadata["ac"])
            xml = package_file(metadata["xml"])
            self.assertTrue(ac.is_file(), ac)
            self.assertTrue(xml.is_file(), xml)
            triangles = ac_triangles(ac)
            self.assertGreater(triangles, 0, name)
            self.assertEqual(triangles, metadata["triangles"], name)
            actual_total += triangles
            self.assertNotIn("texture \"", ac.read_text(encoding="ascii"))
        self.assertEqual(actual_total,
                         self.manifest["budget"]["triangles_total"])
        self.assertEqual(actual_total,
                         sum(self.manifest["budget"]["per_model"].values()))
        self.assertLessEqual(actual_total, 25000)
        self.assertEqual(self.manifest["budget"]["triangle_limit"], 25000)

    def test_placements_match_anchors_models_and_heading_contract(self):
        anchors = self.manifest["region"]["anchors"]
        self.assertEqual(set(anchors), {"rebels", "empire"})
        self.assertEqual(len(self.placements), len(self.manifest["placements"]))
        for placement in self.placements.values():
            self.assertIn(placement["model"], self.models)
            self.assertIn(placement["faction"], anchors)
            self.assertTrue(math.isfinite(placement["lon"]))
            self.assertTrue(math.isfinite(placement["lat"]))
            self.assertTrue(-180 <= placement["lon"] < 180)
            self.assertTrue(-90 <= placement["lat"] <= 90)
            expected_stg = (360.0 - placement["heading_deg"] % 360.0) % 360.0
            self.assertAlmostEqual(placement["stg_heading_deg"], expected_stg)
            self.assertEqual(placement["agl_offset_m"], 0.0)
            anchor = anchors[placement["faction"]]
            east = ((placement["lon"] - anchor["lon"]) * 111320.0
                    * math.cos(math.radians(anchor["lat"])))
            north = (placement["lat"] - anchor["lat"]) * 111320.0
            self.assertAlmostEqual(east, placement["offset_east_m"], delta=0.25)
            self.assertAlmostEqual(north, placement["offset_north_m"], delta=0.25)

    def test_agl_stg_manifest_and_hashes_match_files(self):
        stg_meta = self.manifest["stg"]
        self.assertEqual(stg_meta["entry_kind"], "OBJECT_STATIC_AGL")
        self.assertEqual(stg_meta["agl_offset_m"], 0.0)
        stg = package_file(stg_meta["path"])
        self.assertTrue(stg.is_file(), stg)
        entries = []
        for line in stg.read_text(encoding="ascii").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = shlex.split(line, posix=True)
            self.assertEqual(fields[0], "OBJECT_STATIC_AGL")
            self.assertEqual(len(fields), 6)
            self.assertEqual(float(fields[4]), 0.0)
            entries.append(fields)
        self.assertEqual(len(entries), len(self.placements))
        for fields in entries:
            model_name = PurePosixPath(fields[1]).stem
            self.assertIn(model_name, self.models)
            placement = next(p for p in self.placements.values()
                             if p["model"] == model_name
                             and math.isclose(float(fields[2]), p["lon"], abs_tol=1e-6)
                             and math.isclose(float(fields[3]), p["lat"], abs_tol=1e-6))
            self.assertEqual(float(fields[5]), placement["stg_heading_deg"])
            self.assertEqual(
                package_file(self.models[model_name]["xml"]),
                stg.parent / PurePosixPath(fields[1]))
        artifact_hashes = self.manifest["provenance"]["artifact_sha256"]
        expected_artifacts = set()
        for metadata in self.models.values():
            expected_artifacts.update((metadata["ac"], metadata["xml"]))
        expected_artifacts.add(stg_meta["path"])
        self.assertEqual(set(artifact_hashes), expected_artifacts)
        for relative, expected in artifact_hashes.items():
            self.assertEqual(sha256(package_file(relative)), expected, relative)
        self.assertEqual(self.manifest["provenance"]["generator_sha256"],
                         sha256(SOURCE))

    def test_exactly_one_primary_pose_per_faction_and_safe_fallbacks(self):
        primary = [pose for pose in self.poses.values() if pose.get("primary")]
        self.assertEqual(len(primary), 2)
        self.assertEqual({pose["faction"] for pose in primary},
                         {"rebels", "empire"})
        for pose in primary:
            self.assertTrue(pose["experimental"])
            self.assertIn(pose["on_placement"], self.placements)
            self.assertTrue(-180 <= pose["lon"] < 180)
            self.assertTrue(-90 <= pose["lat"] <= 90)
            self.assertEqual(pose["alt"]["datum"], "design_agl_above_anchor_m")
            self.assertGreater(pose["alt"]["value_m"], 0.0)
        runway = [pose for pose in self.poses.values()
                  if pose.get("airport") == "KSFO" and pose.get("runway") == "28R"]
        self.assertEqual(len(runway), 1)
        airstarts = [pose for pose in self.poses.values()
                     if pose["kind"] == "aircraft_position"]
        self.assertTrue(airstarts)
        for pose in airstarts:
            self.assertEqual(pose["alt"]["datum"], "msl_feet")
            self.assertGreaterEqual(pose["alt"]["value_ft"], 1000.0)
            self.assertGreater(pose["speed_kt_ias"], 0.0)

    def test_routes_are_linked_and_clear_of_static_decorations(self):
        routes = self.manifest["routes"]
        self.assertEqual(len(routes), 2)
        self.assertEqual(len({route["id"] for route in routes}), 2)
        for route in routes:
            pose = self.poses[route["from_pose"]]
            self.assertTrue(pose["primary"])
            self.assertEqual(route["heading_deg"], pose["heading_deg"])
            self.assertGreater(route["length_m"], 0.0)
            self.assertGreater(route["half_width_m"], 0.0)
            for placement in self.placements.values():
                if placement["id"] == pose["on_placement"]:
                    continue
                self.assertFalse(
                    boxes_intersect_route(
                        global_world_box(placement, route["origin"],
                                         self.manifest["region"]["anchors"]),
                        route["heading_deg"],
                        route["length_m"], route["half_width_m"]),
                    "%s intersects declared corridor %s" %
                    (placement["id"], route["id"]),
                )
        self.assertGreater(
            route_separation(routes[0], routes[1]),
            routes[0]["half_width_m"] + routes[1]["half_width_m"],
        )

    def test_unintended_aabb_intersections_are_not_hidden(self):
        allowed = set()
        for pair in self.manifest.get("stack_pairs", []):
            allowed.add(frozenset((pair["lower"], pair["upper"])))
        placements = sorted(self.placements.values(), key=lambda p: p["id"])
        for index, first in enumerate(placements):
            for second in placements[index + 1:]:
                if first["faction"] != second["faction"]:
                    continue
                if frozenset((first["id"], second["id"])) in allowed:
                    continue
                self.assertFalse(
                    boxes_overlap(world_box(first), world_box(second)),
                    "undeclared AABB overlap: %s / %s" %
                    (first["id"], second["id"]),
                )

    def test_surface_and_runtime_claims_remain_unverified(self):
        surfaces = self.manifest["surfaces"]
        primary_pads = {pose["on_placement"] for pose in self.poses.values()
                        if pose.get("primary")}
        pads = [surface for surface in surfaces
                if surface.get("placement_id") in primary_pads]
        self.assertEqual(len(pads), 2)
        for surface in surfaces:
            self.assertNotEqual(surface.get("support"), "verified")
            self.assertNotEqual(surface.get("collision"), "verified")
        self.assertIn("NOT VERIFIED", self.manifest["verification"]["fg_runtime_load"])
        self.assertFalse(self.manifest["verification"]["collisions_globally_disabled"])

    def test_generator_reproduces_committed_artifacts(self):
        spec = importlib.util.spec_from_file_location(
            "space_arena_generator_for_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        generated = self.manifest["provenance"]["artifact_sha256"]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "src").mkdir()
            shutil.copyfile(SOURCE, output / "src" / "generate_package.py")
            module.generate(str(output))
            self.assertEqual((output / "manifest.json").read_bytes(),
                             (PACKAGE / "manifest.json").read_bytes())
            for relative in generated:
                self.assertEqual(package_file(relative).read_bytes(),
                                 (output / relative).read_bytes(), relative)


if __name__ == "__main__":
    unittest.main()
