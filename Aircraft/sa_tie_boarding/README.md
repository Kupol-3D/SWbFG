# sa_tie_boarding - TIE Boarding Craft visual package

`ship_id=boarding_craft`, `team_id=empire`. Generated mechanically by `tools/build_tie_packages.py` from the prepared TIE asset-pack stage-2 exports. This is a static viewer for FlightGear 2024.1.7; it is not an aircraft completion claim.

## Status (do not over-read)

- `<flight-model>null</flight-model>` by default: no flight dynamics are enabled here. The separate assisted-flight adapter is a different generator and is not selected by this package.
- Collision data is copied as inert geometry only: `pending_collision_backend`. No XML references it.
- Boarding visual status: **candidate, not orchestrator-accepted**. The current cockpit window/cabin is a rev3 repair candidate; the previous blocked slot is not claimed as completed.
- No FlightGear launch, visual acceptance, FPS, collision or multiplayer test was performed by this builder.
- Source model license: CC BY, version unstated in the supplied files; see `LICENSE_ATTRIBUTION.md`. Publication requires re-verification.

## Inputs consumed (read-only)

- `boarding_craft/export/game_lod0.ac` (game LOD0 AC)
- `boarding_craft/export/cockpit.ac` (cockpit AC)
- `boarding_craft/export/integration.json` (integration metadata)
- `boarding_craft/flight_profile.json` (flight profile (copied))

Missing required inputs fail the build with a diagnostic; no placeholder model, camera, texture, collider or FDM is created.

## Outputs

- `Models/boarding_craft.ac` - merged game LOD0 + cockpit geometry (AC3Db).
- `Models/boarding_craft.xml` - FG wrapper with the local effect.
- `Models/*_lod*.ac` - static LOD copies when supplied. They are bundled data only; runtime LOD selection is **not** wired or tested.
- `Models/*-collision.*` - inert collision data when supplied.
- `Models/Textures/...` - bundled BSDF textures with rewritten relative references.
- `Effects/visual.eff` - inherits `Effects/model-combined` (TASK2 pattern; ALS is not disabled).
- `Config/flight_profile.json` - byte-for-byte copy of the ship profile; it is a gameplay estimate, not an accepted FDM.
- `Sources/SHA256SUMS`, `Sources/BUILD_MANIFEST.json` - input and generated-file provenance.
- `LICENSE_ATTRIBUTION.md`, `README.md`, `sa_tie_boarding-set.xml`.

## Axes and cameras

Asset frame: X right, Y forward, Z up, metres. FG model frame: X aft, Y right, Z up. Asset point `(X, Y, Z)` -> FG model `(-Y, X, Z)`; FG camera view offsets `(right, up, aft)` = `eye_asset_xyz_m` transformed to `(X, Z, -Y)`. Cameras come from `export/integration.json`; this builder never guesses a viewpoint.

Bundled cameras:

- `pilot` (internal, lookfrom, internal FOV 75.0, near 0.04 m, offsets right/up/aft = (1.6025999784469604, 0.07499999552965164, -2.197999954223633), source: asset-eye-mapping)
- `external` (external, lookat, external FOV 45.0, near 0.1 m, offsets right/up/aft = (0.0, 7.066, 13.076), source: asset-eye-mapping)

## Rebuild and check

From the repository root:

```sh
python3 tools/build_tie_packages.py \
  --assets-root /path/to/TIE_Game_Asset_Pack_v1/ships \
  --output-root "$PWD/Aircraft"
python3 tools/build_tie_packages.py \
  --assets-root /path/to/TIE_Game_Asset_Pack_v1/ships \
  --output-root "$PWD/Aircraft" --check
```

`--check` only compares; it never rewrites files.

## Flight-configuration boundary

`Config/flight_profile.json` is a byte-for-byte copy of the source profile (status `GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS`). The separate assisted-flight generator owns any `flight_config*` file; this builder neither creates nor overwrites one.
