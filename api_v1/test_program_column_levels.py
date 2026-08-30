import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from cell_api import CellAddress, CellOperationResult, RailVoltages
from program_column_levels import LevelSpec, _attempted_rail_prefix, build_level_specs, program_cell_level


def result(current_uA: float, operation: str = "read") -> CellOperationResult:
    return CellOperationResult(
        cell=CellAddress(7, 0),
        operation=operation,
        packet="0x0000",
        rails=RailVoltages(0.5, 2.5),
        current_uA=current_uA,
        decoded_packet="0x0000",
        ok=True,
    )


class LevelMappingTests(unittest.TestCase):
    def test_full_column_maps_codes_zero_through_31_to_zero_through_75(self) -> None:
        specs = build_level_specs()

        self.assertEqual(len(specs), 32)
        self.assertEqual(specs[0].row, 0)
        self.assertAlmostEqual(specs[0].target_uA, 0.0)
        self.assertEqual(specs[-1].row, 31)
        self.assertAlmostEqual(specs[-1].target_uA, 75.0)
        self.assertAlmostEqual(specs[1].target_uA - specs[0].target_uA, 75.0 / 31.0)
        self.assertAlmostEqual(specs[0].upper_uA, 75.0 / 62.0)


class CellQualificationTests(unittest.TestCase):
    def test_cell_requires_ten_consecutive_in_window_reads(self) -> None:
        api = Mock()
        api.config = SimpleNamespace(
            read_rails=RailVoltages(0.5, 2.5),
            set_sweep=SimpleNamespace(vcc_set_v=(1.6,), vcc_wl_set_v=(0.5,)),
            reset_sweep=SimpleNamespace(vcc_set_v=(3.3,), vcc_wl_set_v=(1.0,)),
        )
        api._pulse_and_capture.side_effect = [
            result(7.0),
            result(10.0),
            *[result(10.0) for _ in range(10)],
        ]
        api._program_pulse.return_value = result(0.0, "set")
        spec = LevelSpec(code=7, row=7, target_uA=10.0, lower_uA=9.0, upper_uA=11.0)

        outcome = program_cell_level(api, spec=spec, col=0, confirm_reads=10)

        self.assertTrue(outcome["qualified"])
        self.assertEqual(len(outcome["confirmation_currents_uA"]), 10)
        self.assertEqual(api._pulse_and_capture.call_count, 12)
        api._program_pulse.assert_called_once()

    def test_one_out_of_window_confirmation_fails_cell(self) -> None:
        api = Mock()
        api.config = SimpleNamespace(
            read_rails=RailVoltages(0.5, 2.5),
            set_sweep=SimpleNamespace(vcc_set_v=(1.6,), vcc_wl_set_v=(0.5,)),
            reset_sweep=SimpleNamespace(vcc_set_v=(3.3,), vcc_wl_set_v=(1.0,)),
        )
        api._pulse_and_capture.side_effect = [result(10.0), *[result(10.0) for _ in range(9)], result(11.1)]
        spec = LevelSpec(code=7, row=7, target_uA=10.0, lower_uA=9.0, upper_uA=11.0)

        outcome = program_cell_level(api, spec=spec, col=0, confirm_reads=10)

        self.assertFalse(outcome["qualified"])
        self.assertEqual(outcome["reason"], "unstable_confirmation_reads")
        self.assertEqual(len(outcome["confirmation_currents_uA"]), 10)


class SweepResumeTests(unittest.TestCase):
    def test_resume_starts_after_consecutive_recorded_rails(self) -> None:
        rails = [RailVoltages(3.3, 1.0), RailVoltages(3.3, 1.2), RailVoltages(3.3, 1.4)]
        with TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "manifest.csv"
            manifest.write_text(
                "packet,vcc_set_V,vcc_wl_set_V,ok\n"
                "0x0c03,3.3,1.0,True\n"
                "0x0c03,3.3,1.2,True\n"
            )
            api = SimpleNamespace(manifest=manifest)

            prefix = _attempted_rail_prefix(
                api,
                cell=CellAddress(3, 0),
                operation="reset",
                rails=rails,
            )

        self.assertEqual(prefix, 2)


if __name__ == "__main__":
    unittest.main()
