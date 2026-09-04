"""User-launched conductance/WL pilot. Default invocation is an offline preview.

No repeated test pulses, automatic voltage escalation, or automatic full-grid run.
CSV files are machine-generated experiment logs; JSON checkpoints are authoritative.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time

from cell_api import (CellAddress, DEFAULT_READ_CALIBRATION, FPGA_BITSTREAM_DIR,
                      FPGA_RUNTIME_BITSTREAM, RailVoltages, ScanDebugCellAPI,
                      ScanDebugConfig, SET_PROGRAM_VCC_WL_V, RESET_PROGRAM_VCC_WL_V,
                      SweepConfig)
from program_column_levels import DirectionalWlBracket

ROOT = Path(__file__).resolve().parent
VERSION = 2


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            # Browser readers and Windows security scanners can briefly hold
            # checkpoint files without delete sharing. Preserve atomic writes
            # and retry instead of aborting a healthy hardware run.
            time.sleep(0.1)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_plan(full=False):
    targets = [10., 30., 50., 70.] if full else [30., 50.]
    wl = {'set': [0.44,0.63,0.81,1.,1.12,1.25] if full else [.63,1.],
          'reset': [.94,1.32,1.69,2.07,2.44,2.88] if full else [1.32,2.07]}
    trials = []
    # Repetition blocks separate repeat tests in time. Controls precede each
    # target/direction block; test amplitude ascends within that block.
    for repeat in range(1,4):
        for target in targets:
            for mode in ('set','reset'):
                for voltage in [None, *wl[mode]]:
                    trials.append({'id': f't{len(trials)+1:03d}', 'repeat': repeat,
                                   'target_uS': target, 'mode': mode, 'wl_V': voltage,
                                   'kind': 'control' if voltage is None else 'test'})
    return {'version': VERSION, 'phase': 'full' if full else 'pilot', 'cell': [0,0],
            'read_vcc_set_V': .5, 'read_wl_V': 2.5, 'set_vcc_set_V': 2.3,
            'reset_vcc_set_V': 2.3, 'noise_allowance_uA': 3., 'bin_width_uS': 20.,
            'target_tolerance_uS': 4., 'confirmation_reads': 10, 'post_reads': 10,
            'max_preparation_pulses': 50, 'max_preparation_seconds': 600.,
            'max_qualification_attempts': 3, 'preparation_failure_policy': 'skip_trial_continue',
            'read_feedback_attempts': 3, 'post_settle_seconds': 1.,
            'control_wait_seconds': 5.,
            'control_timing_note': '5 s nominal command-gap control, not an exact latency-matched pulse substitute; actual times logged',
            'preparation_wl_V': {'set': list(SET_PROGRAM_VCC_WL_V), 'reset': list(RESET_PROGRAM_VCC_WL_V)},
            'nominal_clock_period_seconds': .0000005, 'nominal_post_DR_hold_cycles': 100,
            'timing_note': 'Existing runtime bitstream preserved; nominal 2 MHz / 50 us hold, not newly measured',
            'trials': trials}


def make_60_80_fill_plan():
    """Target the currently unmeasured 60--80 uS preparation-map tiles."""
    wl = {
        'set': [0.56, 0.63, 0.75, 0.81, 0.94, 1.00, 1.12, 1.25],
        'reset': [2.26, 2.32, 2.44, 2.57, 2.63, 2.75, 2.88],
    }
    trials = []
    for repeat in range(1, 4):
        for mode in ('set', 'reset'):
            for voltage in wl[mode]:
                trials.append({'id': f't{len(trials)+1:03d}', 'repeat': repeat,
                               'target_uS': 70.0, 'mode': mode, 'wl_V': voltage,
                               'kind': 'test'})
    plan = make_plan(False)
    plan.update(phase='targeted_60_80', trials=trials,
                characterization_note=(
                    'Three independently prepared tests for each of the 15 missing '
                    '60--80 uS SET/RESET WL bins; no interpolation.'))
    return plan


def make_corner_fill_plan():
    """Add measured coverage to the gray SET top-left and RESET top-right."""
    combinations = {
        'set': [(10.0, 1.25), (10.0, 1.37), (30.0, 1.37), (50.0, 1.37)],
        'reset': [(90.0, 2.26), (90.0, 2.44), (90.0, 2.63), (90.0, 2.88)],
    }
    trials = []
    for repeat in range(1, 4):
        for mode in ('set', 'reset'):
            for target, voltage in combinations[mode]:
                trials.append({'id': f't{len(trials)+1:03d}', 'repeat': repeat,
                               'target_uS': target, 'mode': mode, 'wl_V': voltage,
                               'kind': 'test'})
    plan = make_plan(False)
    plan.update(phase='corner_fill', trials=trials,
                characterization_note=(
                    'Three independently prepared repetitions at four SET top-left '
                    'and four RESET top-right conductance/WL points; no interpolation.'))
    return plan


def provenance():
    paths = [DEFAULT_READ_CALIBRATION, FPGA_BITSTREAM_DIR / FPGA_RUNTIME_BITSTREAM,
             ROOT/'characterize_cell.py', ROOT/'cell_api.py', ROOT/'program_column_levels.py',
             ROOT/'prerequisites/fpga_zynq7020/runtime_vio_daemon.tcl',
             ROOT/'prerequisites/fpga_zynq7020/caravel_scan_debug_runtime.v']
    return {str(p.relative_to(ROOT)): file_hash(p) for p in paths}


@contextmanager
def local_lock(folder):
    """OS-released process lock: no stale-lock deletion or parallel same-run writer."""
    stream = (folder/'runner.lock').open('a+b')
    stream.seek(0,2)
    if stream.tell() == 0:
        stream.write(b'0')
        stream.flush()
    stream.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


class PreparationFailed(Exception):
    pass


class StopRequested(Exception):
    pass


class Runner:
    def __init__(self, api, folder, plan, clock=time.monotonic, sleep=time.sleep):
        self.api, self.folder, self.plan = api, folder, plan
        self.clock, self.sleep = clock, sleep
        self.cell = CellAddress(*plan['cell'])
        self.trial = None
        self.started = None

    def save(self):
        self.trial['updated_utc'] = utc()
        atomic_json(self.folder/'trials'/f'{self.trial["id"]}.json', self.trial)

    def event(self, kind, **fields):
        event = {'utc':utc(), 'elapsed_seconds':self.clock()-self.started, 'kind':kind, **fields}
        self.trial['events'].append(event)
        self.save()
        with (self.folder/'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'trial_id':self.trial['id'], **event}, allow_nan=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())

    def check(self, deadline=None):
        if (self.folder/'STOP').exists():
            raise StopRequested('STOP requested; no further operation submitted')
        if deadline is not None and self.clock() >= deadline:
            raise PreparationFailed('preparation_time_limit')

    def read(self, stage, deadline=None):
        self.check(deadline)
        began = self.clock()
        result = self.api._pulse_and_capture(self.cell, 'read', self.api.config.read_rails,
                                             f'{self.trial["id"]}_{stage}')
        raw = asdict(result)
        current = result.current_uA
        if not result.ok or current is None or not math.isfinite(current) or current < -3.:
            raise RuntimeError('Invalid READ result; no programming may follow')
        record = {'current_uA':current, 'conductance_uS':current/.5,
                  'feedback_attempts':result.feedback_attempts,
                  'elapsed_seconds':self.clock()-self.started,
                  'duration_seconds':self.clock()-began, 'capture':result.local_output_dir}
        self.event(stage, reading=record, result=raw)
        self.check(deadline)
        return record

    def pulse(self, mode, wl, role, deadline=None):
        self.check(deadline)
        # Durable intent before transmission: resume never replays this trial.
        self.trial['pending_pulse'] = {'mode':mode, 'wl_V':wl, 'role':role}
        self.event('pulse_intent', **self.trial['pending_pulse'])
        began = self.clock()
        result = self.api._program_pulse(self.cell, mode, RailVoltages(2.3,wl),
                                         f'{self.trial["id"]}_{role}_{mode}_pulse')
        if not result.ok:
            raise RuntimeError('Programming acknowledgement uncertain')
        self.trial['pending_pulse'] = None
        self.trial[f'{role}_pulse_count'] += 1
        self.event('pulse_ack', role=role, result=asdict(result), duration_seconds=self.clock()-began)
        self.check(deadline)

    def inside(self, reading):
        return abs(reading['conductance_uS']-self.trial['target_uS']) <= self.plan['target_tolerance_uS']

    def prepare(self):
        deadline = self.clock()+self.plan['max_preparation_seconds']
        target, tolerance = self.trial['target_uS'], self.plan['target_tolerance_uS']
        approach = self.trial['mode']
        brackets = {mode: DirectionalWlBracket([RailVoltages(2.3,v) for v in self.plan['preparation_wl_V'][mode]])
                    for mode in ('set','reset')}
        current = self.read('preparation_initial_read', deadline)
        conditioned = False
        last_mode = None
        qualification_attempts = 0
        while True:
            self.check(deadline)
            g = current['conductance_uS']
            # Force a documented approach from below for SET, above for RESET.
            correct_side = g < target-tolerance if approach=='set' else g > target+tolerance
            if not conditioned and correct_side:
                conditioned = True
                self.event('approach_conditioned', direction=approach)
            if conditioned and last_mode == approach and self.inside(current):
                qualification_attempts += 1
                self.trial['qualification_attempts'] = qualification_attempts
                self.event('qualification_started', attempt=qualification_attempts)
                pre = []
                failure_reason = 'no_ten_read_streak'
                for _ in range(30):
                    reading = self.read('pre_read', deadline)
                    if reading['feedback_attempts'] > 1:
                        pre.clear()
                    if not self.inside(reading):
                        current = reading
                        failure_reason = 'unstable_preparation'
                        break
                    pre.append(reading)
                    if len(pre)==10:
                        self.trial['pre_reads'] = pre
                        self.trial['approach_direction'] = last_mode
                        self.event('preparation_qualified')
                        return
                if qualification_attempts >= self.plan['max_qualification_attempts']:
                    raise PreparationFailed(failure_reason)
                self.event('qualification_retry', reason=failure_reason,
                           next_attempt=qualification_attempts+1)
                # Re-prepare from the prescribed side; retain the same overall
                # pulse/time budgets and direction brackets for this trial.
                conditioned = False
                last_mode = None
            if self.trial['preparation_pulse_count'] >= self.plan['max_preparation_pulses']:
                raise PreparationFailed('preparation_pulse_limit')
            if not conditioned or (self.inside(current) and last_mode != approach):
                mode = 'reset' if approach=='set' else 'set'
                conditioned = False
            else:
                mode = 'set' if g < target-tolerance else 'reset'
            candidate = brackets[mode].next_rail()
            if candidate is None:
                raise PreparationFailed(f'{mode}_bracket_exhausted')
            rails, selection = candidate
            self.event('preparation_choice', mode=mode, selection=selection, bracket=brackets[mode].snapshot())
            self.pulse(mode, rails.vcc_wl_set_v, 'preparation', deadline)
            current = self.read('preparation_verify', deadline)
            g = current['conductance_uS']
            if g < target-tolerance or g > target+tolerance:
                brackets[mode].observe(rails, insufficient=g < target-tolerance if mode=='set' else g > target+tolerance)
            last_mode = mode

    def run_trial(self, spec):
        self.started = self.clock()
        self.trial = {**spec, 'status':'running', 'started_utc':utc(), 'events':[],
                      'preparation_pulse_count':0, 'test_pulse_count':0, 'pending_pulse':None,
                      'pre_reads':[], 'post_reads':[]}
        self.save()
        print(f'{spec["id"]}: {spec["kind"]} {spec["mode"]}, target {spec["target_uS"]:g} uS, WL {spec["wl_V"]}', flush=True)
        try:
            try:
                self.prepare()
            except PreparationFailed as exc:
                self.trial.update(status='skipped_preparation', reason=str(exc))
                self.event('trial_skipped')
                return self.trial
            self.check()
            if spec['kind']=='control':
                self.event('control_wait_started', seconds=self.plan['control_wait_seconds'])
                self.sleep(self.plan['control_wait_seconds'])
                self.check()
            else:
                self.pulse(spec['mode'], spec['wl_V'], 'test')
            self.sleep(self.plan['post_settle_seconds'])
            for _ in range(self.plan['post_reads']):
                self.trial['post_reads'].append(self.read('post_read'))
                self.save()
            pre = [r['conductance_uS'] for r in self.trial['pre_reads']]
            post = [r['conductance_uS'] for r in self.trial['post_reads']]
            sign = 1 if spec['mode']=='set' else -1
            self.trial.update(status='complete', g_before_mean_uS=statistics.mean(pre),
                              g_after_mean_uS=statistics.mean(post),
                              immediate_delta_uS=sign*(post[0]-pre[-1]),
                              sustained_delta_uS=sign*(statistics.mean(post)-statistics.mean(pre)),
                              pre_stdev_uS=statistics.stdev(pre), post_stdev_uS=statistics.stdev(post),
                              immediate_read_retried=self.trial['post_reads'][0]['feedback_attempts']>1,
                              immediate_read_note='First valid post-read; not time-zero response. Inspect timestamps and retry flag.')
            self.event('trial_complete')
            return self.trial
        except BaseException as exc:
            self.trial.update(status='interrupted' if isinstance(exc,(KeyboardInterrupt,StopRequested)) else 'failed',
                              reason=f'{type(exc).__name__}: {exc}',
                              uncertain_pulse=bool(self.trial['pending_pulse']))
            self.save()
            raise


def make_config(folder):
    return ScanDebugConfig(run_dir=folder/'captures', read_calibration_path=DEFAULT_READ_CALIBRATION,
        read_rails=RailVoltages(.5,2.5), attempts=1, read_feedback_attempts=3,
        set_sweep=SweepConfig.from_ranges(vcc_set_v=[2.3],vcc_wl_set_v=SET_PROGRAM_VCC_WL_V,threshold_uA=25,direction='above'),
        reset_sweep=SweepConfig.from_ranges(vcc_set_v=[2.3],vcc_wl_set_v=RESET_PROGRAM_VCC_WL_V,threshold_uA=10,direction='below'),
        zynq_host=os.environ.get('SCAN_DEBUG_ZYNQ_HOST','geethika@100.116.216.70'),
        zynq_password=os.environ.get('SCAN_DEBUG_ZYNQ_PASSWORD') or None,
        saleae_host=os.environ.get('SCAN_DEBUG_SALEAE_HOST','ubuntu-24-04@100.98.132.51'),
        saleae_usb_recovery_enabled=False, hardware_queue_timeout_seconds=30,
        hardware_queue_stale_seconds=315360000, defer_capture_copy=False)


def continuation_snapshot(source, plan, hashes):
    """Explicit new segment: retain old records; never rewrite old provenance."""
    source = source.resolve()
    with local_lock(source):
        old_plan = json.loads((source/'plan.json').read_text())
        old_status = json.loads((source/'status.json').read_text())
        old_hashes = json.loads((source/'provenance.json').read_text())
        if old_status.get('status') not in ('stopped_needs_review','complete_with_missing_trials','complete'):
            raise ValueError('Source run is not stopped; refusing concurrent continuation')
        if (source/'STOP').exists():
            raise ValueError('Source STOP marker requires deliberate review/removal')
        policy_fields = {'version','max_consecutive_preparation_failures',
                         'max_qualification_attempts','preparation_failure_policy'}
        if {k:v for k,v in old_plan.items() if k not in policy_fields} != {k:v for k,v in plan.items() if k not in policy_fields}:
            raise ValueError('Continuation changes measurement settings, not just preparation policy')
        if {k:v for k,v in old_hashes.items() if k != 'characterize_cell.py'} != {k:v for k,v in hashes.items() if k != 'characterize_cell.py'}:
            raise ValueError('Calibration, hardware code, or bitstream changed; refusing continuation')
        specs = {t['id']:t for t in plan['trials']}
        trials = []
        for path in sorted((source/'trials').glob('*.json')):
            trial = json.loads(path.read_text())
            if trial.get('status') not in ('complete','skipped_preparation','abandoned') or trial.get('pending_pulse'):
                raise ValueError('Uncertain/incomplete trial must be reviewed; never automatically replayed')
            spec = specs.get(trial.get('id'))
            if spec is None or any(trial.get(k) != v for k,v in spec.items()):
                raise ValueError('Source trial does not match the selected measurement grid')
            trials.append({**trial, 'carried_from_run': str(source)})
        return trials, {'source_run':str(source), 'source_plan':old_plan,
                        'source_provenance':old_hashes, 'source_status':old_status,
                        'continued_utc':utc(),
                        'policy_change':'Up to 3 qualification attempts within 50 pulses/600 s; skip unqualified trials and continue'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Explicit hardware execution; otherwise offline preview only')
    parser.add_argument('--full-grid',action='store_true')
    parser.add_argument('--fill-60-80', action='store_true',
                        help='Run the reviewed 45-trial targeted 60--80 uS coverage plan')
    parser.add_argument('--corner-fill', action='store_true',
                        help='Run the reviewed 24-trial SET top-left / RESET top-right coverage plan')
    parser.add_argument('--reviewed-pilot',type=Path,help='Required explicit pilot review acknowledgement for full-grid execution')
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--continue-from',type=Path,help='New segment with changed preparation policy; retain terminal trials, never replay them')
    parser.add_argument('--abandon-incomplete',action='store_true',help='With resume, abandon interrupted trial without replay')
    args = parser.parse_args(argv)
    selected_plans = sum((args.full_grid, args.fill_60_80, args.corner_fill))
    if selected_plans > 1:
        parser.error('Choose only one of --full-grid, --fill-60-80, or --corner-fill')
    plan = (make_corner_fill_plan() if args.corner_fill else
            make_60_80_fill_plan() if args.fill_60_80 else make_plan(args.full_grid))
    if not args.execute:
        print(json.dumps(plan,indent=2))
        return 0
    if args.full_grid:
        if not args.reviewed_pilot:
            parser.error('Full grid requires --reviewed-pilot after reviewing pilot results')
        pilot_plan=json.loads((args.reviewed_pilot/'plan.json').read_text())
        pilot_status=json.loads((args.reviewed_pilot/'status.json').read_text())
        if pilot_plan['phase']!='pilot' or pilot_status['status']!='complete':
            parser.error('Pilot must have completed all trials successfully before full-grid execution')
    if args.resume and not args.run_dir:
        parser.error('--resume requires the exact --run-dir')
    if args.abandon_incomplete and not args.resume:
        parser.error('--abandon-incomplete requires --resume')
    if args.continue_from and args.resume:
        parser.error('Use either --continue-from (new segment) or --resume (unchanged segment)')
    carried, continuation = ([], None)
    if args.continue_from:
        carried, continuation = continuation_snapshot(args.continue_from, plan, provenance())
    folder=(args.run_dir or ROOT/'runs'/f'characterization_r00c00_{plan["phase"]}_{datetime.now():%Y%m%d_%H%M%S_%f}').resolve()
    if not args.resume:
        folder.mkdir(parents=True,exist_ok=False)
    elif not folder.is_dir():
        parser.error('Resume directory does not exist')
    with local_lock(folder):
        if (folder/'STOP').exists():
            parser.error('STOP marker present; remove it deliberately before resuming')
        hashes=provenance()
        if args.resume:
            if json.loads((folder/'plan.json').read_text()) != plan:
                parser.error('Saved plan differs; refusing resume')
            if json.loads((folder/'provenance.json').read_text()) != hashes:
                parser.error('Code, calibration, or bitstream changed; refusing mixed-condition resume')
        else:
            (folder/'trials').mkdir()
            atomic_json(folder/'plan.json',plan)
            atomic_json(folder/'provenance.json',hashes)
            if continuation:
                atomic_json(folder/'continued_from.json',continuation)
                for trial in carried:
                    atomic_json(folder/'trials'/f'{trial["id"]}.json',trial)
        prior={p.stem:json.loads(p.read_text()) for p in (folder/'trials').glob('*.json')}
        for trial in prior.values():
            if trial['status'] in ('running','interrupted','failed'):
                if not args.abandon_incomplete:
                    parser.error(f'{trial["id"]} incomplete or uncertain; review before --abandon-incomplete. It will not be replayed.')
                trial.update(status='abandoned',abandoned_utc=utc())
                atomic_json(folder/'trials'/f'{trial["id"]}.json',trial)
        status={'status':'running','run_dir':str(folder),'started_utc':utc(), 'total_trials':len(plan['trials'])}
        atomic_json(folder/'status.json',status)
        print(f'Pilot output: {folder}\nCtrl+C or a STOP file requests a safe stop between operations.',flush=True)
        try:
            api=ScanDebugCellAPI(make_config(folder))
            if api._read_noise_allowance()!=3.:
                raise ValueError('Expected unchanged 3 uA calibration allowance')
            runner=Runner(api,folder,plan)
            # Existing hardware queue used; never start a competing capture.
            with api.hardware_queue('characterization'):
                for spec in plan['trials']:
                    if spec['id'] in prior:
                        continue
                    result=runner.run_trial(spec)
                    prior[spec['id']]=result
                    status.update(last_trial=spec['id'],last_trial_status=result['status'],updated_utc=utc())
                    atomic_json(folder/'status.json',status)
                    if result['status']=='skipped_preparation':
                        print(f'{spec["id"]}: preparation failed within budget; skipped, continuing.',flush=True)
            status['status']='complete' if all(t['status']=='complete' for t in prior.values()) else 'complete_with_missing_trials'
        except BaseException as exc:
            status.update(status='stopped_needs_review',error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            status['updated_utc']=utc()
            atomic_json(folder/'status.json',status)
            # Export is offline and cannot issue hardware commands.
            from characterize_report import generate
            try:
                generate(folder)
            except Exception as exc:
                status['report_error']=f'{type(exc).__name__}: {exc}'
                atomic_json(folder/'status.json',status)
                print('Report generation failed; raw data/checkpoints preserved. Rerun characterize_report.py.',flush=True)
        print(f'{status["status"]}: {folder}',flush=True)
        return 0 if status['status']=='complete' and 'report_error' not in status else 1


if __name__=='__main__':
    raise SystemExit(main())
