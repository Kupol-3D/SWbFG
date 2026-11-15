# TIE FlightGear visual packages

Separate builder: `tools/build_tie_packages.py`.  It consumes stage-2 exports
(`game_lod0.ac`/`game.ac`, `cockpit.ac`/`game_cockpit.ac`,
`integration.json`, read-only `flight_profile.json`) and writes visual-only
packages under `Aircraft/sa_tie_*`.

Default `<flight-model>null</flight-model>`.  Assisted FDM stays separate:
opt-in only via `--flight-model jsb --aero NAME` (supplied FDM), not enabled
by default.

```sh
python3 tools/build_tie_packages.py \
  --assets-root /workspace/work/TIE_Game_Asset_Pack_v1/ships \
  --output-root /workspace/work/Bombable/Aircraft
python3 tools/build_tie_packages.py \
  --assets-root /workspace/work/TIE_Game_Asset_Pack_v1/ships \
  --output-root /workspace/work/Bombable/Aircraft --check
```

Collision data is copied as inert geometry only (`pending_collision_backend`);
no runtime collision, FDM certification, or FPS claim is made.  Missing inputs
produce a clear error and no placeholder package.

In `integration.json`, `lods` entries and `collision.file` are resolved relative
to the ship's `export/` directory (bare filenames such as `game_lod1.ac` or
`collision.glb`).  Texture `source` paths remain relative to the ship directory
(for example `export/textures/hull.png`).
