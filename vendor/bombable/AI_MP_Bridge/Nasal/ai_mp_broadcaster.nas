###############################################################################
print("AI_MP_Broadcaster LOADED v2.2");
##
##  AI MP Broadcaster v2.2
##  Broadcasts AI scenario model positions and scenario name
##  over MP so other players can see and interact with AI models.
##
##  AI models must use these fixed callsigns in the scenario XML:
##    Alpha, Bravo, Charlie, Delta, Echo, Foxtrot, Golf, Hotel
##
##  Uses:
##    string[11] - Alpha     string[15] - Echo
##    string[12] - Bravo     string[16] - Foxtrot
##    string[13] - Charlie   string[17] - Golf
##    string[14] - Delta     string[18] - Hotel
##    string[19] - sync: scenario name (auto-detected, max 29 chars)
##
##  Position encoding - 29 chars, digits only:
##    lat: (lat + 90)  * 1000000 -> 9 digits
##    lon: (lon + 180) * 1000000 -> 9 digits
##    alt: alt_ft * 10           -> 7 digits
##    hdg: hdg * 10              -> 4 digits
##
##  Copyright (C) 2026 - Aether Project
##
###############################################################################

var AI_MP_Broadcaster = {};

###############################################################################
# Fixed NATO callsigns - must match scenario XML (8 models)
AI_MP_Broadcaster.CALLSIGNS = [
    "Alpha", "Bravo", "Charlie", "Delta",
    "Echo",  "Foxtrot", "Golf",  "Hotel"
];

# Constants
AI_MP_Broadcaster.POS_STRING_BASE  = "/sim/multiplay/generic/string[";
AI_MP_Broadcaster.POS_STRING_FIRST = 11;   # string[11..18] = positions
AI_MP_Broadcaster.SYNC_STRING_IDX  = 19;   # string[19] = scenario name
AI_MP_Broadcaster.POS_INTERVAL_S   = 0.5;
AI_MP_Broadcaster.SYNC_INTERVAL_S  = 10.0; # send sync every 10s (was 60s) for faster late-joiner pickup

###############################################################################
# Internal state
AI_MP_Broadcaster._loopid         = 0;
AI_MP_Broadcaster._sync_loopid    = 0;
AI_MP_Broadcaster._running        = 0;
AI_MP_Broadcaster._ai_nodes       = {};
AI_MP_Broadcaster._active_scenario = "";

###############################################################################
# Auto-detect which scenario contains our NATO callsigns
# Returns scenario name string or "" if not found
AI_MP_Broadcaster._find_active_scenario = func() {
    for (var si = 0; si < 30; si += 1) {
        var name_node = getprop("/sim/ai/scenarios/scenario[" ~ si ~ "]/name");
        if (name_node == nil) break;
        var path_node = getprop("/sim/ai/scenarios/scenario[" ~ si ~ "]/path");
        if (path_node == nil) continue;
        var root = io.read_properties(path_node);
        if (root == nil) continue;
        var scenario = root.getNode("scenario");
        if (scenario == nil) continue;
        var entries = scenario.getChildren("entry");
        foreach (var e; entries) {
            var cs_node = e.getNode("callsign");
            if (cs_node == nil) continue;
            var cs = cs_node.getValue();
            foreach (var valid_cs; AI_MP_Broadcaster.CALLSIGNS) {
                if (cs == valid_cs) {
                    print("AI_MP_Broadcaster: auto-detected scenario '", name_node, "' at index ", si);
                    return name_node;
                }
            }
        }
    }
    print("AI_MP_Broadcaster: WARNING - no scenario with NATO callsigns found!");
    return "";
}

###############################################################################
# Encode one model position - fixed width 29 chars, digits only
AI_MP_Broadcaster._encode_model = func(lat, lon, alt, heading) {
    var ilat = int((lat + 90)  * 1000000);
    var ilon = int((lon + 180) * 1000000);
    var ialt = int(alt * 10);
    var ihdg = int(heading * 10);
    if (ilat < 0) ilat = 0;
    if (ilat > 179999999) ilat = 179999999;
    if (ilon < 0) ilon = 0;
    if (ilon > 359999999) ilon = 359999999;
    if (ialt < 0) ialt = 0;
    if (ialt > 9999999) ialt = 9999999;
    if (ihdg < 0) ihdg = 0;
    if (ihdg > 3600) ihdg = 3600;
    return sprintf("%09d%09d%07d%04d", ilat, ilon, ialt, ihdg);
}

###############################################################################
# Get encoded position of one model by callsign
AI_MP_Broadcaster._get_pos = func(callsign) {
    if (!contains(AI_MP_Broadcaster._ai_nodes, callsign))
        return sprintf("%029d", 0);
    var m = AI_MP_Broadcaster._ai_nodes[callsign];
    if (m == nil) return sprintf("%029d", 0);
    var lat = 0; var lon = 0; var alt = 0; var heading = 0;
    var n = nil;
    n = m.getNode("position/latitude-deg");
    if (n != nil) lat = n.getValue();
    n = m.getNode("position/longitude-deg");
    if (n != nil) lon = n.getValue();
    n = m.getNode("position/altitude-ft");
    if (n != nil) alt = n.getValue();
    n = m.getNode("orientation/true-heading-deg");
    if (n != nil) heading = n.getValue();
    return AI_MP_Broadcaster._encode_model(lat, lon, alt, heading);
}

###############################################################################
# Cache AI model nodes by callsign - ignores proxy nodes (100+)
AI_MP_Broadcaster._cache_ai_nodes = func() {
    AI_MP_Broadcaster._ai_nodes = {};

    # Aircraft - match by callsign
    foreach (var m; props.globals.getNode("/ai/models").getChildren("aircraft")) {
        var path = m.getPath();
        var is_proxy = 0;
        for (var i = 100; i < 110; i += 1) {
            if (path == "/ai/models/aircraft[" ~ i ~ "]") {
                is_proxy = 1; break;
            }
        }
        if (is_proxy) continue;
        var cs_node = m.getNode("callsign");
        if (cs_node == nil) continue;
        var cs = cs_node.getValue();
        if (cs == nil) continue;
        foreach (var valid_cs; AI_MP_Broadcaster.CALLSIGNS) {
            if (cs == valid_cs) {
                AI_MP_Broadcaster._ai_nodes[cs] = m;
                print("AI_MP_Broadcaster: cached aircraft '", cs, "' at ", path);
                break;
            }
        }
    }

    # Ships - match by name (FG doesn't set callsign for ship type)
    foreach (var m; props.globals.getNode("/ai/models").getChildren("ship")) {
        var name_node = m.getNode("name");
        if (name_node == nil) continue;
        var name = name_node.getValue();
        if (name == nil) continue;
        # Find which NATO callsign this name belongs to
        for (var si = 0; si < 30; si += 1) {
            var sname = getprop("/sim/ai/scenarios/scenario[" ~ si ~ "]/name");
            if (sname == nil) break;
            if (sname != AI_MP_Broadcaster._active_scenario) continue;
            var path_node = getprop("/sim/ai/scenarios/scenario[" ~ si ~ "]/path");
            if (path_node == nil) continue;
            var root = io.read_properties(path_node);
            if (root == nil) continue;
            var scenario = root.getNode("scenario");
            if (scenario == nil) continue;
            foreach (var e; scenario.getChildren("entry")) {
                var e_name_node = e.getNode("name");
                var e_cs_node   = e.getNode("callsign");
                if (e_name_node == nil or e_cs_node == nil) continue;
                if (e_name_node.getValue() != name) continue;
                var cs = e_cs_node.getValue();
                foreach (var valid_cs; AI_MP_Broadcaster.CALLSIGNS) {
                    if (cs == valid_cs) {
                        AI_MP_Broadcaster._ai_nodes[cs] = m;
                        print("AI_MP_Broadcaster: cached ship '", cs,
                              "' (name='", name, "') at ", m.getPath());
                        break;
                    }
                }
            }
            break;
        }
    }

    print("AI_MP_Broadcaster: cached ",
          size(keys(AI_MP_Broadcaster._ai_nodes)), " AI models.");
}

###############################################################################
# Send positions - one model per string slot
AI_MP_Broadcaster._send_positions = func() {
    for (var i = 0; i < size(AI_MP_Broadcaster.CALLSIGNS); i += 1) {
        var cs  = AI_MP_Broadcaster.CALLSIGNS[i];
        var idx = AI_MP_Broadcaster.POS_STRING_FIRST + i;
        setprop(AI_MP_Broadcaster.POS_STRING_BASE ~ idx ~ "]",
                AI_MP_Broadcaster._get_pos(cs));
    }
}

###############################################################################
# Send sync string - scenario name so receiver can load-scenario
AI_MP_Broadcaster._send_sync = func() {
    setprop(AI_MP_Broadcaster.POS_STRING_BASE ~
            AI_MP_Broadcaster.SYNC_STRING_IDX ~ "]",
            AI_MP_Broadcaster._active_scenario);
    print("AI_MP_Broadcaster: sent sync: '", AI_MP_Broadcaster._active_scenario, "'");
}

###############################################################################
# Position loop (every 0.5s)
AI_MP_Broadcaster._pos_loop = func(id) {
    if (!AI_MP_Broadcaster._running) return;
    if (id != AI_MP_Broadcaster._loopid) return;
    AI_MP_Broadcaster._send_positions();
    settimer(func { AI_MP_Broadcaster._pos_loop(id); },
             AI_MP_Broadcaster.POS_INTERVAL_S);
}

###############################################################################
# Sync loop (every 10s) - for late joiners
AI_MP_Broadcaster._sync_loop = func(id) {
    if (!AI_MP_Broadcaster._running) return;
    if (id != AI_MP_Broadcaster._sync_loopid) return;
    AI_MP_Broadcaster._send_sync();
    settimer(func { AI_MP_Broadcaster._sync_loop(id); },
             AI_MP_Broadcaster.SYNC_INTERVAL_S);
}

###############################################################################
# Start
AI_MP_Broadcaster.start = func() {
    if (AI_MP_Broadcaster._running) {
        print("AI_MP_Broadcaster: already running."); return;
    }
    AI_MP_Broadcaster._running = 1;
    AI_MP_Broadcaster._active_scenario = AI_MP_Broadcaster._find_active_scenario();  
    AI_MP_Broadcaster._cache_ai_nodes();                                              
    AI_MP_Broadcaster._loopid += 1;
    AI_MP_Broadcaster._sync_loopid += 1;
    AI_MP_Broadcaster._send_sync();
    settimer(func { AI_MP_Broadcaster._pos_loop(AI_MP_Broadcaster._loopid); }, 1.0);
    settimer(func { AI_MP_Broadcaster._sync_loop(AI_MP_Broadcaster._sync_loopid); },
             AI_MP_Broadcaster.SYNC_INTERVAL_S);
    print("AI_MP_Broadcaster: started. Broadcasting ",
          size(keys(AI_MP_Broadcaster._ai_nodes)), " AI models, scenario='",
          AI_MP_Broadcaster._active_scenario, "'.");
}

###############################################################################
# Stop
AI_MP_Broadcaster.stop = func() {
    AI_MP_Broadcaster._running = 0;
    AI_MP_Broadcaster._loopid += 1;
    AI_MP_Broadcaster._sync_loopid += 1;
    for (var i = 0; i < size(AI_MP_Broadcaster.CALLSIGNS); i += 1) {
        var idx = AI_MP_Broadcaster.POS_STRING_FIRST + i;
        setprop(AI_MP_Broadcaster.POS_STRING_BASE ~ idx ~ "]", "");
    }
    setprop(AI_MP_Broadcaster.POS_STRING_BASE ~
            AI_MP_Broadcaster.SYNC_STRING_IDX ~ "]", "");
    print("AI_MP_Broadcaster: stopped.");
}

###############################################################################
# Reload - call if AI models or scenarios changed
AI_MP_Broadcaster.reload = func() {
    AI_MP_Broadcaster._active_scenario = AI_MP_Broadcaster._find_active_scenario(); 
    AI_MP_Broadcaster._cache_ai_nodes();                                               
    AI_MP_Broadcaster._send_sync();
    print("AI_MP_Broadcaster: reloaded. Broadcasting ",
          size(keys(AI_MP_Broadcaster._ai_nodes)), " AI models, scenario='",
          AI_MP_Broadcaster._active_scenario, "'.");
}

###############################################################################
# Auto-start - watch loop that waits until NATO models appear in /ai/models/
# More reliable than a fixed delay
AI_MP_Broadcaster._autostart_loop = func() {
    var ai_list = props.globals.getNode("/ai/models").getChildren("aircraft");
    foreach (var s; props.globals.getNode("/ai/models").getChildren("ship"))
        append(ai_list, s);
    var found = 0;
    foreach (var m; ai_list) {
        var cs_node = m.getNode("callsign");
        if (cs_node == nil) continue;
        var cs = cs_node.getValue();
        foreach (var valid_cs; AI_MP_Broadcaster.CALLSIGNS) {
            if (cs == valid_cs) { found = 1; break; }
        }
        if (found) break;
    }
    if (found) {
        print("AI_MP_Broadcaster: NATO models detected, starting...");
        AI_MP_Broadcaster.start();
    } else {
        settimer(AI_MP_Broadcaster._autostart_loop, 3.0);
    }
}

# Begin watching after 5s (give FG time to initialize)
settimer(AI_MP_Broadcaster._autostart_loop, 5.0);

# Also reload if scenario changes while running
setlistener("/sim/signals/scenario-loaded", func {
    settimer(func { AI_MP_Broadcaster.reload(); }, 2.0);
}, 0, 0);
