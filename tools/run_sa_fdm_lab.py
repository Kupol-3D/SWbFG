#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run real FlightGear in a disposable profile; collect and check lab telemetry.

This is an integration experiment, never part of the static unittest suite.
"""
import argparse
import csv
import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('time_s', 'phase', 'altitude_m', 'airspeed_mps', 'roll_deg',
          'pitch_deg', 'heading_deg', 'north_m', 'east_m', 'distance_m',
          'throttle', 'brake', 'yaw', 'support_lbf', 'down_mps', 'agl_m')


def samples(text):
    rows = []
    for line in text.splitlines():
        if 'SA_FDM_SAMPLE,' not in line:
            continue
        values = next(csv.reader([line.split('SA_FDM_SAMPLE,', 1)[1]]))
        if len(values) != len(FIELDS):
            raise ValueError('Incomplete FlightGear sample: ' + line)
        row = {key: value if key == 'phase' else float(value)
               for key, value in zip(FIELDS, values)}
        if any(not math.isfinite(value) for key, value in row.items() if key != 'phase'):
            raise ValueError('Non-finite FlightGear state')
        rows.append(row)
    return rows


def analyze(text, returncode):
    rows = samples(text)
    failures = []
    if returncode != 0:
        failures.append(f'FlightGear exit code {returncode}')
    if 'SA_FDM_COMPLETE' not in text or len(rows) < 180:
        failures.append('No complete >=3 minute experiment')
    if not rows:
        return {'passed': False, 'failures': failures + ['No telemetry'], 'samples': 0}
    span = rows[-1]['time_s'] - rows[0]['time_s']
    if span < 205:
        failures.append('Less than 205 seconds of simulated flight')
    gaps = [b['time_s'] - a['time_s'] for a, b in zip(rows, rows[1:])]
    if any(gap <= 0 or gap > 2.5 for gap in gaps):
        failures.append('Nonmonotonic or interrupted telemetry')
    if re.search(r'Nasal (?:runtime|parse) error|solver[^\n]*fail|trim[^\n]*fail|FDM NaN check failed|FATAL|Caught exception', text, re.I):
        failures.append('Runtime/solver error in FlightGear log')
    limits = [('altitude_m', 995, 1020), ('airspeed_mps', 0, 45),
              ('roll_deg', -20, 20), ('pitch_deg', -15, 15), ('agl_m', 100, 2000)]
    for key, low, high in limits:
        if any(not low <= row[key] <= high for row in rows):
            failures.append(f'{key} outside declared envelope [{low}, {high}]')
    phases = {}
    for row in rows:
        phases.setdefault(row['phase'], []).append(row)
    for phase in ('hover', 'low-speed', 'accelerate', 'brake', 'turn', 'return', 'returned-hover'):
        if phase not in phases:
            failures.append('Missing phase: ' + phase)
    if 'low-speed' in phases and not 1 <= max(r['airspeed_mps'] for r in phases['low-speed']) <= 8:
        failures.append('Low-speed phase did not reach 1..8 m/s')
    if 'accelerate' in phases and max(r['airspeed_mps'] for r in phases['accelerate']) < 15:
        failures.append('Acceleration did not reach 15 m/s')
    if 'brake' in phases and phases['brake'][-1]['airspeed_mps'] > 1:
        failures.append('Braking did not reduce speed below 1 m/s')
    if 'turn' in phases and abs(phases['turn'][-1]['heading_deg'] - 180) > 10:
        failures.append('Turn did not approach reciprocal heading')
    pulse = [r for r in rows if 10 <= r['time_s'] <= 17]
    recovery = [r for r in rows if 25 <= r['time_s'] <= 30]
    if (not pulse or max(abs(r['roll_deg']) for r in pulse) < 4
            or max(abs(r['pitch_deg']) for r in pulse) < 2):
        failures.append('Attitude commands did not produce the expected response')
    if not recovery or any(abs(r['roll_deg']) > 0.2 or abs(r['pitch_deg']) > 0.2 for r in recovery):
        failures.append('Attitude did not recover after controls were released')
    if max(r['altitude_m'] for r in rows) < 1013.5:
        failures.append('Altitude excursion did not reach commanded height')
    final = rows[-1]
    if abs(final['altitude_m'] - 1000) > 0.5 or final['airspeed_mps'] > 0.5 or final['distance_m'] > 10:
        failures.append('Return did not finish near home in level hover')
    stats = {}
    for name, group in phases.items():
        stats[name] = {'samples': len(group), 'start_s': group[0]['time_s'],
                       'end_s': group[-1]['time_s']}
        for key in ('altitude_m', 'airspeed_mps', 'roll_deg', 'pitch_deg', 'distance_m'):
            stats[name][key] = {'min': min(r[key] for r in group),
                                'max': max(r[key] for r in group), 'end': group[-1][key]}
    return {'passed': not failures, 'failures': failures, 'samples': len(rows),
            'duration_sim_s': span, 'final': final, 'phases': stats}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fgfs', type=Path, required=True)
    parser.add_argument('--fg-root', type=Path, required=True)
    parser.add_argument('--fg-scenery', type=Path)
    parser.add_argument('--output', type=Path, help='New directory outside the repository')
    parser.add_argument('--manual', action='store_true', help='Leave controls to the pilot; no automatic exit')
    parser.add_argument('--headless', action='store_true', help='Skip graphical verification')
    parser.add_argument('--timeout', type=float, default=420)
    args = parser.parse_args()
    output = (args.output or Path(tempfile.mkdtemp(prefix='sa-fdm-lab-'))).resolve()
    if output.is_relative_to(ROOT):
        parser.error('Use an output directory outside the repository')
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ('flightgear.log', 'telemetry.csv', 'summary.json')):
        parser.error('Output already contains a run; choose a new directory')
    home = output / 'fg-home'
    home.mkdir()
    env = os.environ.copy()
    # FG_HOME is read before CLI parsing. --fg-home is not a 2024.1.7 option.
    env['FG_HOME'] = str(home)
    env.pop('FG_AIRCRAFT', None)
    env.pop('FG_SCENERY', None)
    command = [str(args.fgfs.resolve()), '--fg-root=' + str(args.fg_root.resolve()),
               '--fg-aircraft=' + str(ROOT / 'Aircraft'), '--aircraft=sa_fdm_lab',
               '--lat=37.6', '--lon=-122.3', '--altitude=3280.839895013123',
               '--heading=0', '--vc=0', '--in-air', '--trim=0', '--wind=0@0',
               '--disable-terrasync', '--disable-real-weather-fetch',
               '--disable-ai-models', '--disable-ai-traffic', '--disable-sound',
               '--timeofday=noon', '--geometry=1000x700',
               '--prop:/sim/frame-rate-throttle-hz=30',
               '--prop:/sim/space-arena/fdm-lab/run-test=' + ('false' if args.manual else 'true')]
    if args.fg_scenery:
        command.append('--fg-scenery=' + str(args.fg_scenery.resolve()))
    if args.headless:
        command.append('--disable-gui')
    with (output / 'version.txt').open('w') as stream:
        subprocess.run(command[:2] + ['--version'], env=env, cwd=output,
                       stdout=stream, stderr=subprocess.STDOUT, timeout=30, check=True)
    print('FlightGear output:', output, flush=True)
    start = time.monotonic()
    with (output / 'flightgear.log').open('w') as stream:
        process = subprocess.Popen(command, env=env, cwd=output, stdout=stream, stderr=subprocess.STDOUT)
        try:
            returncode = process.wait(timeout=args.timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            returncode = -1
    if args.manual:
        print('Manual flight ended; record measurements using README scenario.')
        return 0 if returncode == 0 else 1
    text = (output / 'flightgear.log').read_text(errors='replace')
    try:
        result = analyze(text, returncode)
        rows = samples(text)
    except ValueError as exc:
        result = {'passed': False, 'failures': [str(exc)]}
        rows = []
    result['duration_wall_s'] = time.monotonic() - start
    with (output / 'telemetry.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
