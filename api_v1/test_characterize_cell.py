import contextlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cell_api import CellAddress, CellOperationResult, RailVoltages
from characterize_cell import (Runner, StopRequested, atomic_json, continuation_snapshot,
                               main, make_config, make_corner_fill_plan, make_plan)
from characterize_report import generate


class FakeAPI:
    def __init__(self,g=22.):
        self.config=SimpleNamespace(read_rails=RailVoltages(.5,2.5))
        self.g=g
        self.pulses=[]
        self.reads=0
        self.fail_test=False
        self.retry_at=None
        self.bad_pre=False
        self.fixed=False

    def _pulse_and_capture(self,cell,mode,rails,stage):
        self.reads+=1
        g=self.g+10 if self.bad_pre and stage.endswith('_pre_read') else self.g
        return CellOperationResult(cell,'read','0x0000',rails,g*.5,ok=True,
                                   feedback_attempts=2 if self.reads==self.retry_at else 1,
                                   local_output_dir=f'FAKE_ONLY_{self.reads}')

    def _program_pulse(self,cell,mode,rails,stage):
        self.pulses.append((mode,rails,stage))
        if self.fail_test and '_test_' in stage:
            raise TimeoutError('synthetic ambiguous acknowledgement')
        if not self.fixed:
            self.g+=8 if mode=='set' else -8
        return CellOperationResult(cell,mode,'0x8000' if mode=='set' else '0x0000',rails,None,ok=True)


class CharacterizationTests(unittest.TestCase):
    def test_atomic_json_retries_transient_windows_file_lock(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'checkpoint.json'
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError('synthetic reader lock')
                return real_replace(source, destination)

            with patch('characterize_cell.os.replace', side_effect=flaky_replace), \
                    patch('characterize_cell.time.sleep') as sleep:
                atomic_json(target, {'status': 'running'})
            self.assertEqual(json.loads(target.read_text()), {'status': 'running'})
            self.assertEqual(attempts, 2)
            sleep.assert_called_once_with(0.1)

    def setup_runner(self,folder,api,plan=None,**kwargs):
        (folder/'trials').mkdir()
        plan=plan or make_plan()
        atomic_json(folder/'plan.json',plan)
        return Runner(api,folder,plan,sleep=lambda _:None,**kwargs)

    def spec(self,kind='test',mode='set'):
        return {'id':'t001','kind':kind,'mode':mode,'target_uS':30.,'wl_V':.63 if kind=='test' else None,'repeat':1}

    def test_grid_counts_and_fixed_rails(self):
        for full,n,c in [(False,24,12),(True,144,24)]:
            plan=make_plan(full)
            self.assertEqual(sum(t['kind']=='test' for t in plan['trials']),n)
            self.assertEqual(sum(t['kind']=='control' for t in plan['trials']),c)
            self.assertEqual(plan['set_vcc_set_V'],2.3)
            self.assertEqual(plan['reset_vcc_set_V'],2.3)
            self.assertEqual(len({t['id'] for t in plan['trials']}),n+c)
        cfg=make_config(Path('unused'))
        self.assertEqual(cfg.attempts,1)
        self.assertEqual(cfg.read_feedback_attempts,3)
        self.assertEqual(cfg.read_rails,RailVoltages(.5,2.5))
        self.assertEqual(cfg.set_sweep.vcc_set_v,(2.3,))
        self.assertEqual(cfg.reset_sweep.vcc_set_v,(2.3,))
        self.assertFalse(cfg.saleae_usb_recovery_enabled)

    def test_corner_fill_plan_has_24_reviewed_points(self):
        plan=make_corner_fill_plan()
        self.assertEqual(plan['phase'],'corner_fill')
        self.assertEqual(len(plan['trials']),24)
        self.assertEqual(len({t['id'] for t in plan['trials']}),24)
        self.assertTrue(all(t['kind']=='test' for t in plan['trials']))
        self.assertEqual(sum(t['mode']=='set' for t in plan['trials']),12)
        self.assertEqual(sum(t['mode']=='reset' for t in plan['trials']),12)
        self.assertEqual({t['target_uS'] for t in plan['trials'] if t['mode']=='reset'},{90.0})

    def test_exactly_one_test_and_ten_pre_post_with_correct_sign(self):
        for mode,g in [('set',22.),('reset',38.)]:
            with self.subTest(mode=mode),TemporaryDirectory() as d:
                api=FakeAPI(g)
                runner=self.setup_runner(Path(d),api)
                trial=runner.run_trial(self.spec(mode=mode))
                self.assertEqual(trial['status'],'complete')
                self.assertEqual(trial['test_pulse_count'],1)
                self.assertEqual(len(trial['pre_reads']),10)
                self.assertEqual(len(trial['post_reads']),10)
                self.assertEqual(trial['immediate_delta_uS'],8.)
                self.assertEqual(trial['sustained_delta_uS'],8.)
                self.assertEqual(trial['approach_direction'],mode)
                self.assertTrue(all(p[1].vcc_set_v==2.3 for p in api.pulses))
                coverage=generate(Path(d),plots=False)
                self.assertEqual(sum(r['count'] for r in coverage),1 if mode=='set' else 0)
                self.assertTrue((Path(d)/'report/reads.csv').exists())

    def test_control_does_not_send_test_pulse(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            trial=self.setup_runner(Path(d),api).run_trial(self.spec(kind='control'))
            self.assertEqual(trial['test_pulse_count'],0)
            self.assertEqual(trial['sustained_delta_uS'],0.)
            self.assertEqual(len(api.pulses),1) # preparation only

    def test_invalid_preparation_skips_test(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            api.bad_pre=True
            trial=self.setup_runner(Path(d),api).run_trial(self.spec())
            self.assertEqual(trial['status'],'skipped_preparation')
            self.assertEqual(trial['test_pulse_count'],0)

    def test_retry_restarts_pre_streak(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            api.retry_at=5
            trial=self.setup_runner(Path(d),api).run_trial(self.spec())
            self.assertEqual(len(trial['pre_reads']),10)
            self.assertGreater(api.reads,22)
            self.assertEqual(trial['pre_reads'][0]['feedback_attempts'],2)

    def test_ambiguous_test_pulse_is_not_retried(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            api.fail_test=True
            runner=self.setup_runner(Path(d),api)
            with self.assertRaises(TimeoutError):
                runner.run_trial(self.spec())
            trial=json.loads((Path(d)/'trials/t001.json').read_text())
            self.assertEqual(trial['status'],'failed')
            self.assertTrue(trial['uncertain_pulse'])
            self.assertEqual(sum('_test_' in p[2] for p in api.pulses),1)
            self.assertEqual(trial['post_reads'],[])

    def test_pulse_budget(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            api.fixed=True
            plan=make_plan()
            plan['max_preparation_pulses']=2
            trial=self.setup_runner(Path(d),api,plan).run_trial(self.spec())
            self.assertEqual(trial['reason'],'preparation_pulse_limit')
            self.assertEqual(len(api.pulses),2)

    def test_time_budget(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            ticks=iter(range(0,10000,601))
            trial=self.setup_runner(Path(d),api,clock=lambda:next(ticks)).run_trial(self.spec())
            self.assertEqual(trial['reason'],'preparation_time_limit')
            self.assertEqual(api.pulses,[])

    def test_stop_before_any_operation(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            runner=self.setup_runner(Path(d),api)
            (Path(d)/'STOP').touch()
            with self.assertRaises(StopRequested):
                runner.run_trial(self.spec())
            self.assertEqual(api.reads,0)
            self.assertEqual(api.pulses,[])

    def test_default_is_offline_preview(self):
        with patch('characterize_cell.ScanDebugCellAPI') as api,contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main([]),0)
            self.assertEqual(json.loads(out.getvalue())['phase'],'pilot')
            api.assert_not_called()

    def test_full_grid_requires_review(self):
        with patch('characterize_cell.ScanDebugCellAPI') as api,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--execute','--full-grid'])
            api.assert_not_called()

    def saved_run(self, folder, plan, prior=()):
        (folder/'trials').mkdir()
        atomic_json(folder/'plan.json',plan)
        atomic_json(folder/'provenance.json',{'synthetic':'test'})
        for trial in prior:
            atomic_json(folder/'trials'/f'{trial["id"]}.json',trial)

    def test_resume_refuses_uncertain_trial_before_hardware(self):
        with TemporaryDirectory() as d:
            folder=Path(d)
            self.saved_run(folder,make_plan(),[{'id':'t001','status':'failed','pending_pulse':{'role':'test'}}])
            with patch('characterize_cell.provenance',return_value={'synthetic':'test'}),patch('characterize_cell.ScanDebugCellAPI') as api,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(['--execute','--resume','--run-dir',str(folder)])
                api.assert_not_called()

    def test_resume_refuses_changed_provenance(self):
        with TemporaryDirectory() as d:
            folder=Path(d)
            self.saved_run(folder,make_plan())
            with patch('characterize_cell.provenance',return_value={'synthetic':'changed'}),patch('characterize_cell.ScanDebugCellAPI') as api,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(['--execute','--resume','--run-dir',str(folder)])
                api.assert_not_called()

    def test_explicit_abandon_never_replays_trial(self):
        with TemporaryDirectory() as d:
            folder=Path(d)
            plan=make_plan()
            plan['trials']=plan['trials'][:3]
            self.saved_run(folder,plan,[{'id':'t001','status':'complete'}, {'id':'t002','status':'failed','pending_pulse':{'role':'test'}}])
            with patch('characterize_cell.make_plan',return_value=plan),patch('characterize_cell.provenance',return_value={'synthetic':'test'}),patch('characterize_cell.ScanDebugCellAPI') as api,patch('characterize_cell.Runner') as runner,patch('characterize_report.generate'):
                api.return_value._read_noise_allowance.return_value=3.
                runner.return_value.run_trial.side_effect=lambda spec:{**spec,'status':'complete'}
                self.assertEqual(main(['--execute','--resume','--abandon-incomplete','--run-dir',str(folder)]),1)
                runner.return_value.run_trial.assert_called_once_with(plan['trials'][2])
                abandoned=json.loads((folder/'trials/t002.json').read_text())
                self.assertEqual(abandoned['status'],'abandoned')
                self.assertEqual(abandoned['pending_pulse'],{'role':'test'})

    def test_preparation_failures_skip_and_continue_entire_pilot(self):
        with TemporaryDirectory() as d:
            folder=Path(d)/'new_run'
            with patch('characterize_cell.provenance',return_value={'synthetic':'test'}),patch('characterize_cell.ScanDebugCellAPI') as api,patch('characterize_cell.Runner') as runner,patch('characterize_report.generate'):
                api.return_value._read_noise_allowance.return_value=3.
                runner.return_value.run_trial.side_effect=lambda spec:{**spec,'status':'skipped_preparation'}
                self.assertEqual(main(['--execute','--run-dir',str(folder)]),1)
                self.assertEqual(runner.return_value.run_trial.call_count,36)
                self.assertEqual(json.loads((folder/'status.json').read_text())['status'],'complete_with_missing_trials')

    def test_qualification_attempts_are_bounded(self):
        with TemporaryDirectory() as d:
            api=FakeAPI()
            original=api._pulse_and_capture
            def unstable(cell,mode,rails,stage):
                result=original(cell,mode,rails,stage)
                if stage.endswith('_pre_read'):
                    api.g=38.
                    result.current_uA=19.
                return result
            api._pulse_and_capture=unstable
            trial=self.setup_runner(Path(d),api).run_trial(self.spec())
            self.assertEqual(trial['status'],'skipped_preparation')
            self.assertEqual(trial['qualification_attempts'],3)
            self.assertEqual(trial['test_pulse_count'],0)
            self.assertEqual(sum(e['kind']=='qualification_retry' for e in trial['events']),2)

    def test_continuation_preserves_records_and_rejects_uncertain_pulses(self):
        with TemporaryDirectory() as d:
            folder=Path(d)
            old_plan=make_plan()
            old_plan.pop('max_qualification_attempts')
            old_plan.pop('preparation_failure_policy')
            old_plan.update(version=1,max_consecutive_preparation_failures=3)
            trial={**old_plan['trials'][0],'status':'skipped_preparation','pending_pulse':None}
            self.saved_run(folder,old_plan,[trial])
            atomic_json(folder/'status.json',{'status':'stopped_needs_review'})
            carried,metadata=continuation_snapshot(folder,make_plan(),{'synthetic':'test'})
            self.assertEqual(carried[0]['id'],'t001')
            self.assertEqual(metadata['source_plan']['version'],1)
            self.assertEqual(json.loads((folder/'trials/t001.json').read_text()),trial)
            trial['pending_pulse']={'role':'test'}
            atomic_json(folder/'trials/t001.json',trial)
            with self.assertRaisesRegex(ValueError,'Uncertain'):
                continuation_snapshot(folder,make_plan(),{'synthetic':'test'})

    def test_continuation_rejects_changed_hardware_provenance(self):
        with TemporaryDirectory() as d:
            folder=Path(d)
            self.saved_run(folder,make_plan())
            atomic_json(folder/'status.json',{'status':'stopped_needs_review'})
            with self.assertRaisesRegex(ValueError,'Calibration'):
                continuation_snapshot(folder,make_plan(),{'synthetic':'changed'})

    def test_mocked_full_pilot_and_report_failure_preserves_data(self):
        with TemporaryDirectory() as d:
            folder=Path(d)/'new_run'
            with patch('characterize_cell.provenance',return_value={'synthetic':'test'}),patch('characterize_cell.ScanDebugCellAPI') as api,patch('characterize_cell.Runner') as runner,patch('characterize_report.generate',side_effect=OSError('synthetic export failure')):
                api.return_value._read_noise_allowance.return_value=3.
                runner.return_value.run_trial.side_effect=lambda spec:{**spec,'status':'complete'}
                self.assertEqual(main(['--execute','--run-dir',str(folder)]),1)
                self.assertEqual(runner.return_value.run_trial.call_count,36)
                status=json.loads((folder/'status.json').read_text())
                self.assertEqual(status['status'],'complete')
                self.assertIn('synthetic export failure',status['report_error'])


if __name__=='__main__':
    unittest.main()
