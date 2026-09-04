"""Offline cycle plot: retain every manifest read, including rejected feedback."""
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
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    with (args.run / 'manifest.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    settings = json.loads((args.run / 'experiment_settings.jsonl').read_text().splitlines()[-1])
    outcome = json.loads((args.run / 'cell_cycles.jsonl').read_text().splitlines()[-1])
    reads = [r for r in rows if r['operation'] == 'read']
    pulses = {op: [r for r in rows if r['operation'] == op] for op in ('set', 'reset')}
    def conductance(row):
        return float(row['la_set_window_mean_uA']) / float(row['vcc_set_V'])
    if not all(math.isfinite(conductance(r)) for r in reads):
        raise ValueError('A read lacks a finite value; explicitly handle missing readings before plotting.')
    args.output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, (ax, rails) = plt.subplots(2, 1, figsize=(15, 8.5), sharex=True,
                                   gridspec_kw={'height_ratios': [3.5, 1.3]})
    fig.subplots_adjust(top=.88, bottom=.23, left=.075, right=.97, hspace=.14)
    fig.suptitle('Cell (0,0) — completed SET / RESET cycle', fontsize=20, fontweight='bold', y=.97)
    fig.text(.5, .925, args.run.name, ha='center', color='#475569')
    x = [int(r['index']) for r in reads]
    y = [conductance(r) for r in reads]
    ax.plot(x, y, color='#334155', lw=1, alpha=.65, zorder=2)
    groups = [('Read', [r for r in reads if r['ok'].lower() == 'true' and r['stage'] != 'confirm_read'], '#2563eb', 'o'),
              ('Confirmation read', [r for r in reads if r['ok'].lower() == 'true' and r['stage'] == 'confirm_read'], '#7c3aed', 'o'),
              ('Rejected read (retained)', [r for r in reads if r['ok'].lower() != 'true'], '#dc2626', 'x')]
    for label, group, color, marker in groups:
        ax.scatter([int(r['index']) for r in group], [conductance(r) for r in group],
                   s=27 if marker == 'o' else 70, color=color, marker=marker, label=label, zorder=4)
    for mode, color in [('set', '#d97706'), ('reset', '#0891b2')]:
        target = settings[f'{mode}_threshold_uA'] / settings['read_voltage_V']
        ax.axhline(target, color=color, ls='--', lw=1.2, label=f'{mode.upper()} target: {target:g} µS')
        bound = target + (1 if mode == 'set' else -1) * settings['noise_allowance_uA'] / settings['read_voltage_V']
        ax.axhline(bound, color=color, ls=':', lw=.8, alpha=.65)
        confirms = outcome[mode]['steps'][-1]['confirm_reads']
        locations = {r['local_output_dir']: int(r['index']) for r in reads}
        indices = [locations[r['local_output_dir']] for r in confirms]
        if outcome[mode]['target_hit'] and len(indices) == settings['confirm_reads']:
            ax.axvspan(min(indices)-.5, max(indices)+.5, color=color, alpha=.10)
        group = pulses[mode]
        px = [int(r['index']) for r in group]
        rails.plot(px, [float(r['vcc_wl_set_V']) for r in group], 'o-', ms=3.5, lw=1.2, color=color,
                   label=f'{mode.upper()} WL ({len(group)} pulses)')
        rails.plot(px, [float(r['vcc_set_V']) for r in group], 's--', ms=3, lw=1, color=color,
                   alpha=.7, label=f'{mode.upper()} Vcc_set')
    ax.axhline(0, color='#64748b', lw=.6)
    ax.set_ylabel('Conductance (µS)')
    ax.legend(loc='upper right', fontsize=9, ncol=2, framealpha=.96)
    ax.set_ylim(min(-12, min(y)-5), max(90, max(y)+18))
    rails.set_ylabel('Pulse rails (V)')
    rails.set_ylim(.2, 3.65)
    rails.set_xlabel('Recorded event index (reads and programming pulses; not elapsed time)')
    rails.legend(loc='upper left', ncol=4, fontsize=9)
    rails.set_xlim(-1, max(int(r['index']) for r in rows)+1)
    rails.xaxis.set_major_locator(MultipleLocator(10))
    for axis in (ax, rails):
        axis.grid(True, color='#e2e8f0', lw=.7)
        axis.set_axisbelow(True)
    rejected = sum(r['ok'].lower() != 'true' for r in reads)
    fig.text(.075, .085, f'{len(rows)} recorded events • {len(reads)} read points (including {rejected} rejected) • no averaging or downsampling\n'
             'G = calibrated current (µA) / 0.50 V. Signed near-zero readings retained; linear axes.\n'
             'Shading: final 10-read qualifications. Dotted lines: conservative 56 µS SET / 14 µS RESET bounds.\n'
             'Pulse rails are commanded values / runtime acknowledgements, not measured analog waveforms.',
             fontsize=9, color='#475569', va='center', linespacing=1.6)
    png = args.output / 'cycle_all_points.png'
    fig.savefig(png, dpi=180, facecolor='white')
    fig.savefig(args.output / 'cycle_all_points.svg', facecolor='white')
    print(json.dumps({'png': str(png.resolve()), 'events': len(rows), 'reads': len(reads),
                      'rejected_reads': rejected, 'set_pulses': len(pulses['set']),
                      'reset_pulses': len(pulses['reset'])}))


if __name__ == '__main__':
    main()
