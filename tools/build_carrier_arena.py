#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build a local carrier overlay on top of the committed space_arena.

The committed ``scenery/space_arena`` package stays the canonical Rebels
greybox and is never modified by this tool.  This builder reads that arena
read-only, copies it into a *new* output root, removes ONLY the Empire
greybox placements whose models are ``emp_carrier``, ``emp_deck``,
``emp_frame`` and ``emp_rack`` (exact paths come from the source manifest),
and adds a prepared external carrier pack to ``Models/venator`` in the same
STG tile.  Rebels placements, models and poses are preserved.

The external pack is not redistributed inside this repository.  It is
supplied on the command line and copied only into the requested output tree;
its licence status is required to be ``unverified``.  No third-party mesh is
written into the repository by this tool.

Contract highlights
-------------------
* ``--arena-root`` defaults to ``<repo>/scenery/space_arena``.
* ``--carrier-root`` is required and must contain:
  ``carrier.xml``, ``carrier_lod0.ac``, ``carrier_lod1.ac``,
  ``carrier_lod2.ac``, ``layout.json`` and optionally ``textures/``.
  Separate geometry (the default) also requires ``hangar.ac``. Unified
  geometry embeds the interior in each LOD and forbids a second hangar mesh.
  In both modes ``carrier.xml`` must load every required AC exactly once,
  each as its own direct ``<model><path>`` child; bare root-level path
  lists, several paths in one model, path references to textures, nested
  models and duplicate references are rejected.  Declared range animations
  must target a real named object group from one of the loaded AC files.
* ``--output-root`` is required.  It must not exist or must be an empty
  directory, it must not overlap the arena or the carrier pack, and it must
  be outside this repository (external assets are never written into the
  repo).
* The arena root and the carrier pack root must not overlap in either
  direction; otherwise the arena copy would pull in the pack's private
  files.
* ``--agl-m`` defaults to 450.0.  It is an explicit DESIGN AGL offset above
  the terrain at the Empire anchor, never a measured MSL elevation.  The
  Empire anchor must itself belong to the source STG tile, not merely every
  placement.
* Non-carrier STG lines are copied verbatim.  Their keyword and offset datum
  (``OBJECT_STATIC_AGL`` = above ground, ``OBJECT_STATIC`` = above sea
  level) and numeric value are recorded per placement; no 0.0 AGL value is
  invented, and a uniform summary is only reported when all active entries
  agree.
* All inputs (manifest, STG and XML references, AC geometry, layout vectors,
  triangle order, spawn ids, bounds, symlinks and output paths) are validated
  before anything is written.  The tree is assembled in a sibling temporary
  directory and renamed into place, so a failed build leaves no partial
  output.

The layout contract is ``bombable.carrier-layout/v1`` with local axes
``X right, Y forward, Z up``.  Local layout coordinates map to AC3Db stored
coordinates as ``AC = (-Y, Z, -X)`` (AC3Db: +x aft, +y up, +z left).  Spawn
slots are local metadata only: this tool does not provide a public spawn or
launch command, and it does not claim collision or network integration.
"""

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARENA_ROOT = REPO_ROOT / "scenery" / "space_arena"

ARENA_SCHEMA = "bombable.space_arena.manifest"
OVERLAY_SCHEMA = "bombable.space_arena.overlay.manifest"
LAYOUT_SCHEMA = "bombable.carrier-layout/v1"
LAYOUT_AXES = "X right, Y forward, Z up"
UNVERIFIED_LICENSE_STATUSES = frozenset((
    "unverified",
    "source_urls_recorded_terms_pending",
))

RETIRED_MODELS = ("emp_carrier", "emp_deck", "emp_frame", "emp_rack")
REQUIRED_CARRIER_FILES = (
    "carrier.xml",
    "carrier_lod0.ac",
    "carrier_lod1.ac",
    "carrier_lod2.ac",
    "hangar.ac",
    "layout.json",
)
CARRIER_AC_FILES = (
    "carrier_lod0.ac",
    "carrier_lod1.ac",
    "carrier_lod2.ac",
    "hangar.ac",
)
CARRIER_XML_REF_FILES = frozenset(CARRIER_AC_FILES)
DEFAULT_AGL_M = 450.0
XML_REFERENCE_TAGS = frozenset(("path", "texture", "texture-path"))
STG_ENTRY_KINDS = frozenset(("OBJECT_STATIC", "OBJECT_STATIC_AGL"))
STG_OFFSET_DATUM = {
    "OBJECT_STATIC_AGL": "agl_offset_m",
    "OBJECT_STATIC": "elevation_msl_m",
}
CARRIER_STG_REL = PurePosixPath("Models", "venator", "carrier.xml")


class BuildError(Exception):
    """A validated input or output constraint was violated."""


def _fail(message):
    raise BuildError(message)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("%s: expected a finite number" % what)
    number = float(value)
    if not math.isfinite(number):
        _fail("%s: expected a finite number, got %r" % (what, value))
    return number


def _positive_int(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("%s: expected a positive integer" % what)
    if value <= 0:
        _fail("%s: expected a positive integer, got %d" % (what, value))
    return value


def _safe_relative(value, what):
    """Validate a relative POSIX path used inside the package."""
    if not isinstance(value, str) or not value.strip():
        _fail("%s: expected a non-empty relative path" % what)
    value = value.strip()
    if "\x00" in value:
        _fail("%s: NUL byte in path" % what)
    if "\\" in value:
        _fail("%s: backslashes are not portable: %r" % (what, value))
    if ":" in value:
        _fail("%s: drive letters, URLs or colon paths are not allowed: %r"
              % (what, value))
    if value.startswith("/") or value.startswith("~"):
        _fail("%s: absolute or home path is not allowed: %r" % (what, value))
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        _fail("%s: absolute path is not allowed: %r" % (what, value))
    if any(part == ".." for part in path.parts):
        _fail("%s: parent traversal is not allowed: %r" % (what, value))
    return path


def _assert_no_symlink(root, relative):
    cursor = Path(root)
    for part in relative.parts:
        cursor = cursor / part
        if os.path.islink(cursor):
            _fail("symlink is not allowed: %s" % cursor)


def _resolve_regular(root, relative, what):
    root = Path(root)
    _assert_no_symlink(root, relative)
    path = root.joinpath(*relative.parts)
    try:
        info = path.stat()
    except OSError as exc:
        _fail("%s: cannot stat %s (%s)" % (what, path, exc))
    if not stat.S_ISREG(info.st_mode):
        _fail("%s: not a regular file: %s" % (what, path))
    if not path.resolve().is_relative_to(root.resolve()):
        _fail("%s: path escapes its root: %s" % (what, path))
    return path


def _resolve_regular_under(root, base, relative, what):
    root = Path(root)
    base = Path(base)
    try:
        base_relative = base.relative_to(root)
    except ValueError:
        _fail("%s: base path is outside its root: %s" % (what, base))
    _assert_no_symlink(root, PurePosixPath(base_relative.as_posix()))
    _assert_no_symlink(base, relative)
    path = base.joinpath(*relative.parts)
    try:
        info = path.stat()
    except OSError as exc:
        _fail("%s: cannot stat %s (%s)" % (what, path, exc))
    if not stat.S_ISREG(info.st_mode):
        _fail("%s: not a regular file: %s" % (what, path))
    if not path.resolve().is_relative_to(root.resolve()):
        _fail("%s: path escapes its root: %s" % (what, path))
    return path


def _walk_regular_files(root, what):
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in list(dirnames):
            path = Path(dirpath) / name
            if os.path.islink(path):
                _fail("%s: directory symlink is not allowed: %s"
                      % (what, path))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if os.path.islink(path):
                _fail("%s: file symlink is not allowed: %s" % (what, path))
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                _fail("%s: non-regular file is not allowed: %s"
                      % (what, path))
            yield path.relative_to(root), path


def _load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        _fail("%s: cannot parse %s (%s)" % (what, path, exc))


def _check_plain_layout_object(value, what):
    if not isinstance(value, dict):
        _fail("%s: expected a JSON object" % what)


def _reject_nonfinite_json(value, what, location="root"):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            _fail("%s: non-finite number at %s" % (what, location))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_json(item, what, "%s.%s" % (location, key))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nonfinite_json(item, what, "%s[%d]" % (location, index))


def _ac_text_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        _fail("AC model %s: cannot read as UTF-8 (%s)" % (path, exc))


def parse_ac(path):
    """Validate an AC3Db ASCII model and return its derived counts."""
    lines = _ac_text_lines(Path(path))
    stripped_lines = [line.strip() for line in lines]
    first = next((line for line in stripped_lines if line), None)
    if first != "AC3Db":
        _fail("AC model %s: expected AC3Db header" % path)

    triangles = 0
    surfaces = 0
    vertices = 0
    object_vertices = None
    textures = []
    object_names = []
    index = 0
    while index < len(lines):
        stripped = stripped_lines[index]
        if stripped.startswith("name "):
            try:
                fields = shlex.split(stripped, posix=True)
            except ValueError:
                _fail("AC model %s:%d: malformed object name"
                      % (path, index + 1))
            if len(fields) != 2 or not fields[1].strip():
                _fail("AC model %s:%d: malformed object name"
                      % (path, index + 1))
            object_names.append(fields[1].strip())
            index += 1
            continue

        if stripped.startswith("numvert "):
            fields = stripped.split()
            if len(fields) != 2:
                _fail("AC model %s:%d: malformed numvert" % (path, index + 1))
            try:
                count = int(fields[1])
            except ValueError:
                _fail("AC model %s:%d: malformed numvert" % (path, index + 1))
            if count <= 0:
                _fail("AC model %s:%d: numvert must be positive"
                      % (path, index + 1))
            if index + count >= len(lines):
                _fail("AC model %s:%d: truncated vertex block"
                      % (path, index + 1))
            for offset in range(1, count + 1):
                values = lines[index + offset].split()
                if len(values) != 3:
                    _fail("AC model %s:%d: expected three vertex values"
                          % (path, index + offset + 1))
                for value in values:
                    try:
                        number = float(value)
                    except ValueError:
                        _fail("AC model %s:%d: non-numeric vertex"
                              % (path, index + offset + 1))
                    if not math.isfinite(number):
                        _fail("AC model %s:%d: non-finite vertex"
                              % (path, index + offset + 1))
            object_vertices = count
            vertices += count
            index += count + 1
            continue

        if stripped.startswith("numsurf "):
            fields = stripped.split()
            if len(fields) != 2:
                _fail("AC model %s:%d: malformed numsurf" % (path, index + 1))
            try:
                declared = int(fields[1])
            except ValueError:
                _fail("AC model %s:%d: malformed numsurf" % (path, index + 1))
            if declared < 0:
                _fail("AC model %s:%d: negative numsurf" % (path, index + 1))
            if object_vertices is None:
                _fail("AC model %s:%d: numsurf before numvert"
                      % (path, index + 1))
            index += 1
            for _surface in range(declared):
                if index >= len(lines) or not stripped_lines[index].startswith(
                        "SURF "):
                    _fail("AC model %s:%d: expected SURF" % (path, index + 1))
                index += 1
                while (index < len(lines)
                       and not stripped_lines[index].startswith("refs ")):
                    if stripped_lines[index].startswith("SURF "):
                        _fail("AC model %s:%d: surface without refs"
                              % (path, index + 1))
                    index += 1
                if index >= len(lines):
                    _fail("AC model %s:%d: truncated surface" % (path, index + 1))
                fields = stripped_lines[index].split()
                if len(fields) != 2:
                    _fail("AC model %s:%d: malformed refs" % (path, index + 1))
                try:
                    refs = int(fields[1])
                except ValueError:
                    _fail("AC model %s:%d: malformed refs" % (path, index + 1))
                if refs < 3:
                    _fail("AC model %s:%d: surface has fewer than 3 vertices"
                          % (path, index + 1))
                triangles += refs - 2
                index += 1
                for _ref in range(refs):
                    if index >= len(lines):
                        _fail("AC model %s:%d: truncated refs block"
                              % (path, index + 1))
                    try:
                        vertex_index = int(lines[index].split()[0])
                    except (IndexError, ValueError):
                        _fail("AC model %s:%d: malformed vertex reference"
                              % (path, index + 1))
                    if vertex_index < 0 or vertex_index >= object_vertices:
                        _fail("AC model %s:%d: vertex reference out of range"
                              % (path, index + 1))
                    index += 1
                surfaces += 1
            continue

        if stripped.startswith("texture "):
            try:
                fields = shlex.split(stripped, posix=True)
            except ValueError:
                _fail("AC model %s:%d: malformed texture line"
                      % (path, index + 1))
            if len(fields) >= 2:
                texture = fields[1]
                if texture not in ("-1", "0", "none", "None", ""):
                    textures.append(texture)
            index += 1
            continue

        index += 1

    if surfaces <= 0 or triangles <= 0:
        _fail("AC model %s: no polygon geometry" % path)
    return {
        "triangles": triangles,
        "surfaces": surfaces,
        "vertices": vertices,
        "textures": textures,
        "object_names": object_names,
    }


def _validate_ac_textures(root, ac_path, geometry, whitelist=None):
    root = Path(root)
    for texture in geometry["textures"]:
        relative = _safe_relative(
            texture, "texture reference in %s" % ac_path)
        candidates = [(ac_path.parent, relative)]
        if len(relative.parts) == 1:
            candidates.append((root / "textures", relative))
        resolved = None
        for base, candidate_relative in candidates:
            candidate = base.joinpath(*candidate_relative.parts)
            if not candidate.exists() and not candidate.is_symlink():
                continue
            resolved = _resolve_regular_under(
                root, base, candidate_relative,
                "texture reference %r in %s" % (texture, ac_path))
            break
        if resolved is None:
            _fail("texture reference %r in %s does not resolve"
                  % (texture, ac_path))
        if whitelist is not None:
            resolved_relative = resolved.relative_to(root).as_posix()
            if resolved_relative not in whitelist:
                _fail("texture %r in %s is outside the copied whitelist"
                      % (texture, ac_path))


def _scan_xml(containment_root, xml_path, allowed=None, require_path=True):
    """Parse an FG PropertyList wrapper and validate every path reference."""
    containment_root = Path(containment_root)
    xml_path = Path(xml_path)
    try:
        tree = ET.parse(xml_path)
    except (OSError, ET.ParseError) as exc:
        _fail("XML %s: cannot parse (%s)" % (xml_path, exc))
    root = tree.getroot()
    if root.tag != "PropertyList":
        _fail("XML %s: expected a PropertyList root" % xml_path)

    references = []
    paths = 0
    for element in root.iter():
        if element.tag not in XML_REFERENCE_TAGS:
            continue
        text = (element.text or "").strip()
        if not text:
            _fail("XML %s: empty <%s> reference"
                  % (xml_path, element.tag))
        relative = _safe_relative(
            text, "XML %s <%s>" % (xml_path, element.tag))
        if allowed is not None and not allowed(relative):
            _fail("XML %s: reference %r is outside the carrier whitelist"
                  % (xml_path, text))
        resolved = _resolve_regular_under(
            containment_root, xml_path.parent, relative,
            "XML %s reference %r" % (xml_path, text))
        if element.tag == "path":
            paths += 1
        references.append({
            "tag": element.tag,
            "text": text,
            "relative": relative.as_posix(),
            "resolved": resolved,
        })
    if require_path and paths == 0:
        _fail("XML %s: no <path> model reference" % xml_path)
    return references


def _validate_carrier_model_structure(xml_path, expected_models):
    """Require one top-level <model><path> per expected AC, exactly once.

    This rejects the load patterns that produce an empty or double-loaded
    carrier: several <path> children inside one <model>, a bare root-level
    <path> list, a <path> that points at a texture, nested <model> wrappers
    and duplicate AC references.  It says nothing about runtime behaviour.
    """
    xml_path = Path(xml_path)
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as exc:
        _fail("XML %s: cannot parse (%s)" % (xml_path, exc))
    if root.tag != "PropertyList":
        _fail("XML %s: expected a PropertyList root" % xml_path)

    model_elements = []
    model_paths = []

    def visit(element, parent, depth):
        if element.tag == "model":
            if depth != 1:
                _fail("XML %s: <model> must be a direct child of "
                      "<PropertyList>" % xml_path)
            model_elements.append(element)
        elif element.tag == "path":
            text = (element.text or "").strip()
            if parent is None or parent.tag != "model" or depth != 2:
                _fail("XML %s: <path> must be the direct child of a "
                      "top-level <model>" % xml_path)
            model_paths.append(text)
        for child in element:
            visit(child, element, depth + 1)

    visit(root, None, 0)

    for model in model_elements:
        children = [child for child in model if child.tag == "path"]
        if len(children) != 1:
            _fail("XML %s: every <model> needs exactly one <path> child, "
                  "found %d" % (xml_path, len(children)))
        name = (model.findtext("name") or "").strip()
        if not name:
            _fail("XML %s: every <model> needs a non-empty <name>"
                  % xml_path)

    stored = []
    for text in model_paths:
        relative = _safe_relative(
            text, "XML %s model path" % xml_path)
        if relative.suffix != ".ac":
            _fail("XML %s: model <path> must reference an .ac file, got %r"
                  % (xml_path, text))
        if len(relative.parts) != 1:
            _fail("XML %s: model <path> must be a sibling .ac file, got %r"
                  % (xml_path, text))
        if relative.as_posix() not in expected_models:
            _fail("XML %s: unexpected model %r; expected exactly %s"
                  % (xml_path, text, ", ".join(sorted(expected_models))))
        stored.append(relative.as_posix())

    expected = sorted(expected_models)
    if sorted(stored) != expected or len(set(stored)) != len(stored):
        _fail("XML %s: model references must match %s exactly once each, "
              "got %r" % (xml_path, expected, stored))


def _validate_carrier_ranges(xml_path, object_names_by_model):
    """Validate declared range animations against real AC group names.

    A declared range must name at least one real object group from one of
    the loaded AC files and must carry numeric bounds when present.  This is
    a static reference check; visibility/culling remains unverified.
    """
    xml_path = Path(xml_path)
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as exc:
        _fail("XML %s: cannot parse (%s)" % (xml_path, exc))

    known = set()
    for names in object_names_by_model.values():
        known.update(names)

    seen_targets = set()
    for index, animation in enumerate(root.iter("animation")):
        types = {(node.text or "").strip()
                 for node in animation.findall("type")}
        if "range" not in types:
            continue
        targets = [(node.text or "").strip()
                   for node in animation.findall("object-name")]
        targets = [target for target in targets if target]
        if not targets:
            _fail("XML %s: range animation %d has no <object-name> target"
                  % (xml_path, index))
        for target in targets:
            if target not in known:
                _fail("XML %s: range animation target %r is not an object "
                      "group name in the loaded AC files (%s)"
                      % (xml_path, target, ", ".join(sorted(known))))
            if target in seen_targets:
                _fail("XML %s: range animation target %r is declared twice"
                      % (xml_path, target))
            seen_targets.add(target)
        bounds = {}
        for tag in ("min-m", "max-m"):
            for node in animation.findall(tag):
                text = (node.text or "").strip()
                try:
                    value = float(text)
                except ValueError:
                    _fail("XML %s: range animation has a non-numeric <%s>"
                          % (xml_path, tag))
                bounds[tag] = _number(
                    value, "XML %s range %s" % (xml_path, tag))
        if ("min-m" in bounds and "max-m" in bounds
                and bounds["min-m"] >= bounds["max-m"]):
            _fail("XML %s: range animation min-m must be < max-m"
                  % xml_path)


def _bucket_and_tile(lon, lat):
    top_lon = math.floor(lon / 10.0) * 10
    top_lat = math.floor(lat / 10.0) * 10
    base_lon = math.floor(lon)
    base_lat = math.floor(lat)
    bucket = "%s%03d%s%02d" % (
        "e" if top_lon >= 0 else "w", abs(int(top_lon)),
        "n" if top_lat >= 0 else "s", abs(int(top_lat)))
    tile = "%s%03d%s%02d" % (
        "e" if base_lon >= 0 else "w", abs(int(base_lon)),
        "n" if base_lat >= 0 else "s", abs(int(base_lat)))
    return bucket, tile


def _validate_stg_cell(stg_relative, lon, lat):
    parts = stg_relative.parts
    if (len(parts) != 4 or parts[0] != "Objects"
            or PurePosixPath(parts[3]).suffix != ".stg"):
        _fail("source STG path is not Objects/<bucket>/<tile>/<id>.stg: %s"
              % stg_relative)
    expected_bucket, expected_tile = _bucket_and_tile(lon, lat)
    if parts[1] != expected_bucket or parts[2] != expected_tile:
        _fail("coordinate %r,%r does not belong to STG cell %s/%s "
              "(expected %s/%s)"
              % (lon, lat, parts[1], parts[2],
                 expected_bucket, expected_tile))


def _stg_record(entry):
    """Copy the factual fields of one parsed STG line."""
    return {
        "line_number": entry["line_number"],
        "kind": entry["kind"],
        "lon": entry["lon"],
        "lat": entry["lat"],
        "offset_m": entry["offset_m"],
        "offset_datum": STG_OFFSET_DATUM[entry["kind"]],
        "heading_deg": entry["heading_deg"],
    }


def _stg_offset_semantics(record):
    if record["kind"] == "OBJECT_STATIC_AGL":
        return (
            "Source STG entry is OBJECT_STATIC_AGL: the offset field %.6f m "
            "is a height above ground level at the anchor. This overlay "
            "copies the source STG line unchanged; runtime semantics were "
            "not verified." % record["offset_m"])
    return (
        "Source STG entry is OBJECT_STATIC: the offset field %.6f m is an "
        "elevation datum (meters above mean sea level by STG convention), "
        "not an AGL offset. This overlay copies the source STG line "
        "unchanged and makes no AGL claim; runtime semantics were not "
        "verified." % record["offset_m"])


def _summarize_active_offsets(placements):
    records = []
    per_placement = {}
    for placement in placements:
        entries = placement.get("source_stg_entries") or []
        if len(entries) != 1:
            continue
        record = entries[0]
        per_placement[placement["id"]] = {
            "kind": record["kind"],
            "offset_m": record["offset_m"],
            "datum": record["offset_datum"],
        }
        records.append(record)
    kinds = sorted({record["kind"] for record in records})
    agl_values = {record["offset_m"] for record in records
                  if record["kind"] == "OBJECT_STATIC_AGL"}
    msl_values = {record["offset_m"] for record in records
                  if record["kind"] == "OBJECT_STATIC"}
    agl = next(iter(agl_values)) if (kinds == ["OBJECT_STATIC_AGL"]
                                     and len(agl_values) == 1) else None
    msl = next(iter(msl_values)) if (kinds == ["OBJECT_STATIC"]
                                     and len(msl_values) == 1) else None
    if agl is not None:
        summary = "uniform_agl"
    elif msl is not None:
        summary = "uniform_msl"
    else:
        summary = "mixed_or_empty"
    return {
        "agl_offset_m": agl,
        "msl_elevation_m": msl,
        "kinds": kinds,
        "summary": summary,
        "per_placement": per_placement,
    }


def validate_layout(layout):
    _check_plain_layout_object(layout, "layout.json")
    _reject_nonfinite_json(layout, "layout.json")
    if layout.get("schema") != LAYOUT_SCHEMA:
        _fail("layout.json: schema must be %r" % LAYOUT_SCHEMA)
    if layout.get("axes") != LAYOUT_AXES:
        _fail("layout.json: axes must be %r" % LAYOUT_AXES)

    bounds = layout.get("bounds_m")
    if not isinstance(bounds, list) or len(bounds) != 3:
        _fail("layout.json: bounds_m must contain three [min,max] axes")
    normalized_bounds = []
    for axis, pair in enumerate(bounds):
        if not isinstance(pair, list) or len(pair) != 2:
            _fail("layout.json: bounds_m[%d] must be [min,max]" % axis)
        low = _number(pair[0], "layout.json bounds_m[%d][0]" % axis)
        high = _number(pair[1], "layout.json bounds_m[%d][1]" % axis)
        if not low < high:
            _fail("layout.json: bounds_m[%d] min must be < max" % axis)
        normalized_bounds.append([low, high])

    geometry_mode = layout.get("geometry_mode", "separate")
    if geometry_mode not in ("separate", "unified"):
        _fail("layout.json: geometry_mode must be 'separate' or 'unified'")
    triangles = layout.get("triangles")
    _check_plain_layout_object(triangles, "layout.json triangles")
    declared = {}
    for key in ("lod0", "lod1", "lod2"):
        declared[key] = _positive_int(
            triangles.get(key), "layout.json triangles.%s" % key)
    if geometry_mode == "unified":
        value = triangles.get("hangar")
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            _fail("layout.json: unified geometry requires triangles.hangar = 0")
        declared["hangar"] = 0
    else:
        declared["hangar"] = _positive_int(
            triangles.get("hangar"), "layout.json triangles.hangar")
    if not declared["lod0"] > declared["lod1"] > declared["lod2"]:
        _fail("layout.json: triangles must satisfy lod0 > lod1 > lod2")

    if layout.get("runtime_verified") is not False:
        _fail("layout.json: runtime_verified must be false")

    provenance = layout.get("provenance")
    if not isinstance(provenance, dict):
        _fail("layout.json: provenance object is required")
    license_status = provenance.get("license_status")
    if license_status not in UNVERIFIED_LICENSE_STATUSES:
        _fail("layout.json: provenance.license_status must be 'unverified' "
              "(or 'source_urls_recorded_terms_pending')")

    slots = layout.get("spawn_slots")
    if not isinstance(slots, list) or not slots:
        _fail("layout.json: spawn_slots must be a non-empty list")
    seen_ids = set()
    normalized_slots = []
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            _fail("layout.json: spawn_slots[%d] must be an object" % index)
        slot_id = slot.get("id")
        if not isinstance(slot_id, str) or not slot_id.strip():
            _fail("layout.json: spawn_slots[%d].id must be a non-empty string"
                  % index)
        if slot_id in seen_ids:
            _fail("layout.json: duplicate spawn slot id %r" % slot_id)
        seen_ids.add(slot_id)
        position = slot.get("position_m")
        if not isinstance(position, list) or len(position) != 3:
            _fail("layout.json: spawn slot %r position_m must have 3 values"
                  % slot_id)
        xyz = [_number(value, "layout.json slot %r position_m[%d]"
                        % (slot_id, axis))
               for axis, value in enumerate(position)]
        for axis in range(3):
            low, high = normalized_bounds[axis]
            if not low <= xyz[axis] <= high:
                _fail("layout.json: spawn slot %r position_m[%d]=%r is "
                      "outside bounds [%r, %r]"
                      % (slot_id, axis, xyz[axis], low, high))
        heading = slot.get("heading_deg", 0.0)
        heading = _number(heading, "layout.json slot %r heading_deg"
                          % slot_id)
        ship_ids = slot.get("ship_ids")
        if not isinstance(ship_ids, list) or not ship_ids:
            _fail("layout.json: spawn slot %r ship_ids must be a non-empty "
                  "list" % slot_id)
        seen_ships = set()
        for ship_id in ship_ids:
            if not isinstance(ship_id, str) or not ship_id.strip():
                _fail("layout.json: spawn slot %r has an empty ship id"
                      % slot_id)
            if ship_id in seen_ships:
                _fail("layout.json: spawn slot %r has duplicate ship id %r"
                      % (slot_id, ship_id))
            seen_ships.add(ship_id)
        normalized = copy.deepcopy(slot)
        normalized["position_m"] = xyz
        normalized["heading_deg"] = heading
        # AC3Db stored axes: ac_x = -layout_y, ac_y = layout_z,
        # ac_z = -layout_x.  This is a local model transform only.
        normalized["position_ac_m"] = [-xyz[1], xyz[2], -xyz[0]]
        normalized_slots.append(normalized)

    return {
        "geometry_mode": geometry_mode,
        "bounds_m": normalized_bounds,
        "triangles": declared,
        "spawn_slots": normalized_slots,
        "provenance": copy.deepcopy(provenance),
        "license_status": license_status,
    }


def _load_arena(root):
    root = Path(root)
    if root.is_symlink():
        _fail("arena root is a symlink: %s" % root)
    if not root.is_dir():
        _fail("arena root is not a directory: %s" % root)

    arena_files = {}
    for relative, path in _walk_regular_files(root, "arena"):
        arena_files[relative.as_posix()] = path

    manifest_path = root / "manifest.json"
    if "manifest.json" not in arena_files:
        _fail("arena manifest.json is missing: %s" % manifest_path)
    manifest = _load_json(manifest_path, "arena manifest")
    _check_plain_layout_object(manifest, "arena manifest.json")
    _reject_nonfinite_json(manifest, "arena manifest.json")
    if manifest.get("schema") != ARENA_SCHEMA:
        _fail("arena manifest.json: schema must be %r" % ARENA_SCHEMA)
    if manifest.get("schema_version") != 1:
        _fail("arena manifest.json: schema_version must be 1")

    models = manifest.get("models")
    if not isinstance(models, dict):
        _fail("arena manifest.json: models object is required")
    placements = manifest.get("placements")
    if not isinstance(placements, list):
        _fail("arena manifest.json: placements list is required")
    poses = manifest.get("poses")
    if not isinstance(poses, list):
        _fail("arena manifest.json: poses list is required")

    region = manifest.get("region")
    if not isinstance(region, dict):
        _fail("arena manifest.json: region object is required")
    anchors = region.get("anchors")
    if not isinstance(anchors, dict):
        _fail("arena manifest.json: region.anchors object is required")
    normalized_anchors = {}
    for faction in ("rebels", "empire"):
        anchor = anchors.get(faction)
        if not isinstance(anchor, dict):
            _fail("arena manifest.json: missing %s anchor" % faction)
        lon = _number(anchor.get("lon"),
                      "arena manifest.json region.anchors.%s.lon" % faction)
        lat = _number(anchor.get("lat"),
                      "arena manifest.json region.anchors.%s.lat" % faction)
        if not -180.0 <= lon < 180.0:
            _fail("arena manifest.json: %s anchor longitude out of range"
                  % faction)
        if not -90.0 <= lat <= 90.0:
            _fail("arena manifest.json: %s anchor latitude out of range"
                  % faction)
        normalized_anchors[faction] = {"lon": lon, "lat": lat}

    stg = manifest.get("stg")
    if not isinstance(stg, dict):
        _fail("arena manifest.json: stg object is required")
    stg_relative = _safe_relative(stg.get("path"),
                                  "arena manifest.json stg.path")
    if stg_relative.suffix != ".stg":
        _fail("arena manifest.json: stg.path must end in .stg")
    stg_path = _resolve_regular(root, stg_relative, "arena STG")
    # The carrier is written at the Empire anchor, so that anchor itself must
    # belong to the STG cell; checking only placements would miss a displaced
    # anchor that lands in a different tile.
    _validate_stg_cell(stg_relative, normalized_anchors["empire"]["lon"],
                       normalized_anchors["empire"]["lat"])

    model_cache = {}

    def load_model(name):
        if name in model_cache:
            return model_cache[name]
        metadata = models.get(name)
        if not isinstance(metadata, dict):
            _fail("arena manifest.json: missing model metadata for %r" % name)
        xml_relative = _safe_relative(
            metadata.get("xml"), "arena model %r xml" % name)
        ac_relative = _safe_relative(
            metadata.get("ac"), "arena model %r ac" % name)
        if xml_relative.suffix != ".xml":
            _fail("arena model %r xml must end in .xml" % name)
        if ac_relative.suffix != ".ac":
            _fail("arena model %r ac must end in .ac" % name)
        xml_path = _resolve_regular(root, xml_relative,
                                    "arena model %r XML" % name)
        ac_path = _resolve_regular(root, ac_relative,
                                   "arena model %r AC" % name)
        references = _scan_xml(root, xml_path, require_path=True)
        resolved_references = {item["resolved"] for item in references}
        if ac_path.resolve() not in resolved_references:
            _fail("arena model %r wrapper does not reference %s"
                  % (name, ac_relative))
        geometry = parse_ac(ac_path)
        _validate_ac_textures(root, ac_path, geometry)
        info = {
            "name": name,
            "metadata": copy.deepcopy(metadata),
            "xml_relative": xml_relative,
            "ac_relative": ac_relative,
            "xml_path": xml_path,
            "ac_path": ac_path,
            "triangles": geometry["triangles"],
        }
        model_cache[name] = info
        return info

    retired_models = {}
    for name in RETIRED_MODELS:
        info = load_model(name)
        retired_models[name] = info

    retired_resolved = set()
    for info in retired_models.values():
        retired_resolved.add(info["xml_path"].resolve())
        retired_resolved.add(info["ac_path"].resolve())

    seen_placement_ids = set()
    active_placements = []
    retired_placements = []
    active_model_names = set()
    for index, placement in enumerate(placements):
        if not isinstance(placement, dict):
            _fail("arena manifest.json: placements[%d] must be an object"
                  % index)
        placement_id = placement.get("id")
        if not isinstance(placement_id, str) or not placement_id.strip():
            _fail("arena manifest.json: placements[%d].id is invalid" % index)
        if placement_id in seen_placement_ids:
            _fail("arena manifest.json: duplicate placement id %r"
                  % placement_id)
        seen_placement_ids.add(placement_id)
        faction = placement.get("faction")
        if faction not in normalized_anchors:
            _fail("arena placement %r has unknown faction %r"
                  % (placement_id, faction))
        model_name = placement.get("model")
        if not isinstance(model_name, str) or model_name not in models:
            _fail("arena placement %r has unknown model %r"
                  % (placement_id, model_name))
        lon = _number(placement.get("lon"),
                      "arena placement %r lon" % placement_id)
        lat = _number(placement.get("lat"),
                      "arena placement %r lat" % placement_id)
        _validate_stg_cell(stg_relative, lon, lat)
        if model_name in RETIRED_MODELS:
            if faction != "empire":
                _fail("retired Empire model placement %r has faction %r"
                      % (placement_id, faction))
            retired_placements.append(copy.deepcopy(placement))
        else:
            active_placements.append(copy.deepcopy(placement))
            active_model_names.add(model_name)
        load_model(model_name)

    if not retired_placements:
        _fail("arena contains none of the retired Empire placements: %s"
              % ", ".join(RETIRED_MODELS))

    carrier_placement = next(
        (placement for placement in placements
         if placement.get("id") == "emp_carrier"
         and placement.get("model") == "emp_carrier"), None)
    if carrier_placement is None:
        carrier_placement = next(
            (placement for placement in placements
             if placement.get("model") == "emp_carrier"), None)
    if carrier_placement is None:
        _fail("arena manifest.json: emp_carrier placement is required for "
              "the source heading")

    heading_stg = carrier_placement.get("stg_heading_deg")
    if heading_stg is None:
        heading_cw = _number(carrier_placement.get("heading_deg"),
                             "arena emp_carrier heading_deg")
        heading_stg = (360.0 - (heading_cw % 360.0)) % 360.0
    heading_stg = _number(heading_stg,
                          "arena emp_carrier stg_heading_deg")
    if not 0.0 <= heading_stg < 360.0:
        _fail("arena emp_carrier stg_heading_deg must be in [0, 360)")

    # The source STG must use the expected direct XML/AC references and must
    # live in the same cell as every placement (independently recomputed).
    try:
        stg_text = stg_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _fail("arena STG cannot be read as UTF-8: %s (%s)" % (stg_path, exc))

    known_model_paths = set()
    for name in models:
        info = load_model(name)
        known_model_paths.add(info["xml_path"].resolve())
        known_model_paths.add(info["ac_path"].resolve())

    entries = []
    removed = []
    for line_number, line in enumerate(stg_text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            fields = shlex.split(stripped, posix=True)
        except ValueError as exc:
            _fail("arena STG %s:%d: cannot parse (%s)"
                  % (stg_path, line_number, exc))
        if len(fields) < 6:
            _fail("arena STG %s:%d: incomplete placement"
                  % (stg_path, line_number))
        if fields[0] not in STG_ENTRY_KINDS:
            _fail("arena STG %s:%d: unsupported keyword %r"
                  % (stg_path, line_number, fields[0]))
        relative = _safe_relative(
            fields[1], "arena STG %s:%d model path" % (stg_path, line_number))
        if relative.suffix not in (".xml", ".ac"):
            _fail("arena STG %s:%d: model path must end in .xml or .ac"
                  % (stg_path, line_number))
        resolved = _resolve_regular_under(
            root, stg_path.parent, relative,
            "arena STG %s:%d model" % (stg_path, line_number))
        if resolved.resolve() not in known_model_paths:
            _fail("arena STG %s:%d: model %r is not listed in the manifest"
                  % (stg_path, line_number, fields[1]))
        numbers = []
        for field_index, value in enumerate(fields[2:6], 2):
            try:
                numeric = float(value)
            except ValueError:
                _fail("arena STG %s:%d field %d is not numeric"
                      % (stg_path, line_number, field_index))
            numbers.append(_number(
                numeric, "arena STG %s:%d field %d"
                % (stg_path, line_number, field_index)))
        entry = {
            "line_number": line_number,
            "raw": line,
            "fields": fields,
            "resolved": resolved.resolve(),
            "kind": fields[0],
            "lon": numbers[0],
            "lat": numbers[1],
            "offset_m": numbers[2],
            "heading_deg": numbers[3],
        }
        entries.append(entry)
        if resolved.resolve() in retired_resolved:
            removed.append(entry)

    if not removed:
        _fail("arena STG has no placements for the retired Empire models; "
              "it may already be an overlay")
    removed_paths = {entry["resolved"] for entry in removed}
    for placement in retired_placements:
        model_info = model_cache[placement["model"]]
        if model_info["xml_path"].resolve() not in removed_paths:
            _fail("retired placement %r has no STG line" % placement["id"])

    carrier_source_paths = {
        retired_models["emp_carrier"]["xml_path"].resolve(),
        retired_models["emp_carrier"]["ac_path"].resolve(),
    }
    carrier_source_line = next(
        (entry for entry in removed
         if entry["resolved"] in carrier_source_paths), None)
    if carrier_source_line is not None:
        if abs(float(carrier_source_line["fields"][5]) - heading_stg) > 1e-6:
            _fail("arena emp_carrier placement heading does not match its STG "
                  "line")

    def _entries_for_placement(placement):
        model_info = model_cache[placement["model"]]
        resolved_model = {
            model_info["xml_path"].resolve(),
            model_info["ac_path"].resolve(),
        }
        candidates = [entry for entry in entries
                      if entry["resolved"] in resolved_model]
        lon = _number(placement.get("lon"),
                      "arena placement %r lon" % placement["id"])
        lat = _number(placement.get("lat"),
                      "arena placement %r lat" % placement["id"])
        return candidates, [
            entry for entry in candidates
            if abs(entry["lon"] - lon) <= 1e-6
            and abs(entry["lat"] - lat) <= 1e-6]

    enriched_active = []
    for placement in active_placements:
        _candidates, matching = _entries_for_placement(placement)
        if not matching:
            _fail("active placement %r has no STG line at its coordinates"
                  % placement["id"])
        if len(matching) > 1:
            heading = placement.get("stg_heading_deg")
            if heading is not None:
                heading = _number(
                    heading, "arena placement %r stg_heading_deg"
                    % placement["id"])
                by_heading = [
                    entry for entry in matching
                    if abs(entry["heading_deg"] - heading) <= 1e-6]
                if len(by_heading) == 1:
                    matching = by_heading
        if len(matching) != 1:
            _fail("active placement %r has %d STG lines at its coordinates; "
                  "exactly one is required"
                  % (placement["id"], len(matching)))
        item = copy.deepcopy(placement)
        item["source_stg_entries"] = [_stg_record(matching[0])]
        enriched_active.append(item)
    active_placements = enriched_active

    enriched_retired = []
    for placement in retired_placements:
        candidates, matching = _entries_for_placement(placement)
        chosen = matching or candidates
        item = copy.deepcopy(placement)
        item["source_stg_entries"] = [_stg_record(entry) for entry in chosen]
        enriched_retired.append(item)
    retired_placements = enriched_retired

    return {
        "root": root,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "manifest_sha256": _sha256(manifest_path),
        "models": model_cache,
        "retired_models": retired_models,
        "retired_resolved": retired_resolved,
        "active_placements": active_placements,
        "retired_placements": retired_placements,
        "active_model_names": active_model_names,
        "anchors": normalized_anchors,
        "stg_relative": stg_relative,
        "stg_path": stg_path,
        "stg_sha256": _sha256(stg_path),
        "stg_text": stg_text,
        "entries": entries,
        "removed_entries": removed,
        "heading_stg": heading_stg,
        "carrier_placement": copy.deepcopy(carrier_placement),
        "arena_files": arena_files,
    }


def _load_carrier_pack(root):
    root = Path(root)
    if root.is_symlink():
        _fail("carrier root is a symlink: %s" % root)
    if not root.is_dir():
        _fail("carrier root is not a directory: %s" % root)

    layout_path = _resolve_regular(root, PurePosixPath("layout.json"),
                                   "carrier layout.json")
    layout = _load_json(layout_path, "carrier layout.json")
    layout_info = validate_layout(layout)
    unified = layout_info["geometry_mode"] == "unified"
    required = tuple(name for name in REQUIRED_CARRIER_FILES
                     if not (unified and name == "hangar.ac"))
    if unified and os.path.lexists(root / "hangar.ac"):
        _fail("unified carrier must not contain a separate hangar.ac")
    pack_files = {}
    for name in required:
        relative = PurePosixPath(name)
        pack_files[name] = _resolve_regular(
            root, relative, "carrier pack file %s" % name)

    layout_path = pack_files["layout.json"]
    try:
        layout_bytes = layout_path.read_bytes()
    except OSError as exc:
        _fail("carrier layout.json: cannot read (%s)" % exc)

    texture_files = {}
    textures_root = root / "textures"
    if textures_root.is_symlink():
        _fail("carrier textures directory is a symlink: %s" % textures_root)
    if textures_root.exists():
        if not textures_root.is_dir():
            _fail("carrier textures is not a directory: %s" % textures_root)
        for relative, path in _walk_regular_files(
                textures_root, "carrier textures"):
            texture_files["textures/" + relative.as_posix()] = path

    whitelist = set(pack_files) | set(texture_files)

    def allowed_reference(relative):
        text = relative.as_posix()
        return (text in CARRIER_XML_REF_FILES and text in pack_files
                or text.startswith("textures/"))

    xml_references = _scan_xml(
        root, pack_files["carrier.xml"], allowed=allowed_reference,
        require_path=True)
    for reference in xml_references:
        stored = reference["resolved"].relative_to(root).as_posix()
        if stored not in whitelist:
            _fail("carrier XML reference %r is outside the copied whitelist"
                  % reference["text"])
    expected_models = {"carrier_lod%d.ac" % i for i in range(3)}
    if not unified:
        expected_models.add("hangar.ac")
    _validate_carrier_model_structure(pack_files["carrier.xml"],
                                      expected_models)

    parsed = {}
    for name in CARRIER_AC_FILES:
        if name not in pack_files:
            continue
        geometry = parse_ac(pack_files[name])
        _validate_ac_textures(root, pack_files[name], geometry,
                              whitelist=whitelist)
        parsed[name] = geometry

    _validate_carrier_ranges(
        pack_files["carrier.xml"],
        {name: geometry["object_names"] for name, geometry in parsed.items()})

    ignored_entries = sorted(
        entry.name for entry in root.iterdir()
        if entry.name not in set(required)
        and entry.name != "textures")

    declared = layout_info["triangles"]
    mismatches = {}
    for key in ("lod0", "lod1", "lod2", "hangar"):
        if unified and key == "hangar":
            continue
        parsed_count = parsed["%s.ac" % ("carrier_" + key
                                         if key != "hangar" else "hangar")
                               ]["triangles"]
        if parsed_count != declared[key]:
            mismatches[key] = {
                "declared": declared[key],
                "parsed": parsed_count,
            }
    if mismatches:
        _fail("carrier triangle counts disagree with layout.json: %r" % mismatches)

    return {
        "root": root,
        "files": pack_files,
        "texture_files": texture_files,
        "whitelist": whitelist,
        "layout": layout,
        "layout_info": layout_info,
        "layout_sha256": hashlib.sha256(layout_bytes).hexdigest(),
        "parsed": parsed,
        "declared_triangles": declared,
        "triangle_mismatches": mismatches,
        "xml_references": xml_references,
        "ignored_entries": ignored_entries,
    }


def _check_input_roots(arena_root, carrier_root):
    """Reject overlapping arena and carrier roots in both directions."""
    arena_resolved = Path(arena_root).resolve()
    carrier_resolved = Path(carrier_root).resolve()
    if (arena_resolved == carrier_resolved
            or arena_resolved.is_relative_to(carrier_resolved)
            or carrier_resolved.is_relative_to(arena_resolved)):
        _fail("arena root and carrier pack root must not overlap: %s, %s"
              % (arena_root, carrier_root))


def _check_output_root(output_root, arena_root, carrier_root):
    output_root = Path(output_root)
    if output_root.is_symlink():
        _fail("output root is a symlink: %s" % output_root)
    if output_root.exists():
        if not output_root.is_dir():
            _fail("output root exists and is not a directory: %s"
                  % output_root)
        if any(output_root.iterdir()):
            _fail("output root exists and is not empty: %s" % output_root)
    output_resolved = output_root.resolve()
    for label, other in (("arena", arena_root), ("carrier pack", carrier_root)):
        other_resolved = Path(other).resolve()
        if (output_resolved == other_resolved
                or output_resolved.is_relative_to(other_resolved)
                or other_resolved.is_relative_to(output_resolved)):
            _fail("output root overlaps the %s root: %s" % (label,
                                                            output_root))
    if output_resolved.is_relative_to(REPO_ROOT.resolve()):
        _fail("output root must be outside the repository: %s" % output_root)
    return output_root


def _copy_arena(arena, temporary):
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(arena["root"], temporary, dirs_exist_ok=True,
                    symlinks=False, ignore=ignored)
    for relative, source in arena["arena_files"].items():
        if (PurePosixPath(relative).suffix == ".pyc"
                or "__pycache__" in PurePosixPath(relative).parts):
            continue
        copied = Path(temporary).joinpath(*PurePosixPath(relative).parts)
        if not copied.is_file() or _sha256(copied) != _sha256(source):
            _fail("arena copy verification failed for %s" % relative)


def _stage_legacy_source(temporary, arena, fallback_manifest_bytes):
    temporary = Path(temporary)
    source_dir = temporary / "source"
    if source_dir.exists() or source_dir.is_symlink():
        _fail("arena already contains a source/ directory; refusing to "
              "overlay it")
    source_dir.mkdir()
    legacy_files = {}
    for name in ("manifest.json", "README.md", "VALIDATION.md"):
        path = temporary / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            _fail("arena top-level %s is not a regular file" % name)
        path.rename(source_dir / name)
        legacy_files[name] = "source/" + name
    source_tree = temporary / "src"
    if source_tree.exists():
        if source_tree.is_symlink() or not source_tree.is_dir():
            _fail("arena src is not a regular directory")
        source_tree.rename(source_dir / "src")
        legacy_files["src"] = "source/src"
    source_stg = source_dir / (arena["stg_relative"].name + ".original")
    try:
        shutil.copyfile(arena["stg_path"], source_stg)
    except OSError as exc:
        _fail("failed to preserve the source STG: %s" % exc)
    if _sha256(source_stg) != arena["stg_sha256"]:
        _fail("source STG was not preserved byte-for-byte")
    legacy_files["stg"] = "source/" + source_stg.name
    source_manifest = source_dir / "manifest.json"
    if not source_manifest.is_file():
        _fail("failed to preserve source/manifest.json")
    if source_manifest.read_bytes() != fallback_manifest_bytes:
        _fail("source/manifest.json was not preserved byte-for-byte")
    return legacy_files


def _format_agl(value):
    text = ("%.6f" % value).rstrip("0").rstrip(".")
    return text if "." in text else text + ".0"


def _render_stg(arena, agl_m, carrier_source_relative):
    agl_text = _format_agl(agl_m)
    header = [
        "# space_arena carrier overlay placements.",
        "# Non-carrier entries are copied verbatim from the source arena;",
        "# their keyword, offset field and datum are unchanged.",
        "# OBJECT_STATIC_AGL = offset above ground level; OBJECT_STATIC =",
        "# elevation above sea level (STG conventions, runtime NOT verified).",
        "# The four Empire greybox props are intentionally removed; the",
        "# Venator carrier pack replaces them at the Empire anchor.",
        "# The carrier entry uses %s m design AGL: this is NOT a measured"
        % agl_text,
        "# MSL elevation.",
        "# heading = counter-clockwise degrees from north",
        "# (Docs/README.scenery); runtime semantics are NOT verified.",
    ]
    removed = {entry["resolved"] for entry in arena["removed_entries"]}
    body = []
    seen_entry = False
    for line in arena["stg_text"].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if seen_entry:
                body.append(line)
            continue
        try:
            fields = shlex.split(stripped, posix=True)
        except ValueError:
            _fail("source STG changed while rendering")
        relative = _safe_relative(fields[1], "source STG model path")
        resolved = arena["stg_path"].parent.joinpath(
            *relative.parts).resolve()
        seen_entry = True
        if resolved in removed:
            continue
        body.append(line)

    anchor = arena["anchors"]["empire"]
    carrier_line = (
        "OBJECT_STATIC_AGL %s %.6f %.6f %s %.2f"
        % (carrier_source_relative.as_posix(), anchor["lon"], anchor["lat"],
           agl_text, arena["heading_stg"]))
    block = [
        "",
        "# --- external Venator carrier overlay "
        "(tools/build_carrier_arena.py) ---",
        "# source pack licence status: unverified; local build only.",
        carrier_line,
    ]
    return "\n".join(header + body + block) + "\n", carrier_line


def _hash_tree(root, exclude):
    root = Path(root)
    hashes = {}
    for relative, path in _walk_regular_files(root, "output tree"):
        stored = relative.as_posix()
        if stored in exclude:
            continue
        hashes[stored] = _sha256(path)
    return hashes


def _write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False,
                   allow_nan=False) + "\n",
        encoding="utf-8", newline="\n")


def _overlay_readme(arena, agl_m, carrier_line, legacy_files):
    anchor = arena["anchors"]["empire"]
    lines = [
        "# Space Arena - local Venator carrier overlay",
        "",
        "Generated by `tools/build_carrier_arena.py`. Do not edit the",
        "emitted files by hand; rebuild the overlay instead.",
        "",
        "## What this tree is",
        "",
        "* A standalone runtime copy of the committed Rebels greybox arena.",
        "* The four Empire greybox props (`emp_carrier`, `emp_deck`,",
        "  `emp_frame`, `emp_rack`) were removed from the STG tile; their",
        "  original model files remain copied but unreferenced.",
        "* A prepared external carrier pack was copied to",
        "  `Objects/.../Models/venator/` and placed in the same STG tile at",
        "  the existing Empire anchor (%.6f, %.6f)." % (anchor["lon"],
                                                       anchor["lat"]),
        "* Source greybox documentation and generator are preserved under",
        "  `source/` together with the untouched source manifest and the",
        "  original STG before the Empire props were removed.",
        "",
        "## Carrier placement",
        "",
        "* Elevation: %.3f m DESIGN AGL above terrain at the anchor." % agl_m,
        "  This is an explicit build parameter, NOT a measured MSL height",
        "  and not a runtime elevation claim.",
        "* Heading: the STG heading of the original Empire carrier entry.",
        "* STG line:",
        "",
        "```",
        carrier_line,
        "```",
        "",
        "## Status",
        "",
        "* `runtime_verified: false`; FlightGear is not available here.",
        "* No collision, no network integration and no public spawn/launch",
        "  command are provided. Carrier spawn slots are local metadata.",
        "* The external pack licence status is `unverified`; no third-party",
        "  mesh is committed to this repository. Do not redistribute the",
        "  output until the licence is clarified.",
        "",
        "## Source",
        "",
        "The untouched source greybox manifest is `source/manifest.json`.",
        "Its artifact hashes and triangle budget describe the original",
        "package, not this overlay. The overlay manifest (`manifest.json`)",
        "contains freshly computed hashes for every emitted file.",
        "",
    ]
    for name, stored in sorted(legacy_files.items()):
        if name.endswith(".md"):
            lines.append("* Legacy `%s` -> `%s`" % (name, stored))
    return "\n".join(lines) + "\n"


def _build_manifest(arena, pack, agl_m, carrier_line, stg_relative,
                    carrier_relative_dir, legacy_files, temporary,
                    artifact_hashes):
    manifest = arena["manifest"]
    anchor = arena["anchors"]["empire"]
    source_manifest_sha = _sha256(Path(temporary) / "source" / "manifest.json")

    active_models = {}
    for name in sorted(arena["active_model_names"]):
        info = arena["models"][name]
        emitted_ac = Path(temporary).joinpath(*info["ac_relative"].parts)
        emitted_xml = Path(temporary).joinpath(*info["xml_relative"].parts)
        geometry = parse_ac(emitted_ac)
        metadata = copy.deepcopy(info["metadata"])
        metadata["triangles"] = geometry["triangles"]
        metadata["overlay_status"] = "active"
        active_models[name] = metadata
        if not emitted_xml.is_file():
            _fail("active model %s has no emitted XML wrapper" % name)

    retired_models = {}
    for name in RETIRED_MODELS:
        info = arena["retired_models"][name]
        emitted_ac = Path(temporary).joinpath(*info["ac_relative"].parts)
        metadata = copy.deepcopy(info["metadata"])
        metadata["triangles"] = parse_ac(emitted_ac)["triangles"]
        metadata["status"] = "retained_unreferenced"
        metadata["note"] = (
            "Original Empire greybox model file copied unchanged; no STG "
            "placement references it in this overlay.")
        retired_models[name] = metadata

    active_placements = []
    for placement in arena["active_placements"]:
        item = copy.deepcopy(placement)
        item["overlay_status"] = "preserved_from_source"
        records = item.pop("source_stg_entries", [])
        item["source_stg_entries"] = copy.deepcopy(records)
        # The source manifest's own AGL wording is not reused: it may not
        # match a different source arena.  The factual STG record below is
        # the only elevation claim for this placement.
        item.pop("agl_semantics", None)
        item.pop("agl_offset_m", None)
        if len(records) == 1:
            item["source_stg"] = copy.deepcopy(records[0])
            item["elevation_semantics"] = _stg_offset_semantics(records[0])
        elif records:
            item["elevation_semantics"] = (
                "Source placement has %d STG lines; see source_stg_entries."
                % len(records))
        else:
            item["elevation_semantics"] = (
                "No source STG line was found; no elevation claim is made.")
        active_placements.append(item)

    retired_placements = []
    for placement in arena["retired_placements"]:
        item = copy.deepcopy(placement)
        item["status"] = "retired"
        item["reason"] = (
            "Empire greybox placement removed from the STG; the external "
            "carrier overlay is the replacement.")
        records = item.pop("source_stg_entries", [])
        item["source_stg_entries"] = copy.deepcopy(records)
        item.pop("agl_semantics", None)
        item.pop("agl_offset_m", None)
        item["elevation_semantics"] = (
            "Legacy source record only; this placement has no active STG "
            "entry in the overlay. The preserved source_stg_entries describe "
            "the greybox line and are not an overlay elevation claim.")
        retired_placements.append(item)

    retired_placement_ids = {item["id"] for item in retired_placements}

    active_poses = []
    obsolete_poses = []
    for pose in manifest.get("poses", []):
        if (pose.get("faction") == "empire"
                or pose.get("on_placement") in retired_placement_ids):
            item = copy.deepcopy(pose)
            item["status"] = "obsolete"
            item["primary"] = False
            item["reason"] = (
                "Old Empire ground/deck spawn pose; its placement is retired "
                "and the carrier is not runtime-verified.")
            item["replaced_by"] = (
                "carrier_integration.spawn_slots (local metadata only; no "
                "public spawn command)")
            obsolete_poses.append(item)
        else:
            active_poses.append(copy.deepcopy(pose))
    active_pose_ids = {pose["id"] for pose in active_poses}

    active_routes = []
    obsolete_routes = []
    for route in manifest.get("routes", []):
        if (route.get("faction") == "empire"
                or route.get("from_pose") not in active_pose_ids):
            item = copy.deepcopy(route)
            item["status"] = "obsolete"
            item["reason"] = "Depends on an obsolete Empire spawn pose."
            obsolete_routes.append(item)
        else:
            active_routes.append(copy.deepcopy(route))

    active_surfaces = []
    obsolete_surfaces = []
    for surface in manifest.get("surfaces", []):
        if surface.get("placement_id") in retired_placement_ids:
            item = copy.deepcopy(surface)
            item["status"] = "obsolete"
            item["reason"] = "Placement was removed from the overlay STG."
            obsolete_surfaces.append(item)
        else:
            active_surfaces.append(copy.deepcopy(surface))

    active_stack_pairs = []
    obsolete_stack_pairs = []
    for pair in manifest.get("stack_pairs", []):
        if (pair.get("lower") in retired_placement_ids
                or pair.get("upper") in retired_placement_ids):
            item = copy.deepcopy(pair)
            item["status"] = "obsolete"
            item["reason"] = "Both greybox props are retired."
            obsolete_stack_pairs.append(item)
        else:
            active_stack_pairs.append(copy.deepcopy(pair))

    keepouts = copy.deepcopy(manifest.get("keepouts", []))
    for keepout in keepouts:
        if keepout.get("faction") == "empire":
            keepout["status"] = "design_zone_only"
            keepout["note"] = (
                "Horizontal design exclusion zone around the Empire anchor. "
                "The carrier is a %.3f m AGL overlay; this is not a 3D "
                "volume and is not runtime-verified." % agl_m)

    region = copy.deepcopy(manifest.get("region", {}))
    region["overlay_note"] = (
        "Tile and anchors are inherited from the source Rebel/Empire arena; "
        "the carrier line is written into the same STG tile.")

    axes = copy.deepcopy(manifest.get("axes", {}))
    axes["carrier_layout_axes"] = LAYOUT_AXES
    axes["carrier_layout_to_ac"] = (
        "AC = (-Y, Z, -X): ac_x=-layout_y (aft), ac_y=layout_z (up), "
        "ac_z=-layout_x (left); layout is X right, Y forward, Z up")
    axes["ac_axes"] = "AC3Db as stored: +x aft, +y up, +z left"

    offset_summary = _summarize_active_offsets(active_placements)
    elevation_definition = {
        "active_agl_offset_m": offset_summary["agl_offset_m"],
        "active_msl_elevation_m": offset_summary["msl_elevation_m"],
        "active_offset_kinds": offset_summary["kinds"],
        "active_offset_summary": offset_summary["summary"],
        "per_placement": offset_summary["per_placement"],
        "carrier_agl_m": agl_m,
        "carrier_agl_datum": "design_agl_above_terrain_at_empire_anchor",
        "msl_claims": "none; terrain MSL at the anchor was not measured",
        "note": (
            "Non-carrier values are taken from the source STG lines, not "
            "from the source manifest's prose and not invented. A single "
            "uniform AGL (or MSL) value is reported only when every active "
            "placement agrees; otherwise the summary is null and the "
            "per-placement records are authoritative. The carrier uses the "
            "explicit --agl-m design value; it is not a measured MSL "
            "elevation."),
        "terrain_height_band_m": copy.deepcopy(
            manifest.get("elevation_definition", {}).get(
                "terrain_height_band_m")),
    }

    carrier_models = {}
    for key in ("lod0", "lod1", "lod2"):
        name = "carrier_%s.ac" % key
        carrier_models[key] = {
            "file": (carrier_relative_dir / name).as_posix(),
            "triangles": pack["parsed"][name]["triangles"],
        }
    if pack["layout_info"]["geometry_mode"] == "separate":
        carrier_models["hangar"] = {
            "file": (carrier_relative_dir / "hangar.ac").as_posix(),
            "triangles": pack["parsed"]["hangar.ac"]["triangles"],
        }

    carrier_files = {}
    for name, source in sorted(pack["files"].items()):
        target = carrier_relative_dir / name
        emitted = Path(temporary).joinpath(*target.parts)
        if not emitted.is_file():
            _fail("carrier file was not emitted: %s" % name)
        if _sha256(emitted) != _sha256(source):
            _fail("carrier copy verification failed for %s" % name)
        carrier_files[target.as_posix()] = _sha256(emitted)
    for name, source in sorted(pack["texture_files"].items()):
        target = carrier_relative_dir / name
        emitted = Path(temporary).joinpath(*target.parts)
        if not emitted.is_file() or _sha256(emitted) != _sha256(source):
            _fail("carrier texture copy verification failed for %s" % name)
        carrier_files[target.as_posix()] = _sha256(emitted)

    carrier_parsed = {
        key: pack["parsed"].get("%s.ac" % ("carrier_" + key
                                       if key != "hangar" else "hangar"),
                                       {"triangles": 0})["triangles"]
        for key in ("lod0", "lod1", "lod2", "hangar")
    }
    active_total = sum(
        metadata["triangles"] for metadata in active_models.values())
    carrier_runtime_total = carrier_parsed["lod0"] + carrier_parsed["hangar"]
    source_budget = manifest.get("budget")
    if not isinstance(source_budget, dict):
        source_budget = {}
    legacy_limit = source_budget.get("triangle_limit")
    if (isinstance(legacy_limit, bool)
            or not isinstance(legacy_limit, (int, float))
            or not math.isfinite(float(legacy_limit))):
        legacy_limit = None
    source_total = source_budget.get("triangles_total")
    if (isinstance(source_total, int) and not isinstance(source_total, bool)
            and legacy_limit is not None):
        budget_note = (
            "Recomputed from the .ac files emitted in this overlay. The "
            "source manifest's budget (triangles_total=%d, "
            "triangle_limit=%s) describes the original greybox only "
            "(preserved at source/manifest.json) and is not a claim about "
            "this overlay." % (source_total, legacy_limit))
    else:
        budget_note = (
            "Recomputed from the .ac files emitted in this overlay. The "
            "source manifest's budget describes the original greybox only "
            "(preserved at source/manifest.json) and is not a claim about "
            "this overlay.")

    budget = {
        "note": budget_note,
        "legacy_triangle_limit": legacy_limit,
        "source_manifest_budget": {
            "path": "source/manifest.json",
            "note": (
                "The original greybox budget lives only there; its totals "
                "are not reused as overlay claims."),
        },
        "active_per_model": {
            name: metadata["triangles"]
            for name, metadata in sorted(active_models.items())
        },
        "active_total": active_total,
        "carrier": {
            "declared": copy.deepcopy(pack["declared_triangles"]),
            "parsed": carrier_parsed,
            "declared_vs_parsed_mismatches": copy.deepcopy(
                pack["triangle_mismatches"]),
            "runtime_lod0_plus_hangar": carrier_runtime_total,
        },
        "overlay_runtime_total": active_total + carrier_runtime_total,
        "legacy_empire_retained_unused": {
            name: metadata["triangles"]
            for name, metadata in sorted(retired_models.items())
        },
        "within_legacy_limit": (
            None if legacy_limit is None
            else active_total + carrier_runtime_total <= legacy_limit),
    }

    carrier_integration = {
        "schema": LAYOUT_SCHEMA,
        "geometry_mode": pack["layout_info"]["geometry_mode"],
        "layout_axes": LAYOUT_AXES,
        "layout": (carrier_relative_dir / "layout.json").as_posix(),
        "layout_sha256": pack["layout_sha256"],
        "layout_bounds_m": pack["layout_info"]["bounds_m"],
        "agl_m": agl_m,
        "agl_datum": "design_agl_above_terrain_at_empire_anchor",
        "anchor": {
            "faction": "empire",
            "lon": anchor["lon"],
            "lat": anchor["lat"],
            "source": "source/manifest.json region.anchors.empire",
            "agl_m": agl_m,
            "agl_datum": "design_agl_above_terrain_at_empire_anchor",
            "msl_m": None,
            "note": (
                "%.3f m is an explicit design AGL parameter from --agl-m; "
                "terrain MSL was not measured and no MSL elevation is "
                "claimed." % agl_m),
        },
        "stg": {
            "path": stg_relative.as_posix(),
            "line": carrier_line,
            "heading_deg": arena["carrier_placement"].get("heading_deg"),
            "stg_heading_deg": arena["heading_stg"],
            "heading_source": (
                "STG heading of the original Empire emp_carrier placement"),
        },
        "transforms": {
            "ac": ["-Y", "Z", "-X"],
            "layout_to_ac": {
                "x": "-Y",
                "y": "Z",
                "z": "-X",
            },
            "formula": "ac_x=-layout_y; ac_y=layout_z; ac_z=-layout_x",
            "layout_axes": LAYOUT_AXES,
            "ac_axes": "AC3Db stored: +x aft, +y up, +z left",
            "note": (
                "Spawn positions are local model coordinates. No global "
                "FlightGear spawn transform or launch command is provided."),
        },
        "spawn_slots": copy.deepcopy(pack["layout_info"]["spawn_slots"]),
        "models": carrier_models,
        "files": carrier_files,
        "declared_triangles": copy.deepcopy(pack["declared_triangles"]),
        "parsed_triangles": carrier_parsed,
        "triangle_mismatches": copy.deepcopy(pack["triangle_mismatches"]),
        "external_pack": {
            "license_status": "unverified",
            "license_status_source": pack["layout_info"]["license_status"],
            "redistributed_in_repo_git": False,
            "ignored_top_level_entries": copy.deepcopy(
                pack["ignored_entries"]),
            "note": (
                "External pack copied only into the requested output tree; "
                "no third-party mesh is added to the repository. Source "
                "layout statuses that only record pending source URLs are "
                "normalised to unverified."),
        },
        "runtime_verified": False,
        "collision": {
            "status": "not_integrated",
            "note": "No collision shape or collision backend is claimed.",
        },
        "network": {
            "status": "not_integrated",
            "note": "No multiplayer/network spawn integration is claimed.",
        },
    }

    verification = {
        "static_overlay": (
            "python3 -m unittest tools/test_build_carrier_arena.py"),
        "fg_runtime_load": (
            "NOT VERIFIED - FlightGear is not available in this build "
            "environment"),
        "carrier_runtime": "NOT VERIFIED - runtime_verified=false",
        "collision": "NOT INTEGRATED / NOT VERIFIED",
        "network": "NOT INTEGRATED / NOT VERIFIED",
        "spawn": (
            "No public spawn or launch command. Carrier spawn slots are "
            "local metadata only."),
        "license": (
            "External carrier pack license_status=unverified; do not "
            "redistribute until clarified."),
    }

    source = {
        "arena_package": manifest.get("package"),
        "manifest": "source/manifest.json",
        "manifest_sha256": source_manifest_sha,
        "schema": manifest.get("schema"),
        "schema_version": manifest.get("schema_version"),
        "original_stg_sha256": arena["stg_sha256"],
        "legacy_files": legacy_files,
        "note": (
            "source/manifest.json is the untouched original greybox "
            "manifest. Its artifact_sha256 and budget describe that source "
            "package only and are not claims about this overlay."),
    }

    return {
        "schema": OVERLAY_SCHEMA,
        "schema_version": 1,
        "package": "carrier arena overlay",
        "task": "CARRIER-ARENA-OVERLAY",
        "generated_by": "tools/build_carrier_arena.py",
        "builder_sha256": _sha256(Path(__file__)),
        "fg_target_version": manifest.get("fg_target_version"),
        "status_banner": (
            "LOCAL CARRIER OVERLAY - runtime NOT verified; external pack "
            "licence unverified; collision/network/spawn not integrated."),
        "units": manifest.get("units"),
        "axes": axes,
        "region": region,
        "elevation_definition": elevation_definition,
        "stg": {
            "path": stg_relative.as_posix(),
            "entry_kinds": sorted(
                {entry["kind"] for entry in arena["entries"]}
                | {"OBJECT_STATIC_AGL"}),
            "carrier_entry_kind": "OBJECT_STATIC_AGL",
            "entry_fields": (
                "OBJECT_STATIC_AGL|OBJECT_STATIC <model.xml> <lon> <lat> "
                "<offset_or_elevation_m> <heading_deg>"),
            "note": (
                "Non-carrier lines are copied verbatim from the source STG, "
                "so their keyword and datum are preserved (OBJECT_STATIC_AGL "
                "= above ground; OBJECT_STATIC = above sea level by STG "
                "convention). The carrier line uses the design --agl-m "
                "value. There is no single global AGL offset for this "
                "overlay."),
            "carrier": {
                "path": carrier_relative_dir.joinpath("carrier.xml").as_posix(),
                "line": carrier_line,
                "agl_m": agl_m,
                "stg_heading_deg": arena["heading_stg"],
            },
            "heading_note": (
                "Written heading is counter-clockwise degrees from north "
                "per Docs/README.scenery; runtime sign is not verified."),
            "path_base": (
                "Model paths are relative to the tile directory containing "
                "the STG."),
        },
        "placements": active_placements,
        "retired_empire_placements": retired_placements,
        "models": active_models,
        "retired_empire_models": retired_models,
        "poses": active_poses,
        "routes": active_routes,
        "surfaces": active_surfaces,
        "stack_pairs": active_stack_pairs,
        "keepouts": keepouts,
        "obsolete": {
            "note": (
                "Legacy Empire spawn/support claims are kept as explicit "
                "obsolete records and are not active overlay claims."),
            "poses": obsolete_poses,
            "routes": obsolete_routes,
            "surfaces": obsolete_surfaces,
            "stack_pairs": obsolete_stack_pairs,
        },
        "budget": budget,
        "carrier_integration": carrier_integration,
        "source": source,
        "verification": verification,
        "runtime_verified": False,
        "launch": {
            "commands": [],
            "note": (
                "No public launch or ground-spawn command is generated; "
                "carrier runtime and spawn are unverified."),
        },
        "provenance": {
            "builder": "tools/build_carrier_arena.py",
            "builder_sha256": _sha256(Path(__file__)),
            "license": (
                "Builder GPL-2.0-or-later; external carrier assets "
                "license_status=unverified."),
            "artifact_sha256": artifact_hashes,
            "note": (
                "Hashes cover every emitted file except manifest.json "
                "itself; source/manifest.json is preserved byte-for-byte."),
        },
    }


def _validate_emitted(temporary, arena, pack, stg_relative,
                      carrier_relative_dir):
    temporary = Path(temporary)
    manifest_path = temporary / "manifest.json"
    emitted = _load_json(manifest_path, "emitted manifest")
    if emitted.get("schema") != OVERLAY_SCHEMA:
        _fail("emitted manifest schema mismatch")
    if emitted.get("runtime_verified") is not False:
        _fail("emitted manifest must keep runtime_verified=false")

    artifact_hashes = emitted.get("provenance", {}).get(
        "artifact_sha256", {})
    if not artifact_hashes:
        _fail("emitted manifest has no artifact hashes")
    for relative, expected in artifact_hashes.items():
        path = temporary.joinpath(*PurePosixPath(relative).parts)
        if not path.is_file() or _sha256(path) != expected:
            _fail("emitted artifact hash mismatch: %s" % relative)

    carrier_root = temporary.joinpath(*carrier_relative_dir.parts)
    emitted_carrier = {
        path.relative_to(carrier_root).as_posix()
        for _relative, path in _walk_regular_files(
            carrier_root, "emitted carrier tree")
    }
    expected_carrier = set(pack["files"]) | set(pack["texture_files"])
    if emitted_carrier != expected_carrier:
        _fail("emitted carrier tree does not match the whitelist: %s"
              % sorted(emitted_carrier ^ expected_carrier))

    _scan_xml(carrier_root, carrier_root / "carrier.xml",
              allowed=lambda rel: (
                  rel.as_posix() in CARRIER_XML_REF_FILES
                  or rel.as_posix().startswith("textures/")),
              require_path=True)

    stg_path = temporary.joinpath(*stg_relative.parts)
    entries = []
    for line_number, line in enumerate(
            stg_path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = shlex.split(stripped, posix=True)
        if fields[0] not in STG_ENTRY_KINDS or len(fields) != 6:
            _fail("emitted STG %s:%d is malformed" % (stg_path, line_number))
        relative = _safe_relative(fields[1], "emitted STG model path")
        resolved = stg_path.parent.joinpath(*relative.parts)
        if not resolved.is_file():
            _fail("emitted STG model is missing: %s" % fields[1])
        if resolved.resolve() in arena["retired_resolved"]:
            _fail("emitted STG still references a retired Empire model")
        entries.append(fields)
    if len(entries) != len(arena["entries"]) - len(arena["removed_entries"]) + 1:
        _fail("emitted STG entry count is inconsistent")
    carrier_entries = [
        fields for fields in entries
        if fields[1] == (carrier_relative_dir / "carrier.xml").relative_to(
            stg_relative.parent).as_posix()
    ]
    if len(carrier_entries) != 1:
        _fail("emitted STG must contain exactly one carrier entry")
    if carrier_entries[0][0] != "OBJECT_STATIC_AGL":
        _fail("emitted carrier entry must be OBJECT_STATIC_AGL")

    source_manifest = temporary / "source" / "manifest.json"
    if not source_manifest.is_file():
        _fail("emitted tree lost source/manifest.json")
    if _sha256(source_manifest) != arena["manifest_sha256"]:
        _fail("emitted source manifest does not match the source arena")


def build(arena_root=DEFAULT_ARENA_ROOT, carrier_root=None, output_root=None,
          agl_m=DEFAULT_AGL_M):
    """Validate all inputs, build a new overlay tree and rename it into place.

    Returns the emitted overlay manifest.  Raises BuildError on any input,
    output or validation problem; in that case no output root is created (an
    existing empty output root is left empty) and no temporary sibling is
    left behind.
    """
    if carrier_root is None or output_root is None:
        _fail("carrier_root and output_root are required")
    agl = _number(agl_m, "--agl-m")
    if agl < 0.0:
        _fail("--agl-m must be non-negative")

    _check_input_roots(arena_root, carrier_root)
    arena = _load_arena(arena_root)
    pack = _load_carrier_pack(carrier_root)
    output_root = _check_output_root(output_root, arena["root"],
                                     pack["root"])

    stg_relative = arena["stg_relative"]
    carrier_relative_dir = stg_relative.parent / "Models" / "venator"
    carrier_xml_relative = carrier_relative_dir / "carrier.xml"
    carrier_source_relative = carrier_xml_relative.relative_to(
        stg_relative.parent)

    if (arena["root"] / carrier_relative_dir).exists():
        _fail("carrier destination already exists in the source arena: %s"
              % carrier_relative_dir)
    if any(entry["resolved"]
           == (arena["root"] / carrier_xml_relative).resolve()
           for entry in arena["entries"]):
        _fail("source STG already contains the carrier overlay path")

    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=".%s.tmp-" % output_root.name,
        dir=str(output_root.parent)))
    try:
        _copy_arena(arena, temporary)
        legacy_files = _stage_legacy_source(
            temporary, arena, arena["manifest_path"].read_bytes())

        stg_text, carrier_line = _render_stg(
            arena, agl, carrier_source_relative)
        stg_target = temporary.joinpath(*stg_relative.parts)
        stg_target.write_text(stg_text, encoding="utf-8", newline="\n")

        carrier_target = temporary.joinpath(*carrier_relative_dir.parts)
        carrier_target.mkdir(parents=True)
        for name, source in sorted(pack["files"].items()):
            destination = carrier_target / name
            shutil.copyfile(source, destination)
        for name, source in sorted(pack["texture_files"].items()):
            destination = carrier_target.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

        # Verify the emitted carrier references before manifest generation.
        _scan_xml(
            carrier_target, carrier_target / "carrier.xml",
            allowed=lambda rel: (
                rel.as_posix() in CARRIER_XML_REF_FILES
                or rel.as_posix().startswith("textures/")),
            require_path=True)

        readme = _overlay_readme(arena, agl, carrier_line, legacy_files)
        (temporary / "README.md").write_text(
            readme, encoding="utf-8", newline="\n")

        artifact_hashes = _hash_tree(temporary, exclude={"manifest.json"})
        overlay = _build_manifest(
            arena, pack, agl, carrier_line, stg_relative,
            carrier_relative_dir, legacy_files, temporary, artifact_hashes)
        _write_json(temporary / "manifest.json", overlay)
        _validate_emitted(temporary, arena, pack, stg_relative,
                          carrier_relative_dir)
        os.replace(temporary, output_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return overlay


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Build a local Venator carrier overlay on top of the committed "
            "space_arena without modifying the source arena or the external "
            "carrier pack."))
    parser.add_argument(
        "--arena-root", type=Path, default=DEFAULT_ARENA_ROOT,
        help=("source arena package (default: %(default)s); read-only"))
    parser.add_argument(
        "--carrier-root", type=Path, required=True,
        help=("prepared external carrier pack with carrier.xml, "
              "carrier_lod0/1/2.ac, layout.json, optional textures/ and "
              "hangar.ac in separate mode; must not overlap --arena-root"))
    parser.add_argument(
        "--output-root", type=Path, required=True,
        help=("new or empty output directory outside the repository and "
              "outside all input roots"))
    parser.add_argument(
        "--agl-m", type=float, default=DEFAULT_AGL_M,
        help=("carrier design AGL offset above the terrain at the Empire "
              "anchor in meters; this is NOT an MSL elevation "
              "(default: %(default)s)"))
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        overlay = build(args.arena_root, args.carrier_root,
                        args.output_root, args.agl_m)
    except BuildError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    carrier = overlay["carrier_integration"]
    print("carrier arena overlay built at %s" % args.output_root)
    print("stg: %s" % overlay["stg"]["path"])
    print("carrier: %.1f m design AGL at empire anchor %.6f, %.6f; "
          "STG heading %.2f"
          % (carrier["anchor"]["agl_m"], carrier["anchor"]["lon"],
             carrier["anchor"]["lat"], carrier["stg"]["stg_heading_deg"]))
    print("retired Empire placements: %s"
          % ", ".join(sorted(overlay["retired_empire_models"])))
    print("runtime: NOT VERIFIED; collision/network/spawn: not integrated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
