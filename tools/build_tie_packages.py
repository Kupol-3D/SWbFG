#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Assemble FlightGear 2024.1.7 *visual* aircraft packages from prepared TIE
export artifacts.

This tool is a mechanical converter, not an aircraft/asset author.  It reads
stage-2 exports prepared by the TIE asset builder (AC3D models, textures,
integration metadata, gameplay flight profile) and writes three self-contained
viewer packages under ``--output-root``:

    Aircraft/sa_tie_royal_guard/   (ship directory royal_guard)
    Aircraft/sa_tie_m1/            (ship directory m1_bizarro)
    Aircraft/sa_tie_boarding/      (ship directory boarding_craft)

Each package contains:

    Models/<asset>.ac          merged game LOD0 + cockpit geometry (AC3Db)
    Models/<asset>.xml         FG wrapper, effect assignment
    Models/<asset>_lodN.ac     static LOD copies when supplied (optional)
    Models/<asset>-collision.* inert collision data when supplied (optional)
    Models/Textures/...        bundled BSDF textures (optional)
    Effects/visual.eff         inherits Effects/model-combined (TASK2-style)
    Config/flight_profile.json byte-for-byte copy of the ship profile
    Sources/SHA256SUMS         hashes of the immutable source inputs
    Sources/BUILD_MANIFEST.json generated file inventory + hashes
    LICENSE_ATTRIBUTION.md     original author/license/change record
    README.md                  contract, axes, statuses and commands
    <package>-set.xml          FG set file; <flight-model>null</flight-model>

Usage::

    python3 tools/build_tie_packages.py \\
        --assets-root /.../TIE_Game_Asset_Pack_v1/ships \\
        --output-root /.../Bombable/Aircraft
    python3 tools/build_tie_packages.py \\
        --assets-root /.../TIE_Game_Asset_Pack_v1/ships \\
        --output-root /.../Bombable/Aircraft --check

``--check`` regenerates the expected tree in memory, compares it with the
disk and exits 1 when anything is missing or stale.  It never writes.  A
normal build only rewrites files whose bytes differ; files not owned by this
builder are left alone (the separate assisted-flight generator owns any
``Config/flight_config*`` file).

Required per-ship inputs (all read-only):

    ships/<asset>/export/game_lod0.ac     documented alias: game.ac
    ships/<asset>/export/cockpit.ac       documented alias: game_cockpit.ac
    ships/<asset>/export/integration.json metadata contract below
    ships/<asset>/flight_profile.json     gameplay estimate profile (copied)

Optional per-ship inputs (used only when listed in integration.json):
``game_lod1.ac``, ``game_lod2.ac`` (documented aliases ``lod1.ac``,
``lod2.ac``), textures and collision geometry.

Missing required inputs produce a clear diagnostic and exit code 2; no
placeholder model, camera, texture, collision shape or FDM is invented and no
partial package is written.

integration.json contract (canonical ``bombable.tie-integration/v1``)::

    {
      "schema": "bombable.tie-integration/v1",         # optional, v1 supported
      "asset_id": "royal_guard",                      # or "ship_id"; must match dir
      "frame": {"x": "right", "y": "forward", "z": "up"},   # optional, project default
      "ship_id": "tie_royal_guard",                   # optional public Space-Arena id
      "team_id": "empire",                            # optional, default empire
      "title": "...",                                 # optional display title
      "cameras": [                                    # REQUIRED, non-empty
        {
          "id": "pilot",                              # or name/camera_id
          "role": "internal",                         # or internal: true/false
          "type": "lookfrom",                         # internal => lookfrom
          "eye_asset_xyz_m": [x, y, z],               # asset X right, Y forward, Z up
          "fov_deg": 80,                              # internal: 75..80
          "nearplane_m": 0.05,                        # internal: 0.03..0.05
          "heading_offset_deg": 0, "pitch_offset_deg": 0, "roll_offset_deg": 0
        },
        {
          "id": "external", "role": "external", "type": "lookat",
          "eye_asset_xyz_m": [-4.0, 6.0, 2.0],
          "fov_deg": 45, "nearplane_m": 0.1,
          "heading_offset_deg": 180
        }
      ],
      "cockpit": {"mode": "canon"},                   # canon|noncanonical|remote|none
      "textures": [                                   # optional
        {"source": "export/Textures/hull.png", "target": "Textures/hull.png"}
      ],
      "lods": ["game_lod1.ac", "game_lod2.ac"],       # optional, explicit names only;
                                                      # filenames relative to export/
      "collision": {"file": "collision.glb",          # filename relative to export/
                    "status": "pending_collision_backend"},
      "source": {"author": "KonbayanSW",
                 "url": "https://www.thingiverse.com/thing:4930428"},
      "changes": ["..."]                              # optional, replaces defaults
    }

Documented aliases (accepted for forward compatibility, kept explicit):
``camera``/``views`` for ``cameras``; ``eye_xyz_m``/``eye``/
``position_asset_xyz_m`` for ``eye_asset_xyz_m``; ``eye_fg_model_xyz_m``
(FG model coords aft/right/up); ``fg_offsets_right_up_aft_m`` (direct camera
offsets, bypasses conversion); ``field_of_view_deg``; ``near_plane_m``/
``ground_level_nearplane_m``; ``texture_files``/``bsdf_textures``;
``file``/``path``/``source`` for texture and LOD paths.

Coordinate transforms (identical to the TASK2 ``sa_tie`` package):

* Asset eye/model point ``(X right, Y forward, Z up)`` maps to the FlightGear
  model point ``(-Y, X, Z)`` (FG model: X aft, Y right, Z up).
* FG view offsets are camera axes ``(right, up, aft)``.  From an asset eye
  point: ``(X, Z, -Y)``.  Direct FG offsets supplied in integration.json are
  used verbatim; missing eye data is an error, never a guess.

Status boundaries (also written into every generated README):

* ``<flight-model>null</flight-model>`` unless ``--flight-model jsb --aero
  NAME`` is given; even then the named FDM is supplied by the orchestrator and
  is not runtime-accepted by this builder.
* Collision data is copied as inert geometry only:
  ``pending_collision_backend``.  No XML links it.
* The copied ``Config/flight_profile.json`` is a gameplay estimate
  (``GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS``), not an accepted FDM.
* No FlightGear launch, visual acceptance, FPS, collision or MP claim is made.

Only Python's standard library is used.  Source artifacts are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

BUILD_SCHEMA = "bombable.tie-package-build/v1"
INTEGRATION_SCHEMA = "bombable.tie-integration/v1"
MINIMUM_FG_VERSION = "2024.1.7"
AIRCRAFT_VERSION = "0.1.0"
COLLISION_STATUS = "pending_collision_backend"

LOD0_ALIASES = ("game_lod0.ac", "game.ac")
COCKPIT_ALIASES = ("cockpit.ac", "game_cockpit.ac")
LOD_ALIASES = {
    "lod1": ("game_lod1.ac", "lod1.ac"),
    "lod2": ("game_lod2.ac", "lod2.ac"),
}


class BuildError(ValueError):
    """Invalid input data or an unsafe generated result."""


class InputError(BuildError):
    """A required prepared artifact is absent or unreadable."""


@dataclass(frozen=True)
class ShipSpec:
    asset_id: str
    package: str
    title: str
    url: str
    default_cockpit_mode: str = "canon"
    remix_url: str | None = None
    notes: tuple[str, ...] = ()


SHIPS: dict[str, ShipSpec] = {
    "royal_guard": ShipSpec(
        asset_id="royal_guard",
        package="sa_tie_royal_guard",
        title="TIE Royal Guard (Emperor's Royal Guard TIE Interceptor)",
        url="https://www.thingiverse.com/thing:4930428",
        notes=(
            "Author's interpretation; measured dimensions differ from "
            "secondary lore values. See SPEC.md in the asset pack.",
        ),
    ),
    "m1_bizarro": ShipSpec(
        asset_id="m1_bizarro",
        package="sa_tie_m1",
        title="TIE Experimental M1 / Bizarro",
        url="https://www.thingiverse.com/thing:4884295",
        default_cockpit_mode="remote",
        notes=(
            "Unmanned / remotely operated in lore. An optional piloted interior "
            "is a NON_CANON_Gameplay adaptation, not canon and not a flight "
            "dynamic model.",
        ),
    ),
    "boarding_craft": ShipSpec(
        asset_id="boarding_craft",
        package="sa_tie_boarding",
        title="TIE Boarding Craft",
        url="https://www.thingiverse.com/thing:4909409",
        remix_url="https://www.thingiverse.com/thing:3178215",
        notes=(
            "Author remix/enlarged cockpit differs from the secondary "
            "encyclopedia description. Upstream remix attribution is pending.",
        ),
    ),
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildError(f"{label}: expected a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise BuildError(f"{label}: expected a finite number, got {number!r}")
    return number


def _vec3(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise BuildError(f"{label}: expected [x, y, z], got {value!r}")
    return tuple(_finite(value[i], f"{label}[{i}]") for i in range(3))


def _require_dict(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise BuildError(f"{label}: expected an object, got {type(value).__name__}")
    return value


def _first_key(mapping: dict, names: Sequence[str]) -> tuple[Any, str | None]:
    for name in names:
        if name in mapping:
            return mapping[name], name
    return None, None


def _opt_str(mapping: dict, names: Sequence[str], label: str) -> str | None:
    value, name = _first_key(mapping, names)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BuildError(f"{label}.{name}: expected a non-empty string, got {value!r}")
    return value


def _fmt(value: Any) -> str:
    """Stable shortest round-trip representation for generated text."""
    number = float(value)
    if number == 0:
        number = 0.0
    return repr(number)


def _q(value: str) -> str:
    """Quote an AC3D string token."""
    cleaned = []
    for char in value:
        if char in ('"', "\\", "\n", "\r", "\t"):
            cleaned.append("_")
        else:
            cleaned.append(char)
    return '"' + "".join(cleaned) + '"'


def _sanitize_name(value: str) -> str:
    cleaned = []
    for char in value:
        if char in ('"', "\\", "\n", "\r", "\t"):
            cleaned.append("_")
        else:
            cleaned.append(char)
    return "".join(cleaned) or "unnamed"


def _posix(value: str) -> str:
    return value.replace("\\", "/")


def _normalize_rel(value: str) -> str:
    parts = [part for part in _posix(value).split("/") if part not in ("", ".")]
    return "/".join(parts)


def _check_relative(value: str, label: str) -> str:
    """Validate a relative POSIX path and return it normalised."""
    if not isinstance(value, str) or not value.strip():
        raise BuildError(f"{label}: expected a non-empty relative path")
    raw = _posix(value)
    if raw.startswith("/") or raw.startswith("~") or ":" in raw:
        raise BuildError(f"{label}: absolute paths are forbidden: {value!r}")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise BuildError(f"{label}: path must be relative without '.' or '..' "
                         f"segments: {value!r}")
    if any(ord(char) < 32 for char in raw):
        raise BuildError(f"{label}: control characters are forbidden: {value!r}")
    return raw


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path, label: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"{label}: cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"{label}: {path} is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InputError(f"{label}: invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{label}: {path} must contain a JSON object")
    return data


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"{label}: cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"{label}: {path} is not valid UTF-8: {exc}") from exc


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InputError(f"{label}: cannot read {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# AC3Db reading / writing / merging
# ---------------------------------------------------------------------------


@dataclass
class AcMaterial:
    name: str
    rgb: tuple[float, float, float]
    amb: tuple[float, float, float]
    emis: tuple[float, float, float]
    spec: tuple[float, float, float]
    shi: float
    trans: float
    texture: str | None = None

    def key(self) -> tuple:
        return (self.name, self.rgb, self.amb, self.emis, self.spec,
                self.shi, self.trans, self.texture)


@dataclass
class AcNode:
    kind: str                       # world | group | poly
    name: str
    loc: tuple[float, float, float] = (0.0, 0.0, 0.0)
    has_loc: bool = False
    texture: str | None = None      # object-level texture (legacy), None = -1
    crease: float | None = None
    vertices: list[tuple[float, float, float]] = field(default_factory=list)
    # (surface flags, material index, [(vertex index, u, v), ...])
    surfaces: list[tuple[int, int, list[tuple[int, float, float]]]] = field(
        default_factory=list)
    kids: list["AcNode"] = field(default_factory=list)


_MATERIAL_ARITY = {
    "rgb": 3, "amb": 3, "emis": 3, "spec": 3, "shi": 1, "trans": 1,
}


def _tokens(text: str, origin: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError as exc:
        raise InputError(f"{origin}: cannot tokenize AC3D text: {exc}") from exc


def _number_at(tokens: list[str], index: int, origin: str) -> float:
    try:
        raw = tokens[index]
    except IndexError:
        raise InputError(f"{origin}: unexpected end of AC3D data") from None
    try:
        value = float(raw)
    except ValueError:
        raise InputError(f"{origin}: expected a number, got {raw!r} "
                         f"(token {index})") from None
    if not math.isfinite(value):
        raise InputError(f"{origin}: non-finite number {raw!r} (token {index})")
    return value


def _int_at(tokens: list[str], index: int, origin: str) -> int:
    try:
        raw = tokens[index]
    except IndexError:
        raise InputError(f"{origin}: unexpected end of AC3D data") from None
    try:
        return int(raw, 0)
    except ValueError:
        raise InputError(f"{origin}: expected an integer, got {raw!r} "
                         f"(token {index})") from None


def _string_at(tokens: list[str], index: int, origin: str) -> str:
    try:
        return tokens[index]
    except IndexError:
        raise InputError(f"{origin}: unexpected end of AC3D data") from None


def _parse_material(tokens: list[str], index: int, origin: str) -> tuple[AcMaterial, int]:
    # tokens[index] is the material name; fields follow in any order.
    name = _string_at(tokens, index, origin)
    index += 1
    fields: dict[str, tuple] = {}
    texture: str | None = None
    total = len(tokens)
    while index < total:
        key = tokens[index]
        if key in ("MATERIAL", "OBJECT"):
            break
        if key == "texture":
            index += 1
            texture = _string_at(tokens, index, origin)
            index += 1
            continue
        arity = _MATERIAL_ARITY.get(key)
        if arity is None:
            raise InputError(f"{origin}: unsupported MATERIAL field {key!r}")
        index += 1
        fields[key] = tuple(_number_at(tokens, index + offset, origin)
                            for offset in range(arity))
        index += arity
    missing = [key for key in ("rgb", "amb", "emis", "spec", "shi", "trans")
               if key not in fields]
    if missing:
        raise InputError(f"{origin}: MATERIAL {name!r} misses field(s) "
                         f"{', '.join(missing)}")
    return AcMaterial(
        name=name,
        rgb=fields["rgb"],
        amb=fields["amb"],
        emis=fields["emis"],
        spec=fields["spec"],
        shi=fields["shi"][0],
        trans=fields["trans"][0],
        texture=texture,
    ), index


def _parse_surface(tokens: list[str], index: int, origin: str,
                   material_count: int) -> tuple[tuple[int, int, list], int]:
    if _string_at(tokens, index, origin) != "SURF":
        raise InputError(f"{origin}: expected SURF, got {tokens[index]!r}")
    index += 1
    flags = _int_at(tokens, index, origin)
    index += 1
    if flags & 0xF:
        raise InputError(f"{origin}: unsupported AC surface type "
                         f"(SURF 0x{flags:x}); only polygons are supported")
    if _string_at(tokens, index, origin) != "mat":
        raise InputError(f"{origin}: expected mat, got {tokens[index]!r}")
    index += 1
    mat = _int_at(tokens, index, origin)
    index += 1
    if not 0 <= mat < material_count:
        raise InputError(f"{origin}: material index {mat} out of range "
                         f"0..{material_count - 1}")
    if _string_at(tokens, index, origin) != "refs":
        raise InputError(f"{origin}: expected refs, got {tokens[index]!r}")
    index += 1
    count = _int_at(tokens, index, origin)
    index += 1
    if count < 3:
        raise InputError(f"{origin}: surface with {count} refs; triangles "
                         "are required")
    refs = []
    for _ in range(count):
        vertex = _int_at(tokens, index, origin)
        u = _number_at(tokens, index + 1, origin)
        v = _number_at(tokens, index + 2, origin)
        refs.append((vertex, u, v))
        index += 3
    return (flags, mat, refs), index


def _parse_object(tokens: list[str], index: int, origin: str,
                  materials: list[AcMaterial]) -> tuple[AcNode, int]:
    if _string_at(tokens, index, origin) != "OBJECT":
        raise InputError(f"{origin}: expected OBJECT, got {tokens[index]!r}")
    index += 1
    kind = _string_at(tokens, index, origin)
    index += 1
    if kind not in ("world", "group", "poly"):
        raise InputError(f"{origin}: unsupported OBJECT type {kind!r}")
    node = AcNode(kind=kind, name="")
    total = len(tokens)
    while index < total:
        key = tokens[index]
        if key == "OBJECT":
            break
        if key == "name":
            index += 1
            node.name = _string_at(tokens, index, origin)
            index += 1
        elif key == "loc":
            index += 1
            node.loc = (_number_at(tokens, index, origin),
                        _number_at(tokens, index + 1, origin),
                        _number_at(tokens, index + 2, origin))
            node.has_loc = True
            index += 3
        elif key == "texture":
            index += 1
            raw = _string_at(tokens, index, origin)
            node.texture = None if raw == "-1" else raw
            index += 1
        elif key == "crease":
            index += 1
            node.crease = _number_at(tokens, index, origin)
            index += 1
        elif key == "numvert":
            index += 1
            count = _int_at(tokens, index, origin)
            index += 1
            if count < 0:
                raise InputError(f"{origin}: negative vertex count")
            node.vertices = [
                (_number_at(tokens, index + 3 * i, origin),
                 _number_at(tokens, index + 3 * i + 1, origin),
                 _number_at(tokens, index + 3 * i + 2, origin))
                for i in range(count)
            ]
            index += 3 * count
        elif key == "numsurf":
            index += 1
            count = _int_at(tokens, index, origin)
            index += 1
            if count < 0:
                raise InputError(f"{origin}: negative surface count")
            for _ in range(count):
                surface, index = _parse_surface(tokens, index, origin,
                                                len(materials))
                node.surfaces.append(surface)
        elif key == "kids":
            index += 1
            count = _int_at(tokens, index, origin)
            index += 1
            if count < 0:
                raise InputError(f"{origin}: negative children count")
            for _ in range(count):
                child, index = _parse_object(tokens, index, origin, materials)
                node.kids.append(child)
        else:
            raise InputError(f"{origin}: unsupported OBJECT field {key!r} "
                             f"in {node.name or kind}")
    if not node.name:
        raise InputError(f"{origin}: OBJECT {kind!r} has no name")
    for flags, mat, refs in node.surfaces:
        for vertex, _, _ in refs:
            if not 0 <= vertex < len(node.vertices):
                raise InputError(f"{origin}: surface in {node.name!r} "
                                 f"references vertex {vertex} outside "
                                 f"0..{len(node.vertices) - 1}")
        del mat, flags
    return node, index


def parse_ac(text: str, origin: str) -> tuple[list[AcMaterial], AcNode]:
    """Parse the ASCII AC3Db subset used by this project.

    Raises InputError for anything other than flat/poly/group AC3D ASCII.
    """
    tokens = _tokens(text, origin)
    if not tokens:
        raise InputError(f"{origin}: empty AC3D file")
    header = tokens[0]
    if header != "AC3Db":
        raise InputError(
            f"{origin}: unsupported AC3D header {header!r}; only ASCII AC3Db "
            "is supported (compressed/binary variants are rejected)")
    index = 1
    materials: list[AcMaterial] = []
    total = len(tokens)
    while index < total and tokens[index] == "MATERIAL":
        index += 1
        material, index = _parse_material(tokens, index, origin)
        materials.append(material)
    if index >= total or tokens[index] != "OBJECT":
        raise InputError(f"{origin}: no OBJECT tree after materials")
    root, index = _parse_object(tokens, index, origin, materials)
    if index != total:
        raise InputError(f"{origin}: trailing tokens after the AC3D object "
                         f"tree (token {index}: {tokens[index]!r})")
    return materials, root


def write_ac(root: AcNode, materials: list[AcMaterial]) -> str:
    """Serialise a parsed AC3Db tree back to deterministic ASCII."""
    lines: list[str] = ["AC3Db"]
    for material in materials:
        parts = [f"MATERIAL {_q(material.name)}", "rgb",
                 _fmt(material.rgb[0]), _fmt(material.rgb[1]),
                 _fmt(material.rgb[2]), "amb",
                 _fmt(material.amb[0]), _fmt(material.amb[1]),
                 _fmt(material.amb[2]), "emis",
                 _fmt(material.emis[0]), _fmt(material.emis[1]),
                 _fmt(material.emis[2]), "spec",
                 _fmt(material.spec[0]), _fmt(material.spec[1]),
                 _fmt(material.spec[2]), "shi", _fmt(material.shi),
                 "trans", _fmt(material.trans)]
        if material.texture:
            parts += ["texture", _q(material.texture)]
        lines.append(" ".join(parts))

    def emit(node: AcNode) -> None:
        lines.append(f"OBJECT {node.kind}")
        lines.append(f"name {_q(node.name)}")
        if node.has_loc:
            lines.append("loc " + " ".join(_fmt(v) for v in node.loc))
        if node.kind == "poly":
            # Mirror the project exporter: carry a single material texture on
            # the object token as well, so loaders that prefer the object
            # token see the bundled path.  Mixed-texture objects fall back to
            # material-only references (texture -1).
            effective = {materials[mat].texture
                         for _, mat, _ in node.surfaces}
            if len(effective) == 1:
                only = next(iter(effective))
                lines.append(f"texture {_q(only)}" if only else "texture -1")
            else:
                lines.append("texture -1")
            if node.crease is not None:
                lines.append("crease " + _fmt(node.crease))
            lines.append(f"numvert {len(node.vertices)}")
            for vertex in node.vertices:
                lines.append(" ".join(_fmt(v) for v in vertex))
            lines.append(f"numsurf {len(node.surfaces)}")
            for flags, mat, refs in node.surfaces:
                lines.append(f"SURF 0x{flags:02x}")
                lines.append(f"mat {mat}")
                lines.append(f"refs {len(refs)}")
                for vertex, u, v in refs:
                    lines.append(f"{vertex} {_fmt(u)} {_fmt(v)}")
        lines.append(f"kids {len(node.kids)}")
        for kid in node.kids:
            emit(kid)

    emit(root)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class TextureEntry:
    source: Path
    source_display: str        # relative to the ship directory, POSIX
    source_rel_export: str | None  # relative to export/ when inside it
    target: str                # relative to the package Models/ directory


def _map_texture(token: str, entries: list[TextureEntry], where: str) -> str:
    raw = _posix(token).strip()
    if not raw or raw == "-1":
        return ""
    _check_relative(raw, where)
    normalised = _normalize_rel(raw)
    for entry in entries:
        if normalised == entry.source_display:
            return entry.target
    for entry in entries:
        if entry.source_rel_export and normalised == entry.source_rel_export:
            return entry.target
    base = PurePosixPath(normalised).name
    matches = [entry for entry in entries
               if entry.source.name == base]
    if len(matches) == 1:
        return matches[0].target
    raise BuildError(
        f"{where}: texture {token!r} is not declared in integration.json "
        "'textures' and would not resolve inside the package; refusing to "
        "emit a package with a missing texture")


class _Merger:
    """Merge two parsed AC trees into one material table/object tree."""

    def __init__(self, textures: list[TextureEntry]):
        self.textures = textures
        self.materials: list[AcMaterial] = []
        self._index: dict[tuple, int] = {}
        self._names: set[str] = set()

    def _unique_name(self, base: str) -> str:
        candidate = _sanitize_name(base)
        suffix = 2
        while candidate in self._names:
            candidate = f"{candidate}_{suffix}"
            suffix += 1
        self._names.add(candidate)
        return candidate

    def _material_index(self, material: AcMaterial, object_texture: str | None,
                        where: str) -> int:
        texture = material.texture
        if texture is None and object_texture:
            texture = _map_texture(object_texture, self.textures, where)
        key = (material.name, material.rgb, material.amb, material.emis,
               material.spec, material.shi, material.trans, texture)
        index = self._index.get(key)
        if index is None:
            index = len(self.materials)
            self._index[key] = index
            self.materials.append(replace(material, texture=texture))
        return index

    def _convert(self, node: AcNode, prefix: str, materials: list[AcMaterial],
                 where: str) -> AcNode:
        converted = AcNode(
            kind=node.kind,
            name=self._unique_name(prefix + node.name),
            loc=node.loc,
            has_loc=node.has_loc,
            texture=node.texture,
            crease=node.crease,
            vertices=list(node.vertices),
            surfaces=[],
            kids=[],
        )
        for flags, mat_index, refs in node.surfaces:
            material = materials[mat_index]
            new_index = self._material_index(material, node.texture,
                                             f"{where}:{node.name}")
            converted.surfaces.append((flags, new_index, list(refs)))
        for kid in node.kids:
            converted.kids.append(self._convert(kid, prefix, materials, where))
        return converted

    def convert_root(self, root: AcNode, prefix: str,
                     materials: list[AcMaterial], group_name: str,
                     where: str) -> AcNode:
        kids = [self._convert(kid, prefix, materials, where)
                for kid in root.kids]
        return AcNode(kind="group", name=self._unique_name(group_name),
                      loc=root.loc, has_loc=root.has_loc, kids=kids)


def merge_ac(lod_materials: list[AcMaterial], lod_root: AcNode,
             cockpit_materials: list[AcMaterial], cockpit_root: AcNode,
             model_stem: str, textures: list[TextureEntry]) -> str:
    """Merge LOD0 and cockpit trees into one AC3Db document.

    The LOD0 children are grouped under ``Hull``; cockpit children under
    ``Cockpit`` with a ``Cockpit_`` name prefix.  Geometry and transforms are
    copied verbatim; only material table indices and texture references are
    rewritten.
    """
    def resolve_all(materials: list[AcMaterial], origin: str):
        resolved = []
        for index, material in enumerate(materials):
            texture = material.texture
            if texture:
                texture = _map_texture(texture, textures,
                                       f"{origin} material[{index}]")
            resolved.append(replace(material, texture=texture or None))
        return resolved

    resolved_lod = resolve_all(lod_materials, "game_lod0.ac")
    resolved_cockpit = resolve_all(cockpit_materials, "cockpit.ac")
    merger = _Merger(textures)
    hull = merger.convert_root(lod_root, "", resolved_lod, "Hull",
                               "game_lod0.ac")
    cockpit = merger.convert_root(cockpit_root, "Cockpit_", resolved_cockpit,
                                  "Cockpit", "cockpit.ac")
    world = AcNode(kind="world", name=_sanitize_name(model_stem),
                   kids=[hull, cockpit])
    return write_ac(world, merger.materials)


# ---------------------------------------------------------------------------
# integration.json normalisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Camera:
    cam_id: str
    label: str
    internal: bool
    view_type: str                 # lookfrom | lookat
    offsets: tuple[float, float, float]   # right, up, aft
    fov_deg: float
    nearplane_m: float
    heading_offset_deg: float = 0.0
    pitch_offset_deg: float = 0.0
    roll_offset_deg: float = 0.0
    source: str = "asset-eye"


_INTERNAL_ROLES = {"internal", "cockpit", "pilot", "remote", "operator"}
_EXTERNAL_ROLES = {"external", "exterior", "chase", "outside", "orbit"}


def asset_to_fg_model(point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Asset (X right, Y forward, Z up) -> FG model (X aft, Y right, Z up)."""
    return (-point[1], point[0], point[2])


def asset_eye_to_view_offsets(point: tuple[float, float, float]) -> tuple[float, float, float]:
    """Asset eye point -> FG camera offsets (right, up, aft)."""
    return (point[0], point[2], -point[1])


def _normalise_camera(raw: Any, index: int, asset_id: str) -> Camera:
    where = f"{asset_id}: cameras[{index}]"
    raw = _require_dict(raw, where)
    cam_id = _opt_str(raw, ("id", "name", "camera_id"), where) or f"camera{index}"
    label = _opt_str(raw, ("label", "title"), where) or cam_id

    view_type, _ = _first_key(raw, ("type", "view_type"))
    if view_type is not None:
        if not isinstance(view_type, str):
            raise BuildError(f"{where}.type: expected a string")
        normalised = view_type.strip().lower().replace("_", "-")
        if normalised in ("lookfrom", "look-from"):
            view_type = "lookfrom"
        elif normalised in ("lookat", "look-at"):
            view_type = "lookat"
        else:
            raise BuildError(f"{where}.type: unsupported camera type "
                             f"{view_type!r}")

    internal: bool | None = None
    if "internal" in raw:
        value = raw["internal"]
        if not isinstance(value, bool):
            raise BuildError(f"{where}.internal: expected true or false")
        internal = value
    else:
        role, role_key = _first_key(raw, ("role", "kind"))
        if role is not None:
            if not isinstance(role, str):
                raise BuildError(f"{where}.{role_key}: expected a string")
            lowered = role.strip().lower()
            if lowered in _INTERNAL_ROLES:
                internal = True
            elif lowered in _EXTERNAL_ROLES:
                internal = False
            else:
                raise BuildError(f"{where}.{role_key}: unknown role {role!r}")

    if view_type is None:
        if internal is True:
            view_type = "lookfrom"
        elif internal is False:
            view_type = "lookat"
        else:
            raise BuildError(f"{where}: cannot determine type/internal without "
                             "guessing; supply type or role")
    if internal is None:
        internal = view_type == "lookfrom"
    if internal != (view_type == "lookfrom"):
        raise BuildError(f"{where}: internal={internal} conflicts with "
                         f"type={view_type}")

    explicit, explicit_key = _first_key(
        raw, ("fg_offsets_right_up_aft_m", "offsets_right_up_aft_m",
              "fg_camera_offsets_m", "camera_offsets_right_up_aft_m"))
    fg_eye, _ = _first_key(raw, ("eye_fg_model_xyz_m", "eye_model_xyz_m"))
    asset_eye, asset_eye_key = _first_key(
        raw, ("eye_asset_xyz_m", "eye_xyz_m", "eye",
              "position_asset_xyz_m", "eye_position_asset_xyz_m"))

    if explicit is not None:
        offsets = _vec3(explicit, f"{where}.{explicit_key}")
        source = "explicit-fg-offsets"
    elif fg_eye is not None:
        aft, right, up = _vec3(fg_eye, f"{where}.eye_fg_model_xyz_m")
        offsets = (right, up, aft)
        source = "explicit-fg-model-eye"
    elif asset_eye is not None:
        right, forward, up = _vec3(asset_eye, f"{where}.{asset_eye_key}")
        offsets = asset_eye_to_view_offsets((right, forward, up))
        source = "asset-eye-mapping"
    else:
        raise BuildError(f"{where}: no eye/mapping data (expected "
                         "eye_asset_xyz_m, eye_fg_model_xyz_m or "
                         "fg_offsets_right_up_aft_m); refusing to guess a "
                         "camera position")

    fov, fov_key = _first_key(
        raw, ("fov_deg", "field_of_view_deg", "default_field_of_view_deg"))
    if fov is None:
        raise BuildError(f"{where}: missing fov_deg")
    fov = _finite(fov, f"{where}.{fov_key}")
    if internal:
        if not 75.0 <= fov <= 80.0:
            raise BuildError(f"{where}: internal camera fov must be 75..80 deg "
                             f"(got {_fmt(fov)})")
    elif fov <= 0:
        raise BuildError(f"{where}: external camera fov must be > 0 "
                         f"(got {_fmt(fov)})")

    near, near_key = _first_key(
        raw, ("nearplane_m", "ground_level_nearplane_m", "near_plane_m"))
    if near is None:
        raise BuildError(f"{where}: missing nearplane_m")
    near = _finite(near, f"{where}.{near_key}")
    if internal:
        if not 0.03 <= near <= 0.05:
            raise BuildError(f"{where}: internal camera nearplane must be "
                             f"0.03..0.05 m (got {_fmt(near)})")
    elif near <= 0:
        raise BuildError(f"{where}: external camera nearplane must be > 0 "
                         f"(got {_fmt(near)})")

    def offset(name: str) -> float:
        value, key = _first_key(raw, (name,))
        if value is None:
            return 0.0
        return _finite(value, f"{where}.{key}")

    return Camera(
        cam_id=cam_id,
        label=label,
        internal=internal,
        view_type=view_type,
        offsets=offsets,
        fov_deg=fov,
        nearplane_m=near,
        heading_offset_deg=offset("heading_offset_deg"),
        pitch_offset_deg=offset("pitch_offset_deg"),
        roll_offset_deg=offset("roll_offset_deg"),
        source=source,
    )


def _normalise_frame(integration: dict, asset_id: str) -> dict:
    frame = integration.get("frame")
    if frame is None:
        return {"x": "right", "y": "forward", "z": "up"}
    frame = _require_dict(frame, f"{asset_id}: frame")
    axes = {axis: frame.get(axis) for axis in ("x", "y", "z")}
    if axes != {"x": "right", "y": "forward", "z": "up"}:
        raise BuildError(f"{asset_id}: unsupported frame {axes!r}; only "
                         "X right / Y forward / Z up is defined by this "
                         "builder")
    return axes


def _normalise_cameras(integration: dict, asset_id: str) -> list[Camera]:
    raw, key = _first_key(integration, ("cameras", "camera", "views"))
    if raw is None:
        raise InputError(f"{asset_id}: integration.json has no 'cameras' "
                         "(aliases: camera, views); refusing to invent "
                         "camera positions")
    if not isinstance(raw, list) or not raw:
        raise BuildError(f"{asset_id}.{key}: expected a non-empty list")
    cameras = [_normalise_camera(entry, index, asset_id)
               for index, entry in enumerate(raw)]
    if not any(camera.internal for camera in cameras):
        raise BuildError(f"{asset_id}: integration.json defines no internal "
                         "(cockpit/remote) camera")
    return cameras


def _cockpit_mode(integration: dict, spec: ShipSpec) -> tuple[str, str | None]:
    raw, _ = _first_key(integration, ("cockpit", "interior"))
    note = None
    if raw is None:
        return spec.default_cockpit_mode, note
    if isinstance(raw, str):
        mode = raw
    elif isinstance(raw, dict):
        mode = _opt_str(raw, ("mode", "flight_mode", "status", "type"),
                        f"{spec.asset_id}: cockpit")
        if mode is None:
            raise BuildError(f"{spec.asset_id}: cockpit object has no mode")
        note = _opt_str(raw, ("note", "notes", "description"),
                        f"{spec.asset_id}: cockpit")
    else:
        raise BuildError(f"{spec.asset_id}: cockpit must be a string or object")
    normalised = mode.strip().lower().replace("-", "").replace("_", "")
    aliases = {
        "canon": "canon", "canonical": "canon",
        "noncanon": "noncanonical", "noncanonical": "noncanonical",
        "noncanongameplay": "noncanonical",
        "remote": "remote", "unmanned": "remote", "drone": "remote",
        "none": "none", "absent": "none",
    }
    if normalised not in aliases:
        raise BuildError(f"{spec.asset_id}: unsupported cockpit mode {mode!r} "
                         "(expected canon, noncanonical, remote or none)")
    return aliases[normalised], note


def _normalise_textures(integration: dict, ship_dir: Path,
                        asset_id: str) -> list[TextureEntry]:
    raw, key = _first_key(integration, ("textures", "texture_files",
                                        "bsdf_textures"))
    if raw is None:
        return []
    pairs: list[tuple[Any, Any]] = []
    if isinstance(raw, dict):
        pairs.extend(raw.items())
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, str):
                pairs.append((item, None))
            elif isinstance(item, dict):
                source = _opt_str(item, ("source", "src", "path", "file"),
                                  f"{asset_id}.{key}[{index}]")
                target = _opt_str(item, ("target", "dest", "destination",
                                         "output"), f"{asset_id}.{key}[{index}]")
                pairs.append((source, target))
            else:
                raise BuildError(f"{asset_id}.{key}[{index}]: expected a "
                                 "string or object")
    else:
        raise BuildError(f"{asset_id}.{key}: expected a list or object")

    export_dir = (ship_dir / "export").resolve()
    entries: list[TextureEntry] = []
    seen_targets: set[str] = set()
    for index, (source, target) in enumerate(pairs):
        where = f"{asset_id}.{key}[{index}]"
        if not isinstance(source, str) or not source.strip():
            raise BuildError(f"{where}.source: expected a non-empty path")
        source_rel = _check_relative(source, f"{where}.source")
        source_path = (ship_dir / source_rel).resolve()
        if not source_path.is_file():
            raise InputError(f"{where}.source: texture does not exist: "
                             f"{source_path}")
        if not source_path.is_relative_to(ship_dir.resolve()):
            raise BuildError(f"{where}.source: texture escapes the ship "
                             f"directory: {source}")
        if target is None:
            target_rel = "Textures/" + PurePosixPath(source_rel).name
        else:
            if not isinstance(target, str):
                raise BuildError(f"{where}.target: expected a string")
            target_rel = _check_relative(target, f"{where}.target")
        if target_rel in seen_targets:
            raise BuildError(f"{where}.target: duplicate texture target "
                             f"{target_rel!r}")
        seen_targets.add(target_rel)
        try:
            display = source_path.relative_to(ship_dir.resolve()).as_posix()
        except ValueError:
            display = source_rel
        try:
            rel_export = source_path.relative_to(export_dir).as_posix()
        except ValueError:
            rel_export = None
        entries.append(TextureEntry(source=source_path, source_display=display,
                                    source_rel_export=rel_export,
                                    target=target_rel))
    return entries


def _normalise_lods(integration: dict, export_dir: Path,
                    asset_id: str) -> list[tuple[str, Path]]:
    raw, key = _first_key(integration, ("lods", "lod", "levels_of_detail"))
    if raw is None:
        return []
    items: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        items.extend(raw.items())
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, str):
                items.append((f"lod{index + 1}", item))
            elif isinstance(item, dict):
                name = _opt_str(item, ("name", "id", "label"), 
                                f"{asset_id}.{key}[{index}]")
                rel = _opt_str(item, ("file", "path", "source", "ac"),
                               f"{asset_id}.{key}[{index}]")
                items.append((name or f"lod{index + 1}", rel))
            else:
                raise BuildError(f"{asset_id}.{key}[{index}]: expected a "
                                 "string or object")
    else:
        raise BuildError(f"{asset_id}.{key}: expected a list or object")

    lods: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for name, rel in items:
        where = f"{asset_id}: lod {name!r}"
        if isinstance(rel, dict):
            rel = _opt_str(rel, ("file", "path", "source", "ac"), where)
        if not isinstance(rel, str) or not rel.strip():
            raise BuildError(f"{where}: missing file/path")
        rel = _check_relative(rel, where)
        path = (export_dir / rel).resolve()
        if not path.is_file():
            raise InputError(f"{where}: LOD file does not exist: {path}")
        if not path.is_relative_to(export_dir):
            raise BuildError(f"{where}: LOD escapes export/: {rel}")
        if not path.name.endswith(".ac"):
            raise BuildError(f"{where}: LOD must be an .ac file, got {path.name}")
        safe = _sanitize_name(name)
        if safe in seen:
            raise BuildError(f"{where}: duplicate LOD name {name!r}")
        seen.add(safe)
        lods.append((safe, path))
    # Explicit conservative naming: lod1/lod2 aliases are accepted, arbitrary
    # additional levels are copied but never wired into runtime selection.
    return lods


def _normalise_collision(integration: dict, export_dir: Path,
                         asset_id: str) -> Path | None:
    raw, _ = _first_key(integration, ("collision", "collider",
                                      "collision_geometry"))
    if raw is None:
        return None
    if isinstance(raw, bool):
        if raw:
            raise BuildError(f"{asset_id}: collision=true is not an artifact; "
                             "provide {'file': ..., 'status': "
                             "'pending_collision_backend'}")
        return None
    rel: Any = None
    status = COLLISION_STATUS
    if isinstance(raw, str):
        rel = raw
    elif isinstance(raw, dict):
        rel = _opt_str(raw, ("file", "glb", "path", "geometry", "source"),
                       f"{asset_id}: collision")
        status = _opt_str(raw, ("status", "backend", "state"),
                          f"{asset_id}: collision") or COLLISION_STATUS
        if "pending" not in status.lower():
            raise BuildError(f"{asset_id}: collision status {status!r} is not "
                             "pending; this builder never claims a working "
                             "collision backend")
    else:
        raise BuildError(f"{asset_id}: collision must be a string or object")
    if rel is None:
        return None
    if not isinstance(rel, str) or not rel.strip():
        raise BuildError(f"{asset_id}: collision.file must be a non-empty path")
    rel = _check_relative(rel, f"{asset_id}: collision.file")
    path = (export_dir / rel).resolve()
    if not path.is_file():
        raise InputError(f"{asset_id}: collision file does not exist: {path}")
    if not path.is_relative_to(export_dir):
        raise BuildError(f"{asset_id}: collision file escapes export/: {rel}")
    return path


def _normalise_attribution(integration: dict, spec: ShipSpec) -> dict:
    source, _ = _first_key(integration, ("source", "attribution",
                                         "provenance"))
    if source is None:
        source = {}
    source = _require_dict(source, f"{spec.asset_id}: source")
    author = _opt_str(source, ("author", "creator", "artist"),
                      f"{spec.asset_id}: source") or "KonbayanSW"
    url = _opt_str(source, ("url", "source_url", "link", "thingiverse"),
                   f"{spec.asset_id}: source") or spec.url
    if not url.startswith(("http://", "https://")):
        raise BuildError(f"{spec.asset_id}: source.url must be an absolute "
                         f"http(s) URL, got {url!r}")

    changes, changes_key = _first_key(integration, ("changes",
                                                    "modifications"))
    if changes is None:
        changes = _default_changes(spec)
    else:
        if not isinstance(changes, list) or not all(
                isinstance(item, str) and item.strip() for item in changes):
            raise BuildError(f"{spec.asset_id}.{changes_key}: expected a list "
                             "of non-empty strings")
        changes = list(changes)
    return {"author": author, "url": url, "changes": changes}


def _default_changes(spec: ShipSpec) -> list[str]:
    changes = [
        "Prepared from the supplied author STL/artifacts; source files were "
        "not modified by this builder.",
        "Uniform metric scaling, UV/materials and LOD extraction were "
        "performed by the upstream asset-builder stage, not here.",
        "FlightGear package assembled mechanically from export/game_lod0.ac "
        "and export/cockpit.ac.",
    ]
    if spec.default_cockpit_mode == "remote":
        changes.append("M1 is configured as a remote-operated viewer; any "
                       "piloted interior is a noncanonical gameplay "
                       "adaptation.")
    return changes


# ---------------------------------------------------------------------------
# Rendering generated package files
# ---------------------------------------------------------------------------


def _render_wrapper_xml(asset_id: str, spec: ShipSpec) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!-- SPDX-License-Identifier: GPL-2.0-or-later -->\n'
        '<!-- Generated by tools/build_tie_packages.py - do not edit by hand. -->\n'
        '<PropertyList>\n'
        f'  <path>{escape(asset_id)}.ac</path>\n'
        '  <effect>\n'
        f'    <inherits-from>Aircraft/{spec.package}/Effects/visual'
        '</inherits-from>\n'
        '    <default type="bool">true</default>\n'
        '  </effect>\n'
        '</PropertyList>\n'
    )


def _render_effect(spec: ShipSpec) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!-- SPDX-License-Identifier: GPL-2.0-or-later -->\n'
        '<!-- Generated by tools/build_tie_packages.py - do not edit by hand. -->\n'
        '<PropertyList>\n'
        f'  <name>Aircraft/{spec.package}/Effects/visual</name>\n'
        '  <!-- TASK2 pattern: inherit the stock opaque model material with\n'
        '       its native ALS path.  Technique selection and lighting\n'
        '       preferences stay with FlightGear; ALS is not disabled. -->\n'
        '  <inherits-from>Effects/model-combined</inherits-from>\n'
        '</PropertyList>\n'
    )


def _ordered_cameras(cameras: list[Camera]) -> list[Camera]:
    internal = [camera for camera in cameras if camera.internal]
    external = [camera for camera in cameras if not camera.internal]
    preferred = ("pilot", "cockpit", "remote", "operator", "command")
    primary = next((camera for camera in internal
                    if camera.cam_id.lower() in preferred), internal[0])
    ordered = [primary] + [camera for camera in internal if camera is not primary]
    return ordered + external


def _render_view(index: int, camera: Camera) -> list[str]:
    lines = [
        f'    <view n="{index}">',
        f'      <name>{escape(camera.label)}</name>',
        f'      <type>{camera.view_type}</type>',
        f'      <internal type="bool">{"true" if camera.internal else "false"}'
        '</internal>',
        '      <config>',
        '        <from-model type="bool">true</from-model>',
        '        <from-model-idx type="int">0</from-model-idx>',
    ]
    if not camera.internal:
        lines += [
            '        <at-model type="bool">true</at-model>',
            '        <at-model-idx type="int">0</at-model-idx>',
            '        <eye-heading-deg-path>/orientation/heading-deg'
            '</eye-heading-deg-path>',
            '        <eye-pitch-deg-path>/orientation/pitch-deg'
            '</eye-pitch-deg-path>',
            '        <eye-roll-deg-path>/orientation/roll-deg'
            '</eye-roll-deg-path>',
        ]
    lines += [
        f'        <x-offset-m type="double">{_fmt(camera.offsets[0])}'
        '</x-offset-m>',
        f'        <y-offset-m type="double">{_fmt(camera.offsets[1])}'
        '</y-offset-m>',
        f'        <z-offset-m type="double">{_fmt(camera.offsets[2])}'
        '</z-offset-m>',
        f'        <heading-offset-deg type="double">'
        f'{_fmt(camera.heading_offset_deg)}</heading-offset-deg>',
        f'        <pitch-offset-deg type="double">'
        f'{_fmt(camera.pitch_offset_deg)}</pitch-offset-deg>',
        f'        <roll-offset-deg type="double">'
        f'{_fmt(camera.roll_offset_deg)}</roll-offset-deg>',
        f'        <default-field-of-view-deg type="double">'
        f'{_fmt(camera.fov_deg)}</default-field-of-view-deg>',
        f'        <ground-level-nearplane-m type="double">'
        f'{_fmt(camera.nearplane_m)}</ground-level-nearplane-m>',
        '      </config>',
        '    </view>',
    ]
    return lines


def _render_set_xml(spec: ShipSpec, asset_id: str, ship_id: str,
                    team_id: str, cameras: list[Camera], mode: str,
                    cockpit_note: str | None, flight_model: str,
                    aero: str | None, attribution: dict) -> str:
    ordered = _ordered_cameras(cameras)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!-- SPDX-License-Identifier: GPL-2.0-or-later -->',
        '<!-- Generated by tools/build_tie_packages.py - do not edit by hand. -->',
        '<PropertyList>',
        '  <sim>',
        f'    <description>Space Arena {escape(spec.title)} - visual package '
        '(no flight dynamics)</description>',
        f'    <author>Space Arena contributors; original model by '
        f'{escape(attribution["author"])} (see LICENSE_ATTRIBUTION.md)'
        '</author>',
        f'    <aircraft-version>{AIRCRAFT_VERSION}</aircraft-version>',
        f'    <minimum-fg-version>{MINIMUM_FG_VERSION}</minimum-fg-version>',
        '    <status>alpha</status>',
    ]
    if flight_model == "jsb":
        lines.append(f'    <flight-model>jsb</flight-model>'
                     f'<aero>{escape(aero or "")}</aero>')
        lines.append('    <!-- Selected by the orchestrator via the '
                     'flight-model/aero options. The FDM is externally '
                     'supplied and is NOT runtime-accepted by this '
                     'builder. -->')
    else:
        lines.append('    <flight-model>null</flight-model>')
        lines.append('    <!-- Visual viewer only: no FDM is enabled. The '
                     'separate assisted-flight generator is not selected '
                     'here. -->')
    lines += [
        f'    <model><path>Aircraft/{spec.package}/Models/{asset_id}.xml'
        '</path></model>',
        '    <chase-distance-m type="double">-18</chase-distance-m>',
        f'    <startup><splash-title>{escape(spec.title)} - static visual '
        'viewer only</splash-title></startup>',
        '    <space-arena>',
        f'      <ship-id>{escape(ship_id)}</ship-id>'
        f'<team-id>{escape(team_id)}</team-id>',
        '    </space-arena>',
        '    <help>',
        '      <line>Static visual package generated from prepared TIE '
        'artifacts. No FlightGear runtime or MP claim.</line>',
        '      <line>FDM: flight-model is null unless the orchestrator '
        'selected a supplied adapter; this package itself flies nothing.'
        '</line>',
        f'      <line>Collision data: {COLLISION_STATUS}. Any bundled '
        'collision geometry is inert and not linked by XML.</line>',
        '      <line>v cycles views. View offsets are camera axes '
        '(right, up, aft); model axes are (aft, right, up).</line>',
    ]
    if mode in ("noncanonical", "remote"):
        lines.append('      <line>M1 remote/noncanonical gameplay '
                     'adaptation: NON_CANON_Gameplay. Not canon and not a '
                     'flight dynamic model.</line>')
    if attribution.get("remix_pending"):
        lines.append('      <line>Model contains third-party remix parts; '
                     'upstream attribution is pending. See '
                     'LICENSE_ATTRIBUTION.md.</line>')
    lines += [
        '    </help>',
        '    <!-- Camera axes: right, up, aft. Model axes: aft, right, up. -->',
    ]
    for index, camera in enumerate(ordered):
        lines += _render_view(index, camera)
    lines += [
        '  </sim>',
        '</PropertyList>',
        '',
    ]
    return "\n".join(lines)


def _render_license_attribution(spec: ShipSpec, attribution: dict,
                                mode: str) -> str:
    author = attribution["author"]
    url = attribution["url"]
    lines = [
        f'# {spec.package} - attribution and license record',
        '',
        'Generated by `tools/build_tie_packages.py`. This file records the '
        'origin of the prepared artifacts; it is not a legal opinion.',
        '',
        '## Original model',
        '',
        f'- Title: {spec.title}',
        f'- Author: {author}',
        f'- Source: {url}',
        '- License: Creative Commons - Attribution (CC BY). The supplied '
        'source files state "Creative Commons - Attribution" **without a '
        'version number**. The exact CC BY version (2.0 / 3.0 / 4.0) is '
        '**unknown** and must be verified against the source page and full '
        'terms before publication.',
    ]
    if spec.remix_url:
        lines += [
            f'- Remix: this model includes parts from {spec.remix_url}. '
            'The upstream remix author and license are **pending** '
            'verification.',
        ]
    lines += [
        '',
        '## Changes applied',
        '',
    ]
    lines += [f'- {change}' for change in attribution["changes"]]
    if mode == "noncanonical":
        lines.append('- Cockpit/interior is an explicitly noncanonical '
                     'gameplay adaptation (NON_CANON_Gameplay).')
    elif mode == "remote":
        lines.append('- Vessel is presented in a remote-operated gameplay '
                     'configuration; a piloted interior, if present, is a '
                     'noncanonical gameplay adaptation.')
    lines += [
        '',
        '## Generated integration files',
        '',
        'The mechanical package assembly (merged AC3D, XML wrappers, set '
        'file, effect file, this record and README) is Space Arena '
        'contributors, GPL-2.0-or-later (see the repository `LICENSE`). '
        'The original model, textures and their licenses remain under the '
        'terms above; this record does not relicense them.',
        '',
        '## Status',
        '',
        '- Visual package only. No FlightGear runtime, visual-acceptance, '
        'FPS, collision or multiplayer test is claimed.',
        f'- Collision geometry, when bundled: `{COLLISION_STATUS}`. It is '
        'inert data and is not linked by XML.',
        '- Flight dynamics: none enabled here. The copied '
        '`Config/flight_profile.json` is a gameplay estimate '
        '(`GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS`), not an '
        'accepted FDM.',
        '- Star Wars / Lucasfilm trademarks: this record and the GPL notice '
        'do not grant any rights to trademarks or the original fictional '
        'work.',
        '- Source hashes: see `Sources/SHA256SUMS`.',
        '',
    ]
    return "\n".join(lines)


def _render_readme(spec: ShipSpec, asset_id: str, ship_id: str,
                   team_id: str, cameras: list[Camera], mode: str,
                   flight_model: str, aero: str | None,
                   attribution: dict, inputs: dict[str, str]) -> str:
    lines = [
        f'# {spec.package} - {spec.title} visual package',
        '',
        f'`ship_id={ship_id}`, `team_id={team_id}`. Generated mechanically '
        'by `tools/build_tie_packages.py` from the prepared TIE asset-pack '
        'stage-2 exports. This is a static viewer for FlightGear '
        f'{MINIMUM_FG_VERSION}; it is not an aircraft completion claim.',
        '',
        '## Status (do not over-read)',
        '',
        '- `<flight-model>null</flight-model>` by default: no flight '
        'dynamics are enabled here. The separate assisted-flight adapter is '
        'a different generator and is not selected by this package.',
    ]
    if flight_model == "jsb":
        lines.append(f'- FDM selection was requested by the orchestrator '
                     f'(`jsb` / `{aero}`). The named FDM is externally '
                     'supplied and is **not** runtime-accepted by this '
                     'builder.')
    lines += [
        f'- Collision data is copied as inert geometry only: '
        f'`{COLLISION_STATUS}`. No XML references it.',
    ]
    if ship_id == "boarding_craft":
        lines += [
            '- Boarding visual status: **candidate, not orchestrator-accepted**. '
            'The current cockpit window/cabin is a rev3 repair candidate; the '
            'previous blocked slot is not claimed as completed.',
        ]
    lines += [
        '- No FlightGear launch, visual acceptance, FPS, collision or '
        'multiplayer test was performed by this builder.',
        '- Source model license: CC BY, version unstated in the supplied '
        'files; see `LICENSE_ATTRIBUTION.md`. Publication requires '
        're-verification.',
        '',
        '## Inputs consumed (read-only)',
        '',
    ]
    for label, display in inputs.items():
        lines.append(f'- `{display}` ({label})')
    lines += [
        '',
        'Missing required inputs fail the build with a diagnostic; no '
        'placeholder model, camera, texture, collider or FDM is created.',
        '',
        '## Outputs',
        '',
        f'- `Models/{asset_id}.ac` - merged game LOD0 + cockpit geometry '
        '(AC3Db).',
        f'- `Models/{asset_id}.xml` - FG wrapper with the local effect.',
        '- `Models/*_lod*.ac` - static LOD copies when supplied. They are '
        'bundled data only; runtime LOD selection is **not** wired or tested.',
        '- `Models/*-collision.*` - inert collision data when supplied.',
        '- `Models/Textures/...` - bundled BSDF textures with rewritten '
        'relative references.',
        '- `Effects/visual.eff` - inherits `Effects/model-combined` '
        '(TASK2 pattern; ALS is not disabled).',
        '- `Config/flight_profile.json` - byte-for-byte copy of the ship '
        'profile; it is a gameplay estimate, not an accepted FDM.',
        '- `Sources/SHA256SUMS`, `Sources/BUILD_MANIFEST.json` - input and '
        'generated-file provenance.',
        '- `LICENSE_ATTRIBUTION.md`, `README.md`, '
        f'`{spec.package}-set.xml`.',
        '',
        '## Axes and cameras',
        '',
        'Asset frame: X right, Y forward, Z up, metres. FG model frame: '
        'X aft, Y right, Z up. Asset point `(X, Y, Z)` -> FG model '
        '`(-Y, X, Z)`; FG camera view offsets `(right, up, aft)` = '
        '`eye_asset_xyz_m` transformed to `(X, Z, -Y)`. Cameras come from '
        '`export/integration.json`; this builder never guesses a viewpoint.',
        '',
        'Bundled cameras:',
        '',
    ]
    for camera in _ordered_cameras(cameras):
        axis = "internal" if camera.internal else "external"
        offsets = ", ".join(_fmt(value) for value in camera.offsets)
        lines.append(f'- `{camera.cam_id}` ({axis}, {camera.view_type}, '
                     f'{axis} FOV {_fmt(camera.fov_deg)}, near '
                     f'{_fmt(camera.nearplane_m)} m, offsets right/up/aft = '
                     f'({offsets}), source: {camera.source})')
    lines += [
        '',
        '## Rebuild and check',
        '',
        'From the repository root:',
        '',
        '```sh',
        'python3 tools/build_tie_packages.py \\',
        f'  --assets-root /path/to/TIE_Game_Asset_Pack_v1/ships \\',
        f'  --output-root "$PWD/Aircraft"',
        'python3 tools/build_tie_packages.py \\',
        '  --assets-root /path/to/TIE_Game_Asset_Pack_v1/ships \\',
        '  --output-root "$PWD/Aircraft" --check',
        '```',
        '',
        '`--check` only compares; it never rewrites files.',
        '',
        '## Flight-configuration boundary',
        '',
        '`Config/flight_profile.json` is a byte-for-byte copy of the source '
        'profile (status '
        '`GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS`). The separate '
        'assisted-flight generator owns any `flight_config*` file; this '
        'builder neither creates nor overwrites one.',
        '',
    ]
    if mode in ("noncanonical", "remote"):
        lines += [
            '## Gameplay adaptation status',
            '',
            'M1 Bizarro is unmanned/remote in the supplied lore. The '
            'optional piloted interior is a **NON_CANON_Gameplay** '
            'adaptation, not canon and not a validated flight model. See '
            '`LICENSE_ATTRIBUTION.md`.',
            '',
        ]
    return "\n".join(lines)


def _render_sums(sources: list[tuple[str, Path]]) -> str:
    lines = []
    for display, path in sorted(sources, key=lambda item: item[0]):
        lines.append(f"{_sha256(path.read_bytes())}  {display}")
    return "\n".join(lines) + "\n" if lines else ""


def _render_manifest(spec: ShipSpec, asset_id: str, files: dict[str, bytes],
                     sources: list[tuple[str, Path]], flight_model: str,
                     aero: str | None, mode: str) -> str:
    payload = {
        "schema": BUILD_SCHEMA,
        "generator": "tools/build_tie_packages.py",
        "asset_id": asset_id,
        "package": spec.package,
        "flight_model": flight_model,
        "aero": aero,
        "cockpit_mode": mode,
        "collision_status": COLLISION_STATUS,
        "files": {name: _sha256(data) for name, data in sorted(files.items())},
        "sources": {display: _sha256(path.read_bytes())
                    for display, path in sorted(sources, key=lambda i: i[0])},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Input resolution and per-package build
# ---------------------------------------------------------------------------


def resolve_ships_root(assets_root: Path) -> Path:
    root = Path(assets_root)
    if not root.exists():
        raise InputError(f"assets root does not exist: {root}")
    if not root.is_dir():
        raise InputError(f"assets root is not a directory: {root}")
    nested = root / "ships"
    if nested.is_dir():
        return nested.resolve()
    if any((root / asset_id).is_dir() for asset_id in SHIPS):
        return root.resolve()
    raise InputError(
        f"{root}: no 'ships/' directory and none of {sorted(SHIPS)} found; "
        "pass the TIE asset pack's ships directory")


def _find_existing(export_dir: Path,
                   relatives: Sequence[str]) -> Path | None:
    for relative in relatives:
        candidate = export_dir / relative
        if candidate.is_file():
            return candidate.resolve()
    return None


def _preflight_required(ship_dir: Path, asset_id: str,
                        export_dir: Path) -> tuple[Path, Path]:
    """Check every required artifact at once so the diagnostic is complete."""
    problems: list[str] = []
    lod0 = _find_existing(export_dir, LOD0_ALIASES)
    if lod0 is None:
        problems.append(
            "game LOD0 AC: " + ", ".join(str(export_dir / relative)
                                         for relative in LOD0_ALIASES)
            + " (documented alias: game.ac)")
    cockpit = _find_existing(export_dir, COCKPIT_ALIASES)
    if cockpit is None:
        problems.append(
            "cockpit AC: " + ", ".join(str(export_dir / relative)
                                       for relative in COCKPIT_ALIASES)
            + " (documented alias: game_cockpit.ac)")
    integration = export_dir / "integration.json"
    if not integration.is_file():
        problems.append(f"integration metadata: {integration}")
    profile = ship_dir / "flight_profile.json"
    if not profile.is_file():
        problems.append(f"flight profile: {profile}")
    if problems:
        raise InputError(
            f"{asset_id}: missing required stage-2 artifacts:\n  "
            + "\n  ".join(problems)
            + "\n  The asset builder must supply them; no placeholder "
              "model/camera/texture/collider/FDM is created.")
    return lod0, cockpit


def _optional_existing(export_dir: Path, relatives: Sequence[str],
                       label: str) -> Path | None:
    for relative in relatives:
        candidate = export_dir / relative
        if candidate.is_file():
            return candidate.resolve()
    return None


def _load_profile(ship_dir: Path, asset_id: str) -> tuple[bytes, dict]:
    path = ship_dir / "flight_profile.json"
    if not path.is_file():
        raise InputError(
            f"{asset_id}: missing required profile {path}; it is read-only "
            "and is copied to Config/flight_profile.json")
    data = _load_json(path, f"{asset_id}: flight_profile.json")
    ship_id = data.get("ship_id")
    if ship_id != asset_id:
        raise BuildError(f"{asset_id}: flight_profile.json ship_id "
                         f"{ship_id!r} does not match directory {asset_id!r}")
    return path.read_bytes(), data


def build_package(ship_dir: Path, ships_root: Path, spec: ShipSpec,
                  flight_model: str, aero: str | None,
                  output_root: Path) -> dict[str, bytes]:
    asset_id = spec.asset_id
    ship_dir = ship_dir.resolve()
    export_dir = ship_dir / "export"

    lod0_path, cockpit_path = _preflight_required(ship_dir, asset_id,
                                                  export_dir)
    integration_path = export_dir / "integration.json"
    integration = _load_json(integration_path, f"{asset_id}: integration.json")
    schema = integration.get("schema")
    if schema is not None and schema != INTEGRATION_SCHEMA:
        raise BuildError(f"{asset_id}: unsupported integration schema "
                         f"{schema!r}; expected {INTEGRATION_SCHEMA!r}")

    declared_id, _ = _first_key(integration, ("asset_id", "ship_id"))
    if declared_id is not None and declared_id != asset_id:
        raise BuildError(f"{asset_id}: integration.json declares "
                         f"asset_id/ship_id {declared_id!r}, expected "
                         f"{asset_id!r}")
    ship_id = _opt_str(integration, ("ship_id",), f"{asset_id}: integration")
    if ship_id is None:
        ship_id = asset_id
    team_id = _opt_str(integration, ("team_id",), f"{asset_id}: integration")
    if team_id is None:
        team_id = "empire"

    _normalise_frame(integration, asset_id)
    cameras = _normalise_cameras(integration, asset_id)
    mode, cockpit_note = _cockpit_mode(integration, spec)
    textures = _normalise_textures(integration, ship_dir, asset_id)
    lods = _normalise_lods(integration, export_dir, asset_id)
    if not lods:
        # Conservative documented auto-detection for the standard export
        # names; integration.json always wins and runtime selection is never
        # wired up or claimed.
        for name, aliases in LOD_ALIASES.items():
            found = _optional_existing(export_dir, aliases, name)
            if found is not None:
                lods.append((name, found))
    collision = _normalise_collision(integration, export_dir, asset_id)
    attribution = _normalise_attribution(integration, spec)
    if spec.remix_url:
        attribution["remix_pending"] = True

    profile_bytes, _profile = _load_profile(ship_dir, asset_id)
    lod0_materials, lod0_root = parse_ac(_read_text(lod0_path, "game_lod0.ac"),
                                         str(lod0_path))
    cockpit_materials, cockpit_root = parse_ac(
        _read_text(cockpit_path, "cockpit.ac"), str(cockpit_path))
    merged_ac = merge_ac(lod0_materials, lod0_root, cockpit_materials,
                         cockpit_root, asset_id, textures)

    if flight_model == "jsb":
        if not aero:
            raise BuildError(f"{asset_id}: --flight-model jsb requires "
                             "--aero NAME")
        models_dir = (output_root / spec.package / "Models").resolve()
        aero_path = (models_dir / aero).resolve()
        try:
            aero_path.relative_to(models_dir)
        except ValueError:
            raise BuildError(f"{asset_id}: --aero escapes Models/: {aero!r}")
        if not aero_path.is_file():
            raise InputError(
                f"{asset_id}: supplied FDM {aero!r} not found at {aero_path}; "
                "the orchestrator must place the JSBSim XML in the package "
                "before selecting it")

    files: dict[str, bytes] = {}
    files[f"Models/{asset_id}.ac"] = merged_ac.encode("utf-8")
    files[f"Models/{asset_id}.xml"] = _render_wrapper_xml(asset_id,
                                                          spec).encode("utf-8")
    files["Effects/visual.eff"] = _render_effect(spec).encode("utf-8")
    files[f"{spec.package}-set.xml"] = _render_set_xml(
        spec, asset_id, ship_id, team_id, cameras, mode, cockpit_note,
        flight_model, aero, attribution).encode("utf-8")
    files["Config/flight_profile.json"] = profile_bytes
    files["LICENSE_ATTRIBUTION.md"] = _render_license_attribution(
        spec, attribution, mode).encode("utf-8")
    files["README.md"] = _render_readme(
        spec, asset_id, ship_id, team_id, cameras, mode, flight_model, aero,
        attribution, {
            "game LOD0 AC": lod0_path.relative_to(ships_root).as_posix(),
            "cockpit AC": cockpit_path.relative_to(ships_root).as_posix(),
            "integration metadata": integration_path.relative_to(
                ships_root).as_posix(),
            "flight profile (copied)": (
                ship_dir / "flight_profile.json").relative_to(
                    ships_root).as_posix(),
        }).encode("utf-8")

    sources: list[tuple[str, Path]] = [
        (lod0_path.relative_to(ships_root).as_posix(), lod0_path),
        (cockpit_path.relative_to(ships_root).as_posix(), cockpit_path),
        (integration_path.relative_to(ships_root).as_posix(),
         integration_path),
        ((ship_dir / "flight_profile.json").relative_to(
            ships_root).as_posix(), ship_dir / "flight_profile.json"),
    ]

    for entry in textures:
        target = f"Models/{entry.target}"
        files[target] = _read_bytes(entry.source, f"texture {entry.target}")
        sources.append((entry.source.relative_to(ships_root).as_posix(),
                        entry.source))

    for name, path in lods:
        target = f"Models/{asset_id}_{name}.ac"
        files[target] = _read_bytes(path, f"LOD {name}")
        sources.append((path.relative_to(ships_root).as_posix(), path))

    if collision is not None:
        suffix = collision.suffix or ".bin"
        target = f"Models/{asset_id}-collision{suffix}"
        files[target] = _read_bytes(collision, "collision geometry")
        sources.append((collision.relative_to(ships_root).as_posix(),
                        collision))

    sums = _render_sums(sources)
    if sums:
        files["Sources/SHA256SUMS"] = sums.encode("utf-8")
    files["Sources/BUILD_MANIFEST.json"] = _render_manifest(
        spec, asset_id, files, sources, flight_model, aero,
        mode).encode("utf-8")
    return files


# ---------------------------------------------------------------------------
# Installation / checking
# ---------------------------------------------------------------------------


def _old_manifest(package_dir: Path) -> dict | None:
    path = package_dir / "Sources" / "BUILD_MANIFEST.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != BUILD_SCHEMA:
        return None
    files = data.get("files")
    return files if isinstance(files, dict) else None


def _install_package(package: str, files: dict[str, bytes],
                     output_root: Path) -> tuple[int, int, list[str]]:
    package_dir = output_root / package
    written = 0
    unchanged = 0
    problems: list[str] = []

    for relative in sorted(files):
        destination = (package_dir / relative).resolve()
        try:
            destination.relative_to(package_dir.resolve())
        except ValueError:
            problems.append(f"{relative}: escapes package directory")
            continue
        data = files[relative]
        if destination.is_file() and destination.read_bytes() == data:
            unchanged += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        written += 1

    # Remove only files this builder previously generated and no longer owns.
    previous = _old_manifest(package_dir)
    if previous:
        for relative in sorted(previous):
            if relative in files or relative == "Sources/BUILD_MANIFEST.json":
                continue
            stale = (package_dir / relative).resolve()
            try:
                stale.relative_to(package_dir.resolve())
            except ValueError:
                continue
            if stale.is_file():
                stale.unlink()
    return written, unchanged, problems


def _check_package(package: str, files: dict[str, bytes],
                   output_root: Path) -> list[str]:
    package_dir = output_root / package
    problems: list[str] = []
    for relative in sorted(files):
        destination = package_dir / relative
        if not destination.is_file():
            problems.append(f"{package}: missing {relative}")
        elif destination.read_bytes() != files[relative]:
            problems.append(f"{package}: stale {relative}")
    return problems


def build_all(assets_root: Path, output_root: Path, flight_model: str,
              aero: str | None) -> dict[str, dict[str, bytes]]:
    ships_root = resolve_ships_root(assets_root)
    output_root = Path(output_root)
    if output_root.exists() and not output_root.is_dir():
        raise InputError(f"output root is not a directory: {output_root}")
    files_by_package: dict[str, dict[str, bytes]] = {}
    errors: list[str] = []
    for asset_id, spec in SHIPS.items():
        ship_dir = ships_root / asset_id
        if not ship_dir.is_dir():
            errors.append(f"{asset_id}: missing ship directory {ship_dir}")
            continue
        try:
            files_by_package[spec.package] = build_package(
                ship_dir, ships_root, spec, flight_model, aero, output_root)
        except BuildError as exc:
            errors.append(str(exc))
    if errors:
        raise BuildError("cannot assemble TIE packages:\n  " +
                         "\n  ".join(errors))
    return files_by_package


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_tie_packages.py",
        description="Build FlightGear visual TIE packages from prepared "
                    "stage-2 exports (stdlib only; no FlightGear claim).")
    parser.add_argument("--assets-root", required=True,
                        help="TIE asset pack ships directory")
    parser.add_argument("--output-root", required=True,
                        help="Aircraft output directory (e.g. Bombable/Aircraft)")
    parser.add_argument("--check", action="store_true",
                        help="compare generated content with disk; never write")
    parser.add_argument("--flight-model", choices=("null", "jsb"),
                        default="null",
                        help="default null; jsb requires --aero and an "
                             "externally supplied FDM file (not runtime-tested)")
    parser.add_argument("--aero", default=None,
                        help="basename of a supplied JSBSim .xml under "
                             "<package>/Models (requires --flight-model jsb)")
    args = parser.parse_args(argv)
    if args.aero and ("/" in args.aero or "\\" in args.aero
                      or args.aero in (".", "..")):
        parser.error("--aero must be a bare file name inside "
                     "<package>/Models")

    try:
        if args.flight_model != "null" and not args.aero:
            raise BuildError("--flight-model jsb requires --aero NAME")
        files_by_package = build_all(Path(args.assets_root),
                                     Path(args.output_root),
                                     args.flight_model, args.aero)
    except BuildError as exc:
        print(f"build_tie_packages: error: {exc}", file=sys.stderr)
        return 2

    output_root = Path(args.output_root)
    if args.check:
        problems: list[str] = []
        for package, files in files_by_package.items():
            problems.extend(_check_package(package, files, output_root))
        if problems:
            for problem in problems:
                print(f"STALE: {problem}")
            return 1
        print(f"OK: {len(files_by_package)} packages up to date under "
              f"{output_root}")
        return 0

    written = unchanged = 0
    problems: list[str] = []
    for package, files in files_by_package.items():
        count_written, count_unchanged, package_problems = _install_package(
            package, files, output_root)
        written += count_written
        unchanged += count_unchanged
        problems.extend(package_problems)
    if problems:
        for problem in problems:
            print(f"build_tie_packages: error: {problem}", file=sys.stderr)
        return 2
    print(f"built {len(files_by_package)} packages under {output_root} "
          f"({written} written, {unchanged} unchanged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
