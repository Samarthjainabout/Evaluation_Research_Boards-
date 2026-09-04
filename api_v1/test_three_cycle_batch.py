import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import run_three_cycles_r00c00 as batch


class BatchTests(unittest.TestCase):
    def run_batch(self, failure=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                run = Path(command[command.index('--run-dir') + 1])
                if failure != 'missing':
                    content = '{broken' if failure == 'malformed' else json.dumps({
                        'set': {'target_hit': True},
                        'reset': {'target_hit': failure != 'unqualified'},
                    })
                    (run / 'cell_cycles.jsonl').write_text(content)
                return SimpleNamespace(returncode=1 if failure == 'exit' else 0)

            with patch.object(batch, 'ROOT', root), patch.object(
                batch.subprocess, 'run', side_effect=fake_run
            ), contextlib.redirect_stdout(io.StringIO()):
                result = batch.main()
            summary = json.loads(next(root.glob('api_v1/runs/cycle_batch*/batch_summary.json')).read_text())
            return result, summary, commands

    def test_three_fresh_qualified_cycles(self):
        result, summary, commands = self.run_batch()
        self.assertEqual(result, 0)
        self.assertEqual(summary['qualified_cycles'], 3)
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(len({cmd[cmd.index('--run-dir') + 1] for cmd in commands}), 3)
        for cmd in commands:
            for flag, value in {'--row': '0', '--col': '0', '--set-threshold': '25',
                                '--reset-threshold': '10', '--confirm-reads': '10',
                                '--set-vcc-set': '2.3',
                                '--reset-vcc-set': '2.3,2.7,3.1,3.3',
                                '--read-vcc-set': '0.5'}.items():
                self.assertEqual(cmd[cmd.index(flag) + 1], value)

    def test_stop_on_failure_without_replay(self):
        for failure in ('missing', 'malformed', 'unqualified', 'exit'):
            with self.subTest(failure=failure):
                result, summary, commands = self.run_batch(failure)
                self.assertEqual(result, 1)
                self.assertEqual(len(commands), 1)
                self.assertEqual(summary['qualified_cycles'], 0)
                self.assertEqual(summary['status'], 'stopped_needs_review')


if __name__ == '__main__':
    unittest.main()
