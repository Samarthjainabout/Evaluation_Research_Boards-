"""Append-only persistence for the 1024-cell, four-bit array experiment.

The raw record is deliberately boring: one JSONL event ledger plus normalized
CSV tables.  CSV and JSONL files are opened only in append mode and every
append is flushed and fsynced.  Only ``manifest.json`` and ``progress.json``
are replaceable; both use a write-fsync-replace transaction.

The store keeps anomalous values.  Snapshot statistics ignore values that
cannot be interpreted as finite numbers, but the corresponding raw point is
still returned with ``included_in_stats=False`` and remains in its CSV file.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import threading
import uuid
from collections import Counter, defaultdict, deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "1.0.0"
ARRAY_ROWS = 32
ARRAY_COLUMNS = 32
CELL_COUNT = ARRAY_ROWS * ARRAY_COLUMNS
REFERENCE_ROW = 7
REFERENCE_COLUMN = 1
REFERENCE_CELL_ID = "r07_c01"


TABLE_SCHEMAS: dict[str, tuple[str, ...]] = {
    "cell_inventory": (
        "run_id",
        "cell_id",
        "chip_id",
        "rtl_row",
        "rtl_column",
        "physical_row",
        "physical_column",
        "array_index",
        "enabled",
        "planned_order",
        "skip_hardware",
        "initial_status",
        "notes",
    ),
    "cell_status_events": (
        "event_seq",
        "event_id",
        "run_id",
        "cell_id",
        "event_time_utc",
        "status",
        "stage",
        "target_state_code",
        "last_attempt_id",
        "pulse_count",
        "reason",
        "worker_pid",
    ),
    "program_attempts": (
        "event_seq",
        "event_id",
        "run_id",
        "cell_id",
        "attempt_id",
        "idempotency_key",
        "cell_attempt_index",
        "stage",
        "requested_state_code",
        "operation",
        "program_path",
        "pwm_code",
        "pwm_decimal",
        "program_voltage_v",
        "read_voltage_v",
        "dac0_v",
        "dac1_v",
        "dac2_v",
        "dac3_v",
        "dac4_v",
        "dac5_v",
        "dac6_v",
        "dac_mask",
        "scan_clock_mhz",
        "program_clock_mhz",
        "pre_read_count",
        "post_read_count",
        "pre_median_uA",
        "pre_median_uS",
        "post_median_uA",
        "post_median_uS",
        "post_min_uS",
        "post_max_uS",
        "delta_uS",
        "assigned_state_code",
        "assigned_band_low_uS",
        "assigned_band_high_uS",
        "wb_ack",
        "tdc_pass",
        "analog_confirmed",
        "clip",
        "sentinel_pass",
        "safety_stop",
        "result_class",
        "start_time_utc",
        "end_time_utc",
        "source_log_id",
    ),
    "wb_packet_issues": (
        "run_id",
        "cell_id",
        "attempt_id",
        "issue_index",
        "command_value_hex",
        "firmware_transaction",
        "firmware_packet_issues",
        "ack_seen",
        "raw_command_hex",
        "raw_response_hex",
        "timestamp_utc",
    ),
    "tdc_words": (
        "run_id",
        "cell_id",
        "attempt_id",
        "word_index",
        "word_hex",
        "word_uint32",
        "is_nonzero",
        "raw_h0_hex",
        "raw_h1_hex",
        "timestamp_utc",
    ),
    "scan_reads": (
        "event_seq",
        "event_id",
        "run_id",
        "cell_id",
        "attempt_id",
        "read_id",
        "read_role",
        "read_index",
        "state_code_context",
        "elapsed_after_write_ms",
        "read_voltage_v",
        "raw_h0_hex",
        "raw_h1_hex",
        "baseline_count",
        "baseline_sum",
        "baseline_mean_code",
        "active_count",
        "active_sum",
        "active_mean_code",
        "current_uA",
        "conductance_uS",
        "scan_clock_mhz",
        "dac_profile_id",
        "decode_revision",
        "timestamp_utc",
        "qc_flags",
    ),
    "sentinel_reads": (
        "run_id",
        "target_cell_id",
        "attempt_id",
        "sentinel_cell_id",
        "read_phase",
        "read_index",
        "raw_h0_hex",
        "raw_h1_hex",
        "baseline_count",
        "active_count",
        "current_uA",
        "conductance_uS",
        "baseline_current_uA",
        "absolute_drift_uA",
        "pass",
        "timestamp_utc",
    ),
    "shunt_metrics": (
        "run_id",
        "cell_id",
        "attempt_id",
        "path",
        "adc_channel",
        "shunt_resistor",
        "baseline_sample_count",
        "baseline_mean_code",
        "baseline_min_code",
        "baseline_max_code",
        "active_sample_count",
        "active_min_code",
        "active_max_code",
        "threshold_code",
        "positive_max_run",
        "negative_max_run",
        "run_required",
        "sustained_positive",
        "sustained_negative",
        "peak_positive_delta_code",
        "peak_negative_delta_code",
        "peak_excursion_nominal_uA",
        "baseline_clip",
        "active_clip",
        "analog_confirmed",
        "trace_file_id",
    ),
    "state_observations": (
        "run_id",
        "cell_id",
        "observation_id",
        "attempt_id",
        "observation_phase",
        "target_state_code",
        "assigned_state_code",
        "target_low_uS",
        "target_high_uS",
        "sample_count",
        "median_uS",
        "mean_uS",
        "stddev_uS",
        "min_uS",
        "max_uS",
        "distance_to_target_center_uS",
        "landed",
        "classification_revision",
        "source_read_ids",
    ),
    "errors": (
        "event_seq",
        "event_id",
        "run_id",
        "cell_id",
        "attempt_id",
        "error_time_utc",
        "error_class",
        "error_code",
        "message",
        "hardware_action_started",
        "wb_ack_seen",
        "tdc_seen",
        "scan_profile_restored",
        "retry_allowed",
        "resolution",
        "source_log_id",
    ),
    "operator_annotations": (
        "annotation_id",
        "run_id",
        "cell_id",
        "read_id",
        "attempt_id",
        "created_utc",
        "author",
        "action",
        "reason",
        "comment",
    ),
    "raw_files": (
        "raw_file_id",
        "run_id",
        "cell_id",
        "attempt_id",
        "relative_path",
        "file_type",
        "byte_count",
        "sha256",
        "created_utc",
        "closed_cleanly",
    ),
}


PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "cell_inventory": ("run_id", "cell_id"),
    "cell_status_events": ("run_id", "event_id", "cell_id"),
    "program_attempts": ("run_id", "attempt_id"),
    "wb_packet_issues": ("run_id", "attempt_id", "issue_index"),
    "tdc_words": ("run_id", "attempt_id", "word_index"),
    "scan_reads": ("run_id", "read_id"),
    "sentinel_reads": (
        "run_id",
        "attempt_id",
        "sentinel_cell_id",
        "read_phase",
        "read_index",
    ),
    "shunt_metrics": ("run_id", "attempt_id", "path", "adc_channel"),
    "state_observations": ("run_id", "observation_id"),
    "errors": ("run_id", "event_id"),
    "operator_annotations": ("run_id", "annotation_id"),
    "raw_files": ("run_id", "raw_file_id"),
}


STATE_CODE_FIELDS = {
    "target_state_code",
    "requested_state_code",
    "assigned_state_code",
    "state_code_context",
}

CELL_ID_FIELDS = {"cell_id", "target_cell_id", "sentinel_cell_id"}

BOOL_FIELDS = {
    "enabled",
    "skip_hardware",
    "wb_ack",
    "tdc_pass",
    "analog_confirmed",
    "clip",
    "sentinel_pass",
    "safety_stop",
    "ack_seen",
    "is_nonzero",
    "pass",
    "sustained_positive",
    "sustained_negative",
    "baseline_clip",
    "active_clip",
    "landed",
    "hardware_action_started",
    "wb_ack_seen",
    "tdc_seen",
    "scan_profile_restored",
    "retry_allowed",
    "closed_cleanly",
}

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CELL_ID_RE = re.compile(
    r"^(?:chip\d+[_:/-]*)?r(?:ow)?[_:/-]?(\d{1,2})[_:/-]*c(?:ol(?:umn)?)?[_:/-]?(\d{1,2})$",
    re.IGNORECASE,
)
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


class StoreError(RuntimeError):
    """Base error for a run-store invariant violation."""


class SchemaError(StoreError):
    """A table header or row does not match the normalized schema."""


class IdempotencyConflictError(StoreError):
    """The same durable identity was reused for different data."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve()).casefold()
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _target_bands() -> list[dict[str, Any]]:
    bands: list[dict[str, Any]] = []
    for code in range(16):
        low = 53.0 + 4.0 * code
        high = 57.0 + 4.0 * code
        bands.append(
            {
                "state_code": code,
                "state_hex": format(code, "X"),
                "low_uS": low,
                "high_uS": high,
                "center_uS": (low + high) / 2.0,
                "lower_inclusive": True,
                "upper_inclusive": code == 15,
            }
        )
    return bands


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace one of the two mutable JSON documents."""

    if path.name not in {"manifest.json", "progress.json"}:
        raise StoreError(f"Atomic replacement is restricted to manifest/progress, not {path.name!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(_json_safe(value), indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise StoreError(f"Required file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StoreError(f"Expected a JSON object in {path}")
    return value


def _deep_merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _normalise_run_id(value: Any) -> str:
    run_id = str(value).strip()
    if not _RUN_ID_RE.fullmatch(run_id):
        raise StoreError(
            "run_id must start with an alphanumeric and contain only alphanumerics, '.', '_', or '-'"
        )
    return run_id


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"array4b_{timestamp}_{uuid.uuid4().hex[:8]}"


def _normalise_cell_id(value: Any) -> str:
    text = str(value).strip()
    match = _CELL_ID_RE.fullmatch(text)
    if not match:
        raise SchemaError(f"Invalid cell identifier: {text!r}")
    row, column = (int(part) for part in match.groups())
    if not (0 <= row < ARRAY_ROWS and 0 <= column < ARRAY_COLUMNS):
        raise SchemaError(f"Cell is outside the 32x32 array: {text!r}")
    return f"r{row:02d}_c{column:02d}"


def _parse_state_code(value: Any) -> int:
    if isinstance(value, bool):
        raise SchemaError(f"Boolean is not a valid four-bit state code: {value!r}")
    if isinstance(value, int):
        code = value
    else:
        text = str(value).strip()
        if not text:
            raise SchemaError("State code is empty")
        if text.lower().startswith("0x"):
            code = int(text, 16)
        elif len(text) == 1 and text.upper() in "ABCDEF":
            code = int(text, 16)
        else:
            code = int(text, 10)
    if not 0 <= code <= 15:
        raise SchemaError(f"State code must be 0..15, got {value!r}")
    return code


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "pass", "passed", "on"}


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (Mapping, list, tuple, set)):
        return json.dumps(_json_safe(value), separators=(",", ":"), sort_keys=True, allow_nan=False)
    if isinstance(value, datetime):
        return str(_json_safe(value))
    return str(value)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Append write returned no progress")
        view = view[written:]


class RunStore:
    """Durable, append-only store for one 32x32 array experiment."""

    manifest_filename = "manifest.json"
    progress_filename = "progress.json"
    events_filename = "events.jsonl"

    def __init__(self, run_dir: Path, manifest: Mapping[str, Any]) -> None:
        self.run_dir = Path(run_dir).resolve()
        self._lock = _path_lock(self.run_dir)
        self._manifest = deepcopy(dict(manifest))
        self.run_id = _normalise_run_id(self._manifest.get("run_id", self.run_dir.name))
        self.tables_dir = self.run_dir / "tables"
        self.raw_dir = self.run_dir / "raw"
        self.cells_dir = self.run_dir / "cells"
        self.logs_dir = self.run_dir / "logs"
        self.exports_dir = self.run_dir / "exports"
        self.manifest_path = self.run_dir / self.manifest_filename
        self.progress_path = self.run_dir / self.progress_filename
        self.events_path = self.run_dir / self.events_filename
        self._key_cache: dict[str, tuple[int, dict[tuple[str, ...], dict[str, str]]]] = {}
        self._event_seq = 0
        self._events_size = -1

        with self._lock:
            self._ensure_layout()
            self._validate_manifest(self._manifest)
            self._refresh_event_sequence()
            self._ensure_table_files()
            self._ensure_inventory()
            self._ensure_initial_statuses()
            self._ensure_progress()

    @classmethod
    def create(cls, runs_root: str | Path, manifest: Mapping[str, Any] | None = None) -> "RunStore":
        """Create a run, or idempotently reopen a run with the same identity."""

        supplied = deepcopy(dict(manifest or {}))
        run_id = _normalise_run_id(supplied.get("run_id") or _new_run_id())
        runs_root_path = Path(runs_root).expanduser().resolve()
        runs_root_path.mkdir(parents=True, exist_ok=True)
        run_dir = runs_root_path / run_id
        manifest_path = run_dir / cls.manifest_filename

        try:
            run_dir.mkdir(parents=False, exist_ok=False)
            created_directory = True
        except FileExistsError:
            created_directory = False

        if not created_directory and manifest_path.exists():
            existing = _read_json(manifest_path)
            if _normalise_run_id(existing.get("run_id", "")) != run_id:
                raise IdempotencyConflictError(f"Existing manifest identity differs at {run_dir}")
            for identity_key in ("chip_id", "board_id"):
                if identity_key in supplied and identity_key in existing:
                    if str(supplied[identity_key]) != str(existing[identity_key]):
                        raise IdempotencyConflictError(
                            f"Existing {identity_key}={existing[identity_key]!r} conflicts with "
                            f"requested {supplied[identity_key]!r}"
                        )
            return cls.open(run_dir)

        normalised = cls._build_manifest(run_id, supplied)
        _atomic_write_json(manifest_path, normalised)
        return cls(run_dir, normalised)

    @classmethod
    def open(cls, run_dir: str | Path) -> "RunStore":
        """Open an existing run and safely finish any missing initialization."""

        path = Path(run_dir).expanduser().resolve()
        if not path.is_dir():
            raise StoreError(f"Run directory does not exist: {path}")
        manifest_path = path / cls.manifest_filename
        manifest = _read_json(manifest_path)
        normalised = cls._build_manifest(
            _normalise_run_id(manifest.get("run_id", path.name)), manifest, preserve_created=True
        )
        if normalised != manifest:
            _atomic_write_json(manifest_path, normalised)
        return cls(path, normalised)

    @staticmethod
    def _build_manifest(
        run_id: str, supplied: Mapping[str, Any], *, preserve_created: bool = False
    ) -> dict[str, Any]:
        manifest = deepcopy(dict(supplied))
        manifest["schema_version"] = SCHEMA_VERSION
        manifest["run_id"] = run_id
        if not preserve_created or not manifest.get("created_at_utc"):
            manifest.setdefault("created_at_utc", _utc_now())
        manifest.setdefault("chip_id", 4)
        manifest["array"] = {
            "rows": ARRAY_ROWS,
            "columns": ARRAY_COLUMNS,
            "cell_count": CELL_COUNT,
            "addressing": "zero_based_r00_c00_through_r31_c31",
        }
        manifest["reference_cell"] = {
            "cell_id": REFERENCE_CELL_ID,
            "row": REFERENCE_ROW,
            "column": REFERENCE_COLUMN,
            "status": "reference_complete",
            "skip_hardware": True,
        }
        manifest["target_bands"] = _target_bands()
        manifest["data_policy"] = {
            "events_append_only": True,
            "raw_csv_append_only": True,
            "raw_csv_never_overwritten": True,
            "no_outlier_deletion": True,
            "mutable_atomic_json": ["manifest.json", "progress.json"],
        }
        manifest["tables"] = {
            name: f"tables/{name}.csv" for name in TABLE_SCHEMAS
        }
        return _json_safe(manifest)

    def _validate_manifest(self, manifest: Mapping[str, Any]) -> None:
        if _normalise_run_id(manifest.get("run_id", "")) != self.run_id:
            raise StoreError("Manifest run_id does not match the open store")
        array = manifest.get("array", {})
        if not isinstance(array, Mapping) or (
            int(array.get("rows", -1)) != ARRAY_ROWS
            or int(array.get("columns", -1)) != ARRAY_COLUMNS
            or int(array.get("cell_count", -1)) != CELL_COUNT
        ):
            raise StoreError("Manifest must describe exactly one 32x32 (1024-cell) array")
        bands = manifest.get("target_bands")
        if bands != _target_bands():
            raise StoreError("Manifest target bands must be 53-57 uS with +4 uS per state")

    def _ensure_layout(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.tables_dir,
            self.raw_dir,
            self.raw_dir / "scan",
            self.raw_dir / "wb",
            self.raw_dir / "shunt",
            self.raw_dir / "instrument_logs",
            self.cells_dir,
            self.logs_dir,
            self.exports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self.events_path), flags, 0o644)
        os.close(fd)

    def table_path(self, table_name: str) -> Path:
        name = self._normalise_table_name(table_name)
        return self.tables_dir / f"{name}.csv"

    @staticmethod
    def _normalise_table_name(table_name: str) -> str:
        name = str(table_name).strip()
        if name.endswith(".csv"):
            name = name[:-4]
        if name not in TABLE_SCHEMAS:
            raise SchemaError(f"Unknown table {table_name!r}; expected one of {sorted(TABLE_SCHEMAS)}")
        return name

    def _ensure_table_files(self) -> None:
        for table_name in TABLE_SCHEMAS:
            self._append_csv_materialised(table_name, [])

    def _validate_csv_header(self, table_name: str, path: Path) -> None:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            first_line = handle.readline()
        if not first_line:
            return
        observed = tuple(next(csv.reader([first_line.rstrip("\r\n")])))
        expected = TABLE_SCHEMAS[table_name]
        if observed != expected:
            raise SchemaError(
                f"Header mismatch in {path}: expected {expected!r}, observed {observed!r}. "
                "The append-only store will not rewrite it."
            )

    def _normalise_row(self, table_name: str, row: Mapping[str, Any]) -> dict[str, str]:
        headers = TABLE_SCHEMAS[table_name]
        unknown = set(row) - set(headers)
        if unknown:
            raise SchemaError(f"Unknown columns for {table_name}: {sorted(unknown)}")

        values: dict[str, Any] = {header: row.get(header, "") for header in headers}
        if "run_id" in values:
            if values["run_id"] in (None, ""):
                values["run_id"] = self.run_id
            elif _normalise_run_id(values["run_id"]) != self.run_id:
                raise SchemaError(
                    f"Row run_id {values['run_id']!r} does not match open run {self.run_id!r}"
                )

        for field in CELL_ID_FIELDS.intersection(values):
            if values[field] not in (None, ""):
                values[field] = _normalise_cell_id(values[field])
        for field in STATE_CODE_FIELDS.intersection(values):
            if values[field] not in (None, ""):
                values[field] = _parse_state_code(values[field])
        for field in BOOL_FIELDS.intersection(values):
            if values[field] not in (None, ""):
                values[field] = _as_bool(values[field])

        now = _utc_now()
        # Measurement timestamps are evidence, not bookkeeping. Never invent
        # timestamp_utc when a historical instrument log did not record one.
        for timestamp_field in ("created_utc", "error_time_utc"):
            if timestamp_field in values and values[timestamp_field] in (None, ""):
                values[timestamp_field] = now

        if table_name == "raw_files" and values["relative_path"]:
            relative = Path(str(values["relative_path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise SchemaError("raw_files.relative_path must stay within the run directory")

        return {header: _csv_value(values[header]) for header in headers}

    def _encode_csv_rows(
        self, table_name: str, rows: Sequence[Mapping[str, str]], *, include_header: bool
    ) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=TABLE_SCHEMAS[table_name],
            extrasaction="raise",
            lineterminator="\n",
        )
        if include_header:
            writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8")

    def _append_csv_materialised(
        self, table_name: str, rows: Sequence[Mapping[str, str]]
    ) -> None:
        path = self.table_path(table_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        exists_with_data = path.exists() and path.stat().st_size > 0
        if exists_with_data:
            self._validate_csv_header(table_name, path)

        prefix = b""
        if exists_with_data:
            with path.open("rb") as check:
                check.seek(-1, os.SEEK_END)
                if check.read(1) not in {b"\n", b"\r"}:
                    prefix = b"\n"
        payload = prefix + self._encode_csv_rows(
            table_name, rows, include_header=not exists_with_data
        )
        if not payload:
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(path), flags, 0o644)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._key_cache.pop(table_name, None)

    def _read_table_unlocked(self, table_name: str) -> list[dict[str, str]]:
        path = self.table_path(table_name)
        if not path.exists() or path.stat().st_size == 0:
            return []
        self._validate_csv_header(table_name, path)
        rows: list[dict[str, str]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for line_number, row in enumerate(reader, start=2):
                if row is None or not any(value not in (None, "") for value in row.values()):
                    continue
                if None in row:
                    raise SchemaError(f"Malformed CSV row at {path}:{line_number}")
                rows.append({str(key): "" if value is None else value for key, value in row.items()})
        return rows

    def read_table(self, table_name: str) -> list[dict[str, str]]:
        name = self._normalise_table_name(table_name)
        with self._lock:
            return self._read_table_unlocked(name)

    def _load_key_cache(self, table_name: str) -> dict[tuple[str, ...], dict[str, str]]:
        key_fields = PRIMARY_KEYS.get(table_name)
        if not key_fields:
            return {}
        path = self.table_path(table_name)
        size = path.stat().st_size if path.exists() else 0
        cached = self._key_cache.get(table_name)
        if cached and cached[0] == size:
            return cached[1]
        keys: dict[tuple[str, ...], dict[str, str]] = {}
        for row in self._read_table_unlocked(table_name):
            key = tuple(row.get(field, "") for field in key_fields)
            if not all(key):
                continue
            prior = keys.get(key)
            if prior is not None and prior != row:
                raise IdempotencyConflictError(
                    f"Conflicting duplicate primary key {key!r} already exists in {table_name}"
                )
            keys[key] = row
        self._key_cache[table_name] = (size, keys)
        return keys

    def append_row(self, table_name: str, row: Mapping[str, Any]) -> bool:
        """Append one normalized row.

        Returns ``True`` if a new row was written and ``False`` for an exact
        idempotent replay.  A reused primary key with different data raises.
        """

        name = self._normalise_table_name(table_name)
        with self._lock:
            materialised = self._normalise_row(name, row)
            keys = self._load_key_cache(name)
            key_fields = PRIMARY_KEYS.get(name)
            if key_fields:
                key = tuple(materialised[field] for field in key_fields)
                if all(key) and key in keys:
                    comparison = dict(materialised)
                    # Generated timestamps must not turn an otherwise exact
                    # replay into a conflict.  If the caller omitted one,
                    # compare using the already-durable timestamp.
                    for timestamp_field in ("timestamp_utc", "created_utc", "error_time_utc"):
                        if timestamp_field in comparison and row.get(timestamp_field) in (None, ""):
                            comparison[timestamp_field] = keys[key].get(timestamp_field, "")
                    if keys[key] == comparison:
                        return False
                    raise IdempotencyConflictError(
                        f"Primary key {key!r} in {name} was replayed with different data"
                    )

            if name == "program_attempts" and materialised.get("idempotency_key"):
                for existing in self._read_table_unlocked(name):
                    if existing.get("idempotency_key") == materialised["idempotency_key"]:
                        semantic = (
                            "cell_id",
                            "requested_state_code",
                            "operation",
                            "program_path",
                            "pwm_code",
                            "program_voltage_v",
                        )
                        if all(existing.get(field, "") == materialised.get(field, "") for field in semantic):
                            return False
                        raise IdempotencyConflictError(
                            f"program_attempts idempotency_key {materialised['idempotency_key']!r} "
                            "was reused for different pulse parameters"
                        )

            self._append_csv_materialised(name, [materialised])
            return True

    def append_rows(self, table_name: str, rows: Iterable[Mapping[str, Any]]) -> int:
        appended = 0
        for row in rows:
            appended += int(self.append_row(table_name, row))
        return appended

    def _ensure_inventory(self) -> None:
        expected_ids = {
            f"r{row:02d}_c{column:02d}"
            for row in range(ARRAY_ROWS)
            for column in range(ARRAY_COLUMNS)
        }
        existing_rows = self._read_table_unlocked("cell_inventory")
        existing: dict[str, dict[str, str]] = {}
        for row in existing_rows:
            cell_id = _normalise_cell_id(row.get("cell_id", ""))
            if cell_id in existing:
                raise SchemaError(f"Duplicate cell_inventory row for {cell_id}")
            if cell_id not in expected_ids:
                raise SchemaError(f"Unexpected inventory cell {cell_id}")
            existing[cell_id] = row

        missing_rows: list[dict[str, Any]] = []
        chip_id = self._manifest.get("chip_id", 4)
        for row in range(ARRAY_ROWS):
            for column in range(ARRAY_COLUMNS):
                cell_id = f"r{row:02d}_c{column:02d}"
                is_reference = cell_id == REFERENCE_CELL_ID
                if cell_id in existing:
                    if is_reference and (
                        not _as_bool(existing[cell_id].get("skip_hardware"))
                        or existing[cell_id].get("initial_status") != "reference_complete"
                    ):
                        raise SchemaError(
                            "Existing r7/c1 inventory row is not reference_complete/skipped; "
                            "append-only inventory cannot be rewritten"
                        )
                    continue
                index = row * ARRAY_COLUMNS + column
                missing_rows.append(
                    {
                        "run_id": self.run_id,
                        "cell_id": cell_id,
                        "chip_id": chip_id,
                        "rtl_row": row,
                        "rtl_column": column,
                        "physical_row": row,
                        "physical_column": column,
                        "array_index": index,
                        "enabled": not is_reference,
                        "planned_order": index,
                        "skip_hardware": is_reference,
                        "initial_status": "reference_complete" if is_reference else "pending",
                        "notes": (
                            "pre-characterized reference; no hardware operation permitted"
                            if is_reference
                            else ""
                        ),
                    }
                )

        if missing_rows:
            materialised = [self._normalise_row("cell_inventory", row) for row in missing_rows]
            self._append_csv_materialised("cell_inventory", materialised)

        final_rows = self._read_table_unlocked("cell_inventory")
        final_ids = [_normalise_cell_id(row.get("cell_id", "")) for row in final_rows]
        if len(final_ids) != CELL_COUNT or set(final_ids) != expected_ids:
            raise SchemaError(
                f"cell_inventory must contain exactly {CELL_COUNT} unique cells; "
                f"found {len(final_ids)} rows and {len(set(final_ids))} unique IDs"
            )

    def _ensure_initial_statuses(self) -> None:
        inventory = self._read_table_unlocked("cell_inventory")
        status_rows = self._read_table_unlocked("cell_status_events")
        cells_with_status = {
            _normalise_cell_id(row["cell_id"])
            for row in status_rows
            if row.get("cell_id")
        }
        missing = [row for row in inventory if row["cell_id"] not in cells_with_status]
        if missing:
            event = self.append_event(
                "cell_status_initialization",
                cell_count=len(missing),
                reference_cell_id=REFERENCE_CELL_ID,
            )
            rows: list[dict[str, Any]] = []
            for inventory_row in missing:
                is_reference = inventory_row["cell_id"] == REFERENCE_CELL_ID
                rows.append(
                    {
                        "event_seq": event["event_seq"],
                        "event_id": event["event_id"],
                        "run_id": self.run_id,
                        "cell_id": inventory_row["cell_id"],
                        "event_time_utc": event["timestamp_utc"],
                        "status": "reference_complete" if is_reference else "pending",
                        "stage": "initialization",
                        "reason": (
                            "reference cell is excluded from hardware operations"
                            if is_reference
                            else "inventory initialized"
                        ),
                        "worker_pid": os.getpid(),
                    }
                )
            materialised = [self._normalise_row("cell_status_events", row) for row in rows]
            self._append_csv_materialised("cell_status_events", materialised)

        latest_reference = None
        for row in self._read_table_unlocked("cell_status_events"):
            if row.get("cell_id") == REFERENCE_CELL_ID:
                latest_reference = row
        if latest_reference is None or latest_reference.get("status") != "reference_complete":
            event = self.append_event(
                "reference_cell_completed",
                cell_id=REFERENCE_CELL_ID,
                reason="reference cell is excluded from hardware operations",
            )
            self.append_row(
                "cell_status_events",
                {
                    "event_seq": event["event_seq"],
                    "event_id": event["event_id"],
                    "run_id": self.run_id,
                    "cell_id": REFERENCE_CELL_ID,
                    "event_time_utc": event["timestamp_utc"],
                    "status": "reference_complete",
                    "stage": "initialization",
                    "reason": "reference cell is excluded from hardware operations",
                    "worker_pid": os.getpid(),
                },
            )

    def _default_progress(self) -> dict[str, Any]:
        latest_status = self._latest_status_by_cell()
        status_counts = Counter(row.get("status") or "unknown" for row in latest_status.values())
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_status": "ready",
            "phase": "initialized",
            "total_cells": CELL_COUNT,
            "hardware_cells": CELL_COUNT - 1,
            "reference_cells": 1,
            "reference_cell_id": REFERENCE_CELL_ID,
            "current_cell_id": None,
            "current_state_code": None,
            "last_event_seq": self._event_seq,
            "status_counts": dict(sorted(status_counts.items())),
            "created_at_utc": _utc_now(),
            "updated_at_utc": _utc_now(),
        }

    def _ensure_progress(self) -> None:
        defaults = self._default_progress()
        if not self.progress_path.exists():
            _atomic_write_json(self.progress_path, defaults)
            return
        existing = _read_json(self.progress_path)
        if existing.get("run_id") not in (None, self.run_id):
            raise StoreError("progress.json belongs to a different run")
        merged = deepcopy(existing)
        changed = False
        for key, value in defaults.items():
            if key not in merged:
                merged[key] = value
                changed = True
        if changed:
            _atomic_write_json(self.progress_path, merged)

    def read_manifest(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(_read_json(self.manifest_path))

    def update_manifest(self, **fields: Any) -> dict[str, Any]:
        """Atomically update descriptive manifest fields, never run identity."""

        forbidden = {"run_id", "schema_version", "array", "reference_cell", "target_bands", "data_policy"}
        attempted = forbidden.intersection(fields)
        if attempted:
            raise StoreError(f"Immutable manifest fields cannot be changed: {sorted(attempted)}")
        with self._lock:
            current = _read_json(self.manifest_path)
            updated = _deep_merge(current, fields)
            updated["updated_at_utc"] = _utc_now()
            _atomic_write_json(self.manifest_path, updated)
            self._manifest = deepcopy(updated)
            return deepcopy(updated)

    def read_progress(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(_read_json(self.progress_path))

    def update_progress(self, **fields: Any) -> dict[str, Any]:
        """Deep-merge and atomically replace the resumable progress checkpoint."""

        if "run_id" in fields and fields["run_id"] != self.run_id:
            raise StoreError("Cannot change progress.json run_id")
        with self._lock:
            current = _read_json(self.progress_path)
            updated = _deep_merge(current, fields)
            updated["schema_version"] = SCHEMA_VERSION
            updated["run_id"] = self.run_id
            updated["last_event_seq"] = max(
                _as_int(updated.get("last_event_seq")), self._event_seq
            )
            updated["updated_at_utc"] = _utc_now()
            _atomic_write_json(self.progress_path, updated)
            return deepcopy(updated)

    checkpoint = update_progress

    def _refresh_event_sequence(self) -> None:
        maximum = 0
        if self.events_path.exists():
            with self.events_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    maximum = max(maximum, _as_int(event.get("event_seq")))
            self._events_size = self.events_path.stat().st_size
        else:
            self._events_size = 0
        self._event_seq = maximum

    def append_event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """Append and fsync one canonical event-ledger entry."""

        event_type = str(event_type).strip()
        if not event_type:
            raise StoreError("event_type must not be empty")
        with self._lock:
            current_size = self.events_path.stat().st_size if self.events_path.exists() else 0
            if current_size != self._events_size:
                self._refresh_event_sequence()
                current_size = self._events_size
            self._event_seq += 1
            event: dict[str, Any] = {
                "event_seq": self._event_seq,
                "event_id": uuid.uuid4().hex,
                "event_type": event_type,
                "run_id": self.run_id,
                "timestamp_utc": _utc_now(),
            }
            reserved = set(event)
            event.update(
                {
                    str(key): _json_safe(value)
                    for key, value in fields.items()
                    if str(key) not in reserved
                }
            )
            line = json.dumps(event, separators=(",", ":"), sort_keys=True, allow_nan=False).encode(
                "utf-8"
            ) + b"\n"
            prefix = b""
            if current_size:
                with self.events_path.open("rb") as check:
                    check.seek(-1, os.SEEK_END)
                    if check.read(1) not in {b"\n", b"\r"}:
                        prefix = b"\n"
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(str(self.events_path), flags, 0o644)
            try:
                _write_all(fd, prefix + line)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._events_size = self.events_path.stat().st_size
            return deepcopy(event)

    def read_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events, _errors, _count = self._read_events_unlocked(limit)
            return events

    def _read_events_unlocked(
        self, limit: int | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative or None")
        target: list[dict[str, Any]] | deque[dict[str, Any]]
        target = deque(maxlen=limit) if limit is not None else []
        errors: list[dict[str, Any]] = []
        count = 0
        if not self.events_path.exists():
            return [], errors, count
        with self.events_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("event is not an object")
                except (json.JSONDecodeError, ValueError) as exc:
                    errors.append({"line_number": line_number, "error": str(exc)})
                    continue
                count += 1
                target.append(event)
        return list(target), errors, count

    def target_band(self, state_code: Any) -> dict[str, Any]:
        return deepcopy(_target_bands()[_parse_state_code(state_code)])

    def is_hardware_cell(self, cell_id: Any) -> bool:
        return _normalise_cell_id(cell_id) != REFERENCE_CELL_ID

    def cell_raw_dir(self, cell_id: Any, *, create: bool = True) -> Path:
        normalized = _normalise_cell_id(cell_id)
        path = self.cells_dir / normalized / "raw"
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def register_raw_file(
        self,
        path: str | Path,
        *,
        file_type: str,
        cell_id: str = "",
        attempt_id: str = "",
        raw_file_id: str | None = None,
        closed_cleanly: bool = True,
    ) -> dict[str, Any]:
        absolute = Path(path).expanduser().resolve()
        try:
            relative = absolute.relative_to(self.run_dir)
        except ValueError as exc:
            raise StoreError("Raw files must live inside the run directory") from exc
        digest = hashlib.sha256()
        with absolute.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        row = {
            "raw_file_id": raw_file_id or uuid.uuid4().hex,
            "run_id": self.run_id,
            "cell_id": cell_id,
            "attempt_id": attempt_id,
            "relative_path": relative.as_posix(),
            "file_type": file_type,
            "byte_count": absolute.stat().st_size,
            "sha256": digest.hexdigest(),
            "created_utc": _utc_now(),
            "closed_cleanly": closed_cleanly,
        }
        self.append_row("raw_files", row)
        return row

    def _latest_status_by_cell(self) -> dict[str, dict[str, str]]:
        latest: dict[str, dict[str, str]] = {}
        for row in self._read_table_unlocked("cell_status_events"):
            if row.get("cell_id"):
                latest[row["cell_id"]] = row
        return latest

    def state_occupancy_snapshot(self) -> dict[str, Any]:
        """Return latest-cell occupancy without treating attempts as cells.

        A cell occupies a state only when its *latest* durable state observation
        has a finite median and ``landed=1``.  Protected-reference cells are
        reported separately and never inflate hardware occupancy.
        """

        with self._lock:
            inventory = self._read_table_unlocked("cell_inventory")
            reference_by_cell = {
                row.get("cell_id", ""): _as_bool(row.get("skip_hardware"))
                for row in inventory
                if row.get("cell_id")
            }
            latest_by_cell: dict[str, dict[str, str]] = {}
            for row in self._read_table_unlocked("state_observations"):
                if row.get("cell_id"):
                    latest_by_cell[row["cell_id"]] = row

            states = [
                {
                    **deepcopy(band),
                    "latest_hardware_cell_count": 0,
                    "latest_reference_cell_count": 0,
                    "latest_hardware_cell_ids": [],
                    "latest_reference_cell_ids": [],
                }
                for band in _target_bands()
            ]
            unlanded_hardware: list[str] = []
            unlanded_reference: list[str] = []
            for cell_id, row in latest_by_cell.items():
                raw_code = row.get("assigned_state_code")
                if raw_code in (None, ""):
                    raw_code = row.get("target_state_code", "")
                try:
                    code = _parse_state_code(raw_code)
                except (SchemaError, ValueError):
                    code = None
                landed = (
                    code is not None
                    and row.get("landed") not in (None, "")
                    and _as_bool(row.get("landed"))
                    and _as_float(row.get("median_uS")) is not None
                )
                is_reference = reference_by_cell.get(cell_id, False)
                if not landed:
                    (unlanded_reference if is_reference else unlanded_hardware).append(cell_id)
                    continue
                bucket = states[code]
                prefix = "latest_reference" if is_reference else "latest_hardware"
                bucket[f"{prefix}_cell_count"] += 1
                bucket[f"{prefix}_cell_ids"].append(cell_id)

            for state in states:
                state["latest_hardware_cell_ids"].sort()
                state["latest_reference_cell_ids"].sort()
            unlanded_hardware.sort()
            unlanded_reference.sort()
            return {
                "basis": "latest finite state_observation per cell with landed=1",
                "reference_policy": "protected reference reported separately",
                "states": states,
                "latest_hardware_cell_count": sum(
                    state["latest_hardware_cell_count"] for state in states
                ),
                "latest_reference_cell_count": sum(
                    state["latest_reference_cell_count"] for state in states
                ),
                "observed_hardware_cell_count": sum(
                    not reference_by_cell.get(cell_id, False) for cell_id in latest_by_cell
                ),
                "observed_reference_cell_count": sum(
                    reference_by_cell.get(cell_id, False) for cell_id in latest_by_cell
                ),
                "latest_unlanded_hardware_cell_count": len(unlanded_hardware),
                "latest_unlanded_reference_cell_count": len(unlanded_reference),
                "latest_unlanded_hardware_cell_ids": unlanded_hardware,
                "latest_unlanded_reference_cell_ids": unlanded_reference,
            }

    def distribution_snapshot(self) -> list[dict[str, Any]]:
        """Return landed attempt distributions with explicit cell occupancy.

        Distribution curves use finite, landed hardware state-observation
        medians.  They never fall back to context-tagged scan reads: an empty
        assigned bucket is therefore returned as an explicit zero.  Reference
        observations and latest-cell occupancy remain available as separate
        fields so the GUI cannot confuse attempts, cells, and protected data.
        """

        with self._lock:
            observations = self._read_table_unlocked("state_observations")
            reference_by_cell = {
                row.get("cell_id", ""): _as_bool(row.get("skip_hardware"))
                for row in self._read_table_unlocked("cell_inventory")
                if row.get("cell_id")
            }
            observation_by_code: dict[int, list[dict[str, str]]] = defaultdict(list)
            for row in observations:
                raw_code = row.get("assigned_state_code")
                if raw_code in (None, ""):
                    raw_code = row.get("target_state_code", "")
                try:
                    code = _parse_state_code(raw_code)
                except (SchemaError, ValueError):
                    continue
                observation_by_code[code].append(row)

            occupancy = self.state_occupancy_snapshot()
            occupancy_by_code = {
                state["state_code"]: state for state in occupancy["states"]
            }
            result: list[dict[str, Any]] = []
            for band in _target_bands():
                code = band["state_code"]
                points: list[dict[str, Any]] = []
                hardware_values: list[float] = []
                reference_values: list[float] = []
                for row in observation_by_code[code]:
                    raw_value = row.get("median_uS", "")
                    numeric = _as_float(raw_value)
                    landed = (
                        row.get("landed") not in (None, "")
                        and _as_bool(row.get("landed"))
                    )
                    is_reference = reference_by_cell.get(row.get("cell_id", ""), False)
                    included = numeric is not None and landed and not is_reference
                    if included:
                        hardware_values.append(numeric)
                    elif numeric is not None and landed and is_reference:
                        reference_values.append(numeric)
                    points.append(
                        {
                            "cell_id": row.get("cell_id", ""),
                            "attempt_id": row.get("attempt_id", ""),
                            "observation_id": row.get("observation_id", ""),
                            "phase": row.get("observation_phase", ""),
                            "conductance_uS": numeric,
                            "raw_conductance_uS": raw_value,
                            "landed": landed,
                            "is_reference": is_reference,
                            "included_in_stats": included,
                        }
                    )
                state_occupancy = occupancy_by_code[code]
                stats = {
                    "count_total": len(points),
                    "count_numeric": len(hardware_values),
                    "count_nonfinite": sum(
                        point["raw_conductance_uS"] not in (None, "")
                        and point["conductance_uS"] is None
                        for point in points
                    ),
                    "mean_uS": statistics.fmean(hardware_values) if hardware_values else None,
                    "median_uS": statistics.median(hardware_values) if hardware_values else None,
                    "stddev_uS": (
                        statistics.pstdev(hardware_values)
                        if len(hardware_values) > 1
                        else 0.0 if hardware_values else None
                    ),
                    "min_uS": min(hardware_values) if hardware_values else None,
                    "max_uS": max(hardware_values) if hardware_values else None,
                }
                result.append(
                    {
                        **deepcopy(band),
                        "source": "landed_state_observations",
                        "density_normalization": "unit area per non-empty state",
                        "no_outlier_filter": True,
                        **stats,
                        "all_observation_count": len(points),
                        "hardware_landed_observation_count": len(hardware_values),
                        "reference_landed_observation_count": len(reference_values),
                        "unlanded_observation_count": sum(
                            not point["landed"] for point in points
                        ),
                        "latest_hardware_cell_count": state_occupancy[
                            "latest_hardware_cell_count"
                        ],
                        "latest_reference_cell_count": state_occupancy[
                            "latest_reference_cell_count"
                        ],
                        "latest_hardware_cell_ids": state_occupancy[
                            "latest_hardware_cell_ids"
                        ],
                        "latest_reference_cell_ids": state_occupancy[
                            "latest_reference_cell_ids"
                        ],
                        "values_uS": hardware_values,
                        "reference_values_uS": reference_values,
                        "points": points,
                    }
                )
            return result

    def drift_snapshot(self) -> list[dict[str, Any]]:
        """Return post-write reads even when legacy logs lack elapsed time."""

        with self._lock:
            points: list[dict[str, Any]] = []
            for row in self._read_table_unlocked("scan_reads"):
                role = str(row.get("read_role", "")).lower()
                is_post_program = role in {"post_program", "post_write", "post_program_reference"}
                has_elapsed = row.get("elapsed_after_write_ms") not in (None, "")
                if not has_elapsed and not is_post_program:
                    continue
                try:
                    code = _parse_state_code(row.get("state_code_context", ""))
                except (SchemaError, ValueError):
                    code = None
                raw_value = row.get("conductance_uS", "")
                value = _as_float(raw_value)
                points.append(
                    {
                        "cell_id": row.get("cell_id", ""),
                        "attempt_id": row.get("attempt_id", ""),
                        "read_id": row.get("read_id", ""),
                        "read_role": row.get("read_role", ""),
                        "read_index": (
                            _as_int(row.get("read_index"))
                            if row.get("read_index") not in (None, "")
                            else None
                        ),
                        "state_code": code,
                        "state_hex": format(code, "X") if code is not None else None,
                        "elapsed_after_write_ms": _as_float(row.get("elapsed_after_write_ms")),
                        "conductance_uS": value,
                        "raw_conductance_uS": raw_value,
                        "current_uA": _as_float(row.get("current_uA")),
                        "timestamp_utc": row.get("timestamp_utc", ""),
                        "qc_flags": row.get("qc_flags", ""),
                        "included_in_stats": value is not None,
                    }
                )
            return points

    def cell_snapshot(self, cell_id: Any) -> dict[str, Any]:
        normalized = _normalise_cell_id(cell_id)
        with self._lock:
            inventory = next(
                (
                    row
                    for row in self._read_table_unlocked("cell_inventory")
                    if row.get("cell_id") == normalized
                ),
                None,
            )
            if inventory is None:
                raise StoreError(f"Cell is not in inventory: {normalized}")
            status_events = [
                row
                for row in self._read_table_unlocked("cell_status_events")
                if row.get("cell_id") == normalized
            ]
            attempts = [
                row
                for row in self._read_table_unlocked("program_attempts")
                if row.get("cell_id") == normalized
            ]
            reads = [
                row
                for row in self._read_table_unlocked("scan_reads")
                if row.get("cell_id") == normalized
            ]
            observations = [
                row
                for row in self._read_table_unlocked("state_observations")
                if row.get("cell_id") == normalized
            ]
            return {
                "inventory": inventory,
                "status": status_events[-1].get("status") if status_events else inventory["initial_status"],
                "status_events": status_events,
                "program_attempts": attempts,
                "scan_reads": reads,
                "state_observations": observations,
                "skip_hardware": _as_bool(inventory.get("skip_hardware")),
            }

    def snapshot(self) -> dict[str, Any]:
        """Build the live GUI snapshot entirely from durable append-only data."""

        with self._lock:
            manifest = _read_json(self.manifest_path)
            progress = _read_json(self.progress_path)
            inventory = self._read_table_unlocked("cell_inventory")
            statuses = self._latest_status_by_cell()
            attempts = self._read_table_unlocked("program_attempts")
            reads = self._read_table_unlocked("scan_reads")
            observations = self._read_table_unlocked("state_observations")
            errors = self._read_table_unlocked("errors")
            tdc_words = self._read_table_unlocked("tdc_words")
            shunt_metrics = self._read_table_unlocked("shunt_metrics")
            events, ledger_errors, event_count = self._read_events_unlocked(limit=200)

            attempts_by_cell = Counter(row.get("cell_id", "") for row in attempts)
            reads_by_cell = Counter(row.get("cell_id", "") for row in reads)
            errors_by_cell = Counter(row.get("cell_id", "") for row in errors)
            completed_states: dict[str, set[int]] = defaultdict(set)
            for row in observations:
                if row.get("landed") not in ("", None) and not _as_bool(row.get("landed")):
                    continue
                try:
                    state = _parse_state_code(
                        row.get("assigned_state_code") or row.get("target_state_code", "")
                    )
                except (SchemaError, ValueError):
                    continue
                completed_states[row.get("cell_id", "")].add(state)

            latest_read: dict[str, dict[str, str]] = {}
            for row in reads:
                if row.get("cell_id"):
                    latest_read[row["cell_id"]] = row

            cells: list[dict[str, Any]] = []
            for inventory_row in inventory:
                cell_id = inventory_row["cell_id"]
                status_row = statuses.get(cell_id, {})
                read_row = latest_read.get(cell_id, {})
                states = sorted(completed_states.get(cell_id, set()))
                cells.append(
                    {
                        "cell_id": cell_id,
                        "row": _as_int(inventory_row.get("rtl_row")),
                        "column": _as_int(inventory_row.get("rtl_column")),
                        "array_index": _as_int(inventory_row.get("array_index")),
                        "skip_hardware": _as_bool(inventory_row.get("skip_hardware")),
                        "status": status_row.get("status") or inventory_row.get("initial_status"),
                        "stage": status_row.get("stage", ""),
                        "target_state_code": (
                            _as_int(status_row["target_state_code"])
                            if status_row.get("target_state_code") not in (None, "")
                            else None
                        ),
                        "last_attempt_id": status_row.get("last_attempt_id", ""),
                        "attempt_count": attempts_by_cell[cell_id],
                        "read_count": reads_by_cell[cell_id],
                        "error_count": errors_by_cell[cell_id],
                        "completed_state_codes": states,
                        "completed_state_count": len(states),
                        "latest_conductance_uS": _as_float(read_row.get("conductance_uS")),
                        "latest_current_uA": _as_float(read_row.get("current_uA")),
                        "last_read_time_utc": read_row.get("timestamp_utc", ""),
                        "last_status_time_utc": status_row.get("event_time_utc", ""),
                    }
                )

            status_counts = Counter(cell["status"] or "unknown" for cell in cells)
            table_counts = {
                name: len(self._read_table_unlocked(name)) for name in TABLE_SCHEMAS
            }
            state_occupancy = self.state_occupancy_snapshot()
            distributions = self.distribution_snapshot()
            drift = self.drift_snapshot()
            nonzero_tdc = sum(_as_bool(row.get("is_nonzero")) for row in tdc_words)
            analog_confirmed = sum(_as_bool(row.get("analog_confirmed")) for row in shunt_metrics)
            summary = {
                "total_cells": len(cells),
                "hardware_cells": sum(not cell["skip_hardware"] for cell in cells),
                "reference_cells": sum(cell["skip_hardware"] for cell in cells),
                "status_counts": dict(sorted(status_counts.items())),
                "cells_with_attempts": sum(cell["attempt_count"] > 0 for cell in cells),
                "cells_with_reads": sum(cell["read_count"] > 0 for cell in cells),
                "cells_with_all_16_states": sum(cell["completed_state_count"] == 16 for cell in cells),
                "program_attempt_count": len(attempts),
                "scan_read_count": len(reads),
                "state_observation_count": len(observations),
                "error_count": len(errors),
                "tdc_word_count": len(tdc_words),
                "nonzero_tdc_word_count": nonzero_tdc,
                "shunt_metric_count": len(shunt_metrics),
                "analog_confirmed_count": analog_confirmed,
                "event_count": event_count,
            }
            return {
                "schema_version": SCHEMA_VERSION,
                "generated_at_utc": _utc_now(),
                "run_id": self.run_id,
                "run_dir": str(self.run_dir),
                "manifest": manifest,
                "progress": progress,
                "summary": summary,
                "table_counts": table_counts,
                "target_bands": deepcopy(manifest["target_bands"]),
                "cells": cells,
                "state_occupancy": state_occupancy,
                "state_distributions": distributions,
                "distributions": distributions,
                "drift_points": drift,
                "drift": drift,
                "recent_program_attempts": attempts[-100:],
                "recent_errors": errors[-100:],
                "recent_events": events,
                "ledger_parse_errors": ledger_errors,
                "data_policy": {
                    "no_outlier_filter": True,
                    "raw_points_retained": True,
                    "raw_csv_append_only": True,
                },
            }

    get_snapshot = snapshot
    build_snapshot = snapshot

    def append_cell_status(self, **row: Any) -> bool:
        return self.append_row("cell_status_events", row)

    def append_program_attempt(self, **row: Any) -> bool:
        return self.append_row("program_attempts", row)

    def append_wb_packet_issue(self, **row: Any) -> bool:
        return self.append_row("wb_packet_issues", row)

    def append_tdc_word(self, **row: Any) -> bool:
        return self.append_row("tdc_words", row)

    def append_scan_read(self, **row: Any) -> bool:
        return self.append_row("scan_reads", row)

    def append_sentinel_read(self, **row: Any) -> bool:
        return self.append_row("sentinel_reads", row)

    def append_shunt_metric(self, **row: Any) -> bool:
        return self.append_row("shunt_metrics", row)

    def append_state_observation(self, **row: Any) -> bool:
        return self.append_row("state_observations", row)

    def append_error(self, **row: Any) -> bool:
        return self.append_row("errors", row)

    def append_operator_annotation(self, **row: Any) -> bool:
        return self.append_row("operator_annotations", row)


__all__ = [
    "ARRAY_COLUMNS",
    "ARRAY_ROWS",
    "CELL_COUNT",
    "IdempotencyConflictError",
    "REFERENCE_CELL_ID",
    "RunStore",
    "SCHEMA_VERSION",
    "SchemaError",
    "StoreError",
    "TABLE_SCHEMAS",
]
