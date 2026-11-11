from __future__ import annotations

import unittest
from tkinter import Tcl, TclError

from array_4bit_live.config import CONTROLLER_ARTIFACTS
from array_4bit_live.run_pwm_first_population import PWM_CODES, conditions_for_cell


def manifest() -> dict[str, object]:
    return {
        "phase1_vcc_set_V": 1.65,
        "phase1_vcc_wl_V": 1.65,
        "phase2_split_arms": {
            "set_only": {
                "vcc_wl_V": 1.65,
                "vcc_set_ladder_V": [1.85, 2.0, 2.15, 2.3, 2.5, 2.7, 3.0],
                "pwm_code": 32,
            },
            "wl_only": {
                "vcc_set_V": 1.65,
                "vcc_wl_ladder_V": [1.7, 1.8, 1.9, 2.0],
                "pwm_code": 32,
            },
            "coupled": {
                "rail_pairs_V": [[1.85, 1.7], [2.0, 1.8], [2.2, 1.9], [2.5, 2.0], [2.7, 2.0], [3.0, 2.0]],
                "pwm_code": 32,
            },
        },
    }


class PwmFirstPopulationTests(unittest.TestCase):
    def test_every_arm_exhausts_pwm_before_rail_skew(self) -> None:
        for arm in ("set_only", "wl_only", "coupled"):
            conditions = conditions_for_cell(manifest(), {"split_arm": arm})
            phase1 = conditions[: len(PWM_CODES)]
            self.assertEqual([item["pwm_code"] for item in phase1], list(PWM_CODES))
            self.assertTrue(all(item["phase"] == "pwm_exhaustion" for item in phase1))
            self.assertTrue(all(item["vcc_set_V"] == 1.65 for item in phase1))
            self.assertTrue(all(item["vcc_wl_V"] == 1.65 for item in phase1))
            phase2 = conditions[len(PWM_CODES) :]
            self.assertTrue(phase2)
            self.assertTrue(all(item["phase"] == f"split_{arm}" for item in phase2))
            self.assertTrue(all(item["pwm_code"] == 0x20 for item in phase2))
            self.assertTrue(all(0.0 <= item["vcc_set_V"] <= 3.0 for item in conditions))
            self.assertTrue(all(0.0 <= item["vcc_wl_V"] <= 3.0 for item in conditions))

    def test_three_volts_is_only_in_wb_pwm_split_conditions(self) -> None:
        set_only = conditions_for_cell(manifest(), {"split_arm": "set_only"})
        three_volt = [item for item in set_only if item["vcc_set_V"] == 3.0]
        self.assertEqual(len(three_volt), 1)
        self.assertEqual(three_volt[0]["phase"], "split_set_only")
        self.assertEqual(three_volt[0]["pwm_code"], 0x20)


class IndependentRailControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CONTROLLER_ARTIFACTS["control_tcl"].read_text(encoding="utf-8")
        start = cls.source.index(
            "proc phase_p_profile {op voltage {wl_voltage {}} {cpu_already_held 0}} {"
        )
        end = cls.source.index("\nproc wb_profile", start)
        cls.profile_proc = cls.source[start:end]

    def controller(self) -> Tcl:
        controller = Tcl()
        controller.eval(
            r"""
            set calls {}
            proc hold_cpu {args} { global calls; lappend calls [list hold {*}$args] }
            proc dac_profile_local {kind voltage} { global calls; lappend calls [list macro $kind $voltage] }
            proc dac {channel voltage} { global calls; lappend calls [list dac $channel $voltage] }
            proc power_mask_value {value} { global calls; lappend calls [list mask $value] }
            """
        )
        controller.eval(self.profile_proc)
        return controller

    def test_skewed_set_uses_independent_dac2_and_dac3(self) -> None:
        controller = self.controller()
        controller.eval("phase_p_profile set 2.7 1.9")
        calls = controller.eval("set calls")
        self.assertIn("dac 2 2.7", calls)
        self.assertIn("dac 3 1.9", calls)
        self.assertIn("mask 0x01F3", calls)
        self.assertNotIn("macro set", calls)

    def test_tied_set_retains_verified_local_macro(self) -> None:
        controller = self.controller()
        controller.eval("phase_p_profile set 1.65 1.65")
        calls = controller.eval("set calls")
        self.assertIn("macro set 1.65", calls)
        self.assertNotIn("dac 2", calls)

    def test_pwm_rail_above_three_volts_is_rejected(self) -> None:
        controller = self.controller()
        with self.assertRaises(TclError):
            controller.eval("phase_p_profile set 3.01 2.0")

    def test_pwm_fast_path_does_not_repeat_cpu_hold(self) -> None:
        controller = self.controller()
        controller.eval("phase_p_profile set 1.65 1.65 1")
        calls = controller.eval("set calls")
        self.assertNotIn("hold", calls)
        self.assertIn("macro set 1.65", calls)
        self.assertIn("phase_p_profile $op $voltage $wl_voltage 1", self.source)
        self.assertIn("scan_profile 1", self.source)

    def test_batch_format_accepts_independent_wl_field(self) -> None:
        self.assertIn("if {[llength $fields]==7}", self.source)
        self.assertIn("} elseif {[llength $fields]==8}", self.source)
        self.assertIn("pwm_doe_run $r $c $op $pwm $voltage $post_reads $wl_voltage", self.source)

    def test_read_only_skew_profile_preflight_is_registered(self) -> None:
        self.assertIn("pwm_skew_profile_check", self.source.splitlines()[4])
        self.assertIn("proc pwm_skew_profile_check_run {r c set_voltage wl_voltage}", self.source)
        self.assertIn(
            'PWM_SKEW_PROFILE_CHECK_COMPLETE set_v=$set_voltage wl_v=$wl_voltage program_packets=0',
            self.source,
        )

    def test_multi_column_read_action_is_registered(self) -> None:
        self.assertIn(" column columns array ", self.source.splitlines()[4])
        self.assertIn("columns {", self.source)
        self.assertIn("set last_column [expr {$col+$cycles-1}]", self.source)


if __name__ == "__main__":
    unittest.main()
