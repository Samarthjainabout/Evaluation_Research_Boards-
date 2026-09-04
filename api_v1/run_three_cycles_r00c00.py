"""User-launched three-cycle batch for (0,0); no automatic failed-cycle replay."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

from cell_api import SET_PROGRAM_VCC_SET_V

ROOT = Path(__file__).resolve().parents[1]


def main():
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    batch_dir = ROOT/'api_v1/runs'/f'cycle_batch_r00c00_{stamp}'
    batch_dir.mkdir(parents=True, exist_ok=False)
    summary = {'cell':[0,0], 'requested_cycles':3, 'qualified_cycles':0,
               'set_target_uS':50, 'reset_target_uS':20, 'reset_vcc_set_max_V':3.3,
               'read_voltage_V':0.5, 'confirmation_reads':10, 'runs':[], 'status':'running'}
    summary_path = batch_dir/'batch_summary.json'
    def save():
        summary_path.write_text(json.dumps(summary, indent=2))
    save()
    print(f'Batch summary: {summary_path}', flush=True)
    for number in range(1,4):
        run = ROOT/'api_v1/runs'/f'cycle_r00c00_repeat{number}of3_{stamp}'
        run.mkdir(exist_ok=False)
        command = [sys.executable, str(ROOT/'api_v1/scan_debug_cli.py'), 'cycle',
                   '--row','0','--col','0','--run-dir',str(run),
                   '--read-vcc-set','0.5','--read-vcc-wl-set','2.5',
                   '--set-threshold','25','--reset-threshold','10',
                   '--set-vcc-set',str(SET_PROGRAM_VCC_SET_V),'--reset-vcc-set','2.3,2.7,3.1,3.3',
                   '--confirm-reads','10','--read-feedback-attempts','3',
                   '--hardware-queue-timeout-seconds','30']
        entry = {'cycle':number, 'run_dir':str(run), 'status':'running'}
        summary['runs'].append(entry)
        save()
        print(f'Starting cycle {number}/3: {run.name}', flush=True)
        qualified = False
        returncode = None
        try:
            with (run/'gui_command.log').open('w') as log:
                result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT)
            returncode = result.returncode
            outcome_file = run/'cell_cycles.jsonl'
            outcome = json.loads(outcome_file.read_text().splitlines()[-1])
            qualified = outcome.get('set',{}).get('target_hit') is True and outcome.get('reset',{}).get('target_hit') is True
        except (OSError, ValueError, IndexError, AttributeError) as exc:
            entry['error'] = f'{type(exc).__name__}: {exc}'
        entry['exit_code'] = returncode
        entry['qualified'] = qualified
        if returncode != 0 or not qualified:
            entry['status'] = 'stopped'
            summary['status'] = 'stopped_needs_review'
            save()
            print(f'Stopped after cycle {number}; inspect {run / "gui_command.log"}. No cycle will be replayed.', flush=True)
            return 1
        entry['status'] = 'qualified'
        summary['qualified_cycles'] += 1
        save()
        print(f'Cycle {number}/3 qualified SET and RESET.', flush=True)
    summary['status'] = 'complete'
    save()
    print('All three additional cycles qualified.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
