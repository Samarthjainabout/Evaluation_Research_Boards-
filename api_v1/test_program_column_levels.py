import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from cell_api import CellAddress, CellOperationResult, RailVoltages
from program_column_levels import (
    DirectionalWlBracket,
    LevelSpec,
    _append_detailed_result,
    _attempted_rail_prefix,
    build_level_specs,
    extend_wl_sweep_to_max,
    inclusive_voltage_sweep,
    program_cell_level,
)


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
    def test_explicit_ascending_mapping_maps_row_zero_to_code_zero(self) -> None:
        specs = build_level_specs(min_uA=0.0, max_uA=70.0, row0_code0=True)

        self.assertEqual(len(specs), 32)
        self.assertEqual((specs[0].row, specs[0].code, specs[0].target_uA), (0, 0, 0.0))
        self.assertEqual(specs[-1].row, 31)
        self.assertEqual(specs[-1].code, 31)
        self.assertAlmostEqual(specs[-1].target_uA, 70.0)
        self.assertAlmostEqual(specs[1].target_uA, 70.0 / 31.0)
        self.assertAlmostEqual(specs[0].upper_uA, 70.0 / 62.0)
        self.assertAlmostEqual(specs[1].target_conductance_uS, 140.0 / 31.0)

    def test_full_column_maps_last_row_to_code_zero_and_first_row_to_code_31(self) -> None:
        specs = build_level_specs()

        self.assertEqual(len(specs), 32)
        self.assertEqual(specs[0].row, 31)
        self.assertEqual(specs[0].code, 0)
        self.assertAlmostEqual(specs[0].target_uA, 0.0)
        self.assertEqual(specs[-1].row, 0)
        self.assertEqual(specs[-1].code, 31)
        self.assertAlmostEqual(specs[-1].target_uA, 75.0)
        self.assertAlmostEqual(specs[1].target_uA - specs[0].target_uA, 75.0 / 31.0)
        self.assertAlmostEqual(specs[0].upper_uA, 75.0 / 62.0)

    def test_conductance_mapping_is_primary_and_converts_to_current_at_read_voltage(self) -> None:
        specs = build_level_specs(min_uS=0.0, max_uS=100.0)

        self.assertAlmostEqual(specs[0].target_conductance_uS, 0.0)
        self.assertAlmostEqual(specs[-1].target_conductance_uS, 100.0)
        self.assertAlmostEqual(specs[-1].target_uA, 50.0)
        self.assertAlmostEqual(specs[1].target_conductance_uS, 100.0 / 31.0)

    def test_reset_wl_sweep_extends_to_3p3_with_prior_pattern(self) -> None:
        original = (0.94, 1.01, 2.82, 2.88)

        extended = extend_wl_sweep_to_max(original, 3.3)

        self.assertEqual(extended[: len(original)], original)
        self.assertEqual(extended[-7:], (2.94, 3.0, 3.06, 3.12, 3.18, 3.24, 3.3))

    def test_reset_vcc_sweep_is_inclusive(self) -> None:
        self.assertEqual(
            inclusive_voltage_sweep(2.3, 3.5, 0.1),
            tuple(round(2.3 + index * 0.1, 1) for index in range(13)),
        )

    def test_nested_reset_sweep_runs_all_wl_values_inside_each_vcc_value(self) -> None:
        rails = [
            RailVoltages(vcc, wl)
            for vcc in (2.3, 2.4)
            for wl in (0.94, 1.01)
        ]
        bracket = DirectionalWlBracket(rails)
        observed = []
        for _ in rails:
            rail, selection = bracket.next_rail()
            observed.append(rail)
            self.assertEqual(selection, "projected_sweep")
            bracket.observe(rail, insufficient=True)

        self.assertEqual(observed, rails)
        self.assertIsNone(bracket.next_rail())


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

    def test_current_above_target_uses_reset_direction(self) -> None:
        api = Mock()
        api.config = SimpleNamespace(
            read_rails=RailVoltages(0.5, 2.5),
            set_sweep=SimpleNamespace(vcc_set_v=(2.5,), vcc_wl_set_v=(0.44,)),
            reset_sweep=SimpleNamespace(vcc_set_v=(3.3,), vcc_wl_set_v=(0.94,)),
        )
        api._pulse_and_capture.side_effect = [
            result(13.0),
            result(10.0),
            *[result(10.0) for _ in range(10)],
        ]
        api._program_pulse.return_value = result(0.0, "reset")
        spec = LevelSpec(code=7, row=7, target_uA=10.0, lower_uA=9.0, upper_uA=11.0)

        outcome = program_cell_level(api, spec=spec, col=0, confirm_reads=10)

        self.assertTrue(outcome["qualified"])
        api._program_pulse.assert_called_once()
        _, operation, rails, _ = api._program_pulse.call_args.args
        self.assertEqual(operation, "reset")
        self.assertEqual(rails, RailVoltages(3.3, 0.94))

    def test_set_only_threshold_qualifies_an_already_high_cell_without_reset(self) -> None:
        api = Mock()
        api.config = SimpleNamespace(
            read_rails=RailVoltages(0.5, 2.5),
            set_sweep=SimpleNamespace(vcc_set_v=(2.5,), vcc_wl_set_v=(0.44,)),
            reset_sweep=SimpleNamespace(vcc_set_v=(3.5,), vcc_wl_set_v=(0.94,)),
        )
        api._pulse_and_capture.side_effect = [result(13.0), *[result(12.0) for _ in range(10)]]
        spec = LevelSpec(code=31, row=7, target_uA=10.0, lower_uA=9.0, upper_uA=11.0)

        outcome = program_cell_level(
            api,
            spec=spec,
            col=0,
            confirm_reads=10,
            qualify_above_only=True,
        )

        self.assertTrue(outcome["qualified"])
        api._program_pulse.assert_not_called()

    def test_set_only_threshold_never_uses_reset(self) -> None:
        api = Mock()
        api.config = SimpleNamespace(
            read_rails=RailVoltages(0.5, 2.5),
            set_sweep=SimpleNamespace(vcc_set_v=(2.5,), vcc_wl_set_v=(0.44,)),
            reset_sweep=SimpleNamespace(vcc_set_v=(3.5,), vcc_wl_set_v=(0.94,)),
        )
        api._pulse_and_capture.side_effect = [result(7.0), result(10.0), *[result(10.0) for _ in range(10)]]
        api._program_pulse.return_value = result(0.0, "set")
        spec = LevelSpec(code=31, row=7, target_uA=10.0, lower_uA=9.0, upper_uA=11.0)

        outcome = program_cell_level(
            api,
            spec=spec,
            col=0,
            confirm_reads=10,
            qualify_above_only=True,
        )

        self.assertTrue(outcome["qualified"])
        _, operation, rails, _ = api._program_pulse.call_args.args
        self.assertEqual(operation, "set")
        self.assertEqual(rails, RailVoltages(2.5, 0.44))

    def test_direction_reversal_returns_to_set_bracket_midpoint(self) -> None:
        api = Mock()
        api.config = SimpleNamespace(
            read_rails=RailVoltages(0.5, 2.5),
            set_sweep=SimpleNamespace(vcc_set_v=(2.5,), vcc_wl_set_v=(0.44, 0.50, 0.56)),
            reset_sweep=SimpleNamespace(vcc_set_v=(3.3,), vcc_wl_set_v=(0.94, 1.01)),
        )
        api._pulse_and_capture.side_effect = [
            result(5.0),   # initial: needs Set
            result(7.0),   # Set 0.44 V is insufficient
            result(13.0),  # Set 0.50 V is excessive
            result(8.0),   # weakest Reset crosses below the target
            result(10.0),  # bracketed Set midpoint reaches the window
            *[result(10.0) for _ in range(10)],
        ]
        api._program_pulse.side_effect = lambda _cell, operation, _rails, _stage: result(0.0, operation)
        spec = LevelSpec(code=7, row=7, target_uA=10.0, lower_uA=9.0, upper_uA=11.0)

        outcome = program_cell_level(api, spec=spec, col=0, confirm_reads=10)

        self.assertTrue(outcome["qualified"])
        calls = api._program_pulse.call_args_list
        self.assertEqual([call.args[1] for call in calls], ["set", "set", "reset", "set"])
        self.assertEqual(calls[0].args[2], RailVoltages(2.5, 0.44))
        self.assertEqual(calls[1].args[2], RailVoltages(2.5, 0.50))
        self.assertEqual(calls[2].args[2], RailVoltages(3.3, 0.94))
        weak_code = DirectionalWlBracket._wl_code(0.44)
        strong_code = DirectionalWlBracket._wl_code(0.50)
        expected_midpoint = DirectionalWlBracket._wl_voltage((weak_code + strong_code) // 2)
        self.assertAlmostEqual(calls[3].args[2].vcc_wl_set_v, expected_midpoint)
        self.assertEqual(outcome["events"][4]["rail_selection"], "bracket_midpoint")

    def test_program_pulse_limit_stops_an_immovable_cell(self) -> None:
        api = Mock()
        api.config = SimpleNamespace(
            read_rails=RailVoltages(0.5, 2.5),
            set_sweep=SimpleNamespace(vcc_set_v=(2.5,), vcc_wl_set_v=(0.44, 0.50)),
            reset_sweep=SimpleNamespace(vcc_set_v=(3.3,), vcc_wl_set_v=(0.94, 1.01)),
        )
        api._pulse_and_capture.side_effect = [result(5.0), result(5.0)]
        api._program_pulse.return_value = result(0.0, "set")
        spec = LevelSpec(code=7, row=7, target_uA=10.0, lower_uA=9.0, upper_uA=11.0)

        outcome = program_cell_level(api, spec=spec, col=0, confirm_reads=10, max_program_pulses=1)

        self.assertFalse(outcome["qualified"])
        self.assertEqual(outcome["reason"], "program_pulse_limit_reached")
        self.assertEqual(outcome["program_pulses"], 1)

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


class DetailedCsvTests(unittest.TestCase):
    def test_detailed_csv_flattens_every_pulse_and_read(self) -> None:
        initial = result(12.0)
        pulse = result(0.0, "reset")
        verify = result(10.0)
        confirmation = result(10.1)
        outcome = {
            "cell": {"row": 7, "col": 1},
            "code": 24,
            "target_uA": 38.7,
            "lower_uA": 37.9,
            "upper_uA": 39.5,
            "qualified": True,
            "reason": "qualified",
            "final_current_uA": 10.1,
            "program_pulses": 1,
            "events": [
                {"kind": "initial_read", "result": initial.__dict__ | {"cell": initial.cell.__dict__, "rails": initial.rails.__dict__}},
                {
                    "kind": "program_step",
                    "rail_selection": "projected_sweep",
                    "bracket_after": {"active_vcc_set_V": 3.3},
                    "pulse": pulse.__dict__ | {"cell": pulse.cell.__dict__, "rails": pulse.rails.__dict__},
                    "verify": verify.__dict__ | {"cell": verify.cell.__dict__, "rails": verify.rails.__dict__},
                },
                {
                    "kind": "qualification",
                    "results": [confirmation.__dict__ | {"cell": confirmation.cell.__dict__, "rails": confirmation.rails.__dict__}],
                },
            ],
        }
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "detailed.csv"
            _append_detailed_result(path, outcome)
            with path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(
            [row["measurement_role"] for row in rows],
            ["initial_read", "program_pulse", "verification_read", "qualification_read"],
        )
        self.assertEqual(rows[1]["rail_selection"], "projected_sweep")
        self.assertEqual(rows[-1]["measurement_index"], "1")
        self.assertEqual(float(rows[0]["conductance_uS"]), 24.0)
        self.assertEqual(float(rows[0]["read_voltage_V"]), 0.5)
        self.assertAlmostEqual(float(rows[0]["target_conductance_uS"]), 77.4)


if __name__ == "__main__":
    unittest.main()
