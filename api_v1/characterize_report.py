"""Offline CSV experiment logs and scientific figures; never controls hardware."""
import argparse
import csv
import json
from pathlib import Path
import statistics


def write_csv(path, fields, rows):
    with path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def generate(folder, plots=True):
    folder=Path(folder)
    plan=json.loads((folder/'plan.json').read_text())
    trials=[json.loads(p.read_text()) for p in sorted((folder/'trials').glob('*.json'))]
    out=folder/'report'
    out.mkdir(exist_ok=True)
    fields=['id','kind','repeat','mode','target_uS','wl_V','status','reason','approach_direction',
            'preparation_pulse_count','test_pulse_count','g_before_mean_uS','g_after_mean_uS',
            'immediate_delta_uS','sustained_delta_uS','pre_stdev_uS','post_stdev_uS',
            'immediate_read_retried','uncertain_pulse','started_utc','updated_utc']
    write_csv(out/'trials.csv',fields,trials)
    read_rows=[]
    pulse_rows=[]
    for trial in trials:
        for event in trial['events']:
            if 'reading' in event:
                read_rows.append({'trial_id':trial['id'],'stage':event['kind'],'utc':event['utc'],**event['reading']})
            if event['kind']=='pulse_ack':
                result=event['result']
                pulse_rows.append({'trial_id':trial['id'],'role':event['role'],'utc':event['utc'],
                                   'mode':result['operation'],'packet':result['packet'],
                                   'vcc_set_V':result['rails']['vcc_set_v'], 'wl_V':result['rails']['vcc_wl_set_v'],
                                   'duration_seconds':event['duration_seconds'],'acknowledged':result['ok']})
    write_csv(out/'reads.csv',['trial_id','stage','utc','elapsed_seconds','duration_seconds','current_uA',
                              'conductance_uS','feedback_attempts','capture'],read_rows)
    write_csv(out/'pulses.csv',['trial_id','role','utc','mode','packet','vcc_set_V','wl_V','duration_seconds','acknowledged'],pulse_rows)
    coverage=[]
    for mode in ('set','reset'):
        targets=sorted({s['target_uS'] for s in plan['trials'] if s['mode']==mode})
        voltages=sorted({s['wl_V'] for s in plan['trials'] if s['mode']==mode and s['kind']=='test'})
        for target in targets:
            for voltage in voltages:
                group=[t for t in trials if t['status']=='complete' and t['kind']=='test' and
                       t['mode']==mode and t['target_uS']==target and t['wl_V']==voltage]
                values=[t['sustained_delta_uS'] for t in group]
                coverage.append({'mode':mode,'target_uS':target,'wl_V':voltage,'count':len(values),
                                 'mean_delta_uS':statistics.mean(values) if values else None,
                                 'stdev_delta_uS':statistics.stdev(values) if len(values)>1 else None})
    write_csv(out/'coverage.csv',['mode','target_uS','wl_V','count','mean_delta_uS','stdev_delta_uS'],coverage)
    controls=[{k:t.get(k) for k in fields} for t in trials if t['kind']=='control']
    write_csv(out/'controls.csv',fields,controls)
    if plots:
        plot_maps(out,coverage)
        for trial in trials:
            plot_trial(out,trial)
    return coverage


def plot_maps(out, coverage):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import TwoSlopeNorm
    fig,axes=plt.subplots(2,3,figsize=(15,8),layout='constrained')
    fig.suptitle('Characterization — measured test pulses only',fontsize=18)
    vals=[abs(r['mean_delta_uS']) for r in coverage if r['mean_delta_uS'] is not None]
    limit=max([1.,*vals])
    sdmax=max([1.,*[r['stdev_delta_uS'] for r in coverage if r['stdev_delta_uS'] is not None]])
    for i,mode in enumerate(('set','reset')):
        group=[r for r in coverage if r['mode']==mode]
        targets=sorted({r['target_uS'] for r in group})
        voltages=sorted({r['wl_V'] for r in group})
        for j,(field,title) in enumerate([('mean_delta_uS','Mean sustained ΔG (µS)'),('count','Qualified trial count'),('stdev_delta_uS','Repeat-to-repeat SD (µS)')]):
            ax=axes[i,j]
            arr=np.full((len(voltages),len(targets)),np.nan)
            for row in group:
                if row[field] is not None:
                    arr[voltages.index(row['wl_V']),targets.index(row['target_uS'])]=row[field]
            cmap=plt.get_cmap('bwr' if j==0 else 'viridis').copy()
            cmap.set_bad('#999999')
            kwargs={'norm':TwoSlopeNorm(vmin=-limit,vcenter=0,vmax=limit)} if j==0 else {'vmin':0,'vmax':3 if j==1 else sdmax}
            mesh=ax.imshow(np.ma.masked_invalid(arr),origin='lower',aspect='auto',cmap=cmap,**kwargs)
            for yi in range(len(voltages)):
                for xi in range(len(targets)):
                    if np.isfinite(arr[yi,xi]):
                        ax.text(xi,yi,f'{arr[yi,xi]:.0f}' if j==1 else f'{arr[yi,xi]:+.2f}',ha='center',va='center',
                                bbox={'facecolor':'white','alpha':.75,'edgecolor':'none'},fontsize=9)
            ax.set_xticks(range(len(targets)),[f'{v:g}' for v in targets])
            ax.set_yticks(range(len(voltages)),[f'{v:.2f}' for v in voltages])
            ax.set_xlabel('Prepared conductance target (µS)')
            ax.set_ylabel(f'{mode.upper()} WL test voltage (V)')
            ax.set_title(title)
            fig.colorbar(mesh,ax=ax,shrink=.8)
    fig.supxlabel('Gray: no estimate; SD requires ≥2 trials. Discrete condition grid (not interpolated). Controls exported separately.',fontsize=10)
    fig.savefig(out/'characterization_maps.png',dpi=160)
    plt.close(fig)


def plot_trial(out,trial):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    reads=[e for e in trial['events'] if 'reading' in e]
    if not reads:
        return
    fig,ax=plt.subplots(figsize=(11,4.8),layout='constrained')
    for stage,color in [('preparation','#94a3b8'),('pre_read','#2563eb'),('post_read','#7c3aed')]:
        group=[e for e in reads if (e['kind'].startswith(stage) if stage=='preparation' else e['kind']==stage)]
        ax.plot([e['reading']['elapsed_seconds'] for e in group],[e['reading']['conductance_uS'] for e in group],
                'o-',ms=3,lw=.8,color=color,label=stage)
    for event in trial['events']:
        if event['kind']=='pulse_ack':
            ax.axvline(event['elapsed_seconds'],color='#dc2626' if event['role']=='test' else '#94a3b8',alpha=.6,lw=.8)
    ax.axhspan(trial['target_uS']-4,trial['target_uS']+4,color='#22c55e',alpha=.12)
    ax.set_title(f'{trial["id"]}: {trial["kind"]} {trial["mode"].upper()}, {trial["target_uS"]:g} µS, WL {trial["wl_V"]} — {trial["status"]}')
    ax.set_xlabel('Elapsed host time (s); includes command/capture latency')
    ax.set_ylabel('Conductance (µS)')
    ax.grid(alpha=.2)
    ax.legend()
    fig.savefig(out/f'{trial["id"]}_reads.png',dpi=150)
    plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir',type=Path)
    generate(parser.parse_args().run_dir)
