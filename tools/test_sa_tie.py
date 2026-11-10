#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Package/AC3D acceptance guards. These do not launch or certify FlightGear."""

import hashlib
import math
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "Aircraft/sa_tie"
THROTTLE = "/sim/space-arena/visual/throttle"
LIGHT = "/sim/space-arena/visual/tie-panel-light"


def sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def read_ac(path):
    """Independent parser for the exported AC3Db subset; validate all counts.

    Return materials and flattened named objects in FG model coordinates.
    No imports from the generator: malformed committed files must fail here.
    """
    tokens = iter(shlex.split(path.read_text(encoding="ascii")))

    def expect(value):
        actual = next(tokens)
        if actual != value:
            raise ValueError(f"Expected {value}, got {actual}")

    def number():
        n = float(next(tokens))
        if not math.isfinite(n):
            raise ValueError("Non-finite coordinate/material")
        return n

    def point():
        # SimGear ACProcessPolicy: AC (x,y,z) -> FG (x,-z,y).
        x, y, z = number(), number(), number()
        return (x, -z, y)

    expect("AC3Db")
    materials = []
    token = next(tokens)
    while token == "MATERIAL":
        mat = {"name": next(tokens)}
        for field, count in (("rgb",3), ("amb",3), ("emis",3),
                             ("spec",3), ("shi",1), ("trans",1)):
            expect(field)
            mat[field] = tuple(number() for _ in range(count))
        materials.append(mat)
        token = next(tokens)
    objects = {}

    def obj(start, parent=(0,0,0), ancestry=()):
        if start != "OBJECT":
            raise ValueError("Missing OBJECT")
        kind = next(tokens)
        if kind not in ("world", "group", "poly"):
            raise ValueError("Unsupported object type")
        expect("name")
        name = next(tokens)
        if name in objects:
            raise ValueError("Duplicate object name: " + name)
        expect("loc")
        origin = tuple(a+b for a, b in zip(parent, point()))
        node = {"origin": origin, "vertices": [], "faces": [], "ancestry": ancestry}
        objects[name] = node
        key = next(tokens)
        if kind == "poly":
            if key == "crease":
                number()
                key = next(tokens)
            if key != "numvert":
                raise ValueError("Missing vertices")
            for _ in range(int(next(tokens))):
                node["vertices"].append(tuple(a+b for a, b in zip(origin, point())))
            expect("numsurf")
            for _ in range(int(next(tokens))):
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
                indices = []
                for _ in range(3):
                    i = int(next(tokens))
                    if not 0 <= i < len(node["vertices"]):
                        raise ValueError("Invalid vertex reference")
                    indices.append(i)
                    number(), number()  # UVs
                a, b, c = [node["vertices"][i] for i in indices]
                normal = cross(sub(b,a), sub(c,a))
                if dot(normal,normal) < 1e-16:
                    raise ValueError("Degenerate triangle in " + name)
                node["faces"].append((indices, mat, flags))
            key = next(tokens)
        if key != "kids":
            raise ValueError("Missing children count")
        for _ in range(int(next(tokens))):
            obj(next(tokens), origin, (*ancestry,name))

    obj(token)
    if next(tokens, None) is not None:
        raise ValueError("Trailing data")
    return materials, objects


def triangles(objects):
    for name, obj in objects.items():
        for indices, _, _ in obj["faces"]:
            yield name, tuple(obj["vertices"][i] for i in indices)


def ray_hit(eye, direction, triangle):
    """Moller-Trumbore intersection; double-sided to also catch wrong normals."""
    a, b, c = triangle
    e1, e2 = sub(b,a), sub(c,a)
    h = cross(direction,e2)
    det = dot(e1,h)
    if abs(det) < 1e-10:
        return None
    s = sub(eye,a)
    u = dot(s,h)/det
    q = cross(s,e1)
    v = dot(direction,q)/det
    t = dot(e2,q)/det
    if u >= -1e-8 and v >= -1e-8 and u+v <= 1+1e-8 and t > 1e-7:
        return t
    return None


def point_triangle_distance(p, triangle):
    """Distance to triangle plane when inside, otherwise to its edges."""
    a, b, c = triangle
    ab, ac, ap = sub(b,a), sub(c,a), sub(p,a)
    n = cross(ab,ac)
    nn = dot(n,n)
    u = dot(cross(ap,ac),n)/nn
    v = dot(cross(ab,ap),n)/nn
    if u >= 0 and v >= 0 and u+v <= 1:
        return abs(dot(ap,n))/math.sqrt(nn)
    distances = []
    for x, y in ((a,b),(b,c),(c,a)):
        edge = sub(y,x)
        t = max(0,min(1,dot(sub(p,x),edge)/dot(edge,edge)))
        closest = tuple(q+t*r for q, r in zip(x,edge))
        distances.append(math.sqrt(dot(sub(p,closest),sub(p,closest))))
    return min(distances)


class TestTiePackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.materials, cls.objects = read_ac(PACKAGE / "Models/tie.ac")
        cls.geometry = list(triangles(cls.objects))
        cls.profile = ET.parse(PACKAGE / "sa_tie-set.xml").getroot()
        cls.wrapper = ET.parse(PACKAGE / "Models/tie.xml").getroot()
        view = cls.profile.find("./sim/view[@n='0']/config")
        # View axes right/up/aft -> model aft/right/up.
        cls.eye = tuple(float(view.findtext(tag)) for tag in
                        ("z-offset-m","x-offset-m","y-offset-m"))

    def test_xml_and_resource_paths_are_self_contained(self):
        xmls = list(PACKAGE.rglob("*.xml"))
        self.assertGreaterEqual(len(xmls),2)
        for path in xmls:
            root = ET.parse(path).getroot()
            self.assertEqual(root.tag,"PropertyList")
            for element in root.iter():
                self.assertNotIn("include",element.attrib)
                if element.tag == "path":
                    value = element.text.strip()
                    self.assertFalse(Path(value).is_absolute())
                    resolved = ((ROOT / value) if value.startswith("Aircraft/")
                                else path.parent / value).resolve()
                    self.assertTrue(resolved.is_relative_to(PACKAGE))
                    self.assertTrue(resolved.is_file(),value)
        self.assertEqual(self.profile.findtext("sim/model/path"),
                         "Aircraft/sa_tie/Models/tie.xml")
        self.assertEqual(self.wrapper.findtext("path"),"tie.ac")

    def test_profile_is_viewer_with_contract_defaults(self):
        self.assertEqual(self.profile.findtext("sim/flight-model"),"null")
        self.assertEqual(self.profile.findtext("sim/space-arena/ship-id"),"tie")
        self.assertEqual(self.profile.findtext("sim/space-arena/team-id"),"empire")
        self.assertEqual(self.profile.findtext("."+THROTTLE),"0.0")
        self.assertEqual(self.profile.findtext("."+LIGHT),"false")
        for view in self.profile.findall("sim/view"):
            config = view.find("config")
            self.assertEqual(config.findtext("from-model"),"true")
            self.assertGreater(float(config.findtext("ground-level-nearplane-m")),0)
        self.assertEqual(self.profile.findtext("sim/view[@n='0']/internal"),"true")
        self.assertEqual(self.profile.findtext("sim/view[@n='1']/type"),"lookat")
        self.assertEqual(self.profile.findtext("sim/view[@n='2']/type"),"lookat")
        self.assertEqual(self.profile.findtext("sim/view[@n='3']/internal"),"true")

    def test_material_uses_native_als_techniques(self):
        effects = self.wrapper.findall("effect")
        self.assertEqual(len(effects),1)
        self.assertEqual(effects[0].findtext("default"),"true")
        animated = {n.text for a in self.wrapper.findall("animation[type='material']")
                    for n in a.findall("object-name")}
        self.assertEqual({n.text for n in effects[0].findall("object-name")},animated)
        relative = effects[0].findtext("inherits-from") + ".eff"
        effect_path = (ROOT / relative).resolve()
        self.assertTrue(effect_path.is_relative_to(PACKAGE))
        effect = ET.parse(effect_path).getroot()
        self.assertEqual(effect.tag,"PropertyList")
        self.assertEqual(effect.findtext("inherits-from"),"Effects/model-combined")
        # Inherit the native ALS implementation without disabling or replacing
        # any of FlightGear's technique predicates.
        self.assertIsNone(effect.find(".//technique"))
        self.assertIsNone(effect.find(".//predicate"))
        # A model fix must not write shared rendering preferences in the set
        # file or add bindings/scripts that switch the whole scene's renderer.
        self.assertIsNone(self.profile.find("sim/rendering"))
        self.assertNotIn("/sim/rendering/",ET.tostring(self.wrapper,encoding="unicode"))
        self.assertEqual(list(effect),[
            effect.find("name"),effect.find("inherits-from")])

    def test_named_integration_nodes_and_exit_points(self):
        required = {"hull","cockpit","engine-1","engine-2","muzzle-1","muzzle-2",
                    "panel-port-cells","panel-starboard-cells","pilot-seat"}
        self.assertTrue(required <= self.objects.keys())
        self.assertFalse(any(n.startswith("engine-3") for n in self.objects))
        for i, y in ((1,-0.52),(2,0.52)):
            obj = self.objects[f"muzzle-{i}"]
            self.assertEqual(obj["origin"],(-1.725,y,-0.82))
            self.assertGreater(len(obj["faces"]),0)
        for i, y in ((1,-0.62),(2,0.62)):
            origin = self.objects[f"engine-{i}"]["origin"]
            self.assertEqual(origin,(1.63,y,0.12))
            self.assertIn("hull",self.objects[f"engine-{i}"]["ancestry"])

    def test_budget_dimensions_opaque_materials_and_panel_silhouette(self):
        self.assertGreater(len(self.geometry),1000)
        self.assertLessEqual(len(self.geometry),25000)
        points = [p for obj in self.objects.values() for p in obj["vertices"]]
        dimensions = [max(p[i] for p in points)-min(p[i] for p in points) for i in range(3)]
        for actual, target in zip(dimensions,(7.2,6.7,8.8)):
            self.assertAlmostEqual(actual,target,places=5)
        for side in ("port","starboard"):
            obj = self.objects[f"panel-{side}-cells"]
            outline = {(p[0],p[2]) for p in obj["vertices"]}
            self.assertEqual(len(outline),6)
        for mat in self.materials:
            self.assertEqual(mat["trans"],(0,))
        self.assertNotIn("texture",(PACKAGE / "Models/tie.ac").read_text())

    def test_shell_normals_face_out_and_lining_faces_in(self):
        for name, outward in (("hull-shell",True),("cockpit-lining",False)):
            obj = self.objects[name]
            for indices, _, _ in obj["faces"]:
                a, b, c = [obj["vertices"][i] for i in indices]
                facing = dot(cross(sub(b,a),sub(c,a)),a)
                self.assertGreater(facing if outward else -facing,0)

    def test_pilot_clearance_and_forward_window(self):
        self.assertEqual(self.eye,(-0.25,0,0.35))
        clearance = min(point_triangle_distance(self.eye,t) for _, t in self.geometry)
        self.assertGreater(clearance,0.35)  # actual surfaces, not object bounding boxes
        for horizontal in (-0.10,0,0.10):
            for vertical in (-0.04,0,0.04):
                target = (-1.2,horizontal,0.35+vertical)
                direction = sub(target,self.eye)
                hits = [ray_hit(self.eye,direction,t) for _, t in self.geometry]
                self.assertFalse(any(h is not None for h in hits),
                                 f"Blocked forward sightline {target}")

    def test_switch_is_visible_to_pilot_and_has_nonrepeating_toggle(self):
        target = self.objects["panel-light-switch"]["origin"]
        direction = sub(target,self.eye)
        hits = [(h,name) for name, t in self.geometry
                if (h := ray_hit(self.eye,direction,t)) is not None]
        self.assertEqual(min(hits)[1],"panel-light-switch")
        picks = self.wrapper.findall("animation[type='pick']")
        self.assertEqual(len(picks),1)
        pick = picks[0]
        self.assertEqual(pick.findtext("object-name"),"panel-light-switch")
        self.assertEqual(pick.findtext("action/button"),"0")
        self.assertEqual(pick.findtext("action/repeatable"),"false")
        self.assertEqual(pick.findtext("action/binding/command"),"property-toggle")
        self.assertEqual(pick.findtext("action/binding/property"),LIGHT)
        rotation = self.wrapper.find("animation[type='rotate']")
        self.assertEqual(rotation.findtext("property"),LIGHT)
        center = tuple(float(rotation.findtext(f"center/{axis}-m")) for axis in "xyz")
        self.assertEqual(center,target)

    def test_animation_targets_and_emission_properties(self):
        for element in self.wrapper.findall(".//object-name"):
            self.assertIn(element.text,self.objects)
        animations = self.wrapper.findall("animation[type='material']")
        actual = {a.findtext("emission/factor-prop"):
                  {n.text for n in a.findall("object-name")} for a in animations}
        self.assertEqual(actual,{
            THROTTLE: {"engine-1-glow","engine-2-glow"},
            LIGHT: {"panel-light-indicator","panel-light-strips"}})
        for animation in animations:
            colors = [float(animation.findtext("emission/"+c)) for c in ("red","green","blue")]
            self.assertTrue(any(c > 0 for c in colors))
            for name in actual[animation.findtext("emission/factor-prop")]:
                self.assertTrue(self.objects[name]["faces"])
                for _, mat, _ in self.objects[name]["faces"]:
                    self.assertEqual(self.materials[mat]["emis"],(0,0,0))

    def test_original_source_hashes_and_reproducible_export(self):
        for line in (PACKAGE / "Sources/SHA256SUMS").read_text().splitlines():
            checksum, relative = line.split("  ",1)
            path = (PACKAGE / relative).resolve()
            self.assertTrue(path.is_relative_to(PACKAGE))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),checksum)
        proc = subprocess.run([sys.executable,str(PACKAGE / "Sources/build_tie.py"),"--check"],
                              capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertTrue((PACKAGE / "LICENSE").is_file())
        self.assertTrue((PACKAGE / "README.md").is_file())


if __name__ == "__main__":
    unittest.main()
