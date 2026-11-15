# TIE assisted flight adapter

Separate game flight adapter.  It is **not** Bombable's conventional
aerodynamic FDM and must never be applied together with one.

Status of every profile and value:
`GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS`.

## Builder

```sh
python3 tools/build_assisted_fdm.py --ship-dir PATH [--integration FILE] --output OUT.xml
python3 tools/build_assisted_fdm.py --ship-dir PATH --output OUT.xml --check
```

Positional form is also accepted:

```sh
python3 tools/build_assisted_fdm.py PATH [INTEGRATION.json] --output OUT.xml
```

The builder is stdlib-only and deterministic.  It reads one profile JSON and
optionally a measured `integration.json`.  It writes a JSBSim FDM XML snippet
(`fdm_config`) plus an input-binding snippet.  It does not modify the profile
and does not enable an FDM in a static viewing package.

## Mapping

Asset frame (Blender/game): X right, Y forward, Z up, metres, seconds, kg.
JSBSim structural/body frame: X forward, Y right, Z down.
Transform for a vector: `[x_struct, y_struct, z_struct] = [y_asset, x_asset, -z_asset]`.
Orientation, moments and inertia are transformed too, not only coordinates.
Mass and inertia are authored in kg / kg*m^2 and JSBSim performs the
slug / lbf / ft conversions internally; this builder's numeric conversions for
forces and clips are explicit in the XML comments and FCS coefficients:
`1 slug = 14.593902937 kg`, `1 ft = 0.3048 m`, `1 lbf = 4.448221615 N`.

## Model

- Gravity compensation: repulsor force in `LOCAL` frame, direction `(0,0,-1)` (up),
  applied at CG.  It does not shrink when the hull pitches.
- Forward throttle: bounded acceleration `forward_accel_m_s2`, converted to lbf.
- Brake: bounded deceleration `brake_accel_m_s2`, not an instant velocity reset.
- Speed cap: smooth reduction of thrust near `speed_limit_m_s`; not a physical
  limit and not a velocity teleport.
- Lateral assistance: bounded damping with time constant
  `velocity_assist_time_s`.
- Vertical assistance: bounded by `vertical_accel_m_s2` through the repulsor clip.
- Body rates: exponential response
  `omega_new = omega_target + (omega_old - omega_target) * exp(-dt/tau)` with
  `tau = rate_response_time_s` (converted deg/s -> rad/s).
- CG origin is `not measured`; origin proxy at `(0,0,0)`.
- Inertia: if measured inertia is absent, an explicit labelled uniform-solid
  bounding-box proxy is generated from measured dimensions or, failing that,
  from `scale_reference_length_m` as a cube.  It is never presented as measured.
- No aerodynamic surfaces: `<aerodynamics/>`.

## Keyboard bindings

Standard project style:

| Key | Action |
|---|---|
| w / s | throttle +/-0.1, clipped 0..1 |
| b | brake on |
| j / l | yaw command -1 / +1 |
| k | stop yaw |

Static input direction is tested (j gives negative yaw command, l positive).

## Pending engine items

These are intentionally marked `pending_engine` in the generated XML:

- Wrapper must knowingly select this adapter instead of conventional FDM.
- `/sim/flight-model` must stay `null` until the wrapper selects it.
- Collision backend is not invented by this builder.
- Measured inertia and CG origin are absent; proxies are labelled.

## Missing measured data

- No measured mass, inertia, CG, drag, lift or stability derivatives exist.
- Low-speed gravity compensation is a gameplay assistant, not a physical
  aerodynamic result.
- Real engine runtime tests, flight tests and collision-backend integration are
  pending.

## Validation

```sh
python3 -m unittest tools.test_build_assisted_fdm -v
```
