"""Offline SET/RESET delta-conductance maps from actual pulse-adjacent reads."""
import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
import numpy as np

ROOT = Path(__file__).resolve().parent
RUNS = ['gui_20260903_165418_r00c00_cycle', 'gui_20260903_171353_r00c00_cycle']


def collect(name):
    run = ROOT / 'runs' / name
    with (run / 'manifest.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    settings = json.loads((run / 'experiment_settings.jsonl').read_text().splitlines()[-1])
    result = []
    def valid(r):
        return r['operation'] == 'read' and r['ok'].lower() == 'true' and math.isfinite(float(r['la_set_window_mean_uA']))
    for i, pulse in enumerate(rows):
        if pulse['operation'] not in ('set', 'reset'):
            continue
        before = next((r for r in reversed(rows[:i]) if valid(r)), None)
        following = []
        for r in rows[i+1:]:
            if r['operation'] in ('set', 'reset'):
                break
            following.append(r)
            if valid(r):
                break
        after = following[-1] if following and valid(following[-1]) else None
        if before is None or after is None:
            raise ValueError(f'Pulse {pulse["index"]} missing valid adjacent reads')
        assert float(before['vcc_set_V']) == float(after['vcc_set_V']) == .5
        gb = float(before['la_set_window_mean_uA']) / .5
        ga = float(after['la_set_window_mean_uA']) / .5
        result.append(dict(mode=pulse['operation'], pulse_index=int(pulse['index']),
                           before_index=int(before['index']), after_index=int(after['index']),
                           g_before_uS=gb, g_after_uS=ga,
                           delta_g_uS=(ga-gb) if pulse['operation']=='set' else (gb-ga),
                           wl_V=float(pulse['vcc_wl_set_V']), vcc_set_V=float(pulse['vcc_set_V']),
                           recovered_read=any(r['ok'].lower() != 'true' for r in following)))
    return settings, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cropped-contrast', action='store_true')
    args = parser.parse_args()
    data = [collect(name) for name in RUNS]
    limit = math.ceil(max(abs(p['delta_g_uS']) for _, points in data for p in points)/5)*5
    if args.cropped_contrast:
        limit = 20
    norm = TwoSlopeNorm(vmin=-limit, vcenter=0, vmax=limit)
    cmap = plt.get_cmap('bwr').copy()
    cmap.set_bad('#999999')
    xedges = np.arange(-20, 101, 20)
    out = ROOT / 'exports' / 'two_cycles_r00c00_20260903' / 'delta_G_heatmaps'
    if args.cropped_contrast:
        out = out / 'cropped_contrast'
    out.mkdir(parents=True, exist_ok=True)
    reports = []
    for cycle, (name, (settings, points)) in enumerate(zip(RUNS, data), 1):
        fig, axes = plt.subplots(1, 2, figsize=(15, 10))
        fig.subplots_adjust(left=.08, right=.94, top=.83, bottom=.32, wspace=.4)
        fig.suptitle(f'Cycle {cycle} — cell (0,0): pulse-induced conductance change', fontsize=19, fontweight='bold', y=.96)
        fig.text(.5, .915, name, ha='center', fontsize=11, color='#475569')
        report = {'run': name, 'cycle': cycle, 'points': points, 'view_ranges': {},
                  'color_range_uS': [-limit, limit],
                  'color_saturated_pulse_indices': [p['pulse_index'] for p in points if abs(p['delta_g_uS']) > limit]}
        for ax, mode in zip(axes, ('set','reset')):
            group = [p for p in points if p['mode']==mode]
            levels = np.array(settings[f'{mode}_wl_V'])
            yedges = np.r_[levels[0]-(levels[1]-levels[0])/2,
                           (levels[1:]+levels[:-1])/2, levels[-1]+(levels[-1]-levels[-2])/2]
            matrix = np.full((len(levels),len(xedges)-1), np.nan)
            for p in group:
                ix = np.searchsorted(xedges,p['g_before_uS'],side='right')-1
                iy = int(np.argmin(abs(levels-p['wl_V'])))
                assert 0<=ix<matrix.shape[1] and abs(levels[iy]-p['wl_V'])<.001
                assert np.isnan(matrix[iy,ix]), 'Multiple pulses in one bin: do not silently average'
                matrix[iy,ix] = p['delta_g_uS']
                p['conductance_bin_uS'] = [int(xedges[ix]),int(xedges[ix+1])]
            mesh=ax.pcolormesh(xedges,yedges,np.ma.masked_invalid(matrix),cmap=cmap,norm=norm,shading='flat')
            if args.cropped_contrast:
                yy, xx = np.where(np.isfinite(matrix))
                ax.set_xlim(xedges[min(xx)], xedges[max(xx)+1])
                ax.set_ylim(yedges[min(yy)], yedges[max(yy)+1])
                report['view_ranges'][mode] = {
                    'conductance_bin_limits_uS': [float(xedges[min(xx)]), float(xedges[max(xx)+1])],
                    'measured_wl_limits_V': [min(p['wl_V'] for p in group), max(p['wl_V'] for p in group)],
                }
            for p in group:
                ix=np.searchsorted(xedges,p['g_before_uS'],side='right')-1
                ax.text((xedges[ix]+xedges[ix+1])/2,p['wl_V'],f'{p["delta_g_uS"]:+.1f}'+('*' if p['recovered_read'] else ''),
                        ha='center',va='center',fontsize=8,color='white' if abs(p['delta_g_uS'])>limit*.65 else '#101010')
            rails=sorted(set(p['vcc_set_V'] for p in group))
            ax.set_title(f'{mode.upper()}  |  {len(group)} pulses  |  Vcc_set = '+', '.join(f'{v:.2f} V' for v in rails),fontsize=12,pad=12)
            ax.set_xlabel('Conductance before pulse (µS)',fontsize=12)
            ax.set_ylabel(f'{mode.upper()} Vcc_wl_set (V)',fontsize=12)
            ax.set_xticks([v for v in xedges if ax.get_xlim()[0] <= v <= ax.get_xlim()[1]])
            above = any(p['delta_g_uS'] > limit for p in group)
            below = any(p['delta_g_uS'] < -limit for p in group)
            extend = 'both' if above and below else 'max' if above else 'min' if below else 'neither'
            cb=fig.colorbar(mesh,ax=ax,fraction=.05,pad=.04,extend=extend)
            cb.set_label('ΔG (µS)',fontsize=11)
        fig.text(.08,.18,'SET: ΔG = G_after − G_before\nRESET: ΔG = G_before − G_after',fontsize=12,linespacing=1.7)
        fig.text(.55,.18,'G (µS) = calibrated current (µA) / 0.50 V\nRed: intended change; blue: opposite-direction change',fontsize=11,linespacing=1.7)
        contrast_note = ('Cropped to measured bins. Linear color range: −20 to +20 µS; larger changes saturate red, with actual values printed.\n'
                         if args.cropped_contrast else '')
        fig.text(.08,.065,contrast_note+'Each colored tile = one programming pulse; number = signed ΔG (µS). Gray = no measured pulse in that bin.\n'
                 '20 µS conductance bins; one row per commanded WL step. Identical signed color scale for both cycles; no interpolation or averaging.\n'
                 'Uses the last valid read before and first valid read after each pulse; confirmations alone are not programming pulses.\n'
                 '* Post-pulse READ recovered on retry; rejected feedback excluded from ΔG. Small changes may reflect read noise (3 µA allowance per read).',
                 fontsize=9,color='#475569',linespacing=1.6)
        for ext in ('png','svg'):
            fig.savefig(out/f'cycle_{cycle}_delta_G_heatmaps.{ext}',dpi=180,facecolor='white')
        plt.close(fig)
        (out/f'cycle_{cycle}_pulse_pairs.json').write_text(json.dumps(report,indent=2))
        reports.append({'cycle':cycle,'pulses':len(points),'range_uS':[min(p['delta_g_uS'] for p in points),max(p['delta_g_uS'] for p in points)]})
    print(json.dumps({'output':str(out),'color_limit_uS':limit,'cycles':reports}))


if __name__=='__main__':
    main()
