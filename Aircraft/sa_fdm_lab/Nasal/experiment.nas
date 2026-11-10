# SPDX-License-Identifier: GPL-2.0-or-later
# Opt-in test driver. Applies commands only; never writes aircraft state.
var root = "/fdm/jsbsim/lab/";
var started = 0;
var initialized = 0;
var initial_time = 0;
var home_lat = 0;
var home_lon = 0;
var target_altitude = 3280.839895013123;
var radians = math.pi / 180;
var clamp = func(x, lo, hi) { return math.max(lo, math.min(hi, x)); }
var number = func(path) {
    var x = getprop(path);
    return x == nil ? 0 : x;
}
var command = func(throttle, brake, yaw) {
    setprop(root ~ "throttle", throttle);
    setprop(root ~ "brake", brake);
    setprop(root ~ "yaw", yaw);
}
var attitude_command = func(aileron, elevator) {
    setprop("/controls/flight/aileron", aileron);
    setprop("/controls/flight/elevator", elevator);
}
var tick = func {
    var t = number("/fdm/jsbsim/simulation/sim-time-sec") - initial_time;
    var north = (number("/position/latitude-deg") - home_lat) * radians * 6371000;
    var east = (number("/position/longitude-deg") - home_lon) * radians * 6371000 * math.cos(home_lat * radians);
    var distance = math.sqrt(north*north + east*east);
    var heading = number("/orientation/heading-deg");
    var phase = "hover";
    # Five-second attitude input, followed by release, tests recovery at hover.
    attitude_command(t >= 10 and t < 15 ? 0.4 : 0,
                     t >= 10 and t < 15 ? -0.3 : 0);
    if (t < 30) {
        command(0, 1, 0);
    } elsif (t < 50) {
        phase = "low-speed"; command(0.02, 0, 0);
    } elsif (t < 75) {
        phase = "accelerate"; command(0.12, 0, 0);
    } elsif (t < 90) {
        phase = "brake"; command(0, 1, 0);
    } elsif (t < 115) {
        phase = "turn";
        var error = 180 - heading;
        if (error > 180) error -= 360;
        if (error < -180) error += 360;
        command(0, 1, clamp(error / 25, -1, 1));
    } elsif (t < 180 and distance > 5) {
        phase = "return";
        var bearing = math.atan2(-east, -north) / radians;
        var error = bearing - heading;
        while (error > 180) error -= 360;
        while (error < -180) error += 360;
        var speed = number("/fdm/jsbsim/velocities/u-fps") * 0.3048;
        var desired = math.min(25, distance * 0.25);
        var thrust = clamp((desired - speed) * 0.08, 0, 0.2);
        if (math.abs(error) > 30) thrust = 0;
        command(thrust, speed > desired + 0.2 ? 1 : 0, clamp(error / 25, -1, 1));
    } else {
        phase = "returned-hover"; command(0, 1, 0);
    }
    # An altitude excursion and recovery also exercise the weight controller.
    setprop(root ~ "altitude-ft", target_altitude + (t >= 155 and t < 170 ? 50 : 0));
    if (t >= 205) phase = "complete";
    print("SA_FDM_SAMPLE,", t, ",", phase, ",",
          number("/position/altitude-ft") * 0.3048, ",",
          number("/velocities/airspeed-kt") * 0.5144444444, ",",
          number("/orientation/roll-deg"), ",", number("/orientation/pitch-deg"), ",",
          heading, ",", north, ",", east, ",", distance, ",",
          number(root ~ "throttle"), ",", number(root ~ "brake"), ",",
          number(root ~ "yaw"), ",", number(root ~ "support-lbs"), ",",
          number("/fdm/jsbsim/velocities/v-down-fps") * 0.3048, ",",
          number("/position/altitude-agl-ft") * 0.3048);
    if (t >= 205) {
        print("SA_FDM_COMPLETE");
        fgcommand("exit");
        return;
    }
    settimer(tick, 1);
}
var start = func {
    if (started or !getprop("/sim/space-arena/fdm-lab/run-test")) return;
    started = 1;
    initial_time = number("/fdm/jsbsim/simulation/sim-time-sec");
    home_lat = number("/position/latitude-deg");
    home_lon = number("/position/longitude-deg");
    print("SA_FDM_HEADER,time_s,phase,altitude_m,airspeed_mps,roll_deg,pitch_deg,heading_deg,north_m,east_m,distance_m,throttle,brake,yaw,support_lbf,down_mps,agl_m");
    tick();
}
var initialize = func {
    if (initialized) return;
    initialized = 1;
    # FG 2024.1.7 trim=0 does not transfer common_init's position into IC.
    # Set native initial conditions ONCE, before measurement/manual flight.
    # No position, velocity, attitude or IC is written by tick().
    setprop("/fdm/jsbsim/ic/lat-geod-deg", number("/sim/presets/latitude-deg"));
    setprop("/fdm/jsbsim/ic/long-gc-deg", number("/sim/presets/longitude-deg"));
    setprop("/fdm/jsbsim/ic/h-sl-ft", number("/sim/presets/altitude-ft"));
    setprop("/fdm/jsbsim/ic/phi-deg", 0);
    setprop("/fdm/jsbsim/ic/theta-deg", 0);
    setprop("/fdm/jsbsim/ic/psi-true-deg", number("/sim/presets/heading-deg"));
    setprop("/fdm/jsbsim/ic/u-fps", 0);
    setprop("/fdm/jsbsim/ic/v-fps", 0);
    setprop("/fdm/jsbsim/ic/w-fps", 0);
    setprop("/fdm/jsbsim/simulation/reset", 1);
    print("SA_FDM_INITIALIZED: native IC reset before flight; trim disabled");
    settimer(start, 2);
}
setlistener("/sim/signals/fdm-initialized", func { settimer(initialize, 0); });
