"""Fixed-bias, read-only averaged calibration of T=100*C+99-F."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics as stats

from run_r0c30_cycle_tdc_calibration import ROOT, SOURCE, make_api, pair, save_json, append
from run_fine_count_calibration_20260928 import wb_read
from analyze_existing_pair_calibration import analyze


def summarize(before, after, wb):
    frames=[w['selected_matching'] for w in wb]
    t=[100*f['coarse_cnt']+99-f['fine_cnt'] for f in frames]
    pre=stats.mean(x['G31_minus_G30_uS'] for x in before)
    post=stats.mean(x['G31_minus_G30_uS'] for x in after)
    drift={str(c):stats.mean(x[f'G{c}_uS'] for x in after)-stats.mean(x[f'G{c}_uS'] for x in before) for c in (30,31)}
    flags=[]
    if abs(post-pre)>12: flags.append('differential_drift')
    if any(abs(x)>12 for x in drift.values()): flags.append('individual_cell_drift')
    if len(wb)<10: flags.append('insufficient_valid_conversions')
    if any(f['coarse_cnt']>=51 or f['fine_cnt']>100 for f in frames): flags.append('invalid_tdc_range')
    return {'G31_minus_G30_uS':(pre+post)/2,'G30_minus_G31_uS':-(pre+post)/2,
        'G30_uS':stats.mean(x['G30_uS'] for x in before+after),
        'G31_uS':stats.mean(x['G31_uS'] for x in before+after),
        'delta_drift_uS':post-pre,'individual_cell_drift_uS':drift,
        'scan_before_sd_uS':stats.stdev(x['G31_minus_G30_uS'] for x in before),
        'scan_after_sd_uS':stats.stdev(x['G31_minus_G30_uS'] for x in after),
        'T_mean':stats.mean(t),'T_sd':stats.stdev(t) if len(t)>1 else None,
        'modal_coarse':stats.multimode(f['coarse_cnt'] for f in frames)[0],
        'coarse_mean':stats.mean(f['coarse_cnt'] for f in frames),
        'fine_mean':stats.mean(f['fine_cnt'] for f in frames),
        'fit_eligible':not flags,'quality_flags':flags,'independent_WB_conversions':len(wb)}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run-dir',required=True,type=Path)
    args=p.parse_args()
    run=args.run_dir
    run.mkdir(parents=True,exist_ok=False)
    seed_path=ROOT/'runs/tdc_averaging_r13_20260930_152600/report.json'
    seed=json.loads(seed_path.read_text())
    if not seed.get('complete'): raise RuntimeError('Averaging seed incomplete')
    wb=[x['wb'] for x in seed['readings'] if x['usable']]
    first={'row':13,'visit':0,'source_run':str(seed_path.parent),'state_kind':'read_only',
           'before':seed['before'],'after':seed['after'],'wb':wb,
           'summary':summarize(seed['before'],seed['after'],wb)}
    rows=[30,6,2,24,0,4,9,13]
    report={'complete':False,'status':'waiting_for_hardware','states':[first],'failures':[],
            'rows':rows,'started':datetime.now().isoformat(),'target':'G31-G30 uS',
            'primary_equation':'G31-G30 = a + b * mean(100*C+99-F)',
            'programming':False}
    save_json(run/'plan.json',{'rows_in_order':rows,'seed':str(seed_path),'wb_reads_per_checkpoint':10,
        'scan_pair_reads_before':3,'scan_pair_reads_after':3,'invalid_WB_retry_budget':4,
        'biases_V':{'iref':1,'vcomp':.9,'vbias':1.6,'bias_comp2':.6,'dc_bias':1.5},
        'scan_MHz':2,'wb_MHz':10,'read_rails_V':[.5,2.5],'scan_hold_us':50,
        'primary_equation':report['primary_equation'],'validation':'Leave entire row out, row-cluster bootstrap',
        'no_programming':True,'old_three_read_checkpoints_in_fit':False})
    def save():
        report['analysis']=analyze(report['states'])
        model=report['analysis'].get('models',{}).get('wrap100_minus')
        report['primary_fit']=model or {'status':'need_at_least_six_usable_rows_and_25uS_span'}
        report['interpretation']='Diagnostic linear mapping, not a verified physical time code. Do not force slope sign or suppress worse held-out error.'
        save_json(run/'report.json',report)
    def stop():
        if (run/'STOP').exists(): raise InterruptedError('Requested safe stop')
    api=make_api(run,SOURCE)
    api.config.hardware_queue_timeout_seconds=1800
    api.config.hardware_queue_poll_seconds=5
    save()
    consecutive_failures=0
    try:
        with api.hardware_queue('tdc-averaged-100C-plus99-minusF-calibration'):
            stop()
            api._ensure_runtime_bitstream()
            api._ensure_runtime_vio_daemon()
            for bias,v in (('iref',1),('vcomp',.9),('vbias',1.6),('bias_comp2',.6),('dc_bias',1.5)):
                api.set_bias_voltage(bias,v)
            for row in rows:
                stop()
                checkpoint={'row':row,'visit':1 if row==13 else 0,'state_kind':'read_only',
                            'before':[],'after':[],'wb':[]}
                report['current']=checkpoint
                try:
                    for phase in ('before','wb','after'):
                        report['status']=phase
                        save()
                        if phase!='wb':
                            for i in range(3):
                                stop()
                                value=pair(api,reverse=bool((i+(phase=='after'))%2),row=row)
                                checkpoint[phase].append(value)
                                append(run/'scan_pairs.jsonl',{'row':row,'phase':phase,'repeat':i,**value})
                                save()
                        else:
                            for attempt in range(14):
                                stop()
                                if len(checkpoint['wb'])==10: break
                                try:
                                    value=wb_read(api,row)
                                    f=value['selected_matching']
                                    valid=f['coarse_cnt']<51 and f['fine_cnt']<=100
                                    append(run/'tdc_raw.jsonl',{'row':row,'attempt':attempt+1,'usable':valid,
                                        'T':100*f['coarse_cnt']+99-f['fine_cnt'],'wb':value})
                                    if valid: checkpoint['wb'].append(value)
                                    api._append_progress('tdc-wrap-calibration',f"r{row}: {len(checkpoint['wb'])}/10 independent WB reads",row=row,col=31,**f)
                                except RuntimeError as exc:
                                    append(run/'wb_failures.jsonl',{'row':row,'attempt':attempt+1,'error':str(exc)})
                                save()
                            if len(checkpoint['wb'])<10: raise RuntimeError('WB valid-read budget exhausted')
                    checkpoint['summary']=summarize(checkpoint['before'],checkpoint['after'],checkpoint['wb'])
                    report['states'].append(checkpoint)
                    append(run/'states.jsonl',checkpoint)
                    api._append_progress('tdc-wrap-calibration',f'r{row}: averaged checkpoint complete',row=row,col=31,**checkpoint['summary'])
                    consecutive_failures=0
                    save()
                except RuntimeError as exc:
                    failure={'row':row,'error':str(exc)}
                    report['failures'].append(failure)
                    append(run/'failures.jsonl',failure)
                    save()
                    consecutive_failures+=1
                    if consecutive_failures>=2: raise RuntimeError('Two consecutive rows failed; stop for inspection') from exc
            report.update(complete=True,status='acquisition_complete')
    except Exception as exc:
        report.update(status='stopped',error=f'{type(exc).__name__}: {exc}')
    finally:
        save()
    print(json.dumps({'status':report['status'],'report':str(run/'report.json')}),flush=True)
    return 0 if report['complete'] else 1


if __name__=='__main__':
    raise SystemExit(main())
