import csv
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cell_api import CellAddress, DEFAULT_READ_CALIBRATION, RailVoltages, ScanDebugCellAPI, ScanDebugConfig


class ReadCalibrationTests(unittest.TestCase):
    def api(self, directory, **kwargs):
        return ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(directory),
            read_calibration_path=DEFAULT_READ_CALIBRATION, **kwargs))

    def test_signed_offset_is_subtracted_and_raw_is_preserved(self):
        with TemporaryDirectory() as directory:
            api = self.api(directory)
            value = api._calibrate_read_feedback(-19.42171303873433,
                {'capture_device_id':'A25E1BAA6577FA4D', 'capture_analog_sample_rate':'3125000'},
                0, CellAddress(0,0), 'raw/capture')
            self.assertAlmostEqual(value, 3.119587016731476)
            audit = json.loads((Path(directory)/'read_calibration.jsonl').read_text())
            self.assertAlmostEqual(audit['raw_current_uA'], -19.42171303873433)
            self.assertAlmostEqual(audit['corrected_current_uA'], value)

    def test_unknown_device_or_rate_is_rejected(self):
        with TemporaryDirectory() as directory:
            api = self.api(directory)
            for device, rate in [('OTHER',3125000), ('A25E1BAA6577FA4D',123), (None,3125000)]:
                with self.subTest(device=device,rate=rate), self.assertRaises(RuntimeError):
                    api._calibrate_read_feedback(1, {'capture_device_id':device,
                        'capture_analog_sample_rate':rate}, 0, CellAddress(0,0), 'raw')

    def test_wrong_read_voltage_or_shunt_rejected_at_configuration(self):
        for kwargs in ({'read_rails':RailVoltages(0.9,2.5)}, {'shunt_ohms':1000}):
            with self.subTest(kwargs=kwargs), TemporaryDirectory() as directory, self.assertRaises(ValueError):
                self.api(directory, **kwargs)

    def test_negative_corrected_values_are_not_clipped(self):
        with TemporaryDirectory() as directory:
            api = self.api(directory)
            value = api._calibrate_read_feedback(-100,
                {'capture_device_id':'A25E1BAA6577FA4D', 'capture_analog_sample_rate':6250000},
                0, CellAddress(0,0), 'raw')
            self.assertLess(value, 0)

    def test_near_zero_is_valid_only_with_calibration_and_remains_signed(self):
        with TemporaryDirectory() as directory:
            api = self.api(directory)
            self.assertTrue(api._read_feedback_valid(-0.13282840579826782))
            self.assertFalse(api._read_feedback_valid(-3.01))
            for value in (None, float('nan'), float('inf')):
                self.assertFalse(api._read_feedback_valid(value))
        with TemporaryDirectory() as directory:
            raw_api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(directory)))
            self.assertFalse(raw_api._read_feedback_valid(-0.13))

    def test_target_qualification_uses_conservative_noise_bound(self):
        with TemporaryDirectory() as directory:
            api = self.api(directory)
            self.assertFalse(api._passes_read_threshold(26, 25, 'above'))
            self.assertTrue(api._passes_read_threshold(28.1, 25, 'above'))
            self.assertFalse(api._passes_read_threshold(8, 10, 'below'))
            self.assertTrue(api._passes_read_threshold(6.9, 10, 'below'))
            self.assertTrue(api._passes_read_threshold(-0.13, 10, 'below'))
            self.assertFalse(api._passes_read_threshold(-4, 10, 'below'))

    def test_missing_or_nonfinite_values_are_not_replaced_with_zero(self):
        with TemporaryDirectory() as directory:
            api = self.api(directory)
            self.assertIsNone(api._calibrate_read_feedback(None, {}, 0, CellAddress(0,0), 'raw'))
            self.assertTrue(math.isnan(api._calibrate_read_feedback(float('nan'), {}, 0, CellAddress(0,0), 'raw')))

    def test_burst_import_uses_captured_rate_and_keeps_source_raw(self):
        with TemporaryDirectory() as directory:
            api = self.api(directory)
            capture = Path(directory)/'capture'
            capture.mkdir()
            raw_csv = 'packet,decoded_packet,la_set_mean_uA,error\n0x0000,0x0000,-19.0,\n'
            (capture/'manifest.csv').write_text(raw_csv)
            (capture/'manifest.json').write_text(json.dumps({'saleae':{
                'device_id':'A25E1BAA6577FA4D', 'analog_sample_rate':31250}}))
            result = api._append_burst_manifest(capture, 'remote', 'test.bit')
            self.assertAlmostEqual(result[0]['current_uA'], 3.14416925989963)
            self.assertTrue(result[0]['ok'])
            self.assertEqual((capture/'manifest.csv').read_text(), raw_csv)
            with api.manifest.open(newline='') as handle:
                self.assertAlmostEqual(float(list(csv.DictReader(handle))[0]['la_set_window_mean_uA']), result[0]['current_uA'])


if __name__ == '__main__':
    unittest.main()
