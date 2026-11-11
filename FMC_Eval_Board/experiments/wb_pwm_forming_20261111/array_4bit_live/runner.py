from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from balanced_plan import (
    SCAN_SET_FORMING_DISABLED,
    SCAN_SET_FORMING_GUARDED,
    normalise_balanced_plan,
    policy_artifacts,
)
from config import (
    APP_ROOT,
    CHIP_ID,
    CHIP_SAFETY_FILE,
    COLUMNS,
    CONTROL_FILE,
    CONTROL_SCRIPT,
    CONTROLLER_ARTIFACTS,
    GLOBAL_HARDWARE_LOCK,
    POST_WRITE_READS,
    PRE_READS,
    PWM_CODES,
    READ_HOLD_CLOCKS,
    READ_VOLTAGE_V,
    REFERENCE_CELL,
    RESET_PWM,
    RESET_SAFE_MAX_US,
    RESET_VOLTAGE_V,
    ROWS,
    SCAN_FORM_HOLD_CLOCKS,
    SCAN_FORM_MAX_PULSES_PER_CELL,
    SCAN_FORM_PULSES_PER_JOB,
    SCAN_FORM_THRESHOLD_US,
    SCAN_FORM_VCC_V,
    SCAN_FORM_WL_V,
    SENTINEL_LIMIT_UA,
    SET_CEILING_US,
    SET_FIXED_VOLTAGE_V,
    SET_PRE_PULSE_GUARD_US,
    SET_VOLTAGE_SKEW_V,
    TARGET_BANDS_US,
    VIVADO,
    validate_environment,
)
from parser import AttemptAccumulator, decode_cell_result
from store import TABLE_SCHEMAS, RunStore


PLAN_FINGERPRINT_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def current_plan_fingerprint(
    execution_scope: dict[str, Any] | None = None,
    balanced_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a relocation-stable digest of every pulse-affecting input."""

    normalised_balanced = normalise_balanced_plan(
        balanced_plan,
        rows=ROWS,
        columns=COLUMNS,
        reference_cell=REFERENCE_CELL,
    )
    fingerprint_version = 2 if normalised_balanced is not None else PLAN_FINGERPRINT_VERSION
    artifacts: dict[str, dict[str, Any]] = {}
    artifact_identity: dict[str, str | None] = {}
    artifact_paths = dict(CONTROLLER_ARTIFACTS)
    if normalised_balanced is not None:
        artifact_paths.update(policy_artifacts(APP_ROOT))
    for label, path in sorted(artifact_paths.items()):
        digest = _sha256_file(path)
        artifact_identity[label] = digest
        artifacts[label] = {
            "path": str(path.resolve()),
            "sha256": digest,
            "byte_count": path.stat().st_size if path.is_file() else None,
        }
    plan = {
        "chip_id": CHIP_ID,
        "rows": ROWS,
        "columns": COLUMNS,
        "reference_cell": list(REFERENCE_CELL),
        "read_voltage_v": READ_VOLTAGE_V,
        "read_hold_clocks": READ_HOLD_CLOCKS,
        "pre_reads": PRE_READS,
        "post_write_reads": POST_WRITE_READS,
        "pwm_codes": list(PWM_CODES),
        "set_fixed_voltage_v": SET_FIXED_VOLTAGE_V,
        "reset_pwm": RESET_PWM,
        "reset_voltage_v": RESET_VOLTAGE_V,
        "reset_safe_max_uS": RESET_SAFE_MAX_US,
        "set_ceiling_uS": SET_CEILING_US,
        "set_pre_pulse_guard_uS": SET_PRE_PULSE_GUARD_US,
        "sentinel_limit_uA": SENTINEL_LIMIT_UA,
        "scan_set_forming": {
            "threshold_uS": SCAN_FORM_THRESHOLD_US,
            "vcc_v": SCAN_FORM_VCC_V,
            "wl_v": SCAN_FORM_WL_V,
            "hold_clocks": SCAN_FORM_HOLD_CLOCKS,
            "pulses_per_job": SCAN_FORM_PULSES_PER_JOB,
            "max_pulses_per_cell": SCAN_FORM_MAX_PULSES_PER_CELL,
            "eligibility": "five_fresh_reads_all_strictly_below_threshold",
            "permanent_lockout": "any_verified_target_read_at_or_above_threshold",
            "retry_mode": "repeat_one_guarded_pulse_job_until_first_verified_read_at_or_above_threshold",
            "cap_exhaustion": "safety_stop_no_wb_fallback",
        },
        "set_voltage_skew_v": list(SET_VOLTAGE_SKEW_V),
        "target_bands_uS": [list(band) for band in TARGET_BANDS_US],
    }
    # Preserve the existing full-array fingerprint. Scoped runs add the exact
    # immutable selection to the frozen pulse plan.
    if execution_scope is not None:
        plan["execution_scope"] = execution_scope
    if normalised_balanced is not None:
        plan["balanced_plan"] = normalised_balanced
    identity = {
        "version": fingerprint_version,
        "plan": plan,
        "artifact_sha256": artifact_identity,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "version": fingerprint_version,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "plan": plan,
        "artifacts": artifacts,
    }


def _checkpoint_has_program_activity(checkpoint: dict[str, Any]) -> bool:
    if checkpoint.get("inflight_attempt") or checkpoint.get("completed_attempts"):
        return True
    return any(
        int(cell.get("pulse_count", 0) or 0) > 0 or bool(cell.get("last_attempt_id"))
        for cell in checkpoint.get("cells", {}).values()
    )


def _assert_program_target(row: int, column: int) -> None:
    if not (0 <= row < ROWS and 0 <= column < COLUMNS):
        raise ValueError(f"program target r{row}/c{column} is outside the {ROWS}x{COLUMNS} array")
    if (row, column) == REFERENCE_CELL:
        raise RuntimeError(
            f"hardware programming is prohibited for reference cell r{row}/c{column}"
        )


def nearest_state(conductance_us: float | None) -> tuple[int | None, bool, float | None, float | None]:
    if conductance_us is None:
        return None, False, None, None
    centers = [(low + high) / 2.0 for low, high in TARGET_BANDS_US]
    state = min(range(16), key=lambda code: abs(conductance_us - centers[code]))
    low, high = TARGET_BANDS_US[state]
    return state, low <= conductance_us <= high, low, high


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return None


@dataclass(frozen=True)
class AttemptSpec:
    row: int
    column: int
    stage: str
    stage_index: int
    requested_state_code: int
    operation: str
    program_path: str
    pwm_code: int | None
    voltage_v: float | None
    post_reads: int = POST_WRITE_READS
    program_epoch: int = 0

    @property
    def cell_id(self) -> str:
        return f"chip{CHIP_ID:02d}_r{self.row:02d}_c{self.column:02d}"

    def idempotency_key(self, run_id: str) -> str:
        fields: list[Any] = [
            run_id,
            self.cell_id,
            self.stage,
            self.stage_index,
            self.operation,
            self.program_path,
            self.pwm_code,
            self.voltage_v,
            self.post_reads,
        ]
        # Preserve every pre-amendment attempt ID byte-for-byte.  Only a
        # post-forming WB restart adds an epoch discriminator.
        if self.program_epoch:
            fields.append(f"program_epoch={self.program_epoch}")
        payload = "|".join(map(str, fields))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def attempt_id(self, run_id: str) -> str:
        return self.idempotency_key(run_id)[:32]


class HardwareLock:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self.acquired = False

    def __enter__(self) -> "HardwareLock":
        payload = json.dumps({"run_id": self.run_id, "pid": os.getpid(), "created_utc": utc_now()}) + "\n"
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            detail = self.path.read_text(encoding="utf-8", errors="replace") if self.path.exists() else ""
            raise RuntimeError(f"hardware is already locked: {detail.strip()}") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self.acquired = True
        return self

    def __exit__(self, *_: object) -> None:
        if self.acquired and self.path.is_file():
            self.path.unlink()
        self.acquired = False


class VivadoRunner:
    def __init__(self, store: RunStore):
        self.store = store

    def run(
        self,
        job_id: str,
        arguments: list[str],
        raw_dir: Path,
        on_line: Callable[[str, str, int], None],
    ) -> tuple[int, Path, Path, float]:
        raw_dir.mkdir(parents=True, exist_ok=True)
        vivado_log = raw_dir / f"{job_id}.vivado.log"
        console_log = raw_dir / f"{job_id}.console.jsonl"
        command = [
            str(VIVADO),
            "-mode",
            "batch",
            "-nojournal",
            "-notrace",
            "-log",
            str(vivado_log),
            "-source",
            str(CONTROL_SCRIPT),
            "-tclargs",
            *arguments,
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=str(CONTROL_SCRIPT.parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        assert process.stdout is not None
        with console_log.open("x", encoding="utf-8", newline="\n") as raw:
            for line_number, line in enumerate(process.stdout, 1):
                timestamp = utc_now()
                text = line.rstrip("\r\n")
                raw.write(json.dumps({"time_utc": timestamp, "line_number": line_number, "text": text}) + "\n")
                raw.flush()
                on_line(text, timestamp, line_number)
            raw.flush()
            os.fsync(raw.fileno())
        return process.wait(), vivado_log, console_log, time.monotonic() - started


class ArrayExperiment:
    """One durable hardware writer.  Every method stops at attempt boundaries."""

    def __init__(self, store: RunStore, *, verify_plan: bool = True):
        self.store = store
        self.run_id = store.run_id
        manifest = store.read_manifest()
        self._balanced_plan = normalise_balanced_plan(
            manifest.get("balanced_plan"),
            rows=ROWS,
            columns=COLUMNS,
            reference_cell=REFERENCE_CELL,
        )
        self._fast_response_screen = (
            self._balanced_plan.get("fast_response_screen")
            if self._balanced_plan is not None
            else None
        )
        self._scan_set_forming_policy = (
            self._balanced_plan.get("scan_set_forming_policy", SCAN_SET_FORMING_GUARDED)
            if self._balanced_plan is not None
            else SCAN_SET_FORMING_GUARDED
        )
        raw_scope = manifest.get("execution_scope")
        self.execution_scope = self._normalise_execution_scope(raw_scope)
        self._explicit_execution_scope = self.execution_scope if raw_scope is not None else None
        if self._balanced_plan is not None:
            planned = tuple(
                (item["row"], item["column"])
                for item in self._balanced_plan["execution_cells"]
            )
            scope_cells = tuple(
                (item["row"], item["column"])
                for item in self.execution_scope.get("cells", [])
            )
            if self.execution_scope.get("kind") != "explicit_cells" or scope_cells != planned:
                raise RuntimeError(
                    "balanced_plan execution_cells must exactly match ordered explicit execution_scope.cells"
                )
            self._scope_reference_count = int(
                REFERENCE_CELL[1] in self.execution_scope.get("columns", [])
            )
            self._scope_cells = planned + (
                (REFERENCE_CELL,) if self._scope_reference_count else ()
            )
            self._planned_hardware_cells = planned
            self._balanced_targets = {
                (item["row"], item["column"]): item["target_state_code"]
                for item in self._balanced_plan["execution_cells"]
            }
        else:
            selected_columns = tuple(self.execution_scope["columns"])
            self._scope_cells = tuple(
                (row, column)
                for row in range(ROWS)
                for column in selected_columns
            )
            self._planned_hardware_cells = tuple(
                cell for cell in self._scope_cells if cell != REFERENCE_CELL
            )
            self._scope_reference_count = int(REFERENCE_CELL in self._scope_cells)
            self._balanced_targets: dict[tuple[int, int], int] = {}
        self.vivado = VivadoRunner(store)
        self.checkpoint_path = store.run_dir / "live" / "checkpoint.json"
        self._checkpoint = self._load_checkpoint()
        if verify_plan:
            self._verify_plan_fingerprint(allow_initialize=True)
        self._thread_id = threading.get_ident()

    @staticmethod
    def _normalise_execution_scope(value: Any) -> dict[str, Any]:
        if value is None:
            return {"kind": "full_array", "columns": list(range(COLUMNS))}
        if not isinstance(value, dict):
            raise RuntimeError("manifest execution_scope must be an object")
        raw_kind = str(value.get("kind") or "")
        if raw_kind == "explicit_cells":
            raw_cells = value.get("cells")
            if not isinstance(raw_cells, list) or not raw_cells:
                raise RuntimeError("execution_scope.cells must be a non-empty list")
            cells: list[dict[str, int]] = []
            coordinates: set[tuple[int, int]] = set()
            for index, raw in enumerate(raw_cells):
                if not isinstance(raw, dict):
                    raise RuntimeError(f"execution_scope.cells[{index}] must be an object")
                row = raw.get("row")
                column = raw.get("column")
                if (
                    isinstance(row, bool)
                    or not isinstance(row, int)
                    or isinstance(column, bool)
                    or not isinstance(column, int)
                    or not 0 <= row < ROWS
                    or not 0 <= column < COLUMNS
                ):
                    raise RuntimeError(f"execution_scope.cells[{index}] is outside the array")
                coordinate = (row, column)
                if coordinate in coordinates:
                    raise RuntimeError(f"duplicate execution-scope cell r{row:02d}/c{column:02d}")
                coordinates.add(coordinate)
                cells.append({"row": row, "column": column})
            columns = sorted({item["column"] for item in cells})
            raw_columns = value.get("columns")
            if raw_columns is not None and raw_columns != columns:
                raise RuntimeError("execution_scope.columns conflicts with explicit cells")
            return {"kind": "explicit_cells", "columns": columns, "cells": cells}

        raw_columns = value.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise RuntimeError("manifest execution_scope.columns must be a non-empty list")
        columns: list[int] = []
        for raw in raw_columns:
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise RuntimeError("execution-scope columns must be integers")
            if not 0 <= raw < COLUMNS:
                raise RuntimeError(f"execution-scope column {raw!r} is outside 0..{COLUMNS - 1}")
            columns.append(raw)
        columns = sorted(set(columns))
        expected_kind = "full_array" if len(columns) == COLUMNS else "columns"
        supplied_kind = str(value.get("kind") or expected_kind)
        if supplied_kind != expected_kind:
            raise RuntimeError(
                f"execution_scope.kind={supplied_kind!r} conflicts with selected columns"
            )
        return {"kind": expected_kind, "columns": columns}

    def _append_row(self, table: str, row: dict[str, Any]) -> bool:
        """Keep parser-only convenience fields out of the normalized schema."""
        allowed = TABLE_SCHEMAS[table]
        normalized = {key: value for key, value in row.items() if key in allowed}
        if "event_id" in allowed and not normalized.get("event_id"):
            material = json.dumps(normalized, sort_keys=True, default=str) + utc_now()
            normalized["event_id"] = hashlib.sha256(f"{table}|{material}".encode()).hexdigest()[:24]
        return self.store.append_row(table, normalized)

    def _load_checkpoint(self) -> dict[str, Any]:
        if self.checkpoint_path.is_file():
            return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        checkpoint = {
            "run_id": self.run_id,
            "created_utc": utc_now(),
            "updated_utc": utc_now(),
            "inflight_attempt": None,
            "completed_attempts": {},
            "cells": {
                f"r{r:02d}_c{c:02d}": {
                    "status": "reference_complete" if (r, c) == REFERENCE_CELL else "pending",
                    "stage": "reference" if (r, c) == REFERENCE_CELL else "pending",
                    "states_observed": [],
                }
                for r in range(ROWS)
                for c in range(COLUMNS)
            },
            "durations_seconds": [],
        }
        atomic_json(self.checkpoint_path, checkpoint)
        return checkpoint

    def _verify_plan_fingerprint(self, *, allow_initialize: bool = False) -> None:
        current = current_plan_fingerprint(
            self._explicit_execution_scope,
            self._balanced_plan,
        )
        missing = [
            label
            for label, record in current["artifacts"].items()
            if not record.get("sha256")
        ]
        if missing:
            raise RuntimeError(
                "cannot freeze hardware plan; controller artifacts are missing: "
                + ", ".join(missing)
            )
        self._verify_chip_safety(current)
        stored = self._checkpoint.get("plan_fingerprint")
        if stored is None:
            if not allow_initialize or _checkpoint_has_program_activity(self._checkpoint):
                raise RuntimeError(
                    "legacy checkpoint has programming activity but no frozen plan fingerprint; "
                    "automatic resume is prohibited"
                )
            self._checkpoint["plan_fingerprint"] = current
            self._save_checkpoint()
            return
        if not isinstance(stored, dict) or stored.get("sha256") != current["sha256"]:
            changed: list[str] = []
            old_artifacts = stored.get("artifacts", {}) if isinstance(stored, dict) else {}
            for label, record in current["artifacts"].items():
                if (old_artifacts.get(label) or {}).get("sha256") != record.get("sha256"):
                    changed.append(label)
            if not isinstance(stored, dict) or stored.get("plan") != current["plan"]:
                changed.append("experiment_plan")
            detail = ", ".join(changed) or "fingerprint payload"
            raise RuntimeError(
                f"frozen plan/controller fingerprint mismatch ({detail}); "
                "automatic resume is prohibited"
            )

    def _verify_chip_safety(self, current_plan: dict[str, Any]) -> None:
        """Enforce a chip-wide hold across run directories and restarts."""

        if not CHIP_SAFETY_FILE.is_file():
            return
        try:
            safety = json.loads(CHIP_SAFETY_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("chip safety state is unreadable; hardware access is prohibited") from exc
        status = str(safety.get("status") or "hold")
        if status == "clear":
            return
        if status != "mitigated_run_authorized":
            raise RuntimeError(
                "chip-level safety hold is active: "
                + str(safety.get("reason") or "unspecified hardware safety event")
            )
        if safety.get("allowed_run_id") != self.run_id:
            raise RuntimeError(
                f"chip safety authorization is limited to run {safety.get('allowed_run_id')!r}"
            )
        if safety.get("allowed_plan_sha256") != current_plan.get("sha256"):
            raise RuntimeError("chip safety authorization does not match the frozen plan fingerprint")

    def _engage_chip_safety_hold(
        self,
        reason: str,
        *,
        attempt_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "schema_version": 1,
            "status": "hold",
            "chip_id": CHIP_ID,
            "reason": reason,
            "source_run_id": self.run_id,
            "source_attempt_id": attempt_id,
            "evidence": evidence or {},
            "automatic_new_run_bypass": False,
            "updated_utc": utc_now(),
        }
        atomic_json(CHIP_SAFETY_FILE, payload)
        self.store.append_event(
            "chip_safety_hold_engaged",
            attempt_id=attempt_id,
            reason=reason,
            evidence=evidence or {},
        )

    def _save_checkpoint(self) -> None:
        self._checkpoint["updated_utc"] = utc_now()
        atomic_json(self.checkpoint_path, self._checkpoint)

    def _control(self) -> str:
        if not CONTROL_FILE.is_file():
            return "run"
        try:
            value = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "run"
        if value.get("run_id") != self.run_id:
            return "run"
        return str(value.get("action", "run"))

    def _boundary_check(self) -> bool:
        action = self._control()
        if action not in {"pause", "stop"}:
            return True
        self.store.append_event(f"run_{action}d", reason="operator_request", safe_boundary=True)
        terminal = "paused" if action == "pause" else "stopped"
        self.store.update_progress(status=terminal, run_status=terminal, phase=terminal, message=f"{action.title()}d at a safe attempt boundary")
        return False

    def _set_cell_status(self, row: int, column: int, status: str, stage: str, reason: str = "") -> None:
        key = f"r{row:02d}_c{column:02d}"
        cell = self._checkpoint["cells"][key]
        cell.update(status=status, stage=stage, reason=reason, updated_utc=utc_now())
        self._save_checkpoint()
        self._append_row(
            "cell_status_events",
            {
                "event_seq": "",
                "event_id": hashlib.sha256(f"{self.run_id}|{key}|{status}|{utc_now()}".encode()).hexdigest()[:24],
                "run_id": self.run_id,
                "cell_id": f"chip{CHIP_ID:02d}_{key}",
                "event_time_utc": utc_now(),
                "status": status,
                "stage": stage,
                "target_state_code": "",
                "last_attempt_id": cell.get("last_attempt_id", ""),
                "pulse_count": cell.get("pulse_count", 0),
                "reason": reason,
                "worker_pid": os.getpid(),
            },
        )
        terminal = {
            "complete",
            "reference_complete",
            "target_unreached",
            "safety_stop",
            "hardware_error",
        }
        complete_hardware = sum(
            self._checkpoint["cells"][f"r{planned_row:02d}_c{planned_column:02d}"]["status"] in terminal
            for planned_row, planned_column in self._planned_hardware_cells
        )
        complete = complete_hardware + self._scope_reference_count
        scope_total = len(self._scope_cells)
        self.store.update_progress(
            completed_cells=complete,
            pending_cells=scope_total - complete,
            total_cells=scope_total,
            hardware_cells=len(self._planned_hardware_cells),
            reference_cells=self._scope_reference_count,
            execution_scope=self.execution_scope,
            current_row=row,
            current_column=column,
            current_cell_id=f"r{row:02d}_c{column:02d}",
            current_stage=stage,
            phase=stage,
            message=reason or f"{status}: r{row}/c{column}",
        )

    def run(self) -> None:
        problems = validate_environment()
        if problems:
            raise RuntimeError("; ".join(problems))
        self._verify_plan_fingerprint()
        if self._checkpoint.get("inflight_attempt"):
            attempt = self._checkpoint["inflight_attempt"]
            self.store.update_progress(status="blocked", run_status="blocked", phase="blocked", message=f"Uncertain in-flight attempt {attempt}; read-only recovery required")
            raise RuntimeError(f"refusing automatic replay of uncertain attempt {attempt}")
        with HardwareLock(GLOBAL_HARDWARE_LOCK, self.run_id):
            atomic_json(CONTROL_FILE, {"run_id": self.run_id, "action": "run", "updated_utc": utc_now()})
            scope_total = len(self._scope_cells)
            self.store.append_event(
                "run_started",
                worker_pid=os.getpid(),
                post_write_reads=POST_WRITE_READS,
                execution_scope=self.execution_scope,
                planned_hardware_cells=len(self._planned_hardware_cells),
            )
            self.store.update_progress(
                status="running",
                run_status="running",
                phase="running",
                worker_pid=os.getpid(),
                total_cells=scope_total,
                hardware_cells=len(self._planned_hardware_cells),
                reference_cells=self._scope_reference_count,
                completed_cells=self._scope_reference_count,
                pending_cells=len(self._planned_hardware_cells),
                execution_scope=self.execution_scope,
                message=(
                    "Starting full-array cumulative experiment"
                    if self.execution_scope["kind"] == "full_array"
                    else f"Starting cumulative experiment on columns {self.execution_scope['columns']}"
                ),
            )
            try:
                for row, column in self._planned_hardware_cells:
                    key = f"r{row:02d}_c{column:02d}"
                    if self._checkpoint["cells"][key]["status"] in {
                        "complete",
                        "target_unreached",
                        "safety_stop",
                        "hardware_error",
                    }:
                        continue
                    if not self._boundary_check():
                        return
                    self._run_cell(row, column)
                self.store.append_event(
                    "run_completed",
                    completed_cells=scope_total,
                    execution_scope=self.execution_scope,
                )
                self.store.update_progress(
                    status="completed",
                    run_status="completed",
                    phase="completed",
                    completed_cells=scope_total,
                    pending_cells=0,
                    total_cells=scope_total,
                    hardware_cells=len(self._planned_hardware_cells),
                    reference_cells=self._scope_reference_count,
                    execution_scope=self.execution_scope,
                    message=(
                        "All selected cells accounted for"
                        if self.execution_scope["kind"] != "full_array"
                        else "All 1,024 inventory cells accounted for"
                    ),
                )
            except Exception as exc:
                self.store.append_event("run_hardware_error", error=str(exc), worker_pid=os.getpid())
                self.store.update_progress(status="blocked", run_status="blocked", phase="blocked", message=str(exc))
                raise

    def _run_cell(self, row: int, column: int) -> None:
        if self._balanced_plan is not None:
            self._run_balanced_cell(row, column)
            return
        _assert_program_target(row, column)
        self._set_cell_status(row, column, "running", "initial_read")
        initial = self._run_read_only(row, column, "initial", PRE_READS)
        if not initial["valid"]:
            reason = str(initial.get("error") or "initial scan read was invalid")
            self.store.append_event(
                "read_only_retry_required",
                row=row,
                column=column,
                role="initial",
                failed_job_id=initial.get("job_id", ""),
                failed_attempt_id=initial.get("attempt_id", ""),
                error=reason,
                program_operation_started=False,
                retry_mode="fresh_read_job",
            )
            # A failed initial scan read is not a terminal cell result.  Leaving
            # the cell retryable also prevents the outer row loop from silently
            # advancing to later cells after a transport/cleanup failure.
            self._set_cell_status(row, column, "retry_pending", "initial_read", reason)
            raise RuntimeError(
                f"r{row:02d}/c{column:02d} initial scan read failed; "
                "fresh read-only retry required"
            )

        if not self._boundary_check():
            return
        reset = AttemptSpec(row, column, "wb_reset", 0, 0, "reset", "wb_pwm", RESET_PWM, RESET_VOLTAGE_V)
        reset_result = self._run_program_attempt(reset)
        if not reset_result.get("accepted"):
            self._set_cell_status(row, column, "safety_stop", "wb_reset", reset_result.get("result_class", "gate rejected"))
            return

        baseline_us = reset_result.get("post_median_uS")
        if baseline_us is None or baseline_us > RESET_SAFE_MAX_US:
            if not self._boundary_check():
                return
            fallback = self._run_scan_reset(row, column)
            if not fallback["valid"]:
                self._set_cell_status(row, column, "safety_stop", "scan_reset_fallback", fallback["error"])
                return
            baseline_us = fallback["post_median_uS"]

        if baseline_us is not None and baseline_us >= SET_PRE_PULSE_GUARD_US:
            self._set_cell_status(row, column, "safety_stop", "pre_set_guard", f"baseline {baseline_us:.3f} µS is above SET guard")
            return

        latest: dict[str, Any] | None = None
        for index, pwm in enumerate(PWM_CODES, 1):
            if not self._boundary_check():
                return
            spec = AttemptSpec(row, column, "pwm_ladder", index, pwm // 16, "set", "wb_pwm", pwm, SET_FIXED_VOLTAGE_V)
            latest = self._run_program_attempt(spec)
            if not latest.get("accepted"):
                self._set_cell_status(row, column, "safety_stop", "pwm_ladder", latest.get("result_class", "gate rejected"))
                return
            if latest.get("pre_pulse_skipped") or (latest.get("post_max_uS") or 0.0) >= SET_PRE_PULSE_GUARD_US:
                self._set_cell_status(row, column, "safety_stop", "pwm_ladder", "SET ceiling guard reached")
                return
            if latest.get("assigned_state_code") == 15 and latest.get("landed"):
                self._set_cell_status(
                    row,
                    column,
                    "complete",
                    "pwm_ladder",
                    f"C15 landed at PWM 0x{pwm:02X}; stopped before stronger SET",
                )
                return

        assigned = latest.get("assigned_state_code") if latest else None
        landed = bool(latest and latest.get("landed"))
        if assigned == 15 and landed:
            self._set_cell_status(row, column, "complete", "pwm_ladder", "C15 landed without voltage skew")
            return

        for index, voltage in enumerate(SET_VOLTAGE_SKEW_V, 1):
            if not self._boundary_check():
                return
            requested = min(15, (assigned + 1) if isinstance(assigned, int) else 1)
            spec = AttemptSpec(row, column, "voltage_skew", index, requested, "set", "wb_pwm", 0xF0, voltage)
            latest = self._run_program_attempt(spec)
            if not latest.get("accepted"):
                self._set_cell_status(row, column, "safety_stop", "voltage_skew", latest.get("result_class", "gate rejected"))
                return
            assigned = latest.get("assigned_state_code")
            if latest.get("pre_pulse_skipped") or (latest.get("post_max_uS") or 0.0) >= SET_PRE_PULSE_GUARD_US:
                self._set_cell_status(row, column, "safety_stop", "voltage_skew", "SET ceiling guard reached")
                return
            if assigned == 15 and latest.get("landed"):
                self._set_cell_status(row, column, "complete", "voltage_skew", f"C15 landed at {voltage:.4f} V")
                return

        self._set_cell_status(row, column, "complete", "voltage_skew", "Full cumulative plan completed; C15 not necessarily landed")

    @staticmethod
    def _read_block_statistics(result: dict[str, Any]) -> dict[str, float] | None:
        values = [
            float(read["conductance_uS"])
            for read in result.get("reads", [])
            if read.get("conductance_uS") is not None
        ]
        if not values:
            return None
        return {
            "median_uS": statistics.median(values),
            "mean_uS": statistics.fmean(values),
            "stddev_uS": statistics.stdev(values) if len(values) > 1 else 0.0,
            "min_uS": min(values),
            "max_uS": max(values),
        }

    def _record_balanced_read_observation(
        self,
        row: int,
        column: int,
        target: int,
        result: dict[str, Any],
        phase: str,
    ) -> dict[str, Any]:
        stats = self._read_block_statistics(result)
        if stats is None:
            raise RuntimeError("cannot record balanced observation without scan values")
        assigned, landed, _, _ = nearest_state(stats["median_uS"])
        target_low, target_high = TARGET_BANDS_US[target]
        target_landed = target_low <= stats["median_uS"] <= target_high
        if target_landed:
            # Adjacent inclusive bands share an edge.  Balanced runs always
            # attribute an exact shared edge to the immutable requested target,
            # not nearest_state's legacy downward tie break.
            assigned = target
            landed = True
        attempt_id = str(result.get("attempt_id") or result.get("job_id") or "")
        observation_id = hashlib.sha256(
            f"{attempt_id}|{phase}|target={target}".encode()
        ).hexdigest()[:24]
        self._append_row(
            "state_observations",
            {
                "run_id": self.run_id,
                "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                "observation_id": observation_id,
                "attempt_id": attempt_id,
                "observation_phase": phase,
                "target_state_code": target,
                "assigned_state_code": assigned,
                "target_low_uS": target_low,
                "target_high_uS": target_high,
                "sample_count": len(result.get("reads", [])),
                **stats,
                "distance_to_target_center_uS": abs(
                    stats["median_uS"] - (target_low + target_high) / 2.0
                ),
                "landed": int(target_landed),
                "classification_revision": "4uS-bands-v2-balanced",
                "source_read_ids": " ".join(
                    str(read.get("read_id", "")) for read in result.get("reads", [])
                ),
            },
        )
        return {
            **stats,
            "assigned_state_code": assigned,
            "nearest_state_landed": landed,
            "target_landed": target_landed,
        }

    def _balanced_complete(
        self,
        row: int,
        column: int,
        target: int,
        median_us: float,
        source: str,
    ) -> None:
        key = f"r{row:02d}_c{column:02d}"
        cell = self._checkpoint["cells"][key]
        cell["balanced_target_state_code"] = target
        cell["balanced_final_median_uS"] = median_us
        cell["balanced_target_landed"] = True
        cell["states_observed"] = sorted(set(cell.get("states_observed", [])) | {target})
        self._save_checkpoint()
        self.store.append_event(
            "balanced_target_landed",
            row=row,
            column=column,
            target_state_code=target,
            median_uS=median_us,
            source=source,
        )
        self._set_cell_status(
            row,
            column,
            "complete",
            "balanced_target",
            f"C{target:X} landed at {median_us:.3f} µS ({source})",
        )

    def _balanced_unreached(
        self,
        row: int,
        column: int,
        target: int,
        median_us: float | None,
        reason: str,
    ) -> None:
        key = f"r{row:02d}_c{column:02d}"
        cell = self._checkpoint["cells"][key]
        cell["balanced_target_state_code"] = target
        cell["balanced_final_median_uS"] = median_us
        cell["balanced_target_landed"] = False
        self._save_checkpoint()
        self.store.append_event(
            "balanced_target_unreached",
            row=row,
            column=column,
            target_state_code=target,
            median_uS=median_us,
            reason=reason,
        )
        suffix = "unknown" if median_us is None else f"{median_us:.3f} µS"
        self._set_cell_status(
            row,
            column,
            "target_unreached",
            "balanced_target",
            f"C{target:X} unreached at {suffix}: {reason}",
        )

    def _balanced_read_failure(
        self,
        row: int,
        column: int,
        role: str,
        result: dict[str, Any],
        *,
        program_operation_started: bool,
    ) -> None:
        reason = str(result.get("error") or f"{role} scan read was invalid")
        self.store.append_event(
            "read_only_retry_required",
            row=row,
            column=column,
            role=role,
            failed_job_id=result.get("job_id", ""),
            failed_attempt_id=result.get("attempt_id", ""),
            error=reason,
            program_operation_started=program_operation_started,
            retry_mode="fresh_read_job",
        )
        self._set_cell_status(row, column, "retry_pending", role, reason)
        raise RuntimeError(
            f"r{row:02d}/c{column:02d} {role} scan read failed; "
            "fresh read-only retry required"
        )

    def _balanced_conditions(self) -> tuple[tuple[str, int, float], ...]:
        fixed = tuple(("balanced_pwm", pwm, SET_FIXED_VOLTAGE_V) for pwm in PWM_CODES)
        skew = tuple(("balanced_voltage", 0xF0, voltage) for voltage in SET_VOLTAGE_SKEW_V)
        return fixed + skew

    def _balanced_conditions_for_cell(
        self, cell: dict[str, Any]
    ) -> tuple[tuple[str, int, float], ...]:
        if self._fast_response_screen is None:
            return self._balanced_conditions()
        version = int(self._fast_response_screen["version"])
        expected_policy_name = f"fast_response_screen_v{version}"
        policy_name = cell.setdefault(
            "balanced_condition_policy", expected_policy_name
        )
        if policy_name != expected_policy_name:
            raise RuntimeError(
                f"unsupported durable balanced condition policy {policy_name!r}"
            )
        if version >= 6:
            return tuple(
                (
                    "balanced_threshold_search",
                    int(condition["pwm_code"]),
                    float(condition["voltage_v"]),
                )
                for condition in self._fast_response_screen["conditions"]
            )
        voltage = float(self._fast_response_screen["voltage_v"])
        if version >= 3:
            baseline = cell.get("balanced_initial_median_uS")
            if not isinstance(baseline, (int, float)):
                raise RuntimeError(
                    "adaptive fast-response screen requires a fresh initial median"
                )
            cutoff = float(self._fast_response_screen["low_conductance_cutoff_uS"])
            if float(baseline) < cutoff:
                voltage = float(
                    self._fast_response_screen["low_conductance_voltage_v"]
                )
            cell["balanced_selected_screen_voltage_v"] = voltage
            self._save_checkpoint()
        pulse = (
            "balanced_fast_response",
            int(self._fast_response_screen["pwm_code"]),
            voltage,
        )
        return tuple(pulse for _ in range(int(self._fast_response_screen["max_pulses"])))

    @staticmethod
    def _hard_program_gates_passed(result: dict[str, Any]) -> bool:
        """Require the normalized evidence bundle, not just a controller ACK."""

        return bool(
            result.get("accepted")
            and result.get("wb_ack")
            and result.get("tdc_pass")
            and result.get("analog_confirmed")
            and not result.get("clip")
            and result.get("sentinel_pass")
            and not result.get("safety_stop")
            and result.get("job_complete")
            and result.get("profile_restored")
            and not result.get("pre_pulse_skipped")
            and result.get("validation_errors") == []
            and result.get("pre_read_count") == PRE_READS
            and result.get("post_read_count") == POST_WRITE_READS
        )

    def _run_balanced_reset_recovery(
        self,
        row: int,
        column: int,
        target: int,
    ) -> dict[str, Any]:
        """Run one bounded overshoot-recovery sequence.

        One sequence is exactly one ordinary WB RESET and, only when that RESET
        passed every hard evidence gate but remains above the frozen target's
        upper edge, at most one guarded scan-debug RESET fallback. A rejected or
        ambiguous WB RESET can therefore never lead to a second reset.

        Durable stages are resume points. ``wb_completed_scan_required`` resumes
        from the immutable committed WB result instead of issuing it again.
        """

        key = f"r{row:02d}_c{column:02d}"
        cell = self._checkpoint["cells"][key]
        reset_limit = int(self._balanced_plan["overshoot_reset_limit"])
        completed_sequences = int(cell.get("balanced_recovery_count", 0))
        if completed_sequences >= reset_limit:
            return {"outcome": "budget_exhausted"}

        sequence = completed_sequences + 1
        stage = str(cell.get("balanced_reset_recovery_stage") or "not_started")
        stage_sequence = int(cell.get("balanced_reset_recovery_cycle", sequence))
        resumable = {"wb_planned", "wb_completed_scan_required", "scan_planned"}
        if stage in resumable and stage_sequence != sequence:
            raise RuntimeError(
                f"balanced RESET checkpoint cycle {stage_sequence} conflicts with expected cycle {sequence}"
            )
        if stage not in resumable:
            stage = "not_started"
            cell["balanced_reset_recovery_cycle"] = sequence

        spec = AttemptSpec(
            row,
            column,
            "balanced_overshoot_wb_reset",
            sequence,
            target,
            "reset",
            "wb_pwm",
            RESET_PWM,
            RESET_VOLTAGE_V,
        )
        wb_result: dict[str, Any] | None = None
        if stage in {"wb_completed_scan_required", "scan_planned"}:
            wb_result = self._checkpoint["completed_attempts"].get(
                spec.attempt_id(self.run_id)
            )
            if wb_result is None:
                self._set_cell_status(
                    row,
                    column,
                    "safety_stop",
                    "balanced_overshoot_wb_reset",
                    "checkpoint says WB RESET completed but its immutable result is missing",
                )
                return {"outcome": "terminal"}
        else:
            if not self._boundary_check():
                return {"outcome": "paused"}
            cell["balanced_reset_recovery_cycle"] = sequence
            cell["balanced_reset_recovery_stage"] = "wb_planned"
            self._save_checkpoint()
            # A post-commit interruption is safe: the attempt method returns the
            # immutable cached result and never reissues the packet.
            wb_result = self._run_program_attempt(spec)

        assert wb_result is not None
        if not self._hard_program_gates_passed(wb_result):
            cell["balanced_reset_recovery_stage"] = "wb_hard_stop"
            self._save_checkpoint()
            self._set_cell_status(
                row,
                column,
                "safety_stop",
                "balanced_overshoot_wb_reset",
                str(wb_result.get("result_class") or "WB RESET hard evidence gate rejected"),
            )
            return {"outcome": "terminal"}

        wb_median = wb_result.get("post_median_uS")
        if not isinstance(wb_median, (int, float)):
            cell["balanced_reset_recovery_stage"] = "wb_hard_stop"
            self._save_checkpoint()
            self._set_cell_status(
                row,
                column,
                "safety_stop",
                "balanced_overshoot_wb_reset",
                "accepted WB RESET has no finite five-read median",
            )
            return {"outcome": "terminal"}

        _, target_high = TARGET_BANDS_US[target]
        if float(wb_median) <= target_high:
            cell["balanced_recovery_count"] = sequence
            cell["balanced_reset_recovery_stage"] = "wb_safe"
            cell["balanced_next_condition_index"] = 0
            self._save_checkpoint()
            return {
                "outcome": "safe",
                "source": "WB RESET",
                "post_median_uS": float(wb_median),
                "post_max_uS": wb_result.get("post_max_uS"),
            }

        cell["balanced_reset_recovery_stage"] = "wb_completed_scan_required"
        self._save_checkpoint()
        if not self._boundary_check():
            return {"outcome": "paused"}

        cell["balanced_reset_recovery_stage"] = "scan_planned"
        self._save_checkpoint()
        scan_result = self._run_scan_reset(
            row,
            column,
            target_state_code=target,
        )
        cell["balanced_recovery_count"] = sequence
        cell["balanced_reset_recovery_stage"] = (
            "scan_complete" if scan_result.get("valid") else "scan_failed"
        )
        if scan_result.get("valid"):
            cell["balanced_next_condition_index"] = 0
        self._save_checkpoint()
        if not scan_result.get("valid"):
            self._balanced_unreached(
                row,
                column,
                target,
                scan_result.get("post_median_uS"),
                str(
                    scan_result.get("error")
                    or scan_result.get("result_class")
                    or "guarded scan RESET did not safely reset"
                ),
            )
            return {"outcome": "terminal"}
        return {
            "outcome": "safe",
            "source": "guarded scan RESET fallback",
            "post_median_uS": float(scan_result["post_median_uS"]),
            "post_max_uS": scan_result.get("post_max_uS"),
        }

    def _analog_only_advance_allowed(
        self,
        spec: AttemptSpec,
        result: dict[str, Any],
    ) -> bool:
        gates = result.get("gates", {})
        return bool(
            self._balanced_plan is not None
            and spec.operation == "set"
            and not result.get("accepted")
            and not result.get("pre_pulse_skipped")
            and not result.get("safety_stop")
            and result.get("validation_errors") == ["analog_confirmed"]
            and gates
            and all(
                bool(passed)
                for name, passed in gates.items()
                if name != "analog_confirmed"
            )
        )

    def _run_balanced_cell(self, row: int, column: int) -> None:
        _assert_program_target(row, column)
        target = self._balanced_targets[(row, column)]
        target_low, target_high = TARGET_BANDS_US[target]
        key = f"r{row:02d}_c{column:02d}"
        cell = self._checkpoint["cells"][key]
        stored_target = cell.get("balanced_target_state_code")
        if stored_target not in (None, target):
            raise RuntimeError(
                f"checkpoint target C{stored_target:X} conflicts with frozen C{target:X} for {key}"
            )
        cell["balanced_target_state_code"] = target
        cell.setdefault("balanced_recovery_count", 0)
        cell.setdefault("balanced_next_condition_index", 0)
        self._save_checkpoint()

        self._set_cell_status(row, column, "running", "balanced_initial_read")
        initial = self._run_read_only(
            row,
            column,
            "initial",
            PRE_READS,
            state_code_context=target,
        )
        if not initial["valid"]:
            self._balanced_read_failure(
                row,
                column,
                "balanced_initial_read",
                initial,
                program_operation_started=False,
            )
        stats = self._read_block_statistics(initial)
        if stats is None:
            self._balanced_read_failure(
                row,
                column,
                "balanced_initial_read",
                {**initial, "error": "initial read has no conductance values"},
                program_operation_started=False,
            )
            return
        current_us = stats["median_uS"]
        cell["balanced_initial_median_uS"] = current_us
        self._save_checkpoint()
        if stats["max_uS"] >= SCAN_FORM_THRESHOLD_US:
            # This latch is monotonic.  A later RESET may lower conductance,
            # but it can never re-authorize scan-debug SET for this cell.
            cell["scan_set_permanently_disabled"] = True
            cell["scan_set_disable_reason"] = (
                f"fresh balanced read maximum {stats['max_uS']:.6f} uS is at or above "
                f"{SCAN_FORM_THRESHOLD_US:.6f} uS"
            )
            self._save_checkpoint()
        if stats["max_uS"] >= SET_PRE_PULSE_GUARD_US:
            self._set_cell_status(
                row,
                column,
                "safety_stop",
                "balanced_initial_read",
                f"pre-SET maximum {stats['max_uS']:.3f} µS reached the SET guard",
            )
            return
        if target_low <= current_us <= target_high:
            recorded = self._record_balanced_read_observation(
                row,
                column,
                target,
                initial,
                "balanced_initial_target_verify",
            )
            self._balanced_complete(
                row,
                column,
                target,
                recorded["median_uS"],
                "five-read baseline",
            )
            return

        if (
            self._scan_set_forming_policy == SCAN_SET_FORMING_DISABLED
            and stats["max_uS"] < SCAN_FORM_THRESHOLD_US
        ):
            cell["scan_set_permanently_disabled"] = True
            cell["scan_forming_exhausted"] = True
            cell["scan_set_disable_reason"] = (
                "frozen plan prohibits scan-debug SET after the column-wide sentinel-drift hold"
            )
            recorded = self._record_balanced_read_observation(
                row,
                column,
                target,
                initial,
                "balanced_initial_below_forming_threshold",
            )
            self._save_checkpoint()
            self.store.append_event(
                "scan_set_forming_quarantined",
                row=row,
                column=column,
                target_state_code=target,
                maximum_uS=stats["max_uS"],
                threshold_uS=SCAN_FORM_THRESHOLD_US,
                policy=self._scan_set_forming_policy,
            )
            self._balanced_unreached(
                row,
                column,
                target,
                recorded["median_uS"],
                "scan-debug SET is disabled; fresh five-read maximum remained below 20 uS",
            )
            return

        # The durable pulse count, rather than the legacy exhausted cache, is
        # authoritative.  This lets an amended larger budget resume safely
        # without erasing the already-recorded forming history.
        forming_count = int(cell.get("scan_forming_pulse_count", 0))
        cell["scan_forming_exhausted"] = bool(
            cell.get("scan_set_permanently_disabled")
            or forming_count >= SCAN_FORM_MAX_PULSES_PER_CELL
        )
        self._save_checkpoint()
        forming_reads = initial["reads"]
        while (
            stats["max_uS"] < SCAN_FORM_THRESHOLD_US
            and not cell.get("scan_set_permanently_disabled")
            and forming_count < SCAN_FORM_MAX_PULSES_PER_CELL
        ):
            # This is the explicit safe pause boundary between separate
            # one-pulse hardware jobs.  A pause request after one pulse can
            # never be skipped by immediately issuing the next pulse.
            if not self._boundary_check():
                return
            forming = self._run_scan_set_forming(
                row,
                column,
                target_state_code=target,
                eligibility_reads=forming_reads,
            )
            if not forming.get("valid"):
                self._set_cell_status(
                    row,
                    column,
                    "safety_stop",
                    "scan_set_forming",
                    str(
                        forming.get("result_class")
                        or "guarded scan SET forming hard safety gate rejected"
                    ),
                )
                return
            forming_median = forming.get("post_median_uS")
            forming_max = forming.get("post_max_uS")
            if not isinstance(forming_median, (int, float)) or not isinstance(
                forming_max, (int, float)
            ):
                self._set_cell_status(
                    row,
                    column,
                    "safety_stop",
                    "scan_set_forming",
                    "guarded scan SET forming has no finite five-read result",
                )
                return
            current_us = float(forming_median)
            if target_low <= current_us <= target_high:
                self._balanced_complete(
                    row,
                    column,
                    target,
                    current_us,
                    "guarded scan-debug forming pulse sequence",
                )
                return
            if float(forming_max) >= SCAN_FORM_THRESHOLD_US:
                break
            if not forming.get("issued"):
                self._set_cell_status(
                    row,
                    column,
                    "safety_stop",
                    "scan_set_forming",
                    "guarded scan SET did not issue while the verified result remained below 20 uS",
                )
                return
            forming_count = int(cell.get("scan_forming_pulse_count", 0))
            verified_post_reads = forming.get("verified_post_reads")
            if not isinstance(verified_post_reads, list) or len(verified_post_reads) != POST_WRITE_READS:
                self._set_cell_status(
                    row,
                    column,
                    "safety_stop",
                    "scan_set_forming",
                    "guarded scan SET did not return its five verified post-reads",
                )
                return
            forming_reads = verified_post_reads

        forming_count = int(cell.get("scan_forming_pulse_count", 0))
        if (
            not cell.get("scan_set_permanently_disabled")
            and stats["max_uS"] < SCAN_FORM_THRESHOLD_US
            and forming_count >= SCAN_FORM_MAX_PULSES_PER_CELL
        ):
            self._set_cell_status(
                row,
                column,
                "safety_stop",
                "scan_set_forming",
                f"guarded scan SET exhausted {SCAN_FORM_MAX_PULSES_PER_CELL} pulses below {SCAN_FORM_THRESHOLD_US:.3f} uS; WB fallback prohibited",
            )
            return

        conditions = self._balanced_conditions_for_cell(cell)
        self._save_checkpoint()
        while True:
            if current_us > target_high:
                recovery = self._run_balanced_reset_recovery(row, column, target)
                if recovery["outcome"] in {"paused", "terminal"}:
                    return
                if recovery["outcome"] == "budget_exhausted":
                    self._balanced_unreached(
                        row,
                        column,
                        target,
                        current_us,
                        "overshoot remained after the bounded WB/scan RESET recovery sequence",
                    )
                    return
                current_us = float(recovery["post_median_uS"])
                if (recovery.get("post_max_uS") or current_us) >= SET_PRE_PULSE_GUARD_US:
                    self._set_cell_status(
                        row,
                        column,
                        "safety_stop",
                        "balanced_reset_recovery",
                        "post-RESET maximum reached the SET guard",
                    )
                    return
                if target_low <= current_us <= target_high:
                    self._balanced_complete(
                        row,
                        column,
                        target,
                        current_us,
                        str(recovery["source"]),
                    )
                    return
                if current_us > target_high:
                    self._balanced_unreached(
                        row,
                        column,
                        target,
                        current_us,
                        "guarded RESET recovery remained above the target band",
                    )
                    return
                continue

            next_index = int(cell.get("balanced_next_condition_index", 0))
            if next_index >= len(conditions):
                self._balanced_unreached(
                    row,
                    column,
                    target,
                    current_us,
                    (
                        "maximum-voltage responsiveness screen exhausted"
                        if self._fast_response_screen is not None
                        else "full fixed-PWM and voltage-skew envelope exhausted"
                    ),
                )
                return

            overshot = False
            if not self._boundary_check():
                return
            cycle = int(cell.get("balanced_recovery_count", 0))
            pending_specs = [
                AttemptSpec(
                    row,
                    column,
                    stage,
                    cycle * 1000 + condition_index + 1,
                    target,
                    "set",
                    "wb_pwm",
                    pwm,
                    voltage,
                    program_epoch=int(cell.get("balanced_program_epoch", 0)),
                )
                for condition_index in range(next_index, len(conditions))
                for stage, pwm, voltage in (conditions[condition_index],)
            ]
            batch_results = self._run_balanced_program_sequence(pending_specs)
            for offset, result in enumerate(batch_results):
                condition_index = next_index + offset
                stage, pwm, voltage = conditions[condition_index]
                cell["balanced_next_condition_index"] = condition_index + 1
                self._save_checkpoint()

                post_max = result.get("post_max_uS")
                if (
                    isinstance(post_max, (int, float))
                    and post_max >= SCAN_FORM_THRESHOLD_US
                ):
                    cell["scan_set_permanently_disabled"] = True
                    cell["scan_set_disable_reason"] = (
                        f"WB post-read maximum {float(post_max):.6f} uS is at or above "
                        f"{SCAN_FORM_THRESHOLD_US:.6f} uS"
                    )
                    self._save_checkpoint()
                if result.get("pre_pulse_skipped") or (
                    isinstance(post_max, (int, float))
                    and post_max >= SET_PRE_PULSE_GUARD_US
                ):
                    self._set_cell_status(
                        row,
                        column,
                        "safety_stop",
                        stage,
                        "SET ceiling guard reached",
                    )
                    return
                if not result.get("accepted") and not result.get("advance_allowed"):
                    self._set_cell_status(
                        row,
                        column,
                        "safety_stop",
                        stage,
                        str(result.get("result_class") or "hard evidence gate rejected"),
                    )
                    return
                post_median = result.get("post_median_uS")
                if not isinstance(post_median, (int, float)):
                    self._set_cell_status(
                        row,
                        column,
                        "safety_stop",
                        stage,
                        "program attempt has no finite post-write median",
                    )
                    return
                current_us = float(post_median)

                if target_low <= current_us <= target_high:
                    if result.get("advance_allowed"):
                        if not self._boundary_check():
                            return
                        verify = self._run_read_only(
                            row,
                            column,
                            "post_write",
                            POST_WRITE_READS,
                            parent_attempt_id=result.get("attempt_id"),
                            state_code_context=target,
                        )
                        if not verify["valid"]:
                            self._balanced_read_failure(
                                row,
                                column,
                                "balanced_post_write_verify",
                                verify,
                                program_operation_started=True,
                            )
                        recorded = self._record_balanced_read_observation(
                            row,
                            column,
                            target,
                            verify,
                            "balanced_post_write_verify",
                        )
                        current_us = recorded["median_uS"]
                        if not recorded["target_landed"]:
                            if current_us > target_high:
                                overshot = True
                                break
                            continue
                    self._balanced_complete(
                        row,
                        column,
                        target,
                        current_us,
                        (
                            "independent five-read verify after analog-unconfirmed pulse"
                            if result.get("advance_allowed")
                            else f"{stage} PWM 0x{pwm:02X} at {voltage:.4f} V"
                        ),
                    )
                    return
                if current_us > target_high:
                    overshot = True
                    break

            if overshot:
                continue
            self._balanced_unreached(
                row,
                column,
                target,
                current_us,
                (
                    "maximum-voltage responsiveness screen exhausted"
                    if self._fast_response_screen is not None
                    else "full fixed-PWM and voltage-skew envelope exhausted"
                ),
            )
            return

    def _arguments_for_program(self, spec: AttemptSpec, job_id: str) -> list[str]:
        _assert_program_target(spec.row, spec.column)
        if spec.operation == "set":
            return [
                "pwm_doe_set", str(spec.row), str(spec.column), str(spec.voltage_v), str(spec.pwm_code),
                str(RESET_VOLTAGE_V), str(RESET_PWM), str(READ_HOLD_CLOCKS), str(spec.post_reads), job_id,
            ]
        return [
            "pwm_doe_reset", str(spec.row), str(spec.column), str(SET_FIXED_VOLTAGE_V), str(0x10),
            str(spec.voltage_v), str(spec.pwm_code), str(READ_HOLD_CLOCKS), str(spec.post_reads), job_id,
        ]

    def _run_program_batch(self, specs: list[AttemptSpec]) -> list[dict[str, Any]]:
        """Run a guarded SET ladder in one Vivado process.

        The Tcl controller still executes and records one complete evidence bundle per
        pulse.  It stops before the next pulse when the assigned 4-bit band is reached,
        overshot, or any hard safety gate fires.
        """

        if not specs:
            return []
        first = specs[0]
        if any(
            spec.row != first.row
            or spec.column != first.column
            or spec.operation != "set"
            or spec.requested_state_code != first.requested_state_code
            or spec.post_reads != POST_WRITE_READS
            for spec in specs
        ):
            raise RuntimeError("PWM batch must be one cell, one SET target, and five reads per pulse")
        for spec in specs:
            _assert_program_target(spec.row, spec.column)
            if spec.attempt_id(self.run_id) in self._checkpoint["completed_attempts"]:
                raise RuntimeError("PWM batch cannot contain an already-completed attempt")
        self._verify_plan_fingerprint()

        attempt_ids = [spec.attempt_id(self.run_id) for spec in specs]
        batch_id = hashlib.sha256(
            (self.run_id + "|pwm_batch|" + "|".join(attempt_ids)).encode()
        ).hexdigest()[:32]
        key = f"r{first.row:02d}_c{first.column:02d}"
        raw_dir = self.store.run_dir / "cells" / key / "raw" / "vivado"
        raw_dir.mkdir(parents=True, exist_ok=True)
        plan_lines = [
            (
                f"{spec.row} {spec.column} set {spec.pwm_code} {spec.voltage_v:.6f} "
                f"{spec.post_reads} "
                f"{'fasttarget' if spec.stage == 'balanced_fast_response' else 'target'}"
                f"{spec.requested_state_code}"
            )
            for spec in specs
        ]
        plan_text = "\n".join(plan_lines) + "\n"
        runtime_plan = CONTROL_SCRIPT.parent / "runs" / f"{batch_id}.pwm.txt"
        runtime_plan.parent.mkdir(parents=True, exist_ok=True)
        with runtime_plan.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(plan_text)
            handle.flush()
            os.fsync(handle.fileno())
        evidence_plan = raw_dir / f"{batch_id}.pwm.txt"
        shutil.copy2(runtime_plan, evidence_plan)

        accumulators = [
            AttemptAccumulator(
                spec.row,
                spec.column,
                spec.operation,
                expected_post_reads=spec.post_reads,
            )
            for spec in specs
        ]
        started_indices: list[int] = []
        current_index: int | None = None
        stop_marker = ""

        def on_line(text: str, timestamp_utc: str, line_number: int) -> None:
            nonlocal current_index, stop_marker
            if text.startswith("PWM_BATCH_CELL_START "):
                fields = dict(
                    token.split("=", 1)
                    for token in text.split()[1:]
                    if "=" in token
                )
                index = int(fields["index"]) - 1
                if index != len(started_indices) or not 0 <= index < len(specs):
                    raise RuntimeError(f"non-sequential PWM batch index {index + 1}")
                current_index = index
                started_indices.append(index)
            if text.startswith("PWM_BATCH_STOP_"):
                stop_marker = text
            if text == "JOB_COMPLETE pwm_doe_batch":
                for index in started_indices:
                    accumulators[index].feed(text, timestamp_utc, line_number)
            elif current_index is not None:
                accumulators[current_index].feed(text, timestamp_utc, line_number)

        for spec, attempt_id in zip(specs, attempt_ids):
            self.store.append_event(
                "attempt_planned",
                attempt_id=attempt_id,
                batch_id=batch_id,
                **asdict(spec),
            )
        self._checkpoint["inflight_attempt"] = batch_id
        self._checkpoint["inflight_batch"] = {
            "batch_id": batch_id,
            "attempt_ids": attempt_ids,
            "plan": str(evidence_plan.relative_to(self.store.run_dir)),
        }
        self._save_checkpoint()
        started_utc = utc_now()
        self.store.append_event(
            "hardware_batch_started",
            batch_id=batch_id,
            cell_id=first.cell_id,
            target_state_code=first.requested_state_code,
            planned_attempts=len(specs),
            attempt_ids=attempt_ids,
        )
        arguments = [
            "pwm_doe_batch",
            str(first.row),
            str(first.column),
            str(first.voltage_v),
            str(first.pwm_code),
            str(RESET_VOLTAGE_V),
            str(RESET_PWM),
            str(READ_HOLD_CLOCKS),
            str(POST_WRITE_READS),
            batch_id,
        ]
        try:
            code, vivado_log, console_log, duration = self.vivado.run(
                batch_id,
                arguments,
                raw_dir,
                on_line,
            )
        finally:
            if runtime_plan.is_file():
                runtime_plan.unlink()

        if code != 0 or not started_indices:
            raise RuntimeError(
                f"PWM batch {batch_id} failed or executed no attempts; automatic replay is prohibited"
            )
        decoded: list[tuple[AttemptSpec, dict[str, Any]]] = []
        for index in started_indices:
            result = accumulators[index].finalize(code)
            if not result.get("job_complete") or not result.get("profile_restored"):
                raise RuntimeError(
                    f"PWM batch attempt {attempt_ids[index]} lacks completion or scan-profile restoration"
                )
            decoded.append((specs[index], result))

        normalized: list[dict[str, Any]] = []
        per_attempt_duration = duration / len(decoded)
        for spec, result in decoded:
            sentinel_drift = result.get("summary", {}).get(
                "sentinel_max_abs_drift_uA_computed"
            )
            if isinstance(sentinel_drift, (int, float)) and sentinel_drift > SENTINEL_LIMIT_UA:
                self._engage_chip_safety_hold(
                    "persistent/cross-cell sentinel risk detected during batched WB programming",
                    attempt_id=spec.attempt_id(self.run_id),
                    evidence={"target_cell": spec.cell_id, "sentinel_max_abs_drift_uA": sentinel_drift},
                )
            normalized.append(
                self._persist_program_attempt(
                    spec,
                    result,
                    started_utc=started_utc,
                    duration=per_attempt_duration,
                    vivado_log=vivado_log,
                    console_log=console_log,
                    clear_inflight=False,
                )
            )
        self._checkpoint["inflight_attempt"] = None
        self._checkpoint.pop("inflight_batch", None)
        self._save_checkpoint()
        self.store.append_event(
            "hardware_batch_completed",
            batch_id=batch_id,
            cell_id=first.cell_id,
            planned_attempts=len(specs),
            completed_attempts=len(normalized),
            stop_marker=stop_marker,
            duration_seconds=duration,
        )
        return normalized

    def _run_balanced_program_sequence(
        self, specs: list[AttemptSpec]
    ) -> Any:
        """Production batch path with a narrow simulation seam for legacy tests."""

        injected_single_attempt = self.__dict__.get("_run_program_attempt")
        if injected_single_attempt is not None:
            return (injected_single_attempt(spec) for spec in specs)
        return self._run_program_batch(specs)

    def _run_program_attempt(self, spec: AttemptSpec) -> dict[str, Any]:
        _assert_program_target(spec.row, spec.column)
        attempt_id = spec.attempt_id(self.run_id)
        completed = self._checkpoint["completed_attempts"].get(attempt_id)
        if completed:
            return completed
        self._verify_plan_fingerprint()
        key = f"r{spec.row:02d}_c{spec.column:02d}"
        raw_dir = self.store.run_dir / "cells" / key / "raw" / "vivado"
        accumulator = AttemptAccumulator(spec.row, spec.column, spec.operation, expected_post_reads=spec.post_reads)
        self.store.append_event("attempt_planned", attempt_id=attempt_id, **asdict(spec))
        self._checkpoint["inflight_attempt"] = attempt_id
        self._save_checkpoint()
        started_utc = utc_now()
        self.store.append_event("hardware_job_started", attempt_id=attempt_id, job_id=attempt_id, **asdict(spec))
        try:
            code, vivado_log, console_log, duration = self.vivado.run(
                attempt_id,
                self._arguments_for_program(spec, attempt_id),
                raw_dir,
                accumulator.feed,
            )
            result = accumulator.finalize(code)
            result_summary = result.get("summary", {})
            sentinel_drift = result_summary.get("sentinel_max_abs_drift_uA_computed")
            if isinstance(sentinel_drift, (int, float)) and sentinel_drift > SENTINEL_LIMIT_UA:
                self._engage_chip_safety_hold(
                    "persistent/cross-cell sentinel risk detected during WB programming",
                    attempt_id=attempt_id,
                    evidence={
                        "target_cell": spec.cell_id,
                        "operation": spec.operation,
                        "pwm_code": f"0x{(spec.pwm_code or 0):02X}",
                        "program_voltage_v": spec.voltage_v,
                        "sentinel_max_abs_drift_uA": sentinel_drift,
                        "limit_uA": SENTINEL_LIMIT_UA,
                    },
                )
            if not result.get("job_complete") or not result.get("profile_restored"):
                self._append_row(
                    "errors",
                    {
                        "event_seq": "", "run_id": self.run_id, "cell_id": spec.cell_id,
                        "attempt_id": attempt_id, "error_time_utc": utc_now(),
                        "error_class": "cleanup_or_completion_unconfirmed", "error_code": "AMBIGUOUS_ATTEMPT",
                        "message": "; ".join(result.get("validation_errors", [])) or "job completion/profile restoration missing",
                        "hardware_action_started": 1, "wb_ack_seen": int(bool(result.get("ack"))),
                        "tdc_seen": int(bool(result.get("tdc_words"))),
                        "scan_profile_restored": int(bool(result.get("profile_restored"))),
                        "retry_allowed": 0,
                        "resolution": "global pause; read-only recovery and operator review required",
                        "source_log_id": str(vivado_log.relative_to(self.store.run_dir)),
                    },
                )
                raise RuntimeError(
                    f"attempt {attempt_id} has uncertain completion or rail restoration; automatic replay is prohibited"
                )
        except Exception as exc:
            self._append_row(
                "errors",
                {
                    "event_seq": "", "run_id": self.run_id, "cell_id": spec.cell_id,
                    "attempt_id": attempt_id, "error_time_utc": utc_now(), "error_class": "hardware_job_exception",
                    "error_code": type(exc).__name__, "message": str(exc), "hardware_action_started": 1,
                    "wb_ack_seen": "", "tdc_seen": "", "scan_profile_restored": "", "retry_allowed": 0,
                    "resolution": "read-only recovery required; automatic replay prohibited", "source_log_id": "",
                },
            )
            raise

        return self._persist_program_attempt(
            spec,
            result,
            started_utc=started_utc,
            duration=duration,
            vivado_log=vivado_log,
            console_log=console_log,
        )

    def _persist_program_attempt(
        self,
        spec: AttemptSpec,
        result: dict[str, Any],
        *,
        started_utc: str,
        duration: float,
        vivado_log: Path,
        console_log: Path,
        clear_inflight: bool = True,
    ) -> dict[str, Any]:
        """Normalize and durably commit one fully decoded programming attempt."""

        attempt_id = spec.attempt_id(self.run_id)
        key = f"r{spec.row:02d}_c{spec.column:02d}"
        summary = result.get("summary", {})
        gates = result.get("gates", {})
        controller_safety_stop = bool(result.get("safety_stop"))
        advance_allowed = self._analog_only_advance_allowed(spec, result)
        if advance_allowed:
            result["result_class"] = "analog_unconfirmed_advance_allowed"
            result["evidence_qc_flag"] = "analog_unconfirmed;advance_allowed"
        result.update(
            attempt_id=attempt_id,
            idempotency_key=spec.idempotency_key(self.run_id),
            started_utc=started_utc,
            ended_utc=utc_now(),
            duration_seconds=duration,
            source_log_id=str(vivado_log.relative_to(self.store.run_dir)),
            console_log_id=str(console_log.relative_to(self.store.run_dir)),
            result_class=result.get("result_class") or result.get("status", "invalid"),
            advance_allowed=advance_allowed,
            wb_ack=bool(gates.get("ack")),
            tdc_pass=bool(
                gates.get("tdc_15_unique_words")
                and gates.get("tdc_exact_nonzero_indices_11_14")
                and gates.get("tdc_raw_signatures")
                and gates.get("tdc_gate_marker")
            ),
            analog_confirmed=bool(gates.get("analog_confirmed")),
            clip=not bool(gates.get("no_clip")),
            sentinel_pass=bool(gates.get("sentinel_within_10_uA")),
            safety_stop=bool(
                controller_safety_stop
                or result.get("pre_pulse_skipped")
                or (
                    (result.get("shunt_evidence") or {})
                    .get("gate", {})
                    .get("fields", {})
                    .get("safety_stop")
                    == 1
                )
            ),
            pre_median_uS=summary.get("pre_median_uS"),
            post_median_uS=summary.get("post_median_uS"),
            post_min_uS=(summary.get("post_range_uS") or [None, None])[0],
            post_max_uS=(summary.get("post_range_uS") or [None, None])[1],
        )
        self.store.register_raw_file(
            vivado_log,
            file_type="vivado_log",
            cell_id=spec.cell_id,
            attempt_id=attempt_id,
            raw_file_id=f"{attempt_id}_vivado",
            closed_cleanly=bool(result.get("job_complete") and result.get("profile_restored")),
        )
        self.store.register_raw_file(
            console_log,
            file_type="timestamped_console_jsonl",
            cell_id=spec.cell_id,
            attempt_id=attempt_id,
            raw_file_id=f"{attempt_id}_console",
            closed_cleanly=bool(result.get("job_complete") and result.get("profile_restored")),
        )
        normalized = self._commit_program_result(spec, result)
        if clear_inflight:
            self._checkpoint["inflight_attempt"] = None
        self._checkpoint["completed_attempts"][attempt_id] = normalized
        durations = self._checkpoint.setdefault("durations_seconds", [])
        durations.append(duration)
        del durations[:-512]
        cell = self._checkpoint["cells"][key]
        cell["last_attempt_id"] = attempt_id
        cell["pulse_count"] = int(cell.get("pulse_count", 0)) + int(not normalized.get("pre_pulse_skipped"))
        if isinstance(normalized.get("assigned_state_code"), int):
            cell["states_observed"] = sorted(set(cell.get("states_observed", [])) | {normalized["assigned_state_code"]})
        self._save_checkpoint()
        self.store.append_event("attempt_completed", attempt_id=attempt_id, **{
            key: normalized.get(key) for key in (
                "accepted", "advance_allowed", "result_class", "post_median_uS", "post_min_uS", "post_max_uS",
                "assigned_state_code", "landed", "pre_pulse_skipped",
            )
        })
        mean_seconds = statistics.fmean(durations) if durations else 0.0
        self.store.update_progress(
            current_row=spec.row,
            current_column=spec.column,
            current_cell_id=f"r{spec.row:02d}_c{spec.column:02d}",
            current_stage=spec.stage,
            phase=spec.stage,
            current_attempt_id=attempt_id,
            attempts_completed=len(self._checkpoint["completed_attempts"]),
            mean_attempt_seconds=mean_seconds,
            message=f"r{spec.row}/c{spec.column} {spec.operation} 0x{(spec.pwm_code or 0):02X}: {normalized.get('result_class')}",
        )
        return normalized

    def import_inflight_program_evidence(self, spec: AttemptSpec) -> dict[str, Any]:
        """Persist a rejected attempt whose measurements survived a terminal-marker abort.

        This is deliberately an evidence-only recovery path. It never clears the
        in-flight checkpoint, never marks the attempt completed, and never invokes
        Vivado. The narrow checks prevent a partial log from looking like a valid
        programming result.
        """

        _assert_program_target(spec.row, spec.column)
        attempt_id = spec.attempt_id(self.run_id)
        current_inflight = self._checkpoint.get("inflight_attempt")
        inflight_batch = self._checkpoint.get("inflight_batch")
        batch_member = bool(
            isinstance(inflight_batch, dict)
            and inflight_batch.get("batch_id") == current_inflight
            and attempt_id in inflight_batch.get("attempt_ids", [])
        )
        if current_inflight != attempt_id and not batch_member:
            raise RuntimeError(
                f"checkpoint in-flight operation is {current_inflight!r}, and does not "
                f"contain attempt {attempt_id!r}"
            )
        if attempt_id in self._checkpoint.get("completed_attempts", {}):
            raise RuntimeError(
                f"refusing evidence-only import because {attempt_id} is already completed"
            )

        expected_result_class = "rejected_ambiguous_sentinel_drift_completion_missing"
        existing = next(
            (
                row
                for row in self.store.read_table("program_attempts")
                if row.get("attempt_id") == attempt_id
            ),
            None,
        )
        if existing is not None and existing.get("result_class") != expected_result_class:
            raise RuntimeError(
                f"existing program row for {attempt_id} has conflicting result class "
                f"{existing.get('result_class')!r}"
            )
        newly_imported = existing is None

        key = f"r{spec.row:02d}_c{spec.column:02d}"
        raw_dir = self.store.run_dir / "cells" / key / "raw" / "vivado"
        raw_job_id = str(current_inflight) if batch_member else attempt_id
        vivado_log = raw_dir / f"{raw_job_id}.vivado.log"
        console_log = raw_dir / f"{raw_job_id}.console.jsonl"
        if not vivado_log.is_file() or not console_log.is_file():
            raise RuntimeError(f"raw logs for in-flight attempt {attempt_id} are missing")

        accumulator = AttemptAccumulator(
            spec.row,
            spec.column,
            spec.operation,
            expected_post_reads=spec.post_reads,
        )
        started_utc: str | None = None
        ended_utc: str | None = None
        with console_log.open("r", encoding="utf-8") as handle:
            for fallback_line_number, raw_line in enumerate(handle, 1):
                record = json.loads(raw_line)
                timestamp = str(record.get("time_utc") or "")
                text = str(record.get("text") or "")
                line_number = int(record.get("line_number") or fallback_line_number)
                if timestamp:
                    started_utc = started_utc or timestamp
                    ended_utc = timestamp
                accumulator.feed(text, timestamp, line_number)

        # Vivado returned nonzero for this terminal-marker abort. The exact nonzero
        # value does not affect decoded measurements or the evidence gates below.
        result = accumulator.finalize(returncode=1)
        summary = result.get("summary", {})
        gates = result.get("gates", {})
        evidence_errors: list[str] = []
        if not result.get("profile_restored"):
            evidence_errors.append("scan profile restoration was not observed")
        if not result.get("wb_ack"):
            evidence_errors.append("valid WB ACK was not observed")
        if not result.get("tdc_pass"):
            evidence_errors.append("complete WB TDC return was not observed")
        if not result.get("analog_confirmed"):
            evidence_errors.append("R11/R10 shunt confirmation was not observed")
        if result.get("clip"):
            evidence_errors.append("a scan or shunt measurement clipped")
        if result.get("sentinel_pass"):
            evidence_errors.append("attempt was not rejected by the sentinel gate")
        sentinel_reported = summary.get("sentinel_max_abs_drift_uA_reported")
        sentinel_computed = summary.get("sentinel_max_abs_drift_uA_computed")
        if not gates.get("pwm_metric"):
            evidence_errors.append("PWM metric marker was not observed")
        if len(result.get("sentinel_reads", [])) != 8:
            evidence_errors.append(
                f"expected 8 sentinel frames, got {len(result.get('sentinel_reads', []))}"
            )
        if len(result.get("sentinel_drift", [])) != 4:
            evidence_errors.append(
                f"expected 4 sentinel drift pairs, got {len(result.get('sentinel_drift', []))}"
            )
        if not isinstance(sentinel_reported, (int, float)) or sentinel_reported <= SENTINEL_LIMIT_UA:
            evidence_errors.append("reported sentinel drift did not exceed the safety limit")
        if not isinstance(sentinel_computed, (int, float)) or sentinel_computed <= SENTINEL_LIMIT_UA:
            evidence_errors.append("computed sentinel drift did not exceed the safety limit")
        if (
            isinstance(sentinel_reported, (int, float))
            and isinstance(sentinel_computed, (int, float))
            and abs(sentinel_reported - sentinel_computed) > 1e-6
        ):
            evidence_errors.append("reported and computed sentinel drift disagree")
        if summary.get("pre_read_count") != PRE_READS:
            evidence_errors.append(
                f"expected {PRE_READS} target pre-reads, got {summary.get('pre_read_count')}"
            )
        if summary.get("post_read_count") != spec.post_reads:
            evidence_errors.append(
                f"expected {spec.post_reads} target post-reads, got {summary.get('post_read_count')}"
            )
        if result.get("job_complete") or result.get("completion"):
            evidence_errors.append("attempt has terminal completion markers; use the normal commit path")
        if evidence_errors:
            raise RuntimeError(
                f"refusing to import incomplete evidence for {attempt_id}: "
                + "; ".join(evidence_errors)
            )

        result.update(
            attempt_id=attempt_id,
            idempotency_key=spec.idempotency_key(self.run_id),
            started_utc=started_utc or utc_now(),
            ended_utc=ended_utc or utc_now(),
            source_log_id=str(vivado_log.relative_to(self.store.run_dir)),
            console_log_id=str(console_log.relative_to(self.store.run_dir)),
            accepted=False,
            result_class=expected_result_class,
            evidence_qc_flag="attempt_rejected;sentinel_drift_gate_failed;terminal_marker_missing",
            observation_phase_override="post_program_rejected_ambiguous",
            evidence_replay=True,
            wb_ack=bool(gates.get("ack")),
            tdc_pass=bool(
                gates.get("tdc_15_unique_words")
                and gates.get("tdc_exact_nonzero_indices_11_14")
                and gates.get("tdc_raw_signatures")
                and gates.get("tdc_gate_marker")
            ),
            analog_confirmed=bool(gates.get("analog_confirmed")),
            clip=not bool(gates.get("no_clip")),
            sentinel_pass=False,
            safety_stop=True,
        )

        def register_raw_once(path: Path, file_type: str, raw_file_id: str) -> None:
            registered = next(
                (
                    row
                    for row in self.store.read_table("raw_files")
                    if row.get("raw_file_id") == raw_file_id
                ),
                None,
            )
            if registered is not None:
                actual_digest = _sha256_file(path)
                actual_relative = path.relative_to(self.store.run_dir).as_posix()
                if (
                    registered.get("attempt_id") != attempt_id
                    or registered.get("relative_path") != actual_relative
                    or registered.get("sha256") != actual_digest
                    or str(registered.get("closed_cleanly", "")).strip().lower()
                    not in {"0", "false"}
                ):
                    raise RuntimeError(f"conflicting raw-file registration for {raw_file_id}")
                return
            self.store.register_raw_file(
                path,
                file_type=file_type,
                cell_id=spec.cell_id,
                attempt_id=attempt_id,
                raw_file_id=raw_file_id,
                closed_cleanly=False,
            )

        register_raw_once(vivado_log, "vivado_log", f"{attempt_id}_vivado")
        register_raw_once(console_log, "timestamped_console_jsonl", f"{attempt_id}_console")
        normalized = self._commit_program_result(spec, result)
        if newly_imported:
            self.store.append_event(
                "ambiguous_attempt_evidence_committed",
                cell_id=spec.cell_id,
                attempt_id=attempt_id,
                checkpoint_retained=True,
                replay_permitted=False,
                result_class=result["result_class"],
                post_read_count=summary.get("post_read_count"),
                post_median_uS=summary.get("post_median_uS"),
                sentinel_max_abs_drift_uA=summary.get("sentinel_max_abs_drift_uA_computed"),
            )
            self.store.update_progress(
                status="blocked",
                run_status="blocked",
                phase="blocked",
                message=(
                    f"Rejected evidence for {attempt_id} was recorded; checkpoint remains "
                    "blocked and automatic replay is prohibited"
                ),
            )
        return {
            **normalized,
            "attempt_id": attempt_id,
            "already_imported": not newly_imported,
            "checkpoint_retained": True,
            "sentinel_max_abs_drift_uA": sentinel_computed,
        }

    def _commit_program_result(self, spec: AttemptSpec, result: dict[str, Any]) -> dict[str, Any]:
        attempt_id = result["attempt_id"]
        ack_time = (result.get("ack") or {}).get("timestamp_utc")
        existing_read_event_ids: dict[str, str] = {}
        existing_program_event_id = ""
        if result.get("evidence_replay"):
            existing_read_event_ids = {
                str(row.get("read_id")): str(row.get("event_id") or "")
                for row in self.store.read_table("scan_reads")
                if row.get("attempt_id") == attempt_id and row.get("read_id")
            }
            existing_program = next(
                (
                    row
                    for row in self.store.read_table("program_attempts")
                    if row.get("attempt_id") == attempt_id
                ),
                None,
            )
            if existing_program is not None:
                existing_program_event_id = str(existing_program.get("event_id") or "")
        reads: list[dict[str, Any]] = []
        for marker in result.get("scan_reads", []):
            label = str(marker.get("label", ""))
            role = "pre_program" if label.startswith("phase_p_pre_") else "post_program" if label.startswith("phase_p_post_") else "recovery"
            channel = marker.get("channels", [{}, {}, {}])[2]
            read_index = marker.get("read_index") or 0
            read_id = hashlib.sha256(f"{attempt_id}|{label}|{read_index}".encode()).hexdigest()[:24]
            elapsed = _seconds_between(ack_time, marker.get("timestamp_utc"))
            frame_flags = "" if marker.get("frame_valid") else ";".join(marker.get("frame_errors", []))
            evidence_flag = str(result.get("evidence_qc_flag") or "")
            qc_flags = ";".join(item for item in (frame_flags, evidence_flag) if item)
            reads.append(
                {
                    "event_seq": "",
                    "event_id": existing_read_event_ids.get(read_id)
                    or hashlib.sha256(
                        f"{self.run_id}|evidence_scan_read|{read_id}".encode()
                    ).hexdigest()[:24],
                    "read_id": read_id,
                    "read_role": role,
                    "read_index": read_index,
                    "elapsed_after_write_ms": elapsed * 1000 if elapsed is not None and role == "post_program" else "",
                    "raw_h0_hex": marker.get("raw_h0"),
                    "raw_h1_hex": marker.get("raw_h1"),
                    "baseline_count": marker.get("baseline_count"),
                    "baseline_sum": channel.get("baseline_sum"),
                    "baseline_mean_code": channel.get("baseline_mean_code"),
                    "active_count": marker.get("active_count"),
                    "active_sum": channel.get("active_sum"),
                    "active_mean_code": channel.get("active_mean_code"),
                    "current_uA": marker.get("current_uA"),
                    "conductance_uS": marker.get("conductance_uS"),
                    "timestamp_utc": marker.get("timestamp_utc"),
                    "qc_flags": qc_flags,
                    "row": marker.get("row"),
                    "column": marker.get("col"),
                }
            )
        post = [r for r in reads if r.get("read_role") == "post_program" and (r.get("row"), r.get("column")) == (spec.row, spec.column)]
        pre = [r for r in reads if r.get("read_role") == "pre_program" and (r.get("row"), r.get("column")) == (spec.row, spec.column)]
        post_values = [float(r["conductance_uS"]) for r in post if r.get("conductance_uS") is not None]
        pre_values = [float(r["conductance_uS"]) for r in pre if r.get("conductance_uS") is not None]
        post_median = statistics.median(post_values) if post_values else result.get("post_median_uS")
        pre_median = statistics.median(pre_values) if pre_values else result.get("pre_median_uS")
        assigned, landed, low, high = nearest_state(post_median)
        frozen_target = self._balanced_targets.get((spec.row, spec.column))
        if (
            frozen_target is not None
            and spec.requested_state_code == frozen_target
            and isinstance(post_median, (int, float))
            and TARGET_BANDS_US[frozen_target][0]
            <= post_median
            <= TARGET_BANDS_US[frozen_target][1]
        ):
            assigned = frozen_target
            landed = True
            low, high = TARGET_BANDS_US[frozen_target]
        accepted = bool(result.get("accepted")) and not bool(result.get("pre_pulse_skipped"))
        result_class = result.get("result_class", "accepted" if accepted else "gate_rejected")

        for read in reads:
            row = dict(read)
            row.update(
                run_id=self.run_id,
                cell_id=spec.cell_id,
                attempt_id=attempt_id,
                state_code_context=spec.requested_state_code,
                read_voltage_v=READ_VOLTAGE_V,
                scan_clock_mhz=2,
                dac_profile_id="scan_read_0p5V",
                decode_revision="scan-frame-v2-160",
            )
            self._append_row("scan_reads", row)
        for word in result.get("tdc_words", []):
            self._append_row(
                "tdc_words",
                {
                    "run_id": self.run_id, "cell_id": spec.cell_id, "attempt_id": attempt_id,
                    "word_index": word.get("index"), "word_hex": word.get("tdc_word_hex"),
                    "word_uint32": word.get("tdc_word"), "is_nonzero": int(bool(word.get("nonzero"))),
                    "raw_h0_hex": word.get("raw_h0"), "raw_h1_hex": word.get("raw_h1"),
                    "timestamp_utc": word.get("timestamp_utc"),
                },
            )
        ack = result.get("ack") or {}
        first_word = (result.get("tdc_words") or [{}])[0]
        if ack:
            for issue_index in (1, 2):
                self._append_row(
                    "wb_packet_issues",
                    {
                        "run_id": self.run_id, "cell_id": spec.cell_id, "attempt_id": attempt_id,
                        "issue_index": issue_index, "command_value_hex": first_word.get("packet_hex", ""),
                        "firmware_transaction": 1, "firmware_packet_issues": 2,
                        "ack_seen": int(bool(result.get("wb_ack"))),
                        "raw_command_hex": first_word.get("packet_hex", ""),
                        "raw_response_hex": ack.get("raw_line", ""),
                        "timestamp_utc": ack.get("timestamp_utc"),
                    },
                )
        shunt_bundle = result.get("shunt_evidence") or {}
        observation = shunt_bundle.get("observation") or {}
        shunt_gate = shunt_bundle.get("gate") or {}
        fields = observation.get("fields", {})
        gate_fields = shunt_gate.get("fields", {})
        if observation:
            self._append_row(
                "shunt_metrics",
                {
                    "run_id": self.run_id, "cell_id": spec.cell_id, "attempt_id": attempt_id,
                    "path": fields.get("path"), "adc_channel": fields.get("channel"),
                    "shunt_resistor": "R10" if spec.operation == "set" else "R11",
                    "baseline_sample_count": fields.get("baseline_count"),
                    "baseline_mean_code": fields.get("baseline_mean_code"),
                    "baseline_min_code": fields.get("baseline_min"), "baseline_max_code": fields.get("baseline_max"),
                    "active_sample_count": fields.get("active_samples_at_least"),
                    "active_min_code": fields.get("active_min"), "active_max_code": fields.get("active_max"),
                    "threshold_code": fields.get("threshold_code"), "positive_max_run": fields.get("positive_max_run"),
                    "negative_max_run": fields.get("negative_max_run"), "run_required": fields.get("run_required"),
                    "sustained_positive": fields.get("sustained_positive"), "sustained_negative": fields.get("sustained_negative"),
                    "peak_positive_delta_code": fields.get("peak_positive_delta_code"),
                    "peak_negative_delta_code": fields.get("peak_negative_delta_code"),
                    "peak_excursion_nominal_uA": fields.get("peak_excursion_nominal_uA_56ohm_gain7"),
                    "baseline_clip": fields.get("baseline_clip"), "active_clip": fields.get("active_clip"),
                    "analog_confirmed": gate_fields.get("analog_confirmed"), "trace_file_id": "",
                },
            )
        drift_by_cell = {(item["row"], item["col"]): item for item in result.get("sentinel_drift", [])}
        for sentinel in result.get("sentinel_reads", []):
            key = (sentinel.get("row"), sentinel.get("col"))
            drift = drift_by_cell.get(key, {})
            phase = "pre" if "sentinel_pre" in str(sentinel.get("label")) else "post"
            self._append_row(
                "sentinel_reads",
                {
                    "run_id": self.run_id, "target_cell_id": spec.cell_id, "attempt_id": attempt_id,
                    "sentinel_cell_id": f"chip{CHIP_ID:02d}_r{key[0]:02d}_c{key[1]:02d}",
                    "read_phase": phase, "read_index": 1, "raw_h0_hex": sentinel.get("raw_h0"),
                    "raw_h1_hex": sentinel.get("raw_h1"), "baseline_count": sentinel.get("baseline_count"),
                    "active_count": sentinel.get("active_count"), "current_uA": sentinel.get("current_uA"),
                    "conductance_uS": sentinel.get("conductance_uS"),
                    "baseline_current_uA": drift.get("pre_current_uA"),
                    "absolute_drift_uA": drift.get("abs_drift_uA"),
                    "pass": int((drift.get("abs_drift_uA") or 0) <= SENTINEL_LIMIT_UA),
                    "timestamp_utc": sentinel.get("timestamp_utc"),
                },
            )

        pwm_hex = f"0x{spec.pwm_code:02X}" if spec.pwm_code is not None else ""
        program_row = {
            "event_seq": "",
            "event_id": existing_program_event_id
            or hashlib.sha256(
                f"{self.run_id}|evidence_program_attempt|{attempt_id}".encode()
            ).hexdigest()[:24],
            "run_id": self.run_id, "cell_id": spec.cell_id, "attempt_id": attempt_id,
            "idempotency_key": result["idempotency_key"], "cell_attempt_index": spec.stage_index,
            "stage": spec.stage, "requested_state_code": spec.requested_state_code, "operation": spec.operation,
            "program_path": spec.program_path, "pwm_code": pwm_hex, "pwm_decimal": spec.pwm_code or "",
            "program_voltage_v": spec.voltage_v, "read_voltage_v": READ_VOLTAGE_V,
            "dac0_v": 0, "dac1_v": 0,
            "dac2_v": spec.voltage_v if spec.operation == "set" else 0,
            "dac3_v": spec.voltage_v if spec.operation == "set" else 0,
            "dac4_v": spec.voltage_v if spec.operation == "reset" else 0,
            "dac5_v": spec.voltage_v if spec.operation == "reset" else 0,
            "dac6_v": 0, "dac_mask": "0x01F3" if spec.operation == "set" else "0x01CF",
            "scan_clock_mhz": 2, "program_clock_mhz": 10,
            "pre_read_count": len(pre_values), "post_read_count": len(post_values),
            "pre_median_uA": pre_median / 2 if pre_median is not None else "", "pre_median_uS": pre_median,
            "post_median_uA": post_median / 2 if post_median is not None else "", "post_median_uS": post_median,
            "post_min_uS": min(post_values) if post_values else "", "post_max_uS": max(post_values) if post_values else "",
            "delta_uS": post_median - pre_median if post_median is not None and pre_median is not None else "",
            "assigned_state_code": assigned if assigned is not None else "", "assigned_band_low_uS": low or "",
            "assigned_band_high_uS": high or "", "wb_ack": int(bool(result.get("wb_ack"))),
            "tdc_pass": int(bool(result.get("tdc_pass"))), "analog_confirmed": int(bool(result.get("analog_confirmed"))),
            "clip": int(bool(result.get("clip"))), "sentinel_pass": int(bool(result.get("sentinel_pass"))),
            "safety_stop": int(bool(result.get("safety_stop"))), "result_class": result_class,
            "start_time_utc": result["started_utc"], "end_time_utc": result["ended_utc"],
            "source_log_id": result["source_log_id"],
        }
        self._append_row("program_attempts", program_row)
        if post_values:
            self._append_row(
                "state_observations",
                {
                    "run_id": self.run_id, "cell_id": spec.cell_id,
                    "observation_id": hashlib.sha256(f"{attempt_id}|post".encode()).hexdigest()[:24],
                    "attempt_id": attempt_id,
                    "observation_phase": result.get("observation_phase_override", "post_program"),
                    "target_state_code": spec.requested_state_code, "assigned_state_code": assigned,
                    "target_low_uS": TARGET_BANDS_US[spec.requested_state_code][0],
                    "target_high_uS": TARGET_BANDS_US[spec.requested_state_code][1],
                    "sample_count": len(post_values), "median_uS": post_median,
                    "mean_uS": statistics.fmean(post_values),
                    "stddev_uS": statistics.stdev(post_values) if len(post_values) > 1 else 0.0,
                    "min_uS": min(post_values), "max_uS": max(post_values),
                    "distance_to_target_center_uS": abs(post_median - sum(TARGET_BANDS_US[spec.requested_state_code]) / 2),
                    "landed": int(landed), "classification_revision": "4uS-bands-v1",
                    "source_read_ids": " ".join(str(r.get("read_id", "")) for r in post),
                },
            )
        return {
            "attempt_id": attempt_id,
            "accepted": accepted,
            "result_class": result_class,
            "advance_allowed": bool(result.get("advance_allowed")),
            "wb_ack": bool(result.get("wb_ack")),
            "tdc_pass": bool(result.get("tdc_pass")),
            "analog_confirmed": bool(result.get("analog_confirmed")),
            "clip": bool(result.get("clip")),
            "sentinel_pass": bool(result.get("sentinel_pass")),
            "safety_stop": bool(result.get("safety_stop")),
            "job_complete": bool(result.get("job_complete")),
            "profile_restored": bool(result.get("profile_restored")),
            "validation_errors": list(result.get("validation_errors", [])),
            "pre_read_count": len(pre_values),
            "post_read_count": len(post_values),
            "pre_median_uS": pre_median,
            "post_median_uS": post_median,
            "post_min_uS": min(post_values) if post_values else None,
            "post_max_uS": max(post_values) if post_values else None,
            "assigned_state_code": assigned,
            "landed": landed,
            "pre_pulse_skipped": bool(result.get("pre_pulse_skipped")),
        }

    def _run_read_only(
        self,
        row: int,
        column: int,
        role: str,
        count: int,
        *,
        parent_attempt_id: str | None = None,
        job_id: str | None = None,
        state_code_context: int | str = "",
    ) -> dict[str, Any]:
        self._verify_plan_fingerprint()
        if job_id is None:
            sequence = int(self._checkpoint.get("read_job_sequence", 0)) + 1
            self._checkpoint["read_job_sequence"] = sequence
            self._save_checkpoint()
            seed = f"{self.run_id}|r{row}|c{column}|{role}|read|{sequence}"
            job_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
        record_attempt_id = parent_attempt_id or job_id
        key = f"r{row:02d}_c{column:02d}"
        raw_dir = self.store.run_dir / "cells" / key / "raw" / "vivado"
        reads: list[dict[str, Any]] = []
        errors: list[str] = []
        job_complete = False

        def feed(line: str, timestamp: str, line_number: int) -> None:
            nonlocal job_complete
            if line.startswith("CELL_RESULT "):
                try:
                    reads.append(decode_cell_result(line, timestamp, line_number))
                except ValueError as exc:
                    errors.append(str(exc))
            if line.startswith("JOB_COMPLETE read_repeat"):
                job_complete = True

        code, vivado_log, console_log, _ = self.vivado.run(
            job_id,
            ["read_repeat", str(row), str(column), "2.3", "1.75", "3.0", "3.0", str(READ_HOLD_CLOCKS), str(count), job_id],
            raw_dir,
            feed,
        )
        frames_valid = all(
            (read.get("row"), read.get("column")) == (row, column)
            and not read.get("read_clipped")
            and not read.get("qc_flags")
            for read in reads
        )
        valid = code == 0 and job_complete and len(reads) == count and not errors and frames_valid
        self.store.register_raw_file(vivado_log, file_type="vivado_log", cell_id=f"r{row:02d}_c{column:02d}", attempt_id=record_attempt_id, raw_file_id=f"{job_id}_vivado", closed_cleanly=valid)
        self.store.register_raw_file(console_log, file_type="timestamped_console_jsonl", cell_id=f"r{row:02d}_c{column:02d}", attempt_id=record_attempt_id, raw_file_id=f"{job_id}_console", closed_cleanly=valid)
        for index, read in enumerate(reads, 1):
            read_id = hashlib.sha256(f"{job_id}|{index}".encode()).hexdigest()[:24]
            read["read_id"] = read_id
            self._append_row(
                "scan_reads",
                {
                    "event_seq": "", "run_id": self.run_id,
                    "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}", "attempt_id": record_attempt_id,
                    "read_id": read_id, "read_role": role, "read_index": index, "state_code_context": state_code_context,
                    "elapsed_after_write_ms": "", "read_voltage_v": READ_VOLTAGE_V,
                    **read, "scan_clock_mhz": 2, "dac_profile_id": "scan_read_0p5V",
                    "decode_revision": "scan-frame-v2-160", "qc_flags": "" if valid else "incomplete_read_job",
                },
            )
        self.store.append_event("read_block_completed", row=row, column=column, role=role, count=len(reads), valid=valid, attempt_id=record_attempt_id, job_id=job_id)
        return {
            "valid": valid,
            "reads": reads,
            "error": "; ".join(errors) or ("" if valid else f"read job exit={code}, count={len(reads)}, frames_valid={frames_valid}"),
            "source_log": str(vivado_log),
            "console_log": str(console_log),
            "job_id": job_id,
            "attempt_id": record_attempt_id,
        }

    def _run_scan_set_forming(
        self,
        row: int,
        column: int,
        *,
        target_state_code: int,
        eligibility_reads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Issue exactly one guarded scan-debug SET forming hardware job.

        The caller supplies the five fresh initial reads that authorize entry
        to this path.  The controller independently repeats the same strict
        five-read guard immediately before the possible pulse.  A caller may
        invoke another job only at a later safe boundary and while the durable
        per-cell budget remains.  Once hardware is entered, incomplete or
        internally inconsistent evidence leaves the attempt in flight so it
        can never be replayed automatically.
        """

        _assert_program_target(row, column)
        if not 0 <= target_state_code < len(TARGET_BANDS_US):
            raise ValueError("scan SET forming target_state_code is outside 0..15")

        key = f"r{row:02d}_c{column:02d}"
        cell = self._checkpoint["cells"][key]
        pulse_count = int(cell.get("scan_forming_pulse_count", 0))
        pulse_ordinal = pulse_count + 1
        seed = (
            f"{self.run_id}|r{row}|c{column}|scan_set_form_guarded|"
            f"vcc={SCAN_FORM_VCC_V}|wl={SCAN_FORM_WL_V}|"
            f"hold={SCAN_FORM_HOLD_CLOCKS}|max_pulses={SCAN_FORM_MAX_PULSES_PER_CELL}|"
            f"pulse_ordinal={pulse_ordinal}"
        )
        idempotency_key = hashlib.sha256(seed.encode()).hexdigest()
        attempt_id = idempotency_key[:32]
        completed = self._checkpoint["completed_attempts"].get(attempt_id)
        if completed:
            return completed

        active = self._checkpoint.get("inflight_attempt")
        if active:
            raise RuntimeError(
                f"refusing guarded scan SET while uncertain attempt {active} is in flight"
            )

        # Host-side authorization is intentionally strict.  A malformed read
        # block does not authorize hardware and can be retried as read-only;
        # a clean value at or above 20 uS permanently closes the scan SET path.
        host_values: list[float] = []
        host_clean = len(eligibility_reads) == PRE_READS
        for read in eligibility_reads:
            value = read.get("conductance_uS")
            host_clean = bool(
                host_clean
                and (read.get("row"), read.get("column")) == (row, column)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and not read.get("read_clipped")
                and not read.get("qc_flags")
            )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                host_values.append(float(value))
        if not host_clean or len(host_values) != PRE_READS:
            raise RuntimeError(
                "guarded scan SET requires exactly five fresh, clean target reads"
            )
        host_max = max(host_values)
        if host_max >= SCAN_FORM_THRESHOLD_US:
            cell["scan_set_permanently_disabled"] = True
            cell["scan_set_disable_reason"] = (
                f"fresh target read maximum {host_max:.6f} uS is at or above "
                f"{SCAN_FORM_THRESHOLD_US:.6f} uS"
            )
            self._save_checkpoint()
            return {
                "valid": True,
                "accepted": False,
                "eligible": False,
                "issued": False,
                "result_class": "scan_set_forming_host_guard_skipped",
                "post_median_uS": statistics.median(host_values),
                "post_min_uS": min(host_values),
                "post_max_uS": host_max,
                "permanently_disabled": True,
                "attempt_id": "",
            }
        if cell.get("scan_set_permanently_disabled"):
            return {
                "valid": True,
                "accepted": False,
                "eligible": False,
                "issued": False,
                "result_class": "scan_set_forming_permanently_disabled",
                "post_median_uS": statistics.median(host_values),
                "post_min_uS": min(host_values),
                "post_max_uS": host_max,
                "permanently_disabled": True,
                "attempt_id": "",
            }
        if pulse_count >= SCAN_FORM_MAX_PULSES_PER_CELL:
            return {
                "valid": True,
                "accepted": False,
                "eligible": False,
                "issued": False,
                "result_class": "scan_set_forming_pulse_budget_exhausted",
                "post_median_uS": statistics.median(host_values),
                "post_min_uS": min(host_values),
                "post_max_uS": host_max,
                "permanently_disabled": bool(cell.get("scan_set_permanently_disabled")),
                "attempt_id": "",
            }

        self._verify_plan_fingerprint()
        raw_dir = self.store.run_dir / "cells" / key / "raw" / "vivado"
        target_pre: dict[int, dict[str, Any]] = {}
        target_post: dict[int, dict[str, Any]] = {}
        pulse_feedback: list[dict[str, Any]] = []
        sentinel_pre_items: list[dict[str, Any]] = []
        sentinel_post_items: list[dict[str, Any]] = []
        errors: list[str] = []
        result_marker: dict[str, Any] | None = None
        sentinel_gate: dict[str, Any] | None = None
        profile_restored = False
        restore_failed = False
        job_complete = False

        def feed(line: str, timestamp: str, line_number: int) -> None:
            nonlocal result_marker, sentinel_gate, profile_restored, restore_failed, job_complete
            if line.startswith("CELL_RESULT "):
                try:
                    decoded = decode_cell_result(line, timestamp, line_number)
                    label = str(decoded.get("label") or "")
                    if label.startswith("scan_set_form_pre_"):
                        try:
                            index = int(label.rsplit("_", 1)[1])
                        except ValueError:
                            errors.append(f"malformed forming pre label {label!r}")
                        else:
                            if index in target_pre:
                                errors.append(f"duplicate forming pre index {index}")
                            target_pre[index] = decoded
                    elif label.startswith("scan_set_form_post_"):
                        try:
                            index = int(label.rsplit("_", 1)[1])
                        except ValueError:
                            errors.append(f"malformed forming post label {label!r}")
                        else:
                            if index in target_post:
                                errors.append(f"duplicate forming post index {index}")
                            target_post[index] = decoded
                    elif label == "scan_set_form_pulse_feedback":
                        pulse_feedback.append(decoded)
                    elif label == "scan_set_form_sentinel_pre":
                        sentinel_pre_items.append(decoded)
                    elif label == "scan_set_form_sentinel_post":
                        sentinel_post_items.append(decoded)
                    else:
                        errors.append(f"unexpected CELL_RESULT label {label!r}")
                except ValueError as exc:
                    errors.append(str(exc))
            elif line.startswith("SCAN_SET_FORM_RESULT "):
                parts = line.split()
                try:
                    if len(parts) != 11 or result_marker is not None:
                        raise ValueError("malformed or duplicate SCAN_SET_FORM_RESULT marker")
                    marker_row = int(parts[1])
                    marker_column = int(parts[2])
                    issued = int(parts[3])
                    clipped = int(parts[4])
                    vcc_v = float(parts[5])
                    wl_v = float(parts[6])
                    hold_clocks = int(parts[7])
                    pre_max_uS = float(parts[8])
                    post_max_uS = float(parts[9])
                    crossed20 = int(parts[10])
                    if (
                        (marker_row, marker_column) != (row, column)
                        or issued not in {0, 1}
                        or clipped not in {0, 1}
                        or crossed20 not in {0, 1}
                        or not all(math.isfinite(value) for value in (vcc_v, wl_v, pre_max_uS, post_max_uS))
                        or abs(vcc_v - SCAN_FORM_VCC_V) > 1.0e-9
                        or abs(wl_v - SCAN_FORM_WL_V) > 1.0e-9
                        or hold_clocks != SCAN_FORM_HOLD_CLOCKS
                    ):
                        raise ValueError("invalid SCAN_SET_FORM_RESULT values")
                    result_marker = {
                        "issued": bool(issued),
                        "clipped": bool(clipped),
                        "vcc_v": vcc_v,
                        "wl_v": wl_v,
                        "hold_clocks": hold_clocks,
                        "pre_max_uS": pre_max_uS,
                        "post_max_uS": post_max_uS,
                        "crossed20": bool(crossed20),
                    }
                except ValueError as exc:
                    errors.append(str(exc))
            elif line.startswith("SCAN_SET_FORM_SENTINEL_GATE "):
                parts = line.split()
                try:
                    if len(parts) != 5 or sentinel_gate is not None:
                        raise ValueError("malformed or duplicate SCAN_SET_FORM_SENTINEL_GATE marker")
                    marker_row = int(parts[1])
                    marker_column = int(parts[2])
                    max_drift_uA = float(parts[3])
                    passed = int(parts[4])
                    if (
                        (marker_row, marker_column) != (row, column)
                        or not math.isfinite(max_drift_uA)
                        or max_drift_uA < 0
                        or passed not in {0, 1}
                    ):
                        raise ValueError("invalid SCAN_SET_FORM_SENTINEL_GATE values")
                    sentinel_gate = {
                        "max_abs_drift_uA": max_drift_uA,
                        "pass": bool(passed),
                    }
                except ValueError as exc:
                    errors.append(str(exc))
            elif line.startswith("SCAN_SET_FORM_PROFILE_RESTORED "):
                parts = line.split()
                try:
                    if len(parts) != 3 or profile_restored:
                        raise ValueError("malformed or duplicate SCAN_SET_FORM_PROFILE_RESTORED marker")
                    if (int(parts[1]), int(parts[2])) != (row, column):
                        raise ValueError("SCAN_SET_FORM_PROFILE_RESTORED target mismatch")
                    profile_restored = True
                except ValueError as exc:
                    errors.append(str(exc))
            elif line.startswith("RESTORE_FAILED"):
                restore_failed = True
            elif line.startswith("JOB_COMPLETE scan_set_form_guarded"):
                if job_complete:
                    errors.append("duplicate JOB_COMPLETE scan_set_form_guarded marker")
                job_complete = True

        def block_ambiguous(message: str, source_log: str = "") -> None:
            self._append_row(
                "errors",
                {
                    "event_seq": "",
                    "run_id": self.run_id,
                    "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                    "attempt_id": attempt_id,
                    "error_time_utc": utc_now(),
                    "error_class": "ambiguous_scan_set_forming",
                    "error_code": "AMBIGUOUS_SCAN_SET_FORMING",
                    "message": message,
                    "hardware_action_started": 1,
                    "wb_ack_seen": "",
                    "tdc_seen": "",
                    "scan_profile_restored": int(profile_restored and not restore_failed),
                    "retry_allowed": 0,
                    "resolution": "global block; read-only recovery and operator review required",
                    "source_log_id": source_log,
                },
            )
            self.store.append_event(
                "scan_set_forming_ambiguous",
                row=row,
                column=column,
                attempt_id=attempt_id,
                message=message,
            )
            raise RuntimeError(
                f"guarded scan SET forming {attempt_id} is ambiguous; "
                f"automatic replay is prohibited: {message}"
            )

        self._checkpoint["inflight_attempt"] = attempt_id
        cell["scan_forming_attempt_id"] = attempt_id
        cell["scan_forming_stage"] = "hardware_started"
        self._save_checkpoint()
        started_utc = utc_now()
        self.store.append_event(
            "scan_set_forming_started",
            row=row,
            column=column,
            attempt_id=attempt_id,
            vcc_v=SCAN_FORM_VCC_V,
            wl_v=SCAN_FORM_WL_V,
            hold_clocks=SCAN_FORM_HOLD_CLOCKS,
            host_pre_max_uS=host_max,
            pulse_ordinal=pulse_ordinal,
            pulse_budget=SCAN_FORM_MAX_PULSES_PER_CELL,
        )
        try:
            code, vivado_log, console_log, duration = self.vivado.run(
                attempt_id,
                [
                    "scan_set_form_guarded",
                    str(row),
                    str(column),
                    str(SCAN_FORM_VCC_V),
                    str(SCAN_FORM_WL_V),
                    "3.0",
                    "3.0",
                    str(SCAN_FORM_HOLD_CLOCKS),
                    str(SCAN_FORM_PULSES_PER_JOB),
                    attempt_id,
                ],
                raw_dir,
                feed,
            )
        except Exception as exc:
            block_ambiguous(
                f"scan SET forming hardware job raised {type(exc).__name__}: {exc}"
            )
            raise AssertionError("unreachable")

        source_log_id = str(vivado_log.relative_to(self.store.run_dir))
        pre_reads = [target_pre[index] for index in sorted(target_pre)]
        post_reads = [target_post[index] for index in sorted(target_post)]
        sentinel_pre = {
            (item.get("row"), item.get("column")): item
            for item in sentinel_pre_items
        }
        sentinel_post = {
            (item.get("row"), item.get("column")): item
            for item in sentinel_post_items
        }
        expected_sentinel_coordinates = {
            ((row + 8) & 31, column),
            ((row + 16) & 31, column),
            ((row + 24) & 31, column),
            (row, (column + 16) & 31),
        }

        def clean_target(item: dict[str, Any]) -> bool:
            value = item.get("conductance_uS")
            return bool(
                (item.get("row"), item.get("column")) == (row, column)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and not item.get("read_clipped")
                and not item.get("qc_flags")
            )

        pre_clean = set(target_pre) == set(range(1, PRE_READS + 1)) and all(
            clean_target(item) for item in pre_reads
        )
        post_clean = set(target_post) == set(range(1, POST_WRITE_READS + 1)) and all(
            clean_target(item) for item in post_reads
        )
        feedback_clean = all(clean_target(item) for item in pulse_feedback)
        sentinel_frames_clean = all(
            isinstance(item.get("current_uA"), (int, float))
            and not isinstance(item.get("current_uA"), bool)
            and math.isfinite(float(item["current_uA"]))
            and not item.get("read_clipped")
            and not item.get("qc_flags")
            for item in sentinel_pre_items + sentinel_post_items
        )
        sentinel_coordinates_match = bool(
            len(sentinel_pre_items) == 4
            and len(sentinel_post_items) == 4
            and len(sentinel_pre) == 4
            and len(sentinel_post) == 4
            and set(sentinel_pre) == expected_sentinel_coordinates
            and set(sentinel_pre) == set(sentinel_post)
        )
        sentinel_drifts: list[float] = []
        if sentinel_coordinates_match and sentinel_frames_clean:
            sentinel_drifts = [
                abs(
                    float(sentinel_post[coordinates]["current_uA"])
                    - float(sentinel_pre[coordinates]["current_uA"])
                )
                for coordinates in sentinel_pre
            ]
        computed_sentinel_max = max(sentinel_drifts) if len(sentinel_drifts) == 4 else None

        pre_values = [float(item["conductance_uS"]) for item in pre_reads] if pre_clean else []
        post_values = [float(item["conductance_uS"]) for item in post_reads] if post_clean else []
        computed_pre_max = max(pre_values) if pre_values else None
        computed_post_max = max(post_values) if post_values else None
        issued = bool(result_marker and result_marker["issued"])
        feedback_raw_clipped = False
        if len(pulse_feedback) == 1:
            try:
                feedback_raw_clipped = bool(
                    (int(str(pulse_feedback[0]["raw_h0_hex"]), 16) >> 44) & 1
                )
            except (KeyError, TypeError, ValueError):
                errors.append("pulse feedback has no valid raw h0 clip evidence")
        marker_matches = bool(
            result_marker is not None
            and computed_pre_max is not None
            and computed_post_max is not None
            and abs(float(result_marker["pre_max_uS"]) - computed_pre_max) <= 0.01
            and abs(float(result_marker["post_max_uS"]) - computed_post_max) <= 0.01
            and bool(result_marker["issued"])
            == (computed_pre_max < SCAN_FORM_THRESHOLD_US)
            and bool(result_marker["crossed20"])
            == (computed_post_max >= SCAN_FORM_THRESHOLD_US)
            and len(pulse_feedback) == int(issued)
            and bool(result_marker["clipped"])
            == (feedback_raw_clipped if issued else False)
        )
        sentinel_matches = bool(
            sentinel_gate is not None
            and computed_sentinel_max is not None
            and abs(
                float(sentinel_gate["max_abs_drift_uA"]) - computed_sentinel_max
            )
            <= 0.001
            and bool(sentinel_gate["pass"])
            == (computed_sentinel_max <= SENTINEL_LIMIT_UA)
        )
        evidence_complete = bool(
            code == 0
            and job_complete
            and pre_clean
            and post_clean
            and feedback_clean
            and sentinel_frames_clean
            and sentinel_coordinates_match
            and marker_matches
            and sentinel_matches
            and sentinel_gate is not None
            and sentinel_gate.get("pass") is True
            and profile_restored
            and not restore_failed
            and not errors
        )
        self.store.register_raw_file(
            vivado_log,
            file_type="vivado_log",
            cell_id=f"r{row:02d}_c{column:02d}",
            attempt_id=attempt_id,
            raw_file_id=f"{attempt_id}_vivado",
            closed_cleanly=evidence_complete,
        )
        self.store.register_raw_file(
            console_log,
            file_type="timestamped_console_jsonl",
            cell_id=f"r{row:02d}_c{column:02d}",
            attempt_id=attempt_id,
            raw_file_id=f"{attempt_id}_console",
            closed_cleanly=evidence_complete,
        )
        if sentinel_gate is not None and not sentinel_gate.get("pass"):
            self._engage_chip_safety_hold(
                "cross-cell sentinel drift detected during guarded scan SET forming",
                attempt_id=attempt_id,
                evidence={
                    "target_cell": key,
                    "sentinel_max_abs_drift_uA": sentinel_gate.get("max_abs_drift_uA"),
                    "limit_uA": SENTINEL_LIMIT_UA,
                },
            )
        if not evidence_complete:
            details = [
                f"exit={code}",
                f"job_complete={job_complete}",
                f"pre_count={len(pre_reads)}",
                f"post_count={len(post_reads)}",
                f"feedback_count={len(pulse_feedback)}",
                f"pre_clean={pre_clean}",
                f"post_clean={post_clean}",
                f"feedback_clean={feedback_clean}",
                f"sentinel_frames_clean={sentinel_frames_clean}",
                f"sentinel_coordinates_match={sentinel_coordinates_match}",
                f"marker_matches={marker_matches}",
                f"sentinel_matches={sentinel_matches}",
                f"profile_restored={profile_restored and not restore_failed}",
            ]
            details.extend(errors)
            block_ambiguous(", ".join(details), source_log_id)

        assert result_marker is not None
        assert sentinel_gate is not None
        assert computed_pre_max is not None
        assert computed_post_max is not None
        assert computed_sentinel_max is not None
        pulse_clipped = bool(result_marker["clipped"])
        post_median = statistics.median(post_values)
        target_low, target_high = TARGET_BANDS_US[target_state_code]
        target_landed = target_low <= post_median <= target_high
        assigned, landed, assigned_low, assigned_high = nearest_state(post_median)
        if target_landed:
            assigned = target_state_code
            landed = True
            assigned_low, assigned_high = target_low, target_high
        absolute_ceiling = computed_post_max >= SET_CEILING_US
        safety_stop = pulse_clipped or absolute_ceiling
        if safety_stop:
            self._engage_chip_safety_hold(
                "guarded scan SET forming reached a hard clip/current limit",
                attempt_id=attempt_id,
                evidence={
                    "target_cell": key,
                    "pulse_clipped": pulse_clipped,
                    "post_max_uS": computed_post_max,
                    "ceiling_uS": SET_CEILING_US,
                },
            )
        if pulse_clipped:
            result_class = "scan_set_forming_pulse_clipped"
        elif absolute_ceiling:
            result_class = "scan_set_forming_absolute_ceiling"
        elif not issued:
            result_class = "scan_set_forming_controller_guard_skipped"
        elif computed_post_max >= SCAN_FORM_THRESHOLD_US:
            result_class = "accepted_scan_set_forming_crossed_threshold"
        else:
            result_class = "accepted_scan_set_forming_below_threshold"

        read_ids: dict[int, str] = {}
        ordered_target_reads = [
            ("pre_program", index, target_pre[index])
            for index in range(1, PRE_READS + 1)
        ] + [
            ("post_program", index, target_post[index])
            for index in range(1, POST_WRITE_READS + 1)
        ]
        for role, index, read in ordered_target_reads:
            read_id = hashlib.sha256(
                f"{attempt_id}|{role}|{index}".encode()
            ).hexdigest()[:24]
            read_ids[id(read)] = read_id
            self._append_row(
                "scan_reads",
                {
                    "event_seq": "",
                    "run_id": self.run_id,
                    "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                    "attempt_id": attempt_id,
                    "read_id": read_id,
                    "read_role": role,
                    "read_index": index,
                    "state_code_context": target_state_code,
                    "elapsed_after_write_ms": "",
                    "read_voltage_v": READ_VOLTAGE_V,
                    **read,
                    "scan_clock_mhz": 2,
                    "dac_profile_id": "scan_read_0p5V",
                    "decode_revision": "scan-frame-v2-160",
                    "qc_flags": "",
                },
            )
        for index, read in enumerate(pulse_feedback, 1):
            read_id = hashlib.sha256(
                f"{attempt_id}|pulse_feedback|{index}".encode()
            ).hexdigest()[:24]
            self._append_row(
                "scan_reads",
                {
                    "event_seq": "",
                    "run_id": self.run_id,
                    "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                    "attempt_id": attempt_id,
                    "read_id": read_id,
                    "read_role": "program_feedback",
                    "read_index": index,
                    "state_code_context": target_state_code,
                    "elapsed_after_write_ms": "",
                    "read_voltage_v": "",
                    **read,
                    "scan_clock_mhz": 2,
                    "dac_profile_id": "scan_set_forming_1p20V_2p50V",
                    "decode_revision": "scan-frame-v2-160",
                    "qc_flags": "",
                },
            )

        for phase, items in (("pre", sentinel_pre), ("post", sentinel_post)):
            for coordinates, item in sorted(items.items()):
                pre_current = sentinel_pre[coordinates].get("current_uA")
                post_current = sentinel_post[coordinates].get("current_uA")
                drift = abs(float(post_current) - float(pre_current))
                self._append_row(
                    "sentinel_reads",
                    {
                        "run_id": self.run_id,
                        "target_cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                        "attempt_id": attempt_id,
                        "sentinel_cell_id": f"chip{CHIP_ID:02d}_r{coordinates[0]:02d}_c{coordinates[1]:02d}",
                        "read_phase": phase,
                        "read_index": 1,
                        "raw_h0_hex": item.get("raw_h0_hex"),
                        "raw_h1_hex": item.get("raw_h1_hex"),
                        "baseline_count": item.get("baseline_count"),
                        "active_count": item.get("active_count"),
                        "current_uA": item.get("current_uA"),
                        "conductance_uS": item.get("conductance_uS"),
                        "baseline_current_uA": pre_current,
                        "absolute_drift_uA": drift,
                        "pass": int(drift <= SENTINEL_LIMIT_UA),
                        "timestamp_utc": item.get("timestamp_utc"),
                    },
                )

        self._append_row(
            "program_attempts",
            {
                "event_seq": "",
                "run_id": self.run_id,
                "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                "attempt_id": attempt_id,
                "idempotency_key": idempotency_key,
                "cell_attempt_index": int(cell.get("pulse_count", 0)) + 1,
                "stage": "scan_set_forming",
                "requested_state_code": target_state_code,
                "operation": "set",
                "program_path": "scan_debug_forming",
                "pwm_code": "",
                "pwm_decimal": "",
                "program_voltage_v": SCAN_FORM_VCC_V,
                "read_voltage_v": READ_VOLTAGE_V,
                "dac2_v": SCAN_FORM_VCC_V,
                "dac3_v": SCAN_FORM_WL_V,
                "dac_mask": "0x01F3",
                "scan_clock_mhz": 2,
                "program_clock_mhz": 2,
                "pre_read_count": len(pre_values),
                "post_read_count": len(post_values),
                "pre_median_uA": statistics.median(pre_values) / 2.0,
                "pre_median_uS": statistics.median(pre_values),
                "post_median_uA": post_median / 2.0,
                "post_median_uS": post_median,
                "post_min_uS": min(post_values),
                "post_max_uS": computed_post_max,
                "delta_uS": post_median - statistics.median(pre_values),
                "assigned_state_code": assigned if assigned is not None else "",
                "assigned_band_low_uS": assigned_low if assigned_low is not None else "",
                "assigned_band_high_uS": assigned_high if assigned_high is not None else "",
                "wb_ack": "",
                "tdc_pass": "",
                "analog_confirmed": "",
                "clip": int(pulse_clipped),
                "sentinel_pass": 1,
                "safety_stop": int(safety_stop),
                "result_class": result_class,
                "start_time_utc": started_utc,
                "end_time_utc": utc_now(),
                "source_log_id": source_log_id,
            },
        )
        self._append_row(
            "state_observations",
            {
                "run_id": self.run_id,
                "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                "observation_id": hashlib.sha256(
                    f"{attempt_id}|post_program".encode()
                ).hexdigest()[:24],
                "attempt_id": attempt_id,
                "observation_phase": "post_program",
                "target_state_code": target_state_code,
                "assigned_state_code": assigned,
                "target_low_uS": target_low,
                "target_high_uS": target_high,
                "sample_count": len(post_values),
                "median_uS": post_median,
                "mean_uS": statistics.fmean(post_values),
                "stddev_uS": statistics.stdev(post_values),
                "min_uS": min(post_values),
                "max_uS": computed_post_max,
                "distance_to_target_center_uS": abs(
                    post_median - (target_low + target_high) / 2.0
                ),
                "landed": int(target_landed),
                "classification_revision": "4uS-bands-v2-balanced",
                "source_read_ids": " ".join(
                    read_ids[id(read)] for read in post_reads
                ),
            },
        )

        crossed_threshold = computed_post_max >= SCAN_FORM_THRESHOLD_US
        normalized = {
            "valid": not safety_stop,
            "accepted": issued and not safety_stop,
            "eligible": True,
            "issued": issued,
            "result_class": result_class,
            "program_path": "scan_debug_forming",
            "wb_ack": None,
            "tdc_pass": None,
            "clip": pulse_clipped,
            "safety_stop": safety_stop,
            "sentinel_pass": True,
            "profile_restored": True,
            "job_complete": True,
            "pre_read_count": len(pre_values),
            "post_read_count": len(post_values),
            "pre_median_uS": statistics.median(pre_values),
            "pre_max_uS": computed_pre_max,
            "post_median_uS": post_median,
            "post_min_uS": min(post_values),
            "post_max_uS": computed_post_max,
            "crossed_threshold": crossed_threshold,
            "target_landed": target_landed,
            "assigned_state_code": assigned,
            "sentinel_max_abs_drift_uA": computed_sentinel_max,
            "attempt_id": attempt_id,
            "idempotency_key": idempotency_key,
            "source_log_id": source_log_id,
            "pulse_ordinal": pulse_ordinal,
            "pulse_budget": SCAN_FORM_MAX_PULSES_PER_CELL,
        }
        self._checkpoint["inflight_attempt"] = None
        self._checkpoint["completed_attempts"][attempt_id] = normalized
        cell["last_attempt_id"] = attempt_id
        cell["scan_forming_stage"] = "complete"
        new_forming_count = int(cell.get("scan_forming_pulse_count", 0)) + int(issued)
        cell["scan_forming_pulse_count"] = new_forming_count
        cell["pulse_count"] = int(cell.get("pulse_count", 0)) + int(issued)
        if isinstance(assigned, int):
            cell["states_observed"] = sorted(
                set(cell.get("states_observed", [])) | {assigned}
            )
        if computed_pre_max >= SCAN_FORM_THRESHOLD_US or crossed_threshold:
            cell["scan_set_permanently_disabled"] = True
            cell["scan_set_disable_reason"] = (
                f"guarded forming target read reached {max(computed_pre_max, computed_post_max):.6f} uS"
            )
        cell["scan_forming_exhausted"] = bool(
            cell.get("scan_set_permanently_disabled")
            or new_forming_count >= SCAN_FORM_MAX_PULSES_PER_CELL
        )
        # A post-form crossing gets a fresh WB ladder namespace and begins at
        # the first 1.19-V/0x10 condition.  Below-threshold exhaustion leaves
        # the pre-existing WB cursor untouched.
        if crossed_threshold and not safety_stop:
            cell["balanced_program_epoch"] = int(
                cell.get("balanced_program_epoch", 0)
            ) + 1
            cell["balanced_next_condition_index"] = 0
        self._save_checkpoint()
        self.store.append_event(
            "scan_set_forming_completed",
            row=row,
            column=column,
            attempt_id=attempt_id,
            issued=issued,
            result_class=result_class,
            post_median_uS=post_median,
            post_max_uS=computed_post_max,
            crossed_threshold=crossed_threshold,
            pulse_ordinal=pulse_ordinal,
            pulse_budget=SCAN_FORM_MAX_PULSES_PER_CELL,
            duration_seconds=duration,
        )
        # Keep the five decoded post-reads out of the durable normalized
        # result, but return them to the live caller as the strict host guard
        # for the next one-pulse job.
        return {**normalized, "verified_post_reads": post_reads}

    def _run_scan_reset(
        self,
        row: int,
        column: int,
        *,
        target_state_code: int = 0,
    ) -> dict[str, Any]:
        _assert_program_target(row, column)
        if not 0 <= target_state_code < len(TARGET_BANDS_US):
            raise ValueError("scan RESET target_state_code is outside 0..15")
        # Keep the legacy C0/v1 idempotency key byte-for-byte stable. Balanced
        # nonzero targets add context so their immutable observation cannot be
        # mistaken for a legacy baseline reset.
        seed = f"{self.run_id}|r{row}|c{column}|scan_reset_fallback|post_reads={POST_WRITE_READS}"
        if target_state_code != 0:
            seed += f"|target={target_state_code}"
        idempotency_key = hashlib.sha256(seed.encode()).hexdigest()
        attempt_id = idempotency_key[:32]
        active = self._checkpoint.get("inflight_attempt")
        if active:
            raise RuntimeError(
                f"refusing scan RESET while uncertain attempt {active} is in flight"
            )
        completed = self._checkpoint["completed_attempts"].get(attempt_id)
        if completed:
            return completed
        self._verify_plan_fingerprint()

        key = f"r{row:02d}_c{column:02d}"
        raw_dir = self.store.run_dir / "cells" / key / "raw" / "vivado"
        ramp_reads: list[dict[str, Any]] = []
        sentinel_reads: list[dict[str, Any]] = []
        errors: list[str] = []
        job_complete = False
        ramp_summary: dict[str, Any] | None = None
        sentinel_gate: dict[str, Any] | None = None
        profile_restored = False
        restore_failed = False

        def feed(line: str, timestamp: str, line_number: int) -> None:
            nonlocal job_complete, ramp_summary, sentinel_gate, profile_restored, restore_failed
            if line.startswith("CELL_RESULT "):
                try:
                    decoded = decode_cell_result(line, timestamp, line_number)
                    if str(decoded.get("label", "")).startswith("scan_reset_sentinel_"):
                        sentinel_reads.append(decoded)
                    else:
                        ramp_reads.append(decoded)
                except ValueError as exc:
                    errors.append(str(exc))
            if line.startswith("RAMP_COMPLETE reset"):
                parts = line.split()
                try:
                    if len(parts) != 5 or ramp_summary is not None:
                        raise ValueError("malformed or duplicate RAMP_COMPLETE reset marker")
                    pulses = int(parts[2])
                    target_met = int(parts[3])
                    final_current_uA = float(parts[4])
                    if pulses < 0 or target_met not in {0, 1}:
                        raise ValueError("invalid RAMP_COMPLETE reset values")
                    ramp_summary = {
                        "pulses": pulses,
                        "target_met": bool(target_met),
                        "final_current_uA": final_current_uA,
                        "timestamp_utc": timestamp,
                        "source_line": line_number,
                    }
                except ValueError as exc:
                    errors.append(str(exc))
            if line.startswith("SCAN_RESET_SENTINEL_GATE "):
                parts = line.split()
                try:
                    if len(parts) != 5 or sentinel_gate is not None:
                        raise ValueError("malformed or duplicate SCAN_RESET_SENTINEL_GATE marker")
                    gate_row = int(parts[1])
                    gate_column = int(parts[2])
                    max_drift_uA = float(parts[3])
                    passed = int(parts[4])
                    if (gate_row, gate_column) != (row, column) or max_drift_uA < 0 or passed not in {0, 1}:
                        raise ValueError("invalid SCAN_RESET_SENTINEL_GATE values")
                    sentinel_gate = {
                        "max_abs_drift_uA": max_drift_uA,
                        "pass": bool(passed),
                        "timestamp_utc": timestamp,
                        "source_line": line_number,
                    }
                except ValueError as exc:
                    errors.append(str(exc))
            if line.startswith("DAC_PROFILE scan_read") and ramp_summary is not None:
                profile_restored = True
            if line.startswith("RESTORE_FAILED"):
                restore_failed = True
            if line.startswith("JOB_COMPLETE scan_reset_guarded"):
                job_complete = True

        def block_ambiguous(message: str, source_log: str = "") -> None:
            self._append_row(
                "errors",
                {
                    "event_seq": "", "run_id": self.run_id,
                    "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                    "attempt_id": attempt_id, "error_time_utc": utc_now(),
                    "error_class": "ambiguous_scan_reset", "error_code": "AMBIGUOUS_SCAN_RESET",
                    "message": message, "hardware_action_started": 1,
                    "wb_ack_seen": 0, "tdc_seen": 0,
                    "scan_profile_restored": int(profile_restored and not restore_failed),
                    "retry_allowed": 0,
                    "resolution": "global block; read-only recovery and operator review required",
                    "source_log_id": source_log,
                },
            )
            self.store.append_event(
                "scan_reset_fallback_ambiguous",
                row=row,
                column=column,
                attempt_id=attempt_id,
                message=message,
            )
            raise RuntimeError(
                f"scan RESET fallback {attempt_id} is ambiguous; automatic replay is prohibited: {message}"
            )

        self._checkpoint["inflight_attempt"] = attempt_id
        self._save_checkpoint()
        started_utc = utc_now()
        self.store.append_event("scan_reset_fallback_started", row=row, column=column, job_id=attempt_id, attempt_id=attempt_id, scan_set_prohibited=True)
        try:
            code, vivado_log, console_log, duration = self.vivado.run(
                attempt_id,
                ["scan_reset_guarded", str(row), str(column), "2.3", "1.75", "3.0", "3.0", "1200", "1", attempt_id],
                raw_dir,
                feed,
            )
        except Exception as exc:
            block_ambiguous(f"scan RESET hardware job raised {type(exc).__name__}: {exc}")
            raise AssertionError("unreachable")

        sentinel_pre = {
            (item.get("row"), item.get("column")): item
            for item in sentinel_reads
            if item.get("label") == "scan_reset_sentinel_pre"
        }
        sentinel_post = {
            (item.get("row"), item.get("column")): item
            for item in sentinel_reads
            if item.get("label") == "scan_reset_sentinel_post"
        }
        ramp_frames_clean = bool(ramp_reads) and all(
            (item.get("row"), item.get("column")) == (row, column)
            and not item.get("read_clipped")
            and not item.get("qc_flags")
            for item in ramp_reads
        )
        sentinel_frames_clean = all(
            not item.get("read_clipped") and not item.get("qc_flags")
            for item in sentinel_reads
        )
        sentinel_coordinates_match = bool(
            len(sentinel_pre) == 4
            and len(sentinel_post) == 4
            and set(sentinel_pre) == set(sentinel_post)
        )
        sentinel_drifts = []
        if sentinel_coordinates_match:
            for coordinates in sentinel_pre:
                pre_current = sentinel_pre[coordinates].get("current_uA")
                post_current = sentinel_post[coordinates].get("current_uA")
                if not isinstance(pre_current, (int, float)) or not isinstance(
                    post_current, (int, float)
                ):
                    sentinel_drifts = []
                    break
                sentinel_drifts.append(abs(float(post_current) - float(pre_current)))
        sentinel_drift_frames_pass = bool(
            len(sentinel_drifts) == 4
            and max(sentinel_drifts) <= SENTINEL_LIMIT_UA
            and sentinel_gate is not None
            and sentinel_gate.get("max_abs_drift_uA") is not None
            and float(sentinel_gate["max_abs_drift_uA"]) <= SENTINEL_LIMIT_UA
            and abs(max(sentinel_drifts) - float(sentinel_gate["max_abs_drift_uA"]))
            <= 0.001
        )
        program_complete = bool(
            code == 0
            and job_complete
            and ramp_summary is not None
            and sentinel_gate is not None
            and sentinel_gate.get("pass") is True
            and len(sentinel_reads) == 8
            and len(sentinel_pre) == 4
            and len(sentinel_post) == 4
            and ramp_frames_clean
            and sentinel_frames_clean
            and sentinel_coordinates_match
            and sentinel_drift_frames_pass
            and profile_restored
            and not restore_failed
            and not errors
        )
        self.store.register_raw_file(vivado_log, file_type="vivado_log", cell_id=f"r{row:02d}_c{column:02d}", attempt_id=attempt_id, raw_file_id=f"{attempt_id}_vivado", closed_cleanly=program_complete)
        self.store.register_raw_file(console_log, file_type="timestamped_console_jsonl", cell_id=f"r{row:02d}_c{column:02d}", attempt_id=attempt_id, raw_file_id=f"{attempt_id}_console", closed_cleanly=program_complete)

        for phase, items in (("pre", sentinel_pre), ("post", sentinel_post)):
            for coordinates, item in sorted(items.items()):
                baseline = sentinel_pre.get(coordinates, {})
                pre_current = baseline.get("current_uA")
                post_current = sentinel_post.get(coordinates, {}).get("current_uA")
                drift = (
                    abs(float(post_current) - float(pre_current))
                    if pre_current is not None and post_current is not None
                    else None
                )
                self._append_row(
                    "sentinel_reads",
                    {
                        "run_id": self.run_id,
                        "target_cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                        "attempt_id": attempt_id,
                        "sentinel_cell_id": f"chip{CHIP_ID:02d}_r{coordinates[0]:02d}_c{coordinates[1]:02d}",
                        "read_phase": phase,
                        "read_index": 1,
                        "raw_h0_hex": item.get("raw_h0"),
                        "raw_h1_hex": item.get("raw_h1"),
                        "baseline_count": item.get("baseline_count"),
                        "active_count": item.get("active_count"),
                        "current_uA": item.get("current_uA"),
                        "conductance_uS": item.get("conductance_uS"),
                        "baseline_current_uA": pre_current,
                        "absolute_drift_uA": drift,
                        "pass": int(drift is not None and drift <= SENTINEL_LIMIT_UA),
                        "timestamp_utc": item.get("timestamp_utc"),
                    },
                )
        if sentinel_gate is not None and not sentinel_gate.get("pass"):
            self._engage_chip_safety_hold(
                "cross-cell sentinel drift detected during scan RESET fallback",
                attempt_id=attempt_id,
                evidence={
                    "target_cell": f"r{row:02d}_c{column:02d}",
                    "sentinel_max_abs_drift_uA": sentinel_gate.get("max_abs_drift_uA"),
                    "limit_uA": SENTINEL_LIMIT_UA,
                },
            )
        if not program_complete:
            details = [
                f"exit={code}", f"job_complete={job_complete}",
                f"ramp_complete={ramp_summary is not None}",
                f"sentinel_gate={sentinel_gate}",
                f"ramp_frames_clean={ramp_frames_clean}",
                f"sentinel_frames_clean={sentinel_frames_clean}",
                f"sentinel_coordinates_match={sentinel_coordinates_match}",
                f"sentinel_drift_frames_pass={sentinel_drift_frames_pass}",
                f"profile_restored={profile_restored and not restore_failed}",
            ]
            details.extend(errors)
            block_ambiguous(", ".join(details), str(vivado_log.relative_to(self.store.run_dir)))

        assert ramp_summary is not None
        for index, read in enumerate(ramp_reads, 1):
            read_id = hashlib.sha256(f"{attempt_id}|ramp_trace|{index}".encode()).hexdigest()[:24]
            role = "pre_program" if index == 1 and str(read.get("label", "")).startswith("before_reset") else "reset_ramp_trace"
            self._append_row(
                "scan_reads",
                {
                    "event_seq": "", "run_id": self.run_id,
                    "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                    "attempt_id": attempt_id, "read_id": read_id,
                    "read_role": role, "read_index": index, "state_code_context": 0,
                    "elapsed_after_write_ms": "", "read_voltage_v": READ_VOLTAGE_V,
                    **read, "scan_clock_mhz": 2, "dac_profile_id": "scan_read_0p5V",
                    "decode_revision": "scan-frame-v2-160", "qc_flags": read.get("qc_flags", ""),
                },
            )

        verification_job_id = hashlib.sha256(
            f"{attempt_id}|post_program|{POST_WRITE_READS}".encode()
        ).hexdigest()[:32]
        try:
            verification = self._run_read_only(
                row,
                column,
                "post_program",
                POST_WRITE_READS,
                parent_attempt_id=attempt_id,
                job_id=verification_job_id,
                state_code_context=0,
            )
        except Exception as exc:
            block_ambiguous(
                f"post-RESET verification raised {type(exc).__name__}: {exc}",
                str(vivado_log.relative_to(self.store.run_dir)),
            )
            raise AssertionError("unreachable")
        if not verification["valid"]:
            block_ambiguous(
                f"post-RESET verification incomplete: {verification['error']}",
                str(vivado_log.relative_to(self.store.run_dir)),
            )

        post_reads = verification["reads"]
        post_values = [float(read["conductance_uS"]) for read in post_reads]
        post_us = statistics.median(post_values)
        safe_median = post_us <= RESET_SAFE_MAX_US
        target_met = bool(ramp_summary["target_met"])
        valid = target_met and safe_median
        if not target_met:
            result_class = "scan_reset_target_not_met"
            error = "scan RESET ramp reached its limit without target confirmation"
        elif not safe_median:
            result_class = "scan_reset_above_safe_max"
            error = f"post-RESET median {post_us:.6f} uS exceeds safe maximum {RESET_SAFE_MAX_US:.6f} uS"
        else:
            result_class = "accepted_scan_reset"
            error = ""
        pre_values = [
            float(read["conductance_uS"])
            for read in ramp_reads[:1]
            if read.get("conductance_uS") is not None
        ]
        pre_us = statistics.median(pre_values) if pre_values else None
        assigned, landed, low, high = nearest_state(post_us)
        target_low, target_high = TARGET_BANDS_US[target_state_code]
        target_landed = target_low <= post_us <= target_high
        if self._balanced_plan is not None and target_landed:
            assigned = target_state_code
            landed = True
            low, high = target_low, target_high
        self._append_row(
            "program_attempts",
            {
                "event_seq": "", "run_id": self.run_id,
                "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}", "attempt_id": attempt_id,
                "idempotency_key": idempotency_key, "cell_attempt_index": 0,
                "stage": "scan_reset_fallback", "requested_state_code": target_state_code, "operation": "reset",
                "program_path": "scan_debug_reset_fallback", "pwm_code": "", "pwm_decimal": "",
                "program_voltage_v": 3.0, "read_voltage_v": READ_VOLTAGE_V,
                "pre_read_count": len(pre_values), "post_read_count": len(post_values),
                "pre_median_uA": pre_us / 2 if pre_us is not None else "", "pre_median_uS": pre_us if pre_us is not None else "",
                "post_median_uA": post_us / 2, "post_median_uS": post_us,
                "post_min_uS": min(post_values), "post_max_uS": max(post_values),
                "delta_uS": post_us - pre_us if pre_us is not None else "",
                "assigned_state_code": assigned if assigned is not None else "",
                "assigned_band_low_uS": low if low is not None else "",
                "assigned_band_high_uS": high if high is not None else "",
                "wb_ack": 0, "tdc_pass": 0, "analog_confirmed": "", "clip": 0,
                "sentinel_pass": 1, "safety_stop": int(not valid), "result_class": result_class,
                "start_time_utc": started_utc, "end_time_utc": utc_now(), "source_log_id": str(vivado_log.relative_to(self.store.run_dir)),
            },
        )
        self._append_row(
            "state_observations",
            {
                "run_id": self.run_id,
                "cell_id": f"chip{CHIP_ID:02d}_r{row:02d}_c{column:02d}",
                "observation_id": hashlib.sha256(f"{attempt_id}|post".encode()).hexdigest()[:24],
                "attempt_id": attempt_id, "observation_phase": "post_program",
                "target_state_code": target_state_code, "assigned_state_code": assigned,
                "target_low_uS": target_low, "target_high_uS": target_high,
                "sample_count": len(post_values), "median_uS": post_us,
                "mean_uS": statistics.fmean(post_values),
                "stddev_uS": statistics.stdev(post_values) if len(post_values) > 1 else 0.0,
                "min_uS": min(post_values), "max_uS": max(post_values),
                "distance_to_target_center_uS": abs(post_us - (target_low + target_high) / 2),
                "landed": int(target_landed if self._balanced_plan is not None else landed),
                "classification_revision": (
                    "4uS-bands-v2-balanced" if self._balanced_plan is not None else "4uS-bands-v1"
                ),
                "source_read_ids": " ".join(str(read["read_id"]) for read in post_reads),
            },
        )
        normalized = {
            "valid": valid,
            "accepted": valid,
            "result_class": result_class,
            "post_median_uS": post_us,
            "post_min_uS": min(post_values),
            "post_max_uS": max(post_values),
            "assigned_state_code": assigned,
            "landed": landed,
            "target_state_code": target_state_code,
            "target_landed": target_landed,
            "ramp_target_met": target_met,
            "ramp_pulses": ramp_summary["pulses"],
            "post_read_count": len(post_values),
            "sentinel_max_abs_drift_uA": sentinel_gate.get("max_abs_drift_uA") if sentinel_gate else None,
            "sentinel_pass": bool(sentinel_gate and sentinel_gate.get("pass")),
            "attempt_id": attempt_id,
            "error": error,
        }
        self._checkpoint["inflight_attempt"] = None
        self._checkpoint["completed_attempts"][attempt_id] = normalized
        cell = self._checkpoint["cells"][key]
        cell["last_attempt_id"] = attempt_id
        cell["pulse_count"] = int(cell.get("pulse_count", 0)) + int(ramp_summary["pulses"])
        if isinstance(assigned, int):
            cell["states_observed"] = sorted(set(cell.get("states_observed", [])) | {assigned})
        self._save_checkpoint()
        self.store.append_event(
            "scan_reset_fallback_completed",
            row=row,
            column=column,
            attempt_id=attempt_id,
            valid=valid,
            target_met=target_met,
            pulses=ramp_summary["pulses"],
            post_read_count=len(post_values),
            post_median_uS=post_us,
            sentinel_max_abs_drift_uA=sentinel_gate.get("max_abs_drift_uA") if sentinel_gate else None,
            duration_seconds=duration,
        )
        return normalized


def start_in_thread(store: RunStore) -> threading.Thread:
    experiment = ArrayExperiment(store)

    def target() -> None:
        try:
            experiment.run()
        except Exception:
            # The durable event/error tables contain the complete failure.  Do
            # not let a daemon-thread traceback become the only evidence.
            pass

    thread = threading.Thread(target=target, name=f"array-4bit-{store.run_id}", daemon=True)
    thread.start()
    return thread

