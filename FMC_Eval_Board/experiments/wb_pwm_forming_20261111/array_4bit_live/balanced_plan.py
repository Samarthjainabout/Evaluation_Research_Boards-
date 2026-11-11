from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any


BALANCED_STRATEGY = "balanced_bucket_v2"
BALANCED_VERSION = 2
ANALOG_UNCONFIRMED_POLICY = (
    "advance_only_when_analog_is_the_sole_failed_gate_and_wb_tdc_scan_"
    "sentinel_clip_completion_restore_all_pass"
)
MAX_OVERSHOOT_RESET_LIMIT = 1
FAST_RESPONSE_SCREEN_VERSION = 7
SCAN_SET_FORMING_GUARDED = "guarded_below20_one_pulse_jobs"
SCAN_SET_FORMING_DISABLED = "disabled_quarantine_below20"

# Version 6 is retained only so an existing frozen run remains readable.  It
# must never be silently substituted for the finer version-7 ladder.
THRESHOLD_SEARCH_CONDITIONS_V6 = (
    (1.65, 0x10),
    (1.65, 0x40),
    (1.65, 0x80),
    (1.65, 0xF0),
    (1.70, 0x20),
    (1.75, 0x20),
    (1.80, 0x20),
    (1.90, 0x20),
    (2.00, 0x20),
    (2.10, 0x20),
    (2.20, 0x20),
)

# The version-7 column-2 policy starts strictly above 1.6 V.  The observed
# 1.65-V transition from PWM 0x80 to 0xF0 was too coarse for responsive cells,
# so the old 0xF0 pulse is replaced by independently verified cumulative trim
# pulses: 4 x 0x10, 2 x 0x20, then 1 x 0x40.  The FPGA-host batch evaluates the
# target, overshoot, and safety arms after every five-read result.  Only after
# the trim budget is exhausted does it increase voltage with a short 0x20
# pulse.  No reset to a common baseline is part of this search.
THRESHOLD_SEARCH_CONDITIONS = (
    (1.65, 0x10),
    (1.65, 0x40),
    (1.65, 0x80),
    (1.65, 0x10),
    (1.65, 0x10),
    (1.65, 0x10),
    (1.65, 0x10),
    (1.65, 0x20),
    (1.65, 0x20),
    (1.65, 0x40),
    (1.70, 0x20),
    (1.75, 0x20),
    (1.80, 0x20),
    (1.90, 0x20),
    (2.00, 0x20),
    (2.10, 0x20),
    (2.20, 0x20),
)


def policy_artifacts(app_root: Path) -> dict[str, Path]:
    """Code that can change balanced-run pulse/acceptance decisions."""

    return {
        "balanced_policy": app_root / "balanced_plan.py",
        "balanced_runner": app_root / "runner.py",
        "balanced_parser": app_root / "parser.py",
    }


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RuntimeError(f"{label} must be an integer in {low}..{high}")
    return value


def _normalise_quotas(value: Any) -> list[int]:
    if isinstance(value, list):
        if len(value) != 16:
            raise RuntimeError("balanced_plan.bucket_quotas must have 16 entries")
        return [_integer(item, f"bucket_quotas[{index}]", 0, 32) for index, item in enumerate(value)]
    if isinstance(value, dict):
        quotas: list[int] = []
        for code in range(16):
            raw = value.get(str(code), value.get(f"{code:X}", value.get(code)))
            quotas.append(_integer(raw, f"bucket_quotas[{code}]", 0, 32))
        return quotas
    raise RuntimeError("balanced_plan.bucket_quotas must be a 16-entry list or mapping")


def normalise_balanced_plan(value: Any, *, rows: int, columns: int, reference_cell: tuple[int, int]) -> dict[str, Any] | None:
    """Validate and canonicalise the immutable balanced continuation policy."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("balanced_plan must be an object")
    if value.get("strategy") != BALANCED_STRATEGY:
        raise RuntimeError(f"balanced_plan.strategy must be {BALANCED_STRATEGY!r}")
    if value.get("version") != BALANCED_VERSION:
        raise RuntimeError(f"balanced_plan.version must be {BALANCED_VERSION}")

    parent_run_id = str(value.get("parent_run_id") or "").strip()
    parent_checkpoint_sha256 = str(value.get("parent_checkpoint_sha256") or "").strip().lower()
    if not parent_run_id:
        raise RuntimeError("balanced_plan.parent_run_id is required")
    if len(parent_checkpoint_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in parent_checkpoint_sha256):
        raise RuntimeError("balanced_plan.parent_checkpoint_sha256 must be a lowercase SHA-256")

    raw_cells = value.get("execution_cells")
    if not isinstance(raw_cells, list) or not raw_cells:
        raise RuntimeError("balanced_plan.execution_cells must be a non-empty list")
    execution_cells: list[dict[str, int]] = []
    coordinates: set[tuple[int, int]] = set()
    for index, raw in enumerate(raw_cells):
        if not isinstance(raw, dict):
            raise RuntimeError(f"balanced_plan.execution_cells[{index}] must be an object")
        row = _integer(raw.get("row"), f"execution_cells[{index}].row", 0, rows - 1)
        column = _integer(raw.get("column"), f"execution_cells[{index}].column", 0, columns - 1)
        target = _integer(raw.get("target_state_code"), f"execution_cells[{index}].target_state_code", 0, 15)
        coordinate = (row, column)
        if coordinate == reference_cell:
            raise RuntimeError("balanced_plan may not program the protected reference cell")
        if coordinate in coordinates:
            raise RuntimeError(f"duplicate balanced execution cell r{row:02d}/c{column:02d}")
        coordinates.add(coordinate)
        execution_cells.append({"row": row, "column": column, "target_state_code": target})

    quotas = _normalise_quotas(value.get("bucket_quotas"))
    assigned = Counter(item["target_state_code"] for item in execution_cells)
    if [assigned.get(code, 0) for code in range(16)] != quotas:
        raise RuntimeError("balanced_plan target assignments do not match bucket_quotas")

    policy = str(value.get("analog_unconfirmed_policy") or "")
    if policy != ANALOG_UNCONFIRMED_POLICY:
        raise RuntimeError("balanced_plan analog-unconfirmed policy is not the supported v2 policy")
    reset_limit = _integer(
        value.get("overshoot_reset_limit"),
        "balanced_plan.overshoot_reset_limit",
        0,
        MAX_OVERSHOOT_RESET_LIMIT,
    )
    prescreen = value.get("prescreen", {})
    if not isinstance(prescreen, dict):
        raise RuntimeError("balanced_plan.prescreen must be an object")
    scan_set_forming_policy = str(
        value.get("scan_set_forming_policy") or SCAN_SET_FORMING_GUARDED
    )
    if scan_set_forming_policy not in {
        SCAN_SET_FORMING_GUARDED,
        SCAN_SET_FORMING_DISABLED,
    }:
        raise RuntimeError("balanced_plan.scan_set_forming_policy is unsupported")

    raw_fast = value.get("fast_response_screen")
    fast_response_screen = None
    if raw_fast is not None:
        if not isinstance(raw_fast, dict):
            raise RuntimeError("balanced_plan.fast_response_screen must be an object")
        screen_version = raw_fast.get("version")
        if screen_version == 1:
            expected_fast = {
                "version": 1,
                "operation": "set",
                "pwm_code": 0xF0,
                "voltage_v": 2.2,
                "max_pulses": 4,
                "stop_after_first_nonresponse": True,
                "response_source": "five_post_reads_and_directional_delta",
            }
        elif screen_version == 2:
            expected_fast = {
                "version": 2,
                "operation": "set",
                "pwm_code": 0xF0,
                "voltage_v": 2.0,
                "max_pulses": 4,
                "stop_after_first_nonresponse": True,
                "response_source": "five_post_reads_and_directional_delta",
            }
        elif screen_version == 3:
            expected_fast = {
                "version": 3,
                "operation": "set",
                "pwm_code": 0xF0,
                "voltage_v": 2.0,
                "low_conductance_voltage_v": 1.8,
                "low_conductance_cutoff_uS": 40.0,
                "max_pulses": 1,
                "stop_after_first_nonresponse": True,
                "response_source": "five_post_reads_and_directional_delta",
            }
        elif screen_version == 4:
            expected_fast = {
                "version": 4,
                "operation": "set",
                "pwm_code": 0xF0,
                "voltage_v": 1.8,
                "low_conductance_voltage_v": 1.8,
                "low_conductance_cutoff_uS": 40.0,
                "max_pulses": 1,
                "stop_after_first_nonresponse": True,
                "response_source": "five_post_reads_and_directional_delta",
            }
        elif screen_version == 5:
            expected_fast = {
                "version": 5,
                "operation": "set",
                "pwm_code": 0xF0,
                "voltage_v": 1.6,
                "low_conductance_voltage_v": 1.6,
                "low_conductance_cutoff_uS": 40.0,
                "max_pulses": 1,
                "stop_after_first_nonresponse": True,
                "response_source": "five_post_reads_and_directional_delta",
            }
        elif screen_version in {6, FAST_RESPONSE_SCREEN_VERSION}:
            conditions = (
                THRESHOLD_SEARCH_CONDITIONS_V6
                if screen_version == 6
                else THRESHOLD_SEARCH_CONDITIONS
            )
            expected_fast = {
                "version": screen_version,
                "operation": "set",
                "threshold_start_voltage_v": 1.65,
                "voltage_ceiling_v": 2.2,
                "conditions": [
                    {"voltage_v": voltage, "pwm_code": pwm}
                    for voltage, pwm in conditions
                ],
                "max_pulses": len(conditions),
                "stop_after_first_nonresponse": False,
                "stop_after_target_or_overshoot": True,
                "response_source": "five_post_reads_and_directional_delta",
            }
        else:
            raise RuntimeError(
                "balanced_plan.fast_response_screen version is unsupported"
            )
        if raw_fast != expected_fast:
            raise RuntimeError(
                "balanced_plan.fast_response_screen must exactly match the supported fail-closed policy"
            )
        fast_response_screen = expected_fast

    result = {
        "strategy": BALANCED_STRATEGY,
        "version": BALANCED_VERSION,
        "parent_run_id": parent_run_id,
        "parent_checkpoint_sha256": parent_checkpoint_sha256,
        "execution_cells": execution_cells,
        "bucket_quotas": quotas,
        "analog_unconfirmed_policy": ANALOG_UNCONFIRMED_POLICY,
        "overshoot_reset_limit": reset_limit,
        "prescreen": prescreen,
        "scan_set_forming_policy": scan_set_forming_policy,
    }
    if fast_response_screen is not None:
        result["fast_response_screen"] = fast_response_screen
    return result


__all__ = [
    "ANALOG_UNCONFIRMED_POLICY",
    "BALANCED_STRATEGY",
    "BALANCED_VERSION",
    "FAST_RESPONSE_SCREEN_VERSION",
    "MAX_OVERSHOOT_RESET_LIMIT",
    "SCAN_SET_FORMING_DISABLED",
    "SCAN_SET_FORMING_GUARDED",
    "THRESHOLD_SEARCH_CONDITIONS",
    "THRESHOLD_SEARCH_CONDITIONS_V6",
    "normalise_balanced_plan",
    "policy_artifacts",
]
