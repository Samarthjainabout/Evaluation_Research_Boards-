"""Read-only unchanged-pair test of independent WB averaging; no SET/RESET."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics as stats
import time

from run_r0c30_cycle_tdc_calibration import ROOT, SOURCE, make_api, pair, save_json, append
from run_fine_count_calibration_20260928 import wb_read


def sd(values):
    return stats.stdev(values) if len(values) > 1 else None


def analyze(report):
    frames = [x['wb']['selected_matching'] for x in report['readings'] if x['usable']]
    if not frames:
        return {'valid_conversions': 0}
    beta = report['fixed_model_coefficients']
    series = {
        'coarse': [f['coarse_cnt'] for f in frames],
        'fine_raw_not_wrap_corrected': [f['fine_cnt'] for f in frames],
        'ticks_plus_diagnostic': [100*f['coarse_cnt']+f['fine_cnt'] for f in frames],
        'ticks_minus_diagnostic': [100*f['coarse_cnt']+99-f['fine_cnt'] for f in frames],
        'fixed_model_delta_uS': [beta[0]+beta[1]*f['coarse_cnt']+beta[2]*f['fine_cnt'] for f in frames],
    }
    result = {'valid_conversions':len(frames), 'series':{},
              'caution':'20 conversions cannot establish stationarity or estimate scatter of independent 20-reading batches. Tick mappings and conductance model remain provisional.'}
    for name, values in series.items():
        batches = []
        for n in (1,3,5,10,20):
            if len(values) < n:
                continue
            means = [stats.mean(values[i:i+n]) for i in range(0,len(values)-n+1,n)]
            batches.append({'n':n, 'nonoverlapping_batches':len(means),
                           'prefix_mean':stats.mean(values[:n]), 'batch_means':means,
                           'observed_batch_mean_sd':sd(means),
                           'iid_expected_sem_not_measured':sd(values)/n**.5 if sd(values) is not None else None})
        result['series'][name] = {'mean':stats.mean(values), 'sd':sd(values),
                                 'minimum':min(values),'maximum':max(values),'averaging':batches}
    if len(frames) >= 20:
        for name,values in series.items():
            result['series'][name]['second10_minus_first10']=stats.mean(values[10:20])-stats.mean(values[:10])
    if report['before'] and report['after']:
        before = [x['G31_minus_G30_uS'] for x in report['before']]
        after = [x['G31_minus_G30_uS'] for x in report['after']]
        reference = (stats.mean(before)+stats.mean(after))/2
        result['scan_reference'] = {'before_delta_mean_uS':stats.mean(before),'before_delta_sd_uS':sd(before),
            'after_delta_mean_uS':stats.mean(after),'after_delta_sd_uS':sd(after),
            'delta_drift_uS':stats.mean(after)-stats.mean(before),'bracket_mean_delta_uS':reference,
            'model_mean_minus_scan_uS':stats.mean(series['fixed_model_delta_uS'])-reference,
            'individual_cell_drift_uS':{f'G{c}':stats.mean(x[f'G{c}_uS'] for x in report['after'])-stats.mean(x[f'G{c}_uS'] for x in report['before']) for c in (30,31)}}
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--row',type=int,default=13)
    args=parser.parse_args()
    if not 0 <= args.row < 32:
        parser.error('row outside array')
    run=args.run_dir
    run.mkdir(parents=True,exist_ok=False)
    source=ROOT/'runs/tdc_extended_readonly_20260930_132700/report.json'
    model=json.loads(source.read_text())['analysis']['models']['coarse_fine']['coefficients']
    report={'complete':False,'status':'waiting_for_hardware','row':args.row,'before':[], 'after':[],
            'readings':[], 'failures':[], 'fixed_model_coefficients':model,'model_source':str(source),
            'started':datetime.now().isoformat(),'programming':False}
    save_json(run/'plan.json',{'row':args.row,'columns':[30,31],'independent_wb_conversions':20,
        'scan_pair_repeats_before_and_after':3,'wb_invalid_retry_budget':4,
        'biases_V':{'iref':1,'vcomp':.9,'vbias':1.6,'bias_comp2':.6,'dc_bias':1.5},
        'scan_MHz':2,'wb_MHz':10,'scan_hold_us':50,'read_rails_V':[.5,2.5],
        'no_SET_RESET':True,'target_sign':'G31-G30','averaging_sizes':[1,3,5,10,20]})
    def save():
        report['analysis']=analyze(report)
        save_json(run/'report.json',report)
    def check_stop():
        if (run/'STOP').exists():
            raise InterruptedError('Requested safe stop')
    api=make_api(run,SOURCE)
    api.config.hardware_queue_timeout_seconds=1800
    api.config.hardware_queue_poll_seconds=5
    save()
    try:
        with api.hardware_queue('tdc-independent-20-read-averaging'):
            check_stop()
            api._ensure_runtime_bitstream()
            api._ensure_runtime_vio_daemon()
            for bias,v in (('iref',1),('vcomp',.9),('vbias',1.6),('bias_comp2',.6),('dc_bias',1.5)):
                api.set_bias_voltage(bias,v)
            for phase in ('before','wb','after'):
                report['status']=phase
                save()
                if phase != 'wb':
                    for repeat in range(3):
                        check_stop()
                        item=pair(api,reverse=bool((repeat+(phase=='after'))%2),row=args.row)
                        report[phase].append(item)
                        append(run/'scan_pairs.jsonl',{'phase':phase,'repeat':repeat,**item})
                        save()
                    continue
                for attempt in range(24):
                    check_stop()
                    if sum(x['usable'] for x in report['readings']) >= 20:
                        break
                    try:
                        entry=wb_read(api,args.row)
                        f=entry['selected_matching']
                        usable=f['coarse_cnt']<51 and f['fine_cnt']<=100
                        item={'attempt':attempt+1,'time':time.time(),'wb':entry,'usable':usable}
                        append(run/'tdc_raw.jsonl',item)
                        report['readings'].append(item)
                        api._append_progress('tdc-averaging',f"Independent conversion {sum(x['usable'] for x in report['readings'])}/20",row=args.row,col=31,**f)
                    except RuntimeError as exc:
                        failure={'attempt':attempt+1,'error':str(exc),'time':time.time()}
                        report['failures'].append(failure)
                        append(run/'failures.jsonl',failure)
                    save()
            report['complete']=sum(x['usable'] for x in report['readings'])==20
            report['status']='complete' if report['complete'] else 'incomplete_valid_conversion_budget_exhausted'
    except Exception as exc:
        report.update(status='stopped',error=f'{type(exc).__name__}: {exc}')
    finally:
        save()
    print(json.dumps({'status':report['status'],'report':str(run/'report.json')}),flush=True)
    return 0 if report['complete'] else 1


if __name__=='__main__':
    raise SystemExit(main())
