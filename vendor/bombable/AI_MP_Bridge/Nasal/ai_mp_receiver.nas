###############################################################################
print("AI_MP_Receiver LOADED v2.5");
##
##  AI MP Receiver v2.5
##  Receives AI model positions and scenario name from Office MP broadcaster
##  and updates local proxy AI nodes using FlightGear's built-in interpolate().
##
##  Listens on Office multiplayer node:
##    string[11] - Alpha     string[15] - Echo
##    string[12] - Bravo     string[16] - Foxtrot
##    string[13] - Charlie   string[17] - Golf
##    string[14] - Delta     string[18] - Hotel
##    string[19] - sync: scenario name -> load-scenario then _ensure_proxies
##
##  Position decoding - fixed width 29 chars:
##    chars  0- 8: lat = value / 1000000 - 90
##    chars  9-17: lon = value / 1000000 - 180
##    chars 18-24: alt = value / 10 (feet)
##    chars 25-28: hdg = value / 10 (degrees)
##
##  Interpolation:
##    Uses FGFS built-in interpolate() to smoothly move proxy positions
##    between broadcaster updates. Interpolation time is slightly longer
##    than the broadcast interval so motion is always smooth.
##
##  Copyright (C) 2026 - Aether Project
##
###############################################################################

var AI_MP_Receiver = {};

###############################################################################
# Fixed NATO callsigns - must match broadcaster (8 models)
AI_MP_Receiver.CALLSIGNS = [
    "Alpha", "Bravo", "Charlie", "Delta",
    "Echo",  "Foxtrot", "Golf",  "Hotel"
];

# Constants
AI_MP_Receiver.POS_STRING_FIRST    = 11;
AI_MP_Receiver.SYNC_STRING_IDX     = 19;
AI_MP_Receiver.CHECK_INTERVAL      = 2.0;
AI_MP_Receiver.SERVER_CALLSIGN     = "Office";
AI_MP_Receiver.SCENARIO_LOAD_DELAY = 4.0;

# Interpolation time - slightly longer than broadcaster interval (0.5s)
# so we always have a smooth target to interpolate toward
AI_MP_Receiver.INTERP_TIME        = 0.6;

###############################################################################
# Internal state
AI_MP_Receiver._running         = 0;
AI_MP_Receiver._loopid          = 0;
AI_MP_Receiver._server_node     = nil;
AI_MP_Receiver._proxy_nodes     = {};
AI_MP_Receiver._proxy_paths     = {};   # path strings for interpolate()
AI_MP_Receiver._listeners       = [];
AI_MP_Receiver._sync_listener   = nil;
AI_MP_Receiver._loaded_scenario = "";
AI_MP_Receiver._proxies_ready   = 0;

###############################################################################
# Decode one model from 29-char fixed-width string
AI_MP_Receiver._decode_model = func(str) {
    if (str == nil or size(str) < 29) return nil;
    var ilat = num(substr(str, 0,  9));
    var ilon = num(substr(str, 9,  9));
    var ialt = num(substr(str, 18, 7));
    var ihdg = num(substr(str, 25, 4));
    if (ilat == nil or ilon == nil or ialt == nil or ihdg == nil) return nil;
    if (ilat == 0 and ilon == 0) return nil;
    return {
        lat: ilat / 1000000.0 - 90,
        lon: ilon / 1000000.0 - 180,
        alt: ialt / 10.0,
        hdg: ihdg / 10.0
    };
}

###############################################################################
# Smoothly move proxy to new position using FGFS built-in interpolate()
AI_MP_Receiver._update_proxy = func(callsign, pos) {
    if (!contains(AI_MP_Receiver._proxy_paths, callsign)) return;
    if (!contains(AI_MP_Receiver._proxy_nodes, callsign)) return;
    var p = AI_MP_Receiver._proxy_paths[callsign];
    var m = AI_MP_Receiver._proxy_nodes[callsign];
    var t = AI_MP_Receiver.INTERP_TIME;

    # Detect if this is a ship/ground model (not aircraft)
    var path = m.getPath();
    var is_ship = (substr(path, 0, 16) == "/ai/models/ship[");

    interpolate(p ~ "/position/latitude-deg",        pos.lat, t);
    interpolate(p ~ "/position/longitude-deg",       pos.lon, t);
    interpolate(p ~ "/orientation/true-heading-deg", pos.hdg, t);

    if (is_ship) {
        # Ship/ground models: altitude is terrain-following, don't interpolate
        # Just set it directly (FG may ignore it anyway for ground-hugging models)
        setprop(p ~ "/position/altitude-ft", pos.alt);
    } else {
        interpolate(p ~ "/position/altitude-ft", pos.alt, t);
    }
}

###############################################################################
# Called when a position string arrives from broadcaster
AI_MP_Receiver._on_position_string = func(str_node, model_idx) {
    if (!AI_MP_Receiver._proxies_ready) return;
    var str = str_node.getValue();
    if (str == nil or str == "") return;
    var cs  = AI_MP_Receiver.CALLSIGNS[model_idx];
    var pos = AI_MP_Receiver._decode_model(str);
    if (pos != nil) AI_MP_Receiver._update_proxy(cs, pos);
}

###############################################################################
# Build name->NATO callsign map from scenario XML
# aircraft: matched by callsign, ship: matched by name (FG doesn't set callsign for ships)
AI_MP_Receiver._build_name_map = func(scenario_path) {
    AI_MP_Receiver._name_to_cs   = {};  # name  -> NATO callsign (for ships)
    AI_MP_Receiver._cs_to_cs     = {};  # callsign -> NATO callsign (for aircraft)
    if (scenario_path == nil) return;
    var root = io.read_properties(scenario_path);
    if (root == nil) return;
    var scenario = root.getNode("scenario");
    if (scenario == nil) return;
    foreach (var e; scenario.getChildren("entry")) {
        var cs_node   = e.getNode("callsign");
        var name_node = e.getNode("name");
        var type_node = e.getNode("type");
        if (cs_node == nil) continue;
        var cs   = cs_node.getValue();
        var name = name_node != nil ? name_node.getValue() : nil;
        var type = type_node != nil ? type_node.getValue() : "aircraft";
        # Only care about NATO callsigns
        var is_nato = 0;
        foreach (var valid_cs; AI_MP_Receiver.CALLSIGNS) {
            if (cs == valid_cs) { is_nato = 1; break; }
        }
        if (!is_nato) continue;
        if (type == "ship" and name != nil) {
            AI_MP_Receiver._name_to_cs[name] = cs;
            print("AI_MP_Receiver: ship map: name='", name, "' -> NATO='", cs, "'");
        } else {
            AI_MP_Receiver._cs_to_cs[cs] = cs;
        }
    }
}

###############################################################################
# Find proxy nodes in /ai/models - only after load-scenario completes
# aircraft matched by callsign, ship matched by name (FG limitation)
AI_MP_Receiver._ensure_proxies = func() {
    AI_MP_Receiver._proxy_nodes = {};
    AI_MP_Receiver._proxy_paths = {};

    # Match aircraft by callsign
    foreach (var m; props.globals.getNode("/ai/models").getChildren("aircraft")) {
        var cs_node = m.getNode("callsign");
        if (cs_node == nil) continue;
        var cs = cs_node.getValue();
        foreach (var valid_cs; AI_MP_Receiver.CALLSIGNS) {
            if (cs == valid_cs) {
                AI_MP_Receiver._proxy_nodes[cs] = m;
                AI_MP_Receiver._proxy_paths[cs] = m.getPath();
                print("AI_MP_Receiver: aircraft proxy '", cs, "' at ", m.getPath());
                break;
            }
        }
    }

    # Match ships by name (FG doesn't copy callsign for ship type)
    foreach (var m; props.globals.getNode("/ai/models").getChildren("ship")) {
        var name_node = m.getNode("name");
        if (name_node == nil) continue;
        var name = name_node.getValue();
        if (name == nil) continue;
        # Try direct name match
        if (contains(AI_MP_Receiver._name_to_cs, name)) {
            var nato_cs = AI_MP_Receiver._name_to_cs[name];
            if (!contains(AI_MP_Receiver._proxy_nodes, nato_cs)) {
                AI_MP_Receiver._proxy_nodes[nato_cs] = m;
                AI_MP_Receiver._proxy_paths[nato_cs] = m.getPath();
                print("AI_MP_Receiver: ship proxy '", nato_cs, "' (name='", name, "') at ", m.getPath());
            }
        } else {
            # Fallback: match by callsign node (some FG versions do set it for ship)
            var cs_node = m.getNode("callsign");
            if (cs_node != nil) {
                var cs = cs_node.getValue();
                if (cs != nil) {
                    foreach (var valid_cs; AI_MP_Receiver.CALLSIGNS) {
                        if (cs == valid_cs and !contains(AI_MP_Receiver._proxy_nodes, valid_cs)) {
                            AI_MP_Receiver._proxy_nodes[valid_cs] = m;
                            AI_MP_Receiver._proxy_paths[valid_cs] = m.getPath();
                            print("AI_MP_Receiver: ship proxy (via callsign) '", valid_cs, "' at ", m.getPath());
                            break;
                        }
                    }
                }
            }
        }
    }

    var n = size(keys(AI_MP_Receiver._proxy_nodes));
    print("AI_MP_Receiver: proxies ready: ", n);
    if (n > 0) {
        AI_MP_Receiver._proxies_ready = 1;
    } else {
        print("AI_MP_Receiver: WARNING - no proxy nodes found! Is the scenario loaded?");
    }
}

###############################################################################
# Find scenario path on local machine by name
AI_MP_Receiver._find_scenario_path = func(scenario_name) {
    for (var si = 0; si < 30; si += 1) {
        var name_node = getprop("/sim/ai/scenarios/scenario[" ~ si ~ "]/name");
        if (name_node == nil) break;
        if (name_node == scenario_name) {
            var path_node = getprop("/sim/ai/scenarios/scenario[" ~ si ~ "]/path");
            print("AI_MP_Receiver: found scenario '", scenario_name, "' at index ", si);
            return path_node;
        }
    }
    print("AI_MP_Receiver: WARNING - scenario '", scenario_name, "' not found locally!");
    return nil;
}

###############################################################################
# Decode sync string -> load scenario -> find proxies
AI_MP_Receiver._decode_sync = func(scenario_name) {
    if (scenario_name == nil or scenario_name == "") return;
    if (scenario_name == AI_MP_Receiver._loaded_scenario) return;

    var path = AI_MP_Receiver._find_scenario_path(scenario_name);
    if (path == nil) {
        print("AI_MP_Receiver: cannot load scenario '", scenario_name,
              "' - not found locally. Make sure the scenario file is installed.");
        return;
    }

    print("AI_MP_Receiver: loading scenario '", scenario_name, "'");
    AI_MP_Receiver._loaded_scenario = scenario_name;
    AI_MP_Receiver._proxies_ready   = 0;
    AI_MP_Receiver._proxy_nodes     = {};
    AI_MP_Receiver._proxy_paths     = {};
    # Build name->NATO map from XML before load (ships need this)
    AI_MP_Receiver._build_name_map(path);
    fgcommand("load-scenario", props.Node.new({"name": scenario_name}));
    settimer(func {
        AI_MP_Receiver._ensure_proxies();
    }, AI_MP_Receiver.SCENARIO_LOAD_DELAY);
}

###############################################################################
# Find Office in MP list
AI_MP_Receiver._find_server = func() {
    var mp_list = props.globals.getNode("/ai/models").getChildren("multiplayer");
    foreach (var p; mp_list) {
        var cs = p.getNode("callsign");
        if (cs != nil and cs.getValue() == AI_MP_Receiver.SERVER_CALLSIGN) {
            return p;
        }
    }
    return nil;
}

###############################################################################
# Set up listeners - one per model + sync
AI_MP_Receiver._setup_listeners = func(server_node) {
    AI_MP_Receiver._clear_listeners();
    var base = server_node.getPath();
    print("AI_MP_Receiver: setting up listeners on ", base);

    for (var i = 0; i < size(AI_MP_Receiver.CALLSIGNS); i += 1) {
        var idx      = AI_MP_Receiver.POS_STRING_FIRST + i;
        var str_path = base ~ "/sim/multiplay/generic/string[" ~ idx ~ "]";
        props.globals.getNode(str_path, 1);
        var lid = setlistener(str_path,
            (func(midx) { return func(n) {
                AI_MP_Receiver._on_position_string(n, midx);
            };})(i),
        0, 0);
        append(AI_MP_Receiver._listeners, lid);
    }

    var sync_path = base ~ "/sim/multiplay/generic/string[" ~
                    AI_MP_Receiver.SYNC_STRING_IDX ~ "]";
    props.globals.getNode(sync_path, 1);
    AI_MP_Receiver._sync_listener = setlistener(sync_path, func(n) {
        AI_MP_Receiver._decode_sync(n.getValue());
    }, 1, 0);

    print("AI_MP_Receiver: all listeners active.");
}

###############################################################################
# Clear listeners
AI_MP_Receiver._clear_listeners = func() {
    foreach (var lid; AI_MP_Receiver._listeners) {
        removelistener(lid);
    }
    AI_MP_Receiver._listeners = [];
    if (AI_MP_Receiver._sync_listener != nil) {
        removelistener(AI_MP_Receiver._sync_listener);
        AI_MP_Receiver._sync_listener = nil;
    }
}

###############################################################################
# Watch loop - looks for Office in MP list every CHECK_INTERVAL seconds
AI_MP_Receiver._watch_loop = func(id) {
    if (!AI_MP_Receiver._running) return;
    if (id != AI_MP_Receiver._loopid) return;

    var server = AI_MP_Receiver._find_server();

    if (server != nil and AI_MP_Receiver._server_node == nil) {
        print("AI_MP_Receiver: Office found! Setting up listeners...");
        AI_MP_Receiver._server_node = server;
        AI_MP_Receiver._setup_listeners(server);

    } elsif (server == nil and AI_MP_Receiver._server_node != nil) {
        print("AI_MP_Receiver: Office disconnected.");
        AI_MP_Receiver._server_node = nil;
        AI_MP_Receiver._proxies_ready = 0;
        AI_MP_Receiver._clear_listeners();
    }

    settimer(func { AI_MP_Receiver._watch_loop(id); },
             AI_MP_Receiver.CHECK_INTERVAL);
}

###############################################################################
# Start
AI_MP_Receiver.start = func() {
    if (AI_MP_Receiver._running) {
        print("AI_MP_Receiver: already running."); return;
    }
    AI_MP_Receiver._running = 1;
    AI_MP_Receiver._loopid += 1;
    settimer(func { AI_MP_Receiver._watch_loop(AI_MP_Receiver._loopid); },
             AI_MP_Receiver.CHECK_INTERVAL);
    print("AI_MP_Receiver: started. Watching for Office...");
}

###############################################################################
# Stop
AI_MP_Receiver.stop = func() {
    AI_MP_Receiver._running = 0;
    AI_MP_Receiver._loopid += 1;
    AI_MP_Receiver._clear_listeners();
    AI_MP_Receiver._server_node   = nil;
    AI_MP_Receiver._proxies_ready = 0;
    print("AI_MP_Receiver: stopped.");
}

###############################################################################
# Auto-start
AI_MP_Receiver.start();
