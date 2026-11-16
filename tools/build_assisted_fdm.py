#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Deterministic builder for the separate TIE assisted (game) flight adapter.

This is NOT Bombable's conventional aerodynamic FDM.  It translates one
``flight_profile.json`` (status GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS)
into a JSBSim FDM XML snippet plus input-binding snippet.  The builder never
changes the source profile and never enables an FDM in a static viewing package.

Stdlib only.  Usage:

    python3 tools/build_assisted_fdm.py --ship-dir PATH [--integration FILE] \
        --output OUT.xml
    python3 tools/build_assisted_fdm.py --ship-dir PATH --output OUT.xml --check

A positional form is also accepted:

    python3 tools/build_assisted_fdm.py PATH [INTEGRATION.json] --output OUT.xml
"""

import argparse
import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape

SLUGS_PER_KG_FACTOR = 1.0 / 14.5939029372064  # kg -> slug
FT_PER_M = 1.0 / 0.3048
LBF_PER_N = 1.0 / 4.4482216152605
RAD_PER_DEG = math.pi / 180.0
G0_FT_S2 = 32.174  # standard gravity used by sa_fdm_lab

REQUIRED_PROFILE_KEYS = {
    "schema_version",
    "ship_id",
    "status",
    "frame",
    "scale_reference_length_m",
    "mass_gameplay_kg",
    "mass_status",
    "assisted_flight",
}
REQUIRED_FRAME_KEYS = {"x", "y", "z", "units"}
REQUIRED_ASSISTED_KEYS = {
    "speed_limit_m_s",
    "forward_accel_m_s2",
    "brake_accel_m_s2",
    "lateral_accel_m_s2",
    "vertical_accel_m_s2",
    "max_body_rates_deg_s",
    "rate_response_time_s",
    "velocity_assist_time_s",
    "gravity_compensation",
}
REQUIRED_RATE_KEYS = {"pitch_about_x", "yaw_about_z", "roll_about_y"}


class BuildError(ValueError):
    """Raised for invalid input data."""


def _finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildError(f"{label} must be a finite number")
    if not math.isfinite(value):
        raise BuildError(f"{label} must be finite")
    return float(value)


def _positive(value, label):
    v = _finite_number(value, label)
    if v <= 0:
        raise BuildError(f"{label} must be > 0")
    return v


def load_profile(ship_dir):
    ship_dir = Path(ship_dir)
    if not ship_dir.is_dir():
        raise BuildError(f"ship directory does not exist: {ship_dir}")
    profile_path = ship_dir / "flight_profile.json"
    if not profile_path.is_file():
        raise BuildError(f"missing flight_profile.json in {ship_dir}")
    try:
        with profile_path.open("r", encoding="utf-8") as f:
            profile = json.load(f)
    except json.JSONDecodeError as exc:
        raise BuildError(f"invalid JSON in {profile_path}: {exc}") from exc
    except OSError as exc:
        raise BuildError(f"cannot read {profile_path}: {exc}") from exc
    if not isinstance(profile, dict):
        raise BuildError("flight_profile.json must contain a JSON object")
    missing = REQUIRED_PROFILE_KEYS - set(profile.keys())
    if missing:
        raise BuildError(f"flight_profile.json missing keys: {sorted(missing)}")
    extra = set(profile.keys()) - REQUIRED_PROFILE_KEYS
    # Allow additive keys, but reject obvious schema mismatch by version.
    if profile.get("schema_version") != 1:
        raise BuildError("unsupported schema_version (expected 1)")
    if profile.get("status") != "GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS":
        raise BuildError("unexpected profile status")
    if profile.get("ship_id") != ship_dir.name:
        raise BuildError(f"ship_id {profile.get('ship_id')!r} does not match directory {ship_dir.name!r}")
    frame = profile["frame"]
    if not isinstance(frame, dict) or set(frame.keys()) != REQUIRED_FRAME_KEYS:
        raise BuildError("frame must contain exactly x, y, z, units")
    if frame["x"] != "right" or frame["y"] != "forward" or frame["z"] != "up":
        raise BuildError("unexpected frame axes (expected right/forward/up)")
    if frame["units"] != "SI; angles explicitly deg":
        raise BuildError("unexpected frame units declaration")
    scale = _positive(profile["scale_reference_length_m"], "scale_reference_length_m")
    mass = _positive(profile["mass_gameplay_kg"], "mass_gameplay_kg")
    if not isinstance(profile["mass_status"], str) or not profile["mass_status"]:
        raise BuildError("mass_status must be a nonempty string")

    assisted = profile["assisted_flight"]
    if not isinstance(assisted, dict):
        raise BuildError("assisted_flight must be an object")
    missing_a = REQUIRED_ASSISTED_KEYS - set(assisted.keys())
    if missing_a:
        raise BuildError(f"assisted_flight missing keys: {sorted(missing_a)}")
    speed_limit = _positive(assisted["speed_limit_m_s"], "speed_limit_m_s")
    forward_accel = _positive(assisted["forward_accel_m_s2"], "forward_accel_m_s2")
    brake_accel = _positive(assisted["brake_accel_m_s2"], "brake_accel_m_s2")
    lateral_accel = _positive(assisted["lateral_accel_m_s2"], "lateral_accel_m_s2")
    vertical_accel = _positive(assisted["vertical_accel_m_s2"], "vertical_accel_m_s2")
    rates = assisted["max_body_rates_deg_s"]
    if not isinstance(rates, dict) or set(rates.keys()) != REQUIRED_RATE_KEYS:
        raise BuildError("max_body_rates_deg_s must contain pitch_about_x, yaw_about_z, roll_about_y")
    pitch_rate = _positive(rates["pitch_about_x"], "pitch_about_x")
    yaw_rate = _positive(rates["yaw_about_z"], "yaw_about_z")
    roll_rate = _positive(rates["roll_about_y"], "roll_about_y")
    tau = _positive(assisted["rate_response_time_s"], "rate_response_time_s")
    vel_tau = _positive(assisted["velocity_assist_time_s"], "velocity_assist_time_s")
    if not isinstance(assisted["gravity_compensation"], bool):
        raise BuildError("gravity_compensation must be boolean")

    return {
        "ship_dir": ship_dir,
        "profile": profile,
        "scale_m": scale,
        "mass_kg": mass,
        "speed_limit_m_s": speed_limit,
        "forward_accel_m_s2": forward_accel,
        "brake_accel_m_s2": brake_accel,
        "lateral_accel_m_s2": lateral_accel,
        "vertical_accel_m_s2": vertical_accel,
        "pitch_rate_deg_s": pitch_rate,
        "yaw_rate_deg_s": yaw_rate,
        "roll_rate_deg_s": roll_rate,
        "rate_tau_s": tau,
        "velocity_assist_tau_s": vel_tau,
        "gravity_compensation": assisted["gravity_compensation"],
    }


def load_integration(path):
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        raise BuildError(f"integration file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise BuildError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BuildError("integration.json must contain a JSON object")
    return data


def _integration_dims_m(integration):
    if not integration:
        return None
    candidates = []
    for key in ("measured_dimensions_m", "dimensions_m", "bounding_box_m", "size_m"):
        value = integration.get(key)
        if isinstance(value, dict):
            # accept x/y/z or length/width/height style
            if all(k in value for k in ("x", "y", "z")):
                candidates.append((value["x"], value["y"], value["z"]))
            elif all(k in value for k in ("length", "width", "height")):
                candidates.append((value["width"], value["length"], value["height"]))
            elif all(k in value for k in ("length_m", "width_m", "height_m")):
                candidates.append((value["width_m"], value["length_m"], value["height_m"]))
    if not candidates:
        return None
    dims = candidates[0]
    return tuple(_positive(d, f"integration dimension {i}") for i, d in enumerate(dims))


def _integration_inertia(integration):
    if not integration:
        return None
    for key in ("inertia_kg_m2", "measured_inertia_kg_m2", "inertia"):
        value = integration.get(key)
        if isinstance(value, dict) and all(k in value for k in ("ixx", "iyy", "izz")):
            try:
                return (
                    _positive(value["ixx"], "ixx"),
                    _positive(value["iyy"], "iyy"),
                    _positive(value["izz"], "izz"),
                )
            except BuildError:
                raise
    return None


def _integration_units_ok(integration):
    if not integration:
        return
    for key in ("units", "length_units", "dimension_units"):
        if key in integration:
            val = integration[key]
            if not isinstance(val, str) or "m" not in val.lower():
                raise BuildError(f"integration {key} must declare metres")
    # nested
    for key in ("measured_dimensions_m", "dimensions_m", "bounding_box_m", "size_m"):
        value = integration.get(key)
        if isinstance(value, dict) and "units" in value:
            if not isinstance(value["units"], str) or "m" not in value["units"].lower():
                raise BuildError(f"integration {key}.units must declare metres")


def compute_inertia(model, integration):
    measured = _integration_inertia(integration)
    if measured is not None:
        return measured, "measured integration.json (status: measured)"
    # proxy from measured dims if available, else scale_reference_length_m cube
    dims = _integration_dims_m(integration)
    if dims is None:
        s = model["scale_m"]
        dims = (s, s, s)
        source = "proxy: uniform solid cube from scale_reference_length_m (NOT measured inertia)"
    else:
        source = "proxy: uniform solid box from measured integration dimensions (NOT measured inertia)"
    # dims are asset frame: (right, forward, up) -> structural (forward, right, down)
    asset_x, asset_y, asset_z = dims  # right, forward, up
    # structural axes: x=forward (asset_y), y=right (asset_x), z=down (asset_z)
    sx, sy, sz = asset_y, asset_x, asset_z
    mass = model["mass_kg"]
    ixx = mass * (sy * sy + sz * sz) / 12.0  # about forward
    iyy = mass * (sx * sx + sz * sz) / 12.0  # about right
    izz = mass * (sx * sx + sy * sy) / 12.0  # about down
    return (ixx, iyy, izz), source


def fmt(x):
    # Stable, finite, enough precision.
    if isinstance(x, float):
        if not math.isfinite(x):
            raise BuildError("non-finite value during formatting")
        return repr(x)
    return str(x)


def build(ship_dir, integration=None, output=None, **kwargs):
    """Build XML text. ``integration`` may be a path or dict or None."""
    # Accept common kw names
    if "integration_path" in kwargs and integration is None:
        integration = kwargs.pop("integration_path")
    if "measured_integration" in kwargs and integration is None:
        integration = kwargs.pop("measured_integration")
    if "output_path" in kwargs and output is None:
        output = kwargs.pop("output_path")
    if kwargs:
        raise BuildError(f"unexpected keyword arguments: {sorted(kwargs)}")

    model = load_profile(ship_dir)
    if isinstance(integration, (str, Path)):
        integration = load_integration(integration)
    elif integration is not None and not isinstance(integration, dict):
        raise BuildError("integration must be a path or dict")
    if integration is not None:
        _integration_units_ok(integration)

    inertia, inertia_source = compute_inertia(model, integration)
    ixx_kg, iyy_kg, izz_kg = inertia

    mass_kg = model["mass_kg"]
    mass_slugs = mass_kg * SLUGS_PER_KG_FACTOR
    ixx_slugs = ixx_kg * SLUGS_PER_KG_FACTOR / (FT_PER_M ** -2)  # kg*m2 -> slug*ft2? 
    # Correct conversion: 1 kg*m2 = (1/14.5939) slug * (1/0.3048)^2 ft2
    ixx_slugs = ixx_kg * SLUGS_PER_KG_FACTOR * (FT_PER_M ** 2)
    iyy_slugs = iyy_kg * SLUGS_PER_KG_FACTOR * (FT_PER_M ** 2)
    izz_slugs = izz_kg * SLUGS_PER_KG_FACTOR * (FT_PER_M ** 2)

    speed_limit_fps = model["speed_limit_m_s"] * FT_PER_M
    forward_accel_fps2 = model["forward_accel_m_s2"] * FT_PER_M
    brake_accel_fps2 = model["brake_accel_m_s2"] * FT_PER_M
    lateral_accel_fps2 = model["lateral_accel_m_s2"] * FT_PER_M
    vertical_accel_fps2 = model["vertical_accel_m_s2"] * FT_PER_M
    pitch_rate_rad_s = model["pitch_rate_deg_s"] * RAD_PER_DEG
    yaw_rate_rad_s = model["yaw_rate_deg_s"] * RAD_PER_DEG
    roll_rate_rad_s = model["roll_rate_deg_s"] * RAD_PER_DEG
    tau = model["rate_tau_s"]
    vel_tau = model["velocity_assist_tau_s"]

    thrust_lbf = mass_kg * model["forward_accel_m_s2"] * LBF_PER_N
    brake_lbf = mass_kg * model["brake_accel_m_s2"] * LBF_PER_N
    lateral_force_lbf = mass_kg * model["lateral_accel_m_s2"] * LBF_PER_N
    # clip for lateral damping force uses mass*accel
    lateral_damping_lbf = mass_slugs * (1.0 / vel_tau) * speed_limit_fps  # not used as clip
    # clip magnitudes:
    lateral_clip_lbf = mass_kg * model["lateral_accel_m_s2"] * LBF_PER_N
    vertical_extra_ft_s2 = vertical_accel_fps2

    ship_id = model["profile"]["ship_id"]
    mass_status = model["profile"]["mass_status"]

    # Body-rate moment clips: I * max_rate / tau (slug*ft2 * rad/s / s = lbf*ft)
    roll_moment_clip = ixx_slugs * roll_rate_rad_s / tau
    pitch_moment_clip = iyy_slugs * pitch_rate_rad_s / tau
    yaw_moment_clip = izz_slugs * yaw_rate_rad_s / tau

    cg_status = "not measured (geometry/CG origin proxy at (0,0,0))"
    dims_status = "scale_reference_length_m" if _integration_dims_m(integration) is None else "measured integration dimensions"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- Generated by tools/build_assisted_fdm.py - DO NOT EDIT BY HAND -->
<!-- Profile status: {escape(model['profile']['status'])} -->
<!-- GAMEPLAY_ESTIMATE_NOT_CANON_OR_REAL_AERODYNAMICS -->
<!-- Separate assisted adapter: use EITHER this or conventional aerodynamics, never both. -->
<!-- Coordinate mapping asset (X right, Y forward, Z up) -> structural/body (X forward, Y right, Z down): [Y, X, -Z] -->
<!-- CG origin status: {cg_status} -->
<!-- Inertia status: {escape(inertia_source)} -->
<!-- Mass status: {escape(mass_status)} -->
<!-- Dimensions source: {dims_status} -->
<!-- No lore constants (thousands-of-g / MGLT conversions) are used; all values derive from profile JSON and unit conversions. -->
<fdm_config name="assisted_{escape(ship_id)}" version="2.0" release="ALPHA">
  <fileheader>
    <author>Space Arena contributors; assisted FDM adapter (generated)</author>
    <filecreationdate>2026-11-14</filecreationdate>
    <description>Assisted finite-control adapter for {escape(ship_id)}; gameplay estimate, not canon/real aerodynamics.</description>
  </fileheader>
  <metrics>
    <wingarea unit="M2">1</wingarea><wingspan unit="M">{fmt(model['scale_m'])}</wingspan><chord unit="M">{fmt(model['scale_m'])}</chord>
    <location name="AERORP" unit="M"><x>0</x><y>0</y><z>0</z></location>
    <location name="EYEPOINT" unit="M"><x>0</x><y>0</y><z>0</z></location>
    <location name="VRP" unit="M"><x>0</x><y>0</y><z>0</z></location>
  </metrics>
  <mass_balance>
    <ixx unit="KG*M2">{fmt(ixx_kg)}</ixx>
    <iyy unit="KG*M2">{fmt(iyy_kg)}</iyy>
    <izz unit="KG*M2">{fmt(izz_kg)}</izz>
    <emptywt unit="KG">{fmt(mass_kg)}</emptywt>
    <location name="CG" unit="M"><x>0</x><y>0</y><z>0</z></location>
  </mass_balance>
  <external_reactions>
    <force name="repulsor" frame="LOCAL">
      <location unit="M"><x>0</x><y>0</y><z>0</z></location>
      <direction><x>0</x><y>0</y><z>-1</z></direction>
    </force>
    <force name="drive" frame="BODY">
      <location unit="M"><x>0</x><y>0</y><z>0</z></location>
      <direction><x>1</x><y>0</y><z>0</z></direction>
    </force>
    <force name="lateral-assist" frame="BODY">
      <location unit="M"><x>0</x><y>0</y><z>0</z></location>
      <direction><x>0</x><y>1</y><z>0</z></direction>
    </force>
    <moment name="roll-assist" frame="BODY"><direction><x>1</x><y>0</y><z>0</z></direction></moment>
    <moment name="pitch-assist" frame="BODY"><direction><x>0</x><y>1</y><z>0</z></direction></moment>
    <moment name="yaw-assist" frame="BODY"><direction><x>0</x><y>0</y><z>1</z></direction></moment>
  </external_reactions>
  <flight_control name="Assisted finite-control adapter">
    <property value="1">lab/support</property>
    <property value="1">lab/stabilize</property>
    <property value="{fmt(3280.839895013123)}">lab/altitude-ft</property>
    <property value="0">lab/throttle</property>
    <property value="0">lab/brake</property>
    <property value="0">lab/yaw</property>
    <property value="{fmt(model['rate_tau_s'])}">lab/rate-tau-s</property>
    <property value="{fmt(model['velocity_assist_tau_s'])}">lab/velocity-assist-tau-s</property>
    <property value="{fmt(model['speed_limit_m_s'])}">lab/speed-limit-m-s</property>
    <property value="{fmt(1 if model['gravity_compensation'] else 0)}">lab/gravity-compensation</property>
    <channel name="Bounded commands">
      <pure_gain name="lab/throttle-limited"><input>lab/throttle</input><gain>1</gain><clipto><min>0</min><max>1</max></clipto></pure_gain>
      <pure_gain name="lab/brake-limited"><input>lab/brake</input><gain>1</gain><clipto><min>0</min><max>1</max></clipto></pure_gain>
      <pure_gain name="lab/yaw-limited"><input>lab/yaw</input><gain>1</gain><clipto><min>-1</min><max>1</max></clipto></pure_gain>
    </channel>
    <channel name="Weight support in local vertical">
      <fcs_function name="lab/support-accel">
        <function><sum><value>{fmt(G0_FT_S2)}</value>
          <product><value>0.8</value><difference><property>lab/altitude-ft</property><property>position/h-sl-ft</property></difference></product>
          <product><value>1.8</value><property>velocities/v-down-fps</property></product>
        </sum></function><clipto><min>0</min><max>{fmt(G0_FT_S2 + vertical_extra_ft_s2)}</max></clipto>
      </fcs_function>
      <fcs_function name="lab/support-lbs">
        <function><product><property>lab/support</property><property>inertia/mass-slugs</property><property>lab/support-accel</property></product></function>
        <output>external_reactions/repulsor/magnitude</output>
      </fcs_function>
    </channel>
    <channel name="Longitudinal drive and brake">
      <fcs_function name="lab/speed-scale">
        <function><max><min><value>1</value>
          <difference><value>1</value>
            <quotient><pow><property>velocities/u-fps</property><value>2</value></pow>
                      <pow><value>{fmt(speed_limit_fps)}</value><value>2</value></pow></quotient>
          </difference>
        </min><value>0</value></max></function>
      </fcs_function>
      <fcs_function name="lab/drive-lbs">
        <function><difference>
          <product><value>{fmt(thrust_lbf)}</value><property>lab/throttle-limited</property><property>lab/speed-scale</property></product>
          <product><value>{fmt(brake_lbf)}</value><property>lab/brake-limited</property></product>
        </difference></function><clipto><min>{fmt(-brake_lbf)}</min><max>{fmt(thrust_lbf)}</max></clipto>
        <output>external_reactions/drive/magnitude</output>
      </fcs_function>
      <fcs_function name="lab/lateral-lbs">
        <function><product><value>{fmt(-mass_slugs / vel_tau)}</value><property>lab/stabilize</property><property>velocities/v-fps</property></product></function>
        <clipto><min>{fmt(-lateral_clip_lbf)}</min><max>{fmt(lateral_clip_lbf)}</max></clipto>
        <output>external_reactions/lateral-assist/magnitude</output>
      </fcs_function>
    </channel>
    <channel name="Attitude and yaw rate control">
      <fcs_function name="lab/roll-lbsft">
        <function><product><property>lab/stabilize</property><property>inertia/ixx-slugs_ft2</property>
          <quotient><difference>
            <product><value>{fmt(roll_rate_rad_s)}</value><property>fcs/aileron-cmd-norm</property></product>
            <property>velocities/p-rad_sec</property>
          </difference><value>{fmt(tau)}</value></quotient></product></function>
        <clipto><min>{fmt(-roll_moment_clip)}</min><max>{fmt(roll_moment_clip)}</max></clipto>
        <output>external_reactions/roll-assist/magnitude-lbsft</output>
      </fcs_function>
      <fcs_function name="lab/pitch-lbsft">
        <function><product><property>lab/stabilize</property><property>inertia/iyy-slugs_ft2</property>
          <quotient><difference>
            <product><value>{fmt(pitch_rate_rad_s)}</value><property>fcs/elevator-cmd-norm</property></product>
            <property>velocities/q-rad_sec</property>
          </difference><value>{fmt(tau)}</value></quotient></product></function>
        <clipto><min>{fmt(-pitch_moment_clip)}</min><max>{fmt(pitch_moment_clip)}</max></clipto>
        <output>external_reactions/pitch-assist/magnitude-lbsft</output>
      </fcs_function>
      <fcs_function name="lab/yaw-lbsft">
        <function><product><property>lab/stabilize</property><property>inertia/izz-slugs_ft2</property>
          <quotient><difference>
            <product><value>{fmt(yaw_rate_rad_s)}</value><property>lab/yaw-limited</property></product>
            <property>velocities/r-rad_sec</property>
          </difference><value>{fmt(tau)}</value></quotient></product></function>
        <clipto><min>{fmt(-yaw_moment_clip)}</min><max>{fmt(yaw_moment_clip)}</max></clipto>
        <output>external_reactions/yaw-assist/magnitude-lbsft</output>
      </fcs_function>
    </channel>
  </flight_control>
  <!-- No lifting surfaces: assisted controller replaces conventional aerodynamics. -->
  <aerodynamics/>
  <input_bindings>
    <!-- Standard project keyboard style; static direction is tested, not just key names. -->
    <keyboard>
      <key name="w"><desc>Increase throttle</desc><binding><command>property-adjust</command><property>/fdm/jsbsim/lab/throttle</property><step>0.1</step><min>0</min><max>1</max></binding></key>
      <key name="s"><desc>Decrease throttle</desc><binding><command>property-adjust</command><property>/fdm/jsbsim/lab/throttle</property><step>-0.1</step><min>0</min><max>1</max></binding></key>
      <key name="b"><desc>Brake</desc><binding><command>property-assign</command><property>/fdm/jsbsim/lab/brake</property><value>1</value></binding></key>
      <key name="j"><desc>Yaw left (negative yaw command)</desc><binding><command>property-assign</command><property>/fdm/jsbsim/lab/yaw</property><value>-1</value></binding></key>
      <key name="l"><desc>Yaw right (positive yaw command)</desc><binding><command>property-assign</command><property>/fdm/jsbsim/lab/yaw</property><value>1</value></binding></key>
      <key name="k"><desc>Stop yaw</desc><binding><command>property-assign</command><property>/fdm/jsbsim/lab/yaw</property><value>0</value></binding></key>
    </keyboard>
  </input_bindings>
  <pending_engine>
    <item binding="/sim/space-arena/fdm/assisted/select" status="pending_engine">Wrapper must knowingly select this adapter instead of conventional FDM.</item>
    <item binding="/sim/flight-model" status="pending_engine">Must remain null until wrapper selects adapter; do not enable from this snippet.</item>
    <item binding="collision-backend" status="pending_engine">Collision backend is not invented by this builder.</item>
    <item binding="measured inertia" status="pending_engine">Measured inertia absent; proxy above is labelled and not measured.</item>
    <item binding="CG origin" status="pending_engine">Geometry CG origin not measured; origin proxy used.</item>
  </pending_engine>
</fdm_config>
"""
    if output is not None:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(xml, encoding="utf-8", newline="\n")
    return xml


def build_assisted_fdm(*args, **kwargs):
    # Flexible aliases
    # Support build_assisted_fdm(ship_dir, integration=None, output=None)
    return build(*args, **kwargs)


def check_output(ship_dir, integration, output_path):
    expected = build(ship_dir, integration)
    actual = Path(output_path).read_text(encoding="utf-8")
    return expected == actual


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build assisted FDM XML from a TIE flight profile.")
    parser.add_argument("ship_dir", nargs="?", help="ship directory containing flight_profile.json")
    parser.add_argument("integration", nargs="?", help="optional measured integration.json")
    parser.add_argument("--ship-dir", dest="ship_dir_opt", help="ship directory containing flight_profile.json")
    parser.add_argument("--integration", dest="integration_opt", help="optional measured integration.json")
    parser.add_argument("--measured-integration", dest="integration_opt2", help="alias for --integration")
    parser.add_argument("--output", "-o", dest="output", help="output XML path")
    parser.add_argument("--check", dest="check", nargs="?", const=True, help="check output is up to date")
    args = parser.parse_args(argv)

    ship_dir = args.ship_dir_opt or args.ship_dir
    integration = args.integration_opt or args.integration_opt2 or args.integration
    if not ship_dir:
        parser.error("ship_dir is required")
    output = args.output
    check_target = None
    if args.check is True:
        if not output:
            parser.error("--check requires --output")
        check_target = output
    elif args.check:
        check_target = args.check
        if not output and not check_target:
            parser.error("output path required")
    try:
        if check_target is not None:
            target = check_target if check_target is not True else output
            ok = check_output(ship_dir, integration, target)
            if ok:
                print(f"OK: {target} is up to date")
                return 0
            print(f"STALE: {target} differs from generated output", file=sys.stderr)
            return 1
        xml = build(ship_dir, integration, output)
        if output:
            print(f"wrote {output}")
        else:
            sys.stdout.write(xml)
        return 0
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
