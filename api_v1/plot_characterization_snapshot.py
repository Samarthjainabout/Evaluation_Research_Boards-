"""Offline snapshot plots: reads checkpoint files only; never controls hardware."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics


def valid_read(event, cell, read_voltage):
    reading, result = event.get('reading', {}), event.get('result', {})
    current = reading.get('current_uA')
    return (result.get('ok') is True and result.get('operation') == 'read'
            and result.get('cell') == {'row': cell[0], 'col': cell[1]}
            and result.get('rails', {}).get('vcc_set_v') == read_voltage
            and isinstance(current, (int, float)) and math.isfinite(current))


def pulse_pairs(trial, plan, include_negative_g=False):
    pairs, rejected = [], []
    before, pending = None, None
    voltage, cell = plan['read_vcc_set_V'], plan['cell']
    for index, event in enumerate(trial['events']):
        kind = event.get('kind')
        if kind == 'pulse_intent':
            if pending:
                rejected.append({'trial': trial['id'], 'pulse_event': pending['index'], 'reason': 'no_post_read_before_next_pulse'})
            pending = {'index': index, 'before': before, 'ack': None}
            before = None  # Never pair across an intervening programming pulse.
        elif kind == 'pulse_ack':
            result = event['result']
            if pending and result.get('ok') is True and result.get('cell') == {'row': cell[0], 'col': cell[1]}:
                pending['ack'] = event
        elif 'reading' in event:
            if not valid_read(event, cell, voltage):
                continue
            if pending:
                ack, previous = pending['ack'], pending['before']
                reason = 'missing_ack_or_pre_read'
                if ack and previous:
                    gb = previous['reading']['current_uA'] / voltage
                    ga = event['reading']['current_uA'] / voltage
                    mode = ack['result']['operation']
                    if (gb < 0 or ga < 0) and not include_negative_g:
                        reason = 'negative_before_or_after_G'
                    elif mode in ('set', 'reset'):
                        pairs.append({
                            'trial': trial['id'], 'trial_status': trial['status'],
                            'source_run': trial.get('source_run'),
                            'role': ack['role'], 'mode': mode, 'pulse_event': pending['index'],
                            'pulse_utc': ack['utc'], 'before_utc': previous['utc'], 'after_utc': event['utc'],
                            'before_capture': previous['reading']['capture'], 'after_capture': event['reading']['capture'],
                            'g_before_uS': gb, 'g_after_uS': ga,
                            'delta_g_uS': ga-gb if mode == 'set' else gb-ga,
                            'wl_V': ack['result']['rails']['vcc_wl_set_v'],
                            'vcc_set_V': ack['result']['rails']['vcc_set_v'],
                            'read_retried': previous['reading']['feedback_attempts'] > 1 or event['reading']['feedback_attempts'] > 1,
                            'carried_from_run': trial.get('carried_from_run'),
                        })
                        reason = None
                if reason:
                    rejected.append({'trial': trial['id'], 'pulse_event': pending['index'], 'reason': reason})
                pending = None
            before = event
    if pending:
        rejected.append({'trial': trial['id'], 'pulse_event': pending['index'], 'reason': 'awaiting_ack_or_post_read_at_snapshot'})
    return pairs, rejected


def binned(points, xwidth=20., ywidth=.1):
    groups = {}
    for point in points:
        key = (point['mode'], math.floor(point['g_before_uS']/xwidth),
               math.floor(round(point['wl_V']/ywidth, 9)))
        groups.setdefault(key, []).append(point['delta_g_uS'])
    return [{'mode': mode, 'xbin': x, 'ybin': y, 'count': len(values),
             'g_bin_uS': [x*xwidth, (x+1)*xwidth],
             'wl_bin_V': [round(y*ywidth, 5), round((y+1)*ywidth, 5)],
             'mean_delta_uS': statistics.mean(values),
             'stdev_delta_uS': statistics.stdev(values) if len(values)>1 else None}
            for (mode,x,y),values in sorted(groups.items())]


def make_figures(points, bins, out, captured, source_count=1, include_negative_g=False,
                 min_conductance_uS=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import TwoSlopeNorm
    from matplotlib.ticker import MaxNLocator
    limit = max(1., math.ceil(max((abs(b['mean_delta_uS']) for b in bins), default=0)))
    countmax = max((b['count'] for b in bins), default=1)
    summary = {}
    for metric in ('mean_delta_uS', 'count'):
        fig, axes = plt.subplots(1, 2, figsize=(13, 8))
        fig.subplots_adjust(left=.085, right=.94, top=.81, bottom=.21, wspace=.40)
        fig.suptitle('Cell (0,0) — preparation-pulse ' + ('ΔG heatmaps' if metric=='mean_delta_uS' else 'sample counts'),
                     fontsize=18, fontweight='bold', y=.975)
        fig.text(.5, .923, 'Diagnostic only: adaptive preparation data, NOT a qualified characterization grid',
                 ha='center', fontsize=11, color='#8a3b12')
        source_note = f'  |  Combined sources = {source_count}' if source_count > 1 else ''
        fig.text(.5, .884, f'Snapshot {captured}{source_note}  |  Read Vcc_set = 0.50 V  |  SET / RESET Vcc_set = 2.30 V',
                 ha='center', fontsize=10, color='#475569')
        for ax, mode in zip(axes, ('set','reset')):
            group = [b for b in bins if b['mode']==mode]
            pgroup = [p for p in points if p['mode']==mode]
            if not group:
                ax.set_facecolor('#b6bbc2')
                ax.text(.5,.5,'No paired measurements',transform=ax.transAxes,ha='center')
                continue
            xmin, xmax = min(b['xbin'] for b in group), max(b['xbin'] for b in group)
            ymin, ymax = min(b['ybin'] for b in group), max(b['ybin'] for b in group)
            xe = np.arange(xmin,xmax+2)*20.
            ye = np.arange(ymin,ymax+2)*.1
            matrix = np.full((ymax-ymin+1,xmax-xmin+1),np.nan)
            for b in group:
                matrix[b['ybin']-ymin,b['xbin']-xmin] = b[metric]
            cmap = plt.get_cmap('RdBu_r' if metric=='mean_delta_uS' else 'viridis').copy()
            cmap.set_bad('#b6bbc2')
            norm = {'norm':TwoSlopeNorm(vmin=-limit,vcenter=0,vmax=limit)} if metric=='mean_delta_uS' else {'vmin':0,'vmax':countmax}
            mesh=ax.pcolormesh(xe,ye,np.ma.masked_invalid(matrix),cmap=cmap,shading='flat',**norm)
            for b in group:
                val=b[metric]
                ax.text((b['xbin']+.5)*20,(b['ybin']+.5)*.1,
                        f'{val:+.1f}' if metric=='mean_delta_uS' else str(val),
                        ha='center',va='center',fontsize=8,
                        color='white' if (abs(val)>limit*.55 if metric=='mean_delta_uS' else val<countmax*.4) else '#111827')
            ax.set_title(f'{mode.upper()}  |  {len(pgroup)} pulse pairs',fontsize=13,pad=10)
            ax.set_xlabel('Conductance before pulse (µS)',fontsize=11)
            ax.set_ylabel(f'{mode.upper()} Vcc_wl_set (V)',fontsize=11)
            ax.set_xticks(xe)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=9))
            colorbar=fig.colorbar(mesh,ax=ax,pad=.03,fraction=.045)
            colorbar.set_label('Mean ΔG (µS)' if metric=='mean_delta_uS' else 'Pulse pairs in bin')
            if metric=='count': colorbar.locator=MaxNLocator(integer=True); colorbar.update_ticks()
            summary[mode]={'pulse_pairs':len(pgroup),'occupied_bins':len(group),
                           'negative_delta_pairs':sum(p['delta_g_uS']<0 for p in pgroup)}
        fig.text(.085,.125,'SET ΔG = G_after − G_before; RESET ΔG = G_before − G_after.  G = calibrated current / 0.50 V.',fontsize=10)
        if min_conductance_uS is not None:
            negative_note = (f'Minimal mask: G_before and G_after must both be ≥ {min_conductance_uS:g} µS; '
                             'negative ΔG retained.')
        else:
            negative_note = ('Negative before/after G included; negative ΔG retained.' if include_negative_g else
                             'Negative before/after G excluded; negative ΔG retained.')
        fig.text(.085,.084,'Tiles: 20 µS × 0.10 V bins; repeated observations averaged. Gray = unmeasured. No interpolation.\n'
                 f'{negative_note} Preparation history, read noise and drift can affect these changes.',
                 fontsize=9,color='#475569',linespacing=1.6)
        name='preparation_delta_G_heatmaps' if metric=='mean_delta_uS' else 'preparation_sample_counts'
        for ext in ('png','svg'):
            fig.savefig(out/f'{name}.{ext}',dpi=180,facecolor='white')
        plt.close(fig)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir',type=Path,nargs='+')
    parser.add_argument('--include-negative-g', action='store_true',
                        help='Include finite negative before/after conductance instead of excluding it')
    parser.add_argument('--min-conductance-uS', type=float,
                        help='Keep a pair only when both before and after conductance meet this floor')
    args=parser.parse_args()
    sources=[path.resolve() for path in args.run_dir]
    prefix='characterization_combined_snapshot' if len(sources)>1 else 'characterization_snapshot'
    if args.include_negative_g:
        prefix += '_all_data'
    if args.min_conductance_uS is not None:
        if not math.isfinite(args.min_conductance_uS) or args.min_conductance_uS < 0:
            parser.error('--min-conductance-uS must be a finite non-negative value')
        prefix += f'_minG{args.min_conductance_uS:g}uS'.replace('.', 'p')
    out=Path(__file__).resolve().parent/'exports'/f'{prefix}_{datetime.now():%Y%m%d_%H%M%S}'
    out.mkdir(parents=True,exist_ok=False)
    captured=datetime.now().astimezone().isoformat(timespec='seconds')
    plans=[json.loads((source/'plan.json').read_text()) for source in sources]
    plan=plans[0]
    comparable=('cell','read_vcc_set_V','read_wl_V','set_vcc_set_V','reset_vcc_set_V')
    if any(any(candidate.get(key)!=plan.get(key) for key in comparable) for candidate in plans[1:]):
        raise ValueError('Cannot combine runs with different cell or read/SET/RESET rail conditions')
    trials, hashes=[],{}
    for source in sources:
        source_hashes={}
        for path in sorted((source/'trials').glob('*.json')):
            raw=path.read_bytes()  # atomic trial checkpoints: immutable per-file snapshot
            source_hashes[path.name]=hashlib.sha256(raw).hexdigest()
            trial=json.loads(raw)
            trial['source_run']=source.name
            trials.append(trial)
        hashes[source.name]=source_hashes
    snapshot={'sources':[str(source) for source in sources],'captured_local':captured,
              'plans':plans,'trials':trials,'source_sha256':hashes}
    (out/'checkpoint_snapshot.json').write_text(json.dumps(snapshot,indent=2),encoding='utf-8')
    pairs, rejected=[],[]
    for trial in trials:
        p,r=pulse_pairs(trial,plan,args.include_negative_g)
        pairs.extend(p); rejected.extend(r)
    prep_all=[p for p in pairs if p['role']=='preparation']
    floor_masked=[]
    if args.min_conductance_uS is None:
        prep=prep_all
    else:
        prep=[]
        for point in prep_all:
            if (point['g_before_uS'] >= args.min_conductance_uS
                    and point['g_after_uS'] >= args.min_conductance_uS):
                prep.append(point)
            else:
                floor_masked.append(point)
    bins=binned(prep)
    summary=make_figures(prep,bins,out,captured,len(sources),args.include_negative_g,
                         args.min_conductance_uS)
    report={'sources':[str(source) for source in sources],'snapshot':captured,'negative_G_exclusions':sum(r['reason']=='negative_before_or_after_G' for r in rejected),
            'qualified_test_trials':sum(t['kind']=='test' and t['status']=='complete' for t in trials),
            'pairs':pairs,'excluded_or_incomplete':rejected,'preparation_bins':bins,'summary':summary,
            'include_negative_g':args.include_negative_g,
            'min_conductance_uS':args.min_conductance_uS,
            'preparation_pairs_before_floor_mask':len(prep_all),
            'preparation_pairs_removed_by_floor_mask':len(floor_masked),
            'note':'First valid post-pulse read, not time-zero response; no claim of qualified characterization.'}
    (out/'pulse_pairs_and_bins.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(out),'snapshot':captured,'summary':summary,
                      'qualified_test_trials':report['qualified_test_trials'],
                      'negative_G_exclusions':report['negative_G_exclusions'],
                      'floor_masked_pairs':len(floor_masked),
                      'unpaired':len(rejected)-report['negative_G_exclusions']}))


if __name__=='__main__':
    main()
