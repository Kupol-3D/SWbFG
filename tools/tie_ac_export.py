#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Deterministic exporter: neutral Blender-scene JSON -> AC3Db for FlightGear.

Python stdlib only; no Blender/bpy, no FlightGear, no network. Intended for
the three TIE variants prepared from the Blender working scenes, in the same
independent-check spirit as Aircraft/sa_tie/Sources/build_tie.py (TASK2).

Contract:

    python3 tools/tie_ac_export.py INPUT.json OUTPUT.ac
    python3 tools/tie_ac_export.py INPUT.json OUTPUT.ac --check   # no write

Input is JSON with schema ``bombable.ac-scene/v1``::

    {
      "schema": "bombable.ac-scene/v1",
      "asset_id": "<name>",
      "materials": [
        {"name": str, "diffuse": [r,g,b] 0..1,
         "emission": [r,g,b] 0..1, "specular": [r,g,b] 0..1,
         "shininess": 0..128, "alpha": 0..1, "double_sided": bool,
         "texture": <relative POSIX path or null>}
      ],
      "objects": [
        {"name": str, "matrix_world": [16 row-major, affine],
         "vertices": [[x,y,z], ...], "triangles": [[a,b,c], ...],
         "material_indices": [int per face],
         "uv": [[[u,v] x3] per face] or null,
         "smooth": bool or [bool per face]}
      ]
    }

Coordinate mapping: ``matrix_world`` (Blender convention, row-major flat,
applied as p' = M @ (x, y, z, 1)) is baked into the vertices, giving the
metric working frame **X right, Y forward, Z up**. The file then stores
``ac = (-y, z, -x)`` with ``loc 0 0 0``. That swap is a rotation with
determinant +1, and SimGear's ACProcessPolicy maps AC (x,y,z) to the model
frame (x,-z,y); the composition is Blender (right, forward, up) -> FlightGear
model (aft, right, up) with winding preserved, so triangle order is copied
verbatim. Mirrored input matrices (det < 0) would flip winding; the producer
must avoid them (the JSON carries no other transform to compensate).

Output is ASCII AC3D v11 (``AC3Db``) restricted to what FlightGear 2024.1.7
needs: flat MATERIAL entries, a flat ``OBJECT world`` and ``OBJECT poly``
children. Dialect notes:

  * one output group per distinct (input object, material, smooth) triple,
    in first-face order; names keep ``Game_``/``Cockpit_`` prefixes intact
    and get a deterministic ``_2``-style suffix on collision;
  * ``MATERIAL ... rgb amb emis spec shi trans`` where ``amb`` copies
    ``diffuse``, ``trans = 1 - alpha`` and ``texture "rel/path"`` is the
    per-material texture (AC3D v11 material-line form). Textured groups also
    repeat the same path in the official object-level ``texture`` token;
    untextured groups write ``texture -1``;
  * ``SURF`` flags: 0x10 for smooth faces, plus 0x20 when the material is
    ``double_sided``;
  * UVs arrive in Blender's bottom-left convention and are written as
    ``v_out = 1 - v_in``; a missing face UV is written as literal ``0 0``;
  * vertices are deduplicated inside each output group by the exact printed
    ``(x, y, z, u, v)``, so a UV seam splits the shared corner while
    coincident UVs merge;
  * numbers use 8 significant digits (inside the agreed 7..9 band), ``-0``
    is normalized to ``0`` and the byte stream is deterministic for equal
    input. Objects without triangles are skipped.

Validation problems raise ``ExportError`` carrying the JSON path. The exact
behaviour is pinned by tools/test_tie_ac_export.py; running FlightGear itself
is a separate acceptance step and is not claimed here.
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

SCHEMA = "bombable.ac-scene/v1"
SMOOTH_FLAG = 0x10
TWO_SIDED_FLAG = 0x20
AFFINE_TOLERANCE = 1e-9


class ExportError(ValueError):
    """Schema, range or index problem in the input scene."""


def _fmt(value):
    """8 significant digits, -0.0 normalized to 0 (7..9 digit band)."""
    return f"{0.0 if value == 0 else value:.8g}"


def _number(value, where, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExportError(f"{where}: expected a number, got "
                          f"{type(value).__name__}")
    value = float(value)
    if not math.isfinite(value):
        raise ExportError(f"{where}: expected a finite number, got {value}")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ExportError(f"{where}: {value} is outside {low}..{high}")
    return value


def _index(value, where, count):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExportError(f"{where}: expected an integer index, got "
                          f"{value!r}")
    if not 0 <= value < count:
        if count == 0:
            raise ExportError(f"{where}: index {value} but the target list "
                              "is empty")
        raise ExportError(f"{where}: index {value} out of range 0..{count - 1}")
    return value


def _bool(value, where):
    if not isinstance(value, bool):
        raise ExportError(f"{where}: expected true or false, got {value!r}")
    return value


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ExportError(f"{where}: expected a non-empty string")
    return value


def _vector3(values, where, low=None, high=None):
    if not isinstance(values, list) or len(values) != 3:
        raise ExportError(f"{where}: expected [x, y, z], got {values!r}")
    return [_number(values[i], f"{where}[{i}]", low, high) for i in range(3)]


def _uv_pair(values, where):
    if not isinstance(values, list) or len(values) != 2:
        raise ExportError(f"{where}: expected [u, v], got {values!r}")
    return [_number(values[i], f"{where}[{i}]") for i in range(2)]


def _matrix(values, where):
    if not isinstance(values, list) or len(values) != 16:
        raise ExportError(f"{where}: expected 16 row-major numbers, got "
                          f"{values!r}")
    m = [_number(values[i], f"{where}[{i}]") for i in range(16)]
    if any(abs(m[12 + i]) > AFFINE_TOLERANCE for i in range(3)) \
            or abs(m[15] - 1.0) > AFFINE_TOLERANCE:
        raise ExportError(f"{where}: only affine matrices are supported "
                          "(last row must be 0 0 0 1)")
    return m


def _apply(m, p):
    x, y, z = p
    return (m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def _to_ac(p):
    """Blender working frame (X right, Y forward, Z up) -> AC3D axes."""
    x, y, z = p
    return (-y, z, -x)


def _texture_path(value, where):
    if not isinstance(value, str) or not value:
        raise ExportError(f"{where}: expected a non-empty relative texture "
                          f"path, got {value!r}")
    if not value.isascii() or "\\" in value:
        raise ExportError(f"{where}: texture path must be ASCII POSIX, got "
                          f"{value!r}")
    if value[0] in "/~" or (len(value) >= 2 and value[1] == ":"):
        raise ExportError(f"{where}: absolute texture paths are forbidden: "
                          f"{value!r}")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ExportError(f"{where}: texture path must be relative without "
                          f"'.' or '..' segments: {value!r}")
    if any(char in value for char in '"\r\n\t'):
        raise ExportError(f"{where}: texture path must not contain quotes or "
                          "control characters")
    return value


def _known_keys(entry, allowed, where):
    unknown = sorted(key for key in entry if key not in allowed)
    if unknown:
        raise ExportError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                          f"allowed: {', '.join(sorted(allowed))}")


def _material(entry, where):
    if not isinstance(entry, dict):
        raise ExportError(f"{where}: expected an object")
    _known_keys(entry, {"name", "diffuse", "emission", "specular",
                        "shininess", "alpha", "double_sided", "texture"},
                where)
    texture = entry.get("texture")
    return {
        "name": _text(entry.get("name"), f"{where}.name"),
        "diffuse": _vector3(entry.get("diffuse"), f"{where}.diffuse", 0.0,
                            1.0),
        "emission": _vector3(entry.get("emission", [0.0, 0.0, 0.0]),
                             f"{where}.emission", 0.0, 1.0),
        "specular": _vector3(entry.get("specular", [0.0, 0.0, 0.0]),
                             f"{where}.specular", 0.0, 1.0),
        "shininess": _number(entry.get("shininess", 0.0),
                             f"{where}.shininess", 0.0, 128.0),
        "alpha": _number(entry.get("alpha", 1.0), f"{where}.alpha", 0.0, 1.0),
        "double_sided": _bool(entry.get("double_sided", False),
                              f"{where}.double_sided"),
        "texture": None if texture is None else _texture_path(
            texture, f"{where}.texture"),
    }


def _object(entry, where):
    if not isinstance(entry, dict):
        raise ExportError(f"{where}: expected an object")
    _known_keys(entry, {"name", "matrix_world", "vertices", "triangles",
                        "material_indices", "uv", "smooth"}, where)
    vertices = entry.get("vertices")
    if not isinstance(vertices, list):
        raise ExportError(f"{where}.vertices: expected a list")
    vertices = [_vector3(v, f"{where}.vertices[{i}]")
                for i, v in enumerate(vertices)]
    triangles = entry.get("triangles")
    if not isinstance(triangles, list):
        raise ExportError(f"{where}.triangles: expected a list")
    faces = []
    for i, face in enumerate(triangles):
        if not isinstance(face, list) or len(face) != 3:
            raise ExportError(f"{where}.triangles[{i}]: expected [a, b, c], "
                              f"got {face!r}")
        faces.append([_index(face[j], f"{where}.triangles[{i}][{j}]",
                             len(vertices)) for j in range(3)])
    material_indices = entry.get("material_indices")
    if not isinstance(material_indices, list):
        raise ExportError(f"{where}.material_indices: expected a list with "
                          "one index per face")
    smooth = entry.get("smooth", False)
    if isinstance(smooth, bool):
        smooth_flags = [smooth] * len(faces)
    elif isinstance(smooth, list):
        if len(smooth) != len(faces):
            raise ExportError(f"{where}.smooth: expected a bool or a list of "
                              f"{len(faces)} bools, got {len(smooth)} items")
        smooth_flags = [_bool(v, f"{where}.smooth[{i}]")
                        for i, v in enumerate(smooth)]
    else:
        raise ExportError(f"{where}.smooth: expected a bool or a list of "
                          f"bools, got {type(smooth).__name__}")
    uv = entry.get("uv")
    if uv is None:
        uvs = [None] * len(faces)
    elif isinstance(uv, list):
        if len(uv) != len(faces):
            raise ExportError(f"{where}.uv: expected null or a list of "
                              f"{len(faces)} loop-UV faces, got {len(uv)}")
        uvs = []
        for i, face_uv in enumerate(uv):
            if face_uv is None:
                uvs.append(None)
                continue
            if not isinstance(face_uv, list) or len(face_uv) != 3:
                raise ExportError(f"{where}.uv[{i}]: expected null or three "
                                  f"[u, v] pairs, got {face_uv!r}")
            uvs.append([_uv_pair(loop_uv, f"{where}.uv[{i}][{j}]")
                        for j, loop_uv in enumerate(face_uv)])
    else:
        raise ExportError(f"{where}.uv: expected null or a list, got "
                          f"{type(uv).__name__}")
    return {
        "name": _text(entry.get("name"), f"{where}.name"),
        "matrix": _matrix(entry.get("matrix_world"), f"{where}.matrix_world"),
        "vertices": vertices,
        "triangles": faces,
        "material_indices": material_indices,
        "smooth": smooth_flags,
        "uv": uvs,
    }


_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]+")
_MAT_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.\- ]+")


def _sanitize(name, pattern):
    return pattern.sub("_", name)


class _Names:
    """Deterministic unique AC object names; suffixes never touch prefixes."""

    def __init__(self, first):
        self.used = {first}

    def unique(self, base):
        candidate = base
        suffix = 2
        while candidate in self.used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        self.used.add(candidate)
        return candidate


def _material_line(index, mat):
    name = _sanitize(mat["name"], _MAT_NAME_UNSAFE) or f"material_{index}"
    diffuse = [_fmt(v) for v in mat["diffuse"]]
    parts = [f'MATERIAL "{name}"', "rgb", *diffuse, "amb", *diffuse]
    parts += ["emis", *(_fmt(v) for v in mat["emission"])]
    parts += ["spec", *(_fmt(v) for v in mat["specular"])]
    parts += ["shi", _fmt(mat["shininess"]),
              "trans", _fmt(1.0 - mat["alpha"])]
    if mat["texture"] is not None:
        parts.append(f'texture "{mat["texture"]}"')
    return " ".join(parts)


def export_scene(scene):
    """Validate a bombable.ac-scene/v1 dict and return the AC3Db text."""
    if not isinstance(scene, dict):
        raise ExportError("scene: expected a JSON object")
    _known_keys(scene, {"schema", "asset_id", "materials", "objects"},
                "scene")
    if scene.get("schema") != SCHEMA:
        raise ExportError(f"scene: unsupported schema "
                          f"{scene.get('schema')!r}, expected {SCHEMA!r}")
    asset_id = _text(scene.get("asset_id"), "scene.asset_id")
    if not isinstance(scene.get("materials"), list):
        raise ExportError("scene.materials: expected a list")
    materials = [_material(entry, f"scene.materials[{i}]")
                 for i, entry in enumerate(scene["materials"])]
    if not isinstance(scene.get("objects"), list):
        raise ExportError("scene.objects: expected a list")

    root_name = _sanitize(asset_id, _NAME_UNSAFE) or "world"
    names = _Names(root_name)
    lines = ["AC3Db"]
    lines += [_material_line(i, mat) for i, mat in enumerate(materials)]

    blocks = []
    total_faces = 0
    for i, entry in enumerate(scene["objects"]):
        obj = _object(entry, f"scene.objects[{i}]")
        if not obj["triangles"]:
            continue  # empty objects are skipped entirely
        if len(obj["material_indices"]) != len(obj["triangles"]):
            raise ExportError("scene.objects[%d].material_indices: expected "
                              "%d indices, got %d"
                              % (i, len(obj["triangles"]),
                                 len(obj["material_indices"])))
        for face_index, mat_index in enumerate(obj["material_indices"]):
            _index(mat_index, f"scene.objects[{i}].material_indices"
                   f"[{face_index}]", len(materials))
        base = _sanitize(obj["name"], _NAME_UNSAFE) or f"object_{i}"

        # Stable groups: first-face order of each (material, smooth) pair.
        group_order = []
        groups = {}
        for face_index, face in enumerate(obj["triangles"]):
            key = (obj["material_indices"][face_index], obj["smooth"]
                   [face_index])
            if key not in groups:
                groups[key] = []
                group_order.append(key)
            groups[key].append(face_index)

        for key in group_order:
            mat = materials[key[0]]
            smooth = key[1]
            vertices = []
            vertex_ids = {}
            surfaces = []
            for face_index in groups[key]:
                face_uv = obj["uv"][face_index]
                refs = []
                for loop, vertex_index in enumerate(obj["triangles"]
                                                    [face_index]):
                    x, y, z = _to_ac(_apply(obj["matrix"],
                                            obj["vertices"][vertex_index]))
                    if face_uv is None:
                        u, v = 0.0, 0.0
                    else:
                        u = face_uv[loop][0]
                        v = 1.0 - face_uv[loop][1]  # Blender bottom-left -> AC
                    point = (_fmt(x), _fmt(y), _fmt(z), _fmt(u), _fmt(v))
                    vertex_id = vertex_ids.get(point)
                    if vertex_id is None:
                        vertex_id = len(vertices)
                        vertex_ids[point] = vertex_id
                        vertices.append(point)
                    refs.append((vertex_id, point[3], point[4]))
                surfaces.append(refs)

            flags = (SMOOTH_FLAG if smooth else 0) \
                | (TWO_SIDED_FLAG if mat["double_sided"] else 0)
            block = ["OBJECT poly", f'name "{names.unique(base)}"',
                     "loc 0 0 0",
                     f'texture "{mat["texture"]}"'
                     if mat["texture"] is not None else "texture -1",
                     "crease 45",
                     f"numvert {len(vertices)}"]
            block += [f"{p[0]} {p[1]} {p[2]}" for p in vertices]
            block.append(f"numsurf {len(surfaces)}")
            for refs in surfaces:
                block.append(f"SURF 0x{flags:02x}")
                block.append(f"mat {key[0]}")
                block.append("refs 3")
                block += [f"{v} {u} {w}" for v, u, w in refs]
            block.append("kids 0")
            blocks.append(block)
            total_faces += len(surfaces)

    lines.append("OBJECT world")
    lines.append(f'name "{root_name}"')
    lines.append(f"kids {len(blocks)}")
    for block in blocks:
        lines += block
    return "\n".join(lines) + "\n"


def export_bytes(scene):
    """ASCII AC3Db bytes; raises UnicodeError only on a validation bug."""
    return export_scene(scene).encode("ascii")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export a bombable.ac-scene/v1 JSON scene to AC3Db.")
    parser.add_argument("input", help="input scene JSON")
    parser.add_argument("output", help="output .ac file")
    parser.add_argument("--check", action="store_true",
                        help="verify OUTPUT.ac without writing")
    args = parser.parse_args(argv)

    try:
        raw = Path(args.input).read_text(encoding="utf-8")
    except OSError as exc:
        parser.exit(1, f"tie_ac_export: cannot read {args.input}: {exc}\n")
    try:
        scene = json.loads(raw)
    except ValueError as exc:
        parser.exit(1, f"tie_ac_export: {args.input}: invalid JSON: {exc}\n")
    try:
        data = export_bytes(scene)
    except ExportError as exc:
        parser.exit(1, f"tie_ac_export: {args.input}: {exc}\n")

    output = Path(args.output)
    if args.check:
        if not output.is_file() or output.read_bytes() != data:
            parser.exit(1, f"tie_ac_export: stale or missing export: "
                           f"{output}\n")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    print(f"{scene['asset_id']}: export "
          f"{'verified' if args.check else 'written'} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
