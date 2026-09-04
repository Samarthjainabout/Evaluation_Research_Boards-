"""Offline sequential plot of two recorded cycles, without downsampling."""
import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs=2, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, (ax, rails) = plt.subplots(2, 1, figsize=(17, 8.5), sharex=True,
                                   gridspec_kw={'height_ratios': [3.5, 1.3]})
    fig.subplots_adjust(top=.86, bottom=.22, left=.065, right=.98, hspace=.14)
    fig.suptitle('Cell (0,0) — two completed SET / RESET cycles', fontsize=21, fontweight='bold', y=.97)
    fig.text(.5, .925, 'Every recorded read • linear conductance scale • no averaging or downsampling', ha='center', color='#475569')
    summaries = []
    offset = 0
    for number, run in enumerate(args.runs, 1):
        with (run / 'manifest.csv').open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        settings = json.loads((run / 'experiment_settings.jsonl').read_text().splitlines()[-1])
        outcome = json.loads((run / 'cell_cycles.jsonl').read_text().splitlines()[-1])
        assert settings['row'] == settings['col'] == 0
        assert settings['read_voltage_V'] == .5
        assert settings['set_threshold_uA'] == 25 and settings['reset_threshold_uA'] == 10
        reads = [r for r in rows if r['operation'] == 'read']
        def x(row):
            return int(row['index']) + offset
        def g(row):
            return float(row['la_set_window_mean_uA']) / float(row['vcc_set_V'])
        assert all(math.isfinite(g(r)) for r in reads)
        end = max(x(r) for r in rows)
        ax.plot([x(r) for r in reads], [g(r) for r in reads], color='#475569', lw=.9, alpha=.65)
        groups = [('Read', [r for r in reads if r['ok'].lower() == 'true' and r['stage'] != 'confirm_read'], '#2563eb', 'o'),
                  ('Confirmation read', [r for r in reads if r['ok'].lower() == 'true' and r['stage'] == 'confirm_read'], '#7c3aed', 'o'),
                  ('Rejected read', [r for r in reads if r['ok'].lower() != 'true'], '#dc2626', 'x')]
        for label, group, color, marker in groups:
            ax.scatter([x(r) for r in group], [g(r) for r in group], s=16 if marker == 'o' else 65,
                       marker=marker, color=color, zorder=4,
                       label=label if (number == 1 and marker != 'x') or (marker == 'x' and group) else None)
        counts = {}
        for mode, color in [('set', '#d97706'), ('reset', '#0891b2')]:
            assert outcome[mode]['target_hit']
            confirms = outcome[mode]['steps'][-1]['confirm_reads']
            assert len(confirms) == 10
            locations = {r['local_output_dir']: x(r) for r in reads}
            indices = [locations[r['local_output_dir']] for r in confirms]
            ax.axvspan(min(indices)-.5, max(indices)+.5, color=color, alpha=.10)
            pulses = [r for r in rows if r['operation'] == mode]
            counts[mode] = len(pulses)
            rails.plot([x(r) for r in pulses], [float(r['vcc_wl_set_V']) for r in pulses], 'o-',
                       color=color, ms=3, lw=1, label=f'{mode.upper()} WL' if number == 1 else None)
            rails.plot([x(r) for r in pulses], [float(r['vcc_set_V']) for r in pulses], 's--',
                       color=color, ms=2.5, lw=1, alpha=.65, label=f'{mode.upper()} Vcc_set' if number == 1 else None)
        ax.text((offset+end)/2, 102, f'Cycle {number}  |  {len(reads)} reads  |  {counts["set"]} SET + {counts["reset"]} RESET pulses',
                ha='center', fontsize=11, fontweight='bold')
        if number == 2:
            for axis in (ax, rails):
                axis.axvline(offset-.5, color='#64748b', ls='--', lw=1)
        summaries.append({'run': run.name, 'events': len(rows), 'reads': len(reads),
                          'rejected': sum(r['ok'].lower() != 'true' for r in reads), **counts})
        offset = end+1
    for level, bound, label, color in [(50,56,'SET target: 50 µS','#d97706'), (20,14,'RESET target: 20 µS','#0891b2')]:
        ax.axhline(level, color=color, ls='--', lw=1.1, label=label)
        ax.axhline(bound, color=color, ls=':', lw=.8, alpha=.65)
    ax.axhline(0, color='#64748b', lw=.6)
    ax.set_ylim(-12, 108)
    ax.set_ylabel('Conductance (µS)')
    ax.legend(loc='upper center', bbox_to_anchor=(.5, .96), ncol=5, fontsize=9, framealpha=.95)
    rails.set_ylim(.2,3.4)
    rails.set_ylabel('Pulse rails (V)')
    rails.legend(loc='upper left', ncol=4, fontsize=9)
    rails.set_xlabel('Combined recorded event index (read and programming events; not elapsed time)')
    rails.set_xlim(-1, offset)
    rails.xaxis.set_major_locator(MultipleLocator(20))
    for axis in (ax, rails):
        axis.grid(True, color='#e2e8f0', lw=.7)
        axis.set_axisbelow(True)
    fig.text(.065,.095, '\n'.join([
        f'Cycle 1: {args.runs[0].name}     |     Cycle 2: {args.runs[1].name}',
        'G = calibrated current (µA) / 0.50 V. All signed readings retained, including rejected feedback; no line connects the two runs.',
        'Shading: final 10-read qualifications. Dotted lines: conservative 56 µS SET / 14 µS RESET bounds.',
        'Pulse rails are commanded values / runtime acknowledgements, not measured analog waveforms.',
    ]), fontsize=9, color='#475569', va='center', linespacing=1.6)
    args.output.mkdir(parents=True, exist_ok=True)
    for suffix in ('png','svg'):
        fig.savefig(args.output / f'two_cycles_all_points.{suffix}', dpi=180, facecolor='white')
    print(json.dumps(summaries))


if __name__ == '__main__':
    main()
