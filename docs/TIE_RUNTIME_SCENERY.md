# TIE runtime scenery/shared-model diagnostics (common warning)

## Scope and separation
- This note covers the **common FlightGear startup warning `Missing shared models`** (shared scenery/model resources), not Bombable/TIE aircraft 3D, cockpit, normals, opacity, or FDM/pipeline files.
- No Blender scene/data and no aircraft/3D/pipeline files were modified.
- No existing user FlightGear config was changed globally; no large world terrain was downloaded; the warning is not hidden in UI.

## Observed host/runtime facts (static evidence)
- Host FG binary: `/usr/games/fgfs` (real path), FlightGear **2024.1.4** (`flightgear 1:2024.1.4+dfsg-2`, data packages `*-2024.1.4` installed). Project target is **2024.1.7** (diagnostic only; OS packages not changed).
- `FG_ROOT`: `/usr/share/games/flightgear` (`Models/` present, `Scenery/` absent).
- Existing TerraSync root: `$TERRASYNC_ROOT` with `Models/` and `.dirindex/.dirhash` trees, but incomplete.
- Original problematic launch context: `--fg-scenery=$TERRASYNC_ROOT --disable-terrasync` — with TerraSync disabled, incomplete shared models cannot be auto-repaired.
- FG 2024.1.4 startup sentinel shared-model paths (contiguous in `fgfs` strings; verified on host):  
  `Models/Airport/windsock_lit.xml`, `Models/Industrial/generic_chimney_01.xml`, `Models/Airport/Vehicle/Cobus_3000.xml`, `Models/Industrial/GenericStorageTank40m.ac`, `Models/Boundaries/Fence_50m.ac`, `Models/Residential/french_house_s.xml`.
- Missing before repair (in FG_ROOT + existing TerraSync root): `Models/Airport/Vehicle/Cobus_3000.xml` and `Models/Residential/french_house_s.xml` **plus their local dependencies** (`.ac`/texture PNGs referenced by those models).

## Official source and repair performed
- Source: **official TerraSync HTTP directory/index content** at `https://flightgear.sourceforge.net/scenery` (directory structure with `.dirindex`). Missing files were fetched by exact `.dirindex`-listed paths; SHA1 hashes matched the published `.dirindex` entries.
- Repair scope limited to the missing sentinel shared-model files and their recursively referenced local deps (no whole-archive/world-terrain download; total **7 files, 459,340 bytes**).
- Installed into a **new host/shared-workspace scenery root** (not an existing user scenery root, not FG_ROOT):  
  - Host/container: `<repo>/work/runtime/tie-shared-scenery` 
  - Contents installed: `Models/Airport/Vehicle/Cobus_3000.{xml,ac,png}`, `Models/Residential/french_house_s.{xml,ac}`, `Models/Residential/texture_french_house_s{1,2}.png`
- **Existing files unmodified, none deleted/overwritten, no backups required/created.** No user FG config changed. Newly downloaded scenery is runtime data (not committed to repo).
- Evidence/manifest: `/workspace/work/runtime/tie-scenery-repair/repair-manifest.json` (and host `fetch-missing.json`), including SHA1s, sizes, source URLs, before/after preflight results.

## Preflight helper (recommended)
- `Bombable/tools/check_shared_scenery.py` — read-only by default; checks sentinel shared-model paths and known/parsed local deps across explicit scenery roots (and optional `--fg-root`). Fails clearly with `--fail-on-missing` (suitable for launcher/preflight) instead of relying on a silent/in-engine warning.
- Supports explicit `--scenery-root` (repeatable / separator-delimited). **`--fg-scenery` must point to a scenery root directory that contains `Models/`, never to the `Models/` directory itself.**
- Optional repair mode (default off): `--download-missing-official --target-root <NEW-scenery-root>` fetches missing required files from the official TerraSync base into a new root only; refuses to target existing scenery/fg roots. No automatic downloads unless flag is set.

## Recommended launch (exact, for this host state)
Prepend the repair root so its `Models/` are found before the incomplete TerraSync root:

```bash
--fg-scenery=<repo>/work/runtime/tie-shared-scenery:$TERRASYNC_ROOT --disable-terrasync
```

(If TerraSync is enabled and allowed to complete, it may also supply missing shared models; this path is for the observed `--disable-terrasync` case and uses verified official files in an isolated root.)

## Validation performed
- `python3 Bombable/tools/test_shared_scenery.py -v`: **4 tests OK** (temp fixtures: missing/fail, complete/pass, default no-download/no-write, unsafe target-root rejected).
- Static preflight: **before** = 7 missing sentinel/dependency items in existing roots; **after** (with new root prepended) = **OK, 0 missing** for the checked sentinel set.
- **Runtime (`fgfs`) not launched/verified** in this session (no game run; `ready for single runtime verification`). This confirms presence/resolution of the specific checked shared-model sentinels/deps, not that all possible 3D/engine/scenery errors are eliminated (and unrelated to cockpit/normals/opacity).

## Blockers / notes
- None blocking this specific sentinel repair: official TerraSync HTTP source reachable, files small and verified. If other shared models outside this sentinel set are missing, re-run preflight with additional `--sentinel` paths or allow full TerraSync sync per official mechanism.
