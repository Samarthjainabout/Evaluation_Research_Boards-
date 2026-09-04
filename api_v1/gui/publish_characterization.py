"""Read-only compatibility feed for a GUI server started before pilot support.

Does not import the hardware runner or issue any hardware commands. Exits after
the selected experiment reaches a terminal checkpoint. New GUI servers discover
characterization runs directly; this feed is only for an already-running server.
"""
import argparse
import json
import os
from pathlib import Path
import time

from server import (READ_CONDUCTANCE_VOLTAGE_V, ROOT, RUNS_DIR, STATIC_DIR,
                    _characterization_state, _conductance_uS, _is_read_row,
                    _manifest_for_run, _read_manifest, _run_updated_at)


def _feed_summary(run_dir):
    """Build the live subset without scanning every historical run."""
    rows = _read_manifest(_manifest_for_run(run_dir))
    reads = [row for row in rows if _is_read_row(row)]
    last = rows[-1] if rows else None
    last_read = reads[-1] if reads else None
    previous = reads[-2] if len(reads) > 1 else None
    current = last_read.get('current_uA') if last_read else None
    previous_current = previous.get('current_uA') if previous else None
    delta = (current - previous_current
             if isinstance(current, (int, float)) and isinstance(previous_current, (int, float))
             else None)
    trend = 'rising' if delta is not None and delta > 0 else 'falling' if delta is not None and delta < 0 else 'flat'
    cell = last_read.get('cellAddress') if last_read else None
    visible = rows[-160:]
    visible_reads = [row for row in visible if _is_read_row(row)]
    return {
        'characterization': _characterization_state(run_dir),
        'run': {'id': run_dir.name, 'path': str(run_dir.relative_to(ROOT)),
                'updated': _run_updated_at(run_dir)},
        'last': last, 'lastCell': last.get('cellAddress') if last else None,
        'lastRead': last_read, 'lastReadCell': cell,
        'lastCurrent_uA': current, 'lastConductance_uS': _conductance_uS(current),
        'previousCurrent_uA': previous_current,
        'previousConductance_uS': _conductance_uS(previous_current),
        'currentDelta_uA': delta, 'conductanceDelta_uS': _conductance_uS(delta),
        'trend': trend,
        'counts': {'rows': len(rows), 'read': len(reads), 'program': len(rows)-len(reads),
                   'ok': sum(bool(row.get('ok')) for row in rows)},
        'scale': {'min_uA': current, 'max_uA': current},
        'conductanceScale': {'min_uS': _conductance_uS(current),
                             'max_uS': _conductance_uS(current),
                             'read_voltage_V': READ_CONDUCTANCE_VOLTAGE_V},
        'thresholds_uA': {'set': 70.0, 'reset': 5.0},
        'thresholds_uS': {'set': 140.0, 'reset': 10.0},
        'cells': [last_read] if last_read else [],
        'history': visible, 'readHistory': visible_reads,
        'logEvents': [], 'progressEvents': [], 'activeError': None,
        'arrayResume': {'isArrayRun': False, 'canResume': False},
        'sweepResume': {'isSweepRun': False, 'canResume': False},
    }


def publish(run_dir):
    summary = _feed_summary(run_dir)
    info = summary['characterization']
    if info is None:
        raise ValueError('Not a characterization run')
    payload = {'published': time.time(), 'state': summary}
    output = STATIC_DIR / 'characterization_live.json'
    # A browser/server reader or Windows security scanner can briefly hold the
    # destination without delete sharing.  Use a process-specific temporary
    # file and retry the atomic replacement instead of terminating the live
    # feed and leaving the GUI frozen on an old read.
    tmp = output.with_name(f'{output.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(payload, allow_nan=False), encoding='utf-8')
    for attempt in range(20):
        try:
            os.replace(tmp, output)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)
    return info['status'] == 'running'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.relative_to(RUNS_DIR.resolve())
    while True:
        try:
            keep_running = publish(directory)
        except PermissionError:
            # Preserve the last valid snapshot and retry.  The compatibility
            # feed is read-only, so this never changes or delays hardware work.
            time.sleep(1)
            continue
        if not keep_running:
            break
        time.sleep(3)


if __name__ == '__main__':
    main()
