import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.summarize_capture import validate_commanded_rails, validate_measurement_window


class CaptureWindowTests(unittest.TestCase):
    def test_valid_window(self):
        validate_measurement_window({'dr_rise_s':9e-6, 'tm_fall_s':59e-6}, -20.0, 0.0, 157)

    def test_reversed_window_rejected_even_if_packet_decodes(self):
        with self.assertRaisesRegex(ValueError, 'TM fall must follow'):
            validate_measurement_window({'dr_rise_s':1.34e-6, 'tm_fall_s':0.62e-6}, 0.0, 0.0, 1)

    def test_missing_or_nonfinite_edges_rejected(self):
        for edge in (None, float('nan'), float('inf')):
            with self.subTest(edge=edge), self.assertRaises(ValueError):
                validate_measurement_window({'dr_rise_s':edge, 'tm_fall_s':1}, 0, 0, 1)

    def test_no_samples_rejected(self):
        with self.assertRaisesRegex(ValueError, 'analog samples'):
            validate_measurement_window({'dr_rise_s':0, 'tm_fall_s':1}, 0, 0, 0)

    def test_nonfinite_current_rejected(self):
        for value in (float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_measurement_window({'dr_rise_s':0, 'tm_fall_s':1}, value, 0, 1)

    def test_commanded_read_rails_are_accepted(self):
        with TemporaryDirectory() as temp_dir:
            analog = Path(temp_dir) / "analog.csv"
            analog.write_text(
                "Time [s],Channel 0,Channel 1,Channel 12,Channel 13\n"
                "0.0,0.49,2.49,0,0\n"
                "0.1,0.51,2.51,0,0\n"
            )

            measured = validate_commanded_rails(analog, 0.5, 2.5)

            self.assertAlmostEqual(measured[0], 0.5)
            self.assertAlmostEqual(measured[1], 2.5)

    def test_stale_set_voltage_is_rejected_for_read(self):
        with TemporaryDirectory() as temp_dir:
            analog = Path(temp_dir) / "analog.csv"
            analog.write_text(
                "Time [s],Channel 0,Channel 1\n"
                "0.0,2.29,2.50\n"
            )

            with self.assertRaisesRegex(ValueError, "requested 0.500 V.*measured 2.290 V"):
                validate_commanded_rails(analog, 0.5, 2.5)


if __name__ == '__main__':
    unittest.main()
