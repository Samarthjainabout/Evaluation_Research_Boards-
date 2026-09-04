import unittest
from tools.summarize_capture import validate_measurement_window


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


if __name__ == '__main__':
    unittest.main()
