import unittest
import csv
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch

from gui.server import DEFAULT_THRESHOLDS_UA, STATIC_DIR, _latest_heatmap_cells, _sweep_resume_info, _terminate_windows_process_tree, _read_progress_events
from gui.server import _characterization_state, _manifest_for_run, _latest_run, _run_choices, _combined_cell_history, _parse_scan_debug_process, _latest_jsonl_object, _extract_error_message
from gui.server import API_CAPABILITIES, _normalize_api_operation


class CharacterizationGuiTests(unittest.TestCase):
    def test_nested_capture_discovery_and_qualification_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'characterization_test'
            (run/'captures').mkdir(parents=True)
            (run/'trials').mkdir()
            (run/'captures/manifest.csv').write_text('index,operation,cell,ok,la_set_window_mean_uA\n0,read,0_0,True,15\n')
            (run/'plan.json').write_text(json.dumps({'trials':[{}]*36,'phase':'pilot','cell':[0,0],
                'target_tolerance_uS':4,'confirmation_reads':10,'post_reads':10,
                'read_vcc_set_V':.5,'set_vcc_set_V':2.3,'reset_vcc_set_V':2.3}))
            (run/'status.json').write_text(json.dumps({'status':'running'}))
            events=[{'kind':'pre_read','reading':{'conductance_uS':30,'feedback_attempts':n}} for n in [1,1,2,1]]
            (run/'trials/t001.json').write_text(json.dumps({'id':'t001','status':'running','target_uS':30,
                'events':events,'post_reads':[],'preparation_pulse_count':16}))
            self.assertEqual(_manifest_for_run(run),run/'captures/manifest.csv')
            self.assertEqual(_latest_run(root),run)
            with patch('gui.server.ROOT',root):
                self.assertEqual(_run_choices(root)[0]['id'],run.name)
            info=_characterization_state(run)
            self.assertEqual(info['preReads'],2)
            self.assertEqual(info['total'],36)
            self.assertEqual(info['trial']['preparation_pulse_count'],16)
            self.assertEqual(_latest_heatmap_cells(run)['0_0']['sourceRun'],run.name)
            self.assertEqual(_latest_heatmap_cells(run)['0_0']['conductance_uS'],30)
            rows=[{'operation':'read','current_uA':15}]
            self.assertEqual(_combined_cell_history(run,rows,{'row':0,'col':0}),rows)

    def test_ordinary_run_has_no_characterization_status(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(_characterization_state(Path(temp)))


class DefaultThresholdTests(unittest.TestCase):
    def test_scan_debug_and_wishbone_are_namespaced_without_breaking_legacy_operations(self):
        self.assertEqual(_normalize_api_operation("scan-debug", "read"), ("scan-debug", "read"))
        self.assertEqual(_normalize_api_operation("wishbone", "read"), ("wishbone", "wb-read"))
        self.assertEqual(_normalize_api_operation("wishbone", "write"), ("wishbone", "wb-write"))
        self.assertEqual(_normalize_api_operation("", "wb-read"), ("wishbone", "wb-read"))
        self.assertIn("read", API_CAPABILITIES["scan-debug"])
        self.assertIn("read", API_CAPABILITIES["wishbone"])

    def test_api_mode_rejects_cross_interface_operations(self):
        with self.assertRaisesRegex(ValueError, "not available in scan-debug mode"):
            _normalize_api_operation("scan-debug", "wb-read")
        with self.assertRaisesRegex(ValueError, "not available in wishbone mode"):
            _normalize_api_operation("wishbone", "set")

    def test_clean_wb_uart_status_is_not_reported_as_an_error(self):
        log = '''
#     puts "ERROR: timed out waiting for a fresh passive Caravel UART frame"
WB_UART_VALID=1
WB_UART_ERROR=0
WB_UART_TAG=0x52
WB_UART_VALUE=0x0007F363
WB_UART_PASSIVE_MATCH=1
'''
        self.assertEqual(_extract_error_message(log), "")

    def test_capture_error_progress_is_not_marked_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "progress.jsonl").write_text('{"message":"SSH lost; retrying", "ok":false}\n{"message":"recovered"}\n')
            events = _read_progress_events(run)
            self.assertFalse(events[0]["ok"])
            self.assertTrue(events[1]["ok"])

    def test_gui_thresholds_match_half_volt_read_window(self) -> None:
        self.assertEqual(DEFAULT_THRESHOLDS_UA, {"set": 70.0, "reset": 5.0})


class HeatmapScaleTests(unittest.TestCase):
    def test_default_heatmap_scale_is_linear_zero_to_two_hundred_microsiemens(self) -> None:
        app_js = (STATIC_DIR / "app.js").read_text()
        index_html = (STATIC_DIR / "index.html").read_text()

        self.assertIn("currentMin_uA: 0", app_js)
        self.assertIn("currentMax_uA: 200", app_js)
        self.assertIn("HEATMAP_SCALE_MAX_UA = 300", app_js)
        self.assertIn("CURRENT_DISPLAY_SCALE = 1 / READ_VCC_SET_V", app_js)
        self.assertIn("const t = (clamped - min) / (max - min)", app_js)
        self.assertNotIn("Math.log10(clamped) - Math.log10(logMin)", app_js)
        self.assertIn('aria-label="Linear heatmap color scale"', index_html)
        self.assertIn('<span class="scale-bar">LINEAR</span>', index_html)
        self.assertNotIn('max="500"', index_html)
        self.assertEqual(index_html.count('max="300"'), 4)

    def test_wishbone_controls_include_read_and_write_defaults(self) -> None:
        app_js = (STATIC_DIR / "app.js").read_text()
        index_html = (STATIC_DIR / "index.html").read_text()

        self.assertIn('<option value="wb-read">WB read</option>', index_html)
        self.assertIn('<option value="wb-write">WB write</option>', index_html)
        self.assertIn('value="0x4002AA82"', index_html)
        self.assertIn('DEFAULT_WB_READ_VALUE = "0x4002AA82"', app_js)
        self.assertIn('DEFAULT_WB_WRITE_VALUE = "0x500888FF"', app_js)
        self.assertIn('["wb-read", "wb-write"].includes(payload.operation)', app_js)
        self.assertIn("all existing DAC/PLL values are preserved", app_js)
        self.assertIn("applies one 120 ms reset-only pulse from FPGA to Caravel", app_js)
        self.assertIn("the FPGA makes them high-impedance throughout WB mode", app_js)

    def test_external_wishbone_command_retains_write_value(self) -> None:
        command = "python api_v1/scan_debug_cli.py wb-write --wb-value 305441741 --run-dir api_v1/runs/wb"

        parsed = _parse_scan_debug_process(command)

        self.assertEqual(parsed["operation"], "wb-write")
        self.assertEqual(parsed["wbValue"], "0x1234ABCD")

    def test_wishbone_result_is_available_to_gui(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "wishbone_access.jsonl"
            result_path.write_text('{"operation":"wb-read","return_value":"0x89ABCDEF","ok":true}\n')

            result = _latest_jsonl_object(result_path)

        self.assertEqual(result["return_value"], "0x89ABCDEF")
        app_js = (STATIC_DIR / "app.js").read_text()
        self.assertIn('RETURN: ${row.return_value || "no value"} via FPGA UART', app_js)


class LatestHeatmapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manifest(self, name, readings, modified):
        run = self.root / name
        run.mkdir(exist_ok=True)
        fields = ["index", "cell", "operation", "stage", "kind", "ok", "la_set_window_mean_uA"]
        with (run / "manifest.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for index, (cell, current, stage, timestamp, ok) in enumerate(readings):
                writer.writerow(dict(index=index, cell=cell, operation="read", stage=stage,
                                     kind="read", ok=ok, la_set_window_mean_uA=current))
                log = run / f"capture_{index}_read.log"
                log.write_text("captured")
                os.utime(log, (timestamp, timestamp))
        os.utime(run / "manifest.csv", (modified, modified))
        return run

    def test_new_burst_beats_old_single_even_when_old_run_is_selected(self):
        old = self.manifest("old-single", [("4_1", 62, "read", 100, True)], 100)
        self.manifest("new-burst", [("4_1", 134, "array_burst", 200, True)], 200)
        row = _latest_heatmap_cells(old)["4_1"]
        self.assertEqual(row["current_uA"], 134)
        self.assertEqual(row["sourceRun"], "new-burst")
        self.assertEqual(row["measurementMode"], "burst")

    def test_new_single_only_replaces_its_cell_and_failed_read_is_ignored(self):
        burst = self.manifest("burst", [("4_1", 134, "array_burst", 200, True),
                                         ("5_1", 100, "array_burst", 200, True)], 200)
        self.manifest("single", [("4_1", 130, "read", 300, True),
                                  ("4_1", 999, "read", 400, False)], 400)
        cells = _latest_heatmap_cells(burst)
        self.assertEqual(cells["4_1"]["current_uA"], 130)
        self.assertEqual(cells["5_1"]["current_uA"], 100)

    def test_resuming_old_run_does_not_refresh_its_untouched_cells(self):
        old = self.manifest("resumed", [("4_1", 62, "read", 100, True),
                                        ("3_1", 147, "read", 300, True)], 300)
        self.manifest("burst", [("4_1", 134, "array_burst", 200, True),
                                ("3_1", 140, "array_burst", 200, True)], 200)
        os.utime(old, (500, 500))
        cells = _latest_heatmap_cells(old)
        self.assertEqual(cells["4_1"]["current_uA"], 134)
        self.assertEqual(cells["3_1"]["current_uA"], 147)


class WindowsProcessTreeTerminationTests(unittest.TestCase):
    @patch("gui.server.subprocess.run")
    def test_taskkill_terminates_parent_and_children(self, run: Mock) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "SUCCESS"

        _terminate_windows_process_tree(1234)

        run.assert_called_once_with(
            ["taskkill", "/PID", "1234", "/T", "/F"],
            text=True,
            stdout=-1,
            stderr=-2,
        )

    @patch("gui.server.subprocess.run")
    def test_taskkill_failure_is_reported(self, run: Mock) -> None:
        run.return_value.returncode = 128
        run.return_value.stdout = "process not found"

        with self.assertRaisesRegex(OSError, "process not found"):
            _terminate_windows_process_tree(1234)


class SweepResumeTests(unittest.TestCase):
    @staticmethod
    def reset_rows(count: int) -> list[dict[str, object]]:
        rails = [(vcc_set, vcc_wl) for vcc_set in (3.3, 3.4, 3.5, 3.6, 3.7) for vcc_wl in (1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.3)]
        return [
            {
                "operation": "reset",
                "ok": True,
                "cellAddress": {"row": 18, "col": 0},
                "vcc_set_V": vcc_set,
                "vcc_wl_set_V": vcc_wl,
            }
            for vcc_set, vcc_wl in rails[:count]
        ]

    def test_completed_reset_sweep_is_not_resumable(self) -> None:
        info = _sweep_resume_info(Path("gui_test_r18c00_reset"), self.reset_rows(40))

        self.assertFalse(info["canResume"])
        self.assertEqual(info["completedPulses"], 40)
        self.assertEqual(info["remainingPulses"], 0)

    def test_interrupted_reset_sweep_remains_resumable(self) -> None:
        info = _sweep_resume_info(Path("gui_test_r18c00_reset"), self.reset_rows(17))

        self.assertTrue(info["canResume"])
        self.assertEqual(info["completedPulses"], 17)
        self.assertEqual(info["remainingPulses"], 23)


if __name__ == "__main__":
    unittest.main()
