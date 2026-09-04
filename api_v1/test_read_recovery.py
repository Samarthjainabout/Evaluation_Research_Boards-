import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from cell_api import CellAddress, CellOperationResult, InvalidReadFeedbackError, RailVoltages, ScanDebugCellAPI, ScanDebugConfig
from scan_debug_cli import apply_saved_experiment_defaults


class ReadRecoveryTests(unittest.TestCase):
    def result(self, attempts=1):
        return CellOperationResult(CellAddress(0,0), 'read', '0x0000', RailVoltages(0.5,2.5),
                                   30.0, ok=True, feedback_attempts=attempts)

    def test_invalid_feedback_retries_only_read(self):
        with TemporaryDirectory() as directory, patch('cell_api.time.sleep'):
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(directory), read_feedback_attempts=3))
            api._pulse_and_capture_once = Mock(side_effect=[InvalidReadFeedbackError('noise'), self.result()])
            api._program_pulse = Mock()
            result = api.read(0,0)
            self.assertEqual(result.feedback_attempts, 2)
            self.assertEqual(api._pulse_and_capture_once.call_count, 2)
            self.assertTrue(all(c.args[1] == 'read' for c in api._pulse_and_capture_once.call_args_list))
            api._program_pulse.assert_not_called()

    def test_retry_budget_is_bounded_and_other_errors_are_not_retried(self):
        with TemporaryDirectory() as directory, patch('cell_api.time.sleep'):
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(directory), read_feedback_attempts=99))
            api._pulse_and_capture_once = Mock(side_effect=InvalidReadFeedbackError('invalid'))
            with self.assertRaises(InvalidReadFeedbackError):
                api.read(0,0)
            self.assertEqual(api._pulse_and_capture_once.call_count, 3)
            api._pulse_and_capture_once = Mock(side_effect=RuntimeError('device mismatch'))
            with self.assertRaises(RuntimeError):
                api.read(0,0)
            self.assertEqual(api._pulse_and_capture_once.call_count, 1)

    def test_set_packet_is_never_retried_by_feedback_recovery(self):
        with TemporaryDirectory() as directory:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(directory), read_feedback_attempts=3))
            api._pulse_and_capture_once = Mock(side_effect=InvalidReadFeedbackError('ambiguous'))
            with self.assertRaises(InvalidReadFeedbackError):
                api._pulse_and_capture(CellAddress(0,0), 'set', RailVoltages(2.5,1), 'set_pulse')
            self.assertEqual(api._pulse_and_capture_once.call_count, 1)

    def test_confirmation_restarts_after_retry(self):
        with TemporaryDirectory() as directory:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(directory)))
            api._pulse_and_capture = Mock(side_effect=[self.result(), self.result(), self.result(2)] +
                                                   [self.result() for _ in range(9)])
            results = api.confirm_reads(CellAddress(0,0), 10, 25, 'above')
            self.assertEqual(len(results), 10)
            self.assertEqual(api._pulse_and_capture.call_count, 12)

    def test_continual_retries_cannot_qualify_short_confirmation_streak(self):
        with TemporaryDirectory() as directory:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(directory)))
            api._pulse_and_capture = Mock(side_effect=lambda *args: self.result(2))
            results = api.confirm_reads(CellAddress(0,0), 10, 25, 'above')
            self.assertLess(len(results), 10)
            self.assertEqual(api._pulse_and_capture.call_count, 30)


class ExperimentDefaultTests(unittest.TestCase):
    def args(self, row=0):
        return argparse.Namespace(operation='cycle',row=row,col=0,set_threshold=70,
                                  reset_threshold=5,set_vcc_set='2.5',reset_vcc_set='2.3,2.7,3.1,3.5',confirm_reads=10)

    def test_requested_cell_cycle_defaults(self):
        args = self.args()
        apply_saved_experiment_defaults(args, ['cycle','--row','0','--col','0'])
        self.assertEqual(args.set_threshold,25)
        self.assertEqual(args.reset_threshold,10)
        self.assertEqual(args.set_vcc_set,'2.3')
        self.assertEqual(args.reset_vcc_set,'2.3,2.7,3.1,3.3')

    def test_other_cells_and_explicit_overrides_are_untouched(self):
        args = self.args(1)
        apply_saved_experiment_defaults(args, [])
        self.assertEqual(args.set_threshold,70)
        args = self.args()
        apply_saved_experiment_defaults(args, ['--set-threshold=70','--reset-vcc-set','2.3,2.7,3.1,3.5'])
        self.assertEqual(args.set_threshold,70)
        self.assertEqual(args.reset_vcc_set,'2.3,2.7,3.1,3.5')
        self.assertEqual(args.reset_threshold,10)


if __name__ == '__main__':
    unittest.main()
