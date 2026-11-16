#!/usr/bin/env python3
"""
Preflight/diagnostic helper for FlightGear shared scenery/model resources.

Default behavior: read-only checks. With --download-missing-official, missing
sentinel shared-model files may be fetched from an explicit official TerraSync
HTTP base into --target-root (a NEW scenery root), without modifying existing
scenery roots or user backups.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.request
from dataclasses import dataclass
from typing import Iterable

# Known FG startup sentinels observed for FG 2024.1.4 (host evidence). These are
# common/shared models, not aircraft or terrain tiles.
DEFAULT_SENTINELS = [
    "Models/Airport/windsock_lit.xml",
    "Models/Industrial/generic_chimney_01.xml",
    "Models/Airport/Vehicle/Cobus_3000.xml",
    "Models/Industrial/GenericStorageTank40m.ac",
    "Models/Boundaries/Fence_50m.ac",
    "Models/Residential/french_house_s.xml",
]

# Official TerraSync HTTP content base (hosts .dirindex trees). Used only when
# --download-missing-official is requested with an explicit/default base.
OFFICIAL_TERRASYNC_BASE = "https://flightgear.sourceforge.net/scenery"

# Minimal dependency hints for sentinel XML/AC files we may fetch. This is not a
# full FG model resolver; it covers the recursive deps of the known 2024.1.4
# sentinel set so repairs are not empty placeholders.
SENTINEL_EXTRA_DEPS = {
    "Models/Airport/Vehicle/Cobus_3000.xml": [
        "Models/Airport/Vehicle/Cobus_3000.ac",
        "Models/Airport/Vehicle/Cobus_3000.png",
    ],
    "Models/Residential/french_house_s.xml": [
        "Models/Residential/french_house_s.ac",
        "Models/Residential/texture_french_house_s1.png",
        "Models/Residential/texture_french_house_s2.png",
    ],
}

_XML_REF_RE = re.compile(
    r"<path>\s*([^<\s][^<]*)\s*</path>|<texture>\s*([^<\s][^<]*)\s*</texture>|<inherits-from>\s*([^<\s][^<]*)\s*</inherits-from>",
    re.IGNORECASE,
)
_AC_TEXTURE_RE = re.compile(r'^texture\s+"?([^"\r\n]+)"?', re.MULTILINE)


@dataclass
class FoundPath:
    rel: str
    root: str
    path: str
    size: int


def split_roots(values: Iterable[str] | None) -> list[str]:
    roots: list[str] = []
    for value in values or []:
        if not value:
            continue
        # Allow launcher-style separators; keep Windows drive letters intact.
        if ";" in value:
            parts = value.split(";")
        elif ":" in value and not re.match(r"^[A-Za-z]:[\\/]", value):
            parts = value.split(":")
        else:
            parts = [value]
        for part in parts:
            part = part.strip()
            if part:
                roots.append(os.path.abspath(os.path.expanduser(part)))
    # de-dup preserving order
    out = []
    seen = set()
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def candidate_file_locations(rel: str, scenery_roots: list[str], fg_root: str | None) -> list[str]:
    rel_norm = rel.replace("\\", "/")
    candidates = []
    for root in scenery_roots:
        candidates.append(os.path.join(root, rel_norm))
        # tolerate accidentally passing a Models dir as root, but callers should
        # use scenery roots (parent of Models/) for --fg-scenery.
        if os.path.basename(root) == "Models" and rel_norm.startswith("Models/"):
            candidates.append(os.path.join(root, rel_norm[len("Models/"):]))
    if fg_root:
        candidates.append(os.path.join(fg_root, rel_norm))
    return candidates


def find_first(rel: str, scenery_roots: list[str], fg_root: str | None) -> FoundPath | None:
    for path in candidate_file_locations(rel, scenery_roots, fg_root):
        if os.path.isfile(path):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = -1
            # best-effort root attribution
            root = path
            for r in scenery_roots + ([fg_root] if fg_root else []):
                if r and os.path.commonpath([os.path.abspath(r), os.path.abspath(path)]) == os.path.abspath(r):
                    root = r
                    break
            return FoundPath(rel=rel, root=root, path=os.path.abspath(path), size=size)
    return None


def parse_local_deps(path: str, rel: str) -> list[str]:
    deps = []
    base_dir = os.path.dirname(rel.replace("\\", "/"))
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8", "replace")
    except OSError:
        return deps
    if path.lower().endswith(".xml"):
        for m in _XML_REF_RE.finditer(text):
            ref = next((g for g in m.groups() if g), None)
            if not ref:
                continue
            ref = ref.strip().replace("\\", "/")
            if not ref or ref.startswith("/") or ref.startswith("$") or ":" in ref.split("/")[0]:
                continue
            # FG property/model refs can be non-file; keep likely local files.
            if not re.search(r"\.(ac|xml|png|rgb|dds|jpg|jpeg|osgb|eff)$", ref, re.IGNORECASE):
                continue
            if ref.startswith("Models/"):
                deps.append(ref)
            else:
                deps.append(os.path.normpath(os.path.join(base_dir, ref)).replace("\\", "/"))
    elif path.lower().endswith(".ac"):
        for m in _AC_TEXTURE_RE.finditer(text):
            ref = m.group(1).strip().strip('"').replace("\\", "/")
            if not ref or ref.startswith("/"):
                continue
            if ref.startswith("Models/"):
                deps.append(ref)
            else:
                deps.append(os.path.normpath(os.path.join(base_dir, ref)).replace("\\", "/"))
    # unique preserve order
    out = []
    seen = set()
    for d in deps:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def collect_required(rels: Iterable[str], scenery_roots: list[str], fg_root: str | None) -> dict:
    required = []
    seen = set()
    queue = list(rels)
    for rel in queue:
        rel = rel.replace("\\", "/")
        if rel in seen:
            continue
        seen.add(rel)
        required.append(rel)
        # explicit known extra deps
        for dep in SENTINEL_EXTRA_DEPS.get(rel, []):
            if dep not in seen:
                queue.append(dep)
        # parse deps from an already-found local file (for diagnostics)
        found = find_first(rel, scenery_roots, fg_root)
        if found:
            for dep in parse_local_deps(found.path, rel):
                if dep not in seen:
                    queue.append(dep)
    return {"required": required}


def sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check(scenery_roots: list[str], fg_root: str | None, sentinels: list[str]) -> dict:
    req = collect_required(sentinels, scenery_roots, fg_root)["required"]
    items = []
    missing = []
    for rel in req:
        found = find_first(rel, scenery_roots, fg_root)
        if found:
            items.append({"rel": rel, "status": "found", "path": found.path, "root": found.root, "size": found.size})
        else:
            items.append({"rel": rel, "status": "missing", "candidates": candidate_file_locations(rel, scenery_roots, fg_root)})
            missing.append(rel)
    return {
        "scenery_roots": scenery_roots,
        "fg_root": fg_root,
        "sentinels": sentinels,
        "required_count": len(req),
        "missing_count": len(missing),
        "missing": missing,
        "items": items,
        "ok": len(missing) == 0,
    }


def download_missing(report: dict, target_root: str, base_url: str, timeout: int = 30) -> dict:
    target_root = os.path.abspath(os.path.expanduser(target_root))
    # Safety: target must look like a scenery root we create/use for repair, not
    # FG_ROOT and not an existing user Models dir passed directly.
    if os.path.basename(target_root.rstrip("/")) == "Models":
        raise ValueError("target-root must be a scenery root (parent containing Models/), not a Models directory")
    os.makedirs(target_root, exist_ok=True)
    downloaded = []
    errors = []
    for rel in report.get("missing", []):
        url = base_url.rstrip("/") + "/" + rel.replace("\\", "/")
        dest = os.path.join(target_root, rel.replace("\\", "/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            continue
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            downloaded.append({"rel": rel, "url": url, "dest": os.path.abspath(dest), "bytes": len(data), "sha1": hashlib.sha1(data).hexdigest()})
        except Exception as e:
            errors.append({"rel": rel, "url": url, "dest": os.path.abspath(dest), "error": str(e)})
    return {
        "target_root": target_root,
        "base_url": base_url,
        "downloaded": downloaded,
        "download_errors": errors,
        "downloaded_count": len(downloaded),
        "downloaded_bytes": sum(x["bytes"] for x in downloaded),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Check/download FlightGear shared scenery model sentinels.")
    p.add_argument("--scenery-root", action="append", default=[], help="Scenery root path (repeatable or separator-delimited). Must be a directory containing Models/ for shared models.")
    p.add_argument("--fg-root", default=None, help="FG_ROOT/FGData path (optional, read-only lookup).")
    p.add_argument("--sentinel", action="append", default=[], help="Additional relative sentinel path (e.g. Models/...).")
    p.add_argument("--json", action="store_true", help="Print JSON report.")
    p.add_argument("--fail-on-missing", action="store_true", help="Exit non-zero if required resources are missing.")
    p.add_argument("--download-missing-official", action="store_true", help="Download missing required files from official TerraSync HTTP base into --target-root. Disabled by default.")
    p.add_argument("--target-root", default=None, help="NEW scenery root for downloads (created if needed). Never an existing user scenery root.")
    p.add_argument("--base-url", default=OFFICIAL_TERRASYNC_BASE, help=f"Official TerraSync HTTP base (default: {OFFICIAL_TERRASYNC_BASE}).")
    p.add_argument("--manifest-out", default=None, help="Write JSON manifest/report to this file.")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds for downloads.")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    scenery_roots = split_roots(args.scenery_root)
    fg_root = os.path.abspath(os.path.expanduser(args.fg_root)) if args.fg_root else None
    sentinels = list(DEFAULT_SENTINELS)
    for s in args.sentinel:
        if s and s not in sentinels:
            sentinels.append(s)
    report = check(scenery_roots, fg_root, sentinels)

    download = None
    if args.download_missing_official:
        if not args.target_root:
            print("error: --download-missing-official requires --target-root", file=sys.stderr)
            return 2
        target_root = os.path.abspath(os.path.expanduser(args.target_root))
        # refuse to target existing provided scenery roots or fg_root
        protected = set(scenery_roots + ([fg_root] if fg_root else []))
        if target_root in protected:
            print("error: --target-root must not be an existing scenery/fg root", file=sys.stderr)
            return 2
        download = download_missing(report, target_root, args.base_url, timeout=args.timeout)
        # re-check including target root
        post_roots = list(scenery_roots)
        if target_root not in post_roots:
            post_roots.append(target_root)
        report["post_download_check"] = check(post_roots, fg_root, sentinels)
        report["download"] = download

    if args.manifest_out:
        out_path = os.path.abspath(os.path.expanduser(args.manifest_out))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("Shared scenery/model preflight")
        print(f"  scenery_roots: {scenery_roots}")
        if fg_root:
            print(f"  fg_root: {fg_root}")
        print(f"  required: {report['required_count']}  missing: {report['missing_count']}")
        for item in report["items"]:
            mark = "OK" if item["status"] == "found" else "MISSING"
            loc = item.get("path") or ""
            print(f"  [{mark}] {item['rel']} {loc}")
        if download:
            print(f"  downloaded: {download['downloaded_count']} files, {download['downloaded_bytes']} bytes into {download['target_root']}")
            for err in download.get("download_errors", []):
                print(f"  [DOWNLOAD-ERROR] {err['rel']}: {err['error']}")
        if report["missing_count"] and not (download and report.get("post_download_check", {}).get("ok")):
            print("  RESULT: missing shared models/resources; FG may show 'Missing shared models'.")
            print("  Recommended: add a complete shared-model scenery root via --fg-scenery=<scenery-root> (directory containing Models/),")
            print("  or run this helper with --download-missing-official --target-root <new-scenery-root> and prepend that root.")

    if args.fail_on_missing:
        final_missing = report["missing_count"]
        if download and report.get("post_download_check"):
            final_missing = report["post_download_check"]["missing_count"]
        return 1 if final_missing else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
