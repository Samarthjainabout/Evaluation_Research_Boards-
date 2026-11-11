from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config import GLOBAL_HARDWARE_LOCK  # noqa: E402
from parser import decode_cell_result  # noqa: E402
from runner import HardwareLock, VivadoRunner, utc_now  # noqa: E402


PROGRAM_MARKER = re.compile(
    r"^(?:PWM_DOE_COMPLETE|PWM_BATCH_CELL_START|WB_(?:SET|RESET|SHUNT)|"
    r"RAMP_COMPLETE\s+(?:set|reset)|SCAN_SET_FORM_RESULT|SCAN_RESET_SENTINEL_GATE)\b"
)
FIELDS = (
    "row",
    "column",
    "conductance_uS",
    "current_uA",
    "read_clipped",
    "qc_flags",
    "raw_h0_hex",
    "raw_h1_hex",
    "source_line",
    "timestamp_utc",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_reads(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda item: (int(item["row"]), int(item["column"]))))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_reads(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def run_columns(
    output_dir: Path, start_column: int, column_count: int, attempt: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    end_column = start_column + column_count - 1
    if column_count < 1 or column_count > 10 or end_column > 31:
        raise RuntimeError("invalid prescreen column batch")
    job_id = uuid.uuid4().hex
    raw_dir = (
        output_dir
        / "batches"
        / f"c{start_column:02d}_c{end_column:02d}"
        / f"attempt_{attempt:02d}"
    )
    observed: list[tuple[str, str, int]] = []

    def on_line(text: str, timestamp_utc: str, line_number: int) -> None:
        observed.append((text, timestamp_utc, line_number))

    return_code, vivado_log, console_log, elapsed = VivadoRunner(None).run(
        job_id,
        [
            "columns",
            "0",
            str(start_column),
            "0.5",
            "2.5",
            "3.0",
            "3.0",
            "2400",
            str(column_count),
            job_id,
        ],
        raw_dir,
        on_line,
    )
    text_lines = [text for text, _, _ in observed]
    evidence = {
        "attempt": attempt,
        "job_id": job_id,
        "return_code": return_code,
        "elapsed_seconds": elapsed,
        "vivado_log": str(vivado_log.relative_to(output_dir)),
        "vivado_log_sha256": sha256(vivado_log),
        "console_log": str(console_log.relative_to(output_dir)),
        "console_log_sha256": sha256(console_log),
        "job_complete": sum(text == "JOB_COMPLETE columns" for text in text_lines) == 1,
        "program_markers": [text for text in text_lines if PROGRAM_MARKER.match(text)],
        "start_column": start_column,
        "end_column": end_column,
        "column_count": column_count,
    }
    if return_code != 0 or not evidence["job_complete"]:
        raise RuntimeError(json.dumps(evidence, sort_keys=True))
    if evidence["program_markers"]:
        raise RuntimeError(
            "programming marker found in read-only column batch "
            f"{start_column}..{end_column}: {evidence['program_markers'][:3]!r}"
        )

    decoded: list[dict[str, Any]] = []
    for text, timestamp_utc, line_number in observed:
        if text.startswith("CELL_RESULT ") and " read 2400 " in text:
            item = decode_cell_result(text, timestamp_utc, line_number)
            if item["read_clipped"] or item["qc_flags"]:
                raise RuntimeError(
                    f"invalid scan read at r{item['row']}/c{item['column']}: {item['qc_flags']}"
                )
            decoded.append(item)
    coordinates = {(int(item["row"]), int(item["column"])) for item in decoded}
    expected = {
        (row, column)
        for column in range(start_column, end_column + 1)
        for row in range(32)
    }
    if len(decoded) != 32 * column_count or coordinates != expected:
        raise RuntimeError(
            f"columns {start_column}..{end_column} incomplete: "
            f"rows={len(decoded)} unique={len(coordinates)} "
            f"missing={sorted(expected - coordinates)!r}"
        )
    evidence["read_count"] = len(decoded)
    return decoded, evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a resumable, read-only 32x32 prescreen by column.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--forming-threshold-us", type=float, default=20.0)
    parser.add_argument("--max-attempts-per-column", type=int, default=3)
    parser.add_argument("--columns-per-job", type=int, default=4)
    parser.add_argument("--retry-delay-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if args.max_attempts_per_column < 1:
        raise RuntimeError("max attempts per column must be positive")
    if not 1 <= args.columns_per_job <= 10:
        raise RuntimeError("columns per job must be within 1..10")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    reads_path = output_dir / "prescreen_reads.partial.csv"
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if float(checkpoint["forming_threshold_uS"]) != float(args.forming_threshold_us):
            raise RuntimeError("forming threshold does not match the existing prescreen")
    else:
        if any(output_dir.iterdir()):
            raise RuntimeError(f"new output directory must be empty: {output_dir}")
        checkpoint = {
            "schema": "resumable-full-array-readonly-prescreen-v1",
            "created_utc": utc_now(),
            "forming_threshold_uS": float(args.forming_threshold_us),
            "completed_columns": [],
            "column_evidence": {},
            "status": "initialized",
        }
        atomic_json(checkpoint_path, checkpoint)

    rows = load_reads(reads_path)
    completed = {int(value) for value in checkpoint.get("completed_columns", [])}
    row_coordinates = {(int(item["row"]), int(item["column"])) for item in rows}
    expected_existing = {(row, column) for column in completed for row in range(32)}
    if len(rows) != len(row_coordinates) or row_coordinates != expected_existing:
        raise RuntimeError("partial prescreen CSV conflicts with the checkpoint")

    checkpoint.update(status="running", updated_utc=utc_now())
    atomic_json(checkpoint_path, checkpoint)
    with HardwareLock(GLOBAL_HARDWARE_LOCK, f"resumable-prescreen-{uuid.uuid4().hex}"):
        while len(completed) < 32:
            start_column = next(column for column in range(32) if column not in completed)
            column_count = 1
            while (
                column_count < args.columns_per_job
                and start_column + column_count < 32
                and start_column + column_count not in completed
            ):
                column_count += 1
            end_column = start_column + column_count - 1
            if any(column in completed for column in range(start_column, end_column + 1)):
                continue
            failures: list[str] = []
            batch_dir = output_dir / "batches" / f"c{start_column:02d}_c{end_column:02d}"
            existing_attempts = len(list(batch_dir.glob("attempt_*")))
            for local_attempt in range(1, args.max_attempts_per_column + 1):
                attempt = existing_attempts + local_attempt
                try:
                    batch_rows, evidence = run_columns(
                        output_dir, start_column, column_count, attempt
                    )
                    rows.extend(batch_rows)
                    write_reads(reads_path, rows)
                    for column in range(start_column, end_column + 1):
                        completed.add(column)
                        checkpoint["column_evidence"][str(column)] = {
                            **evidence,
                            "column": column,
                            "shared_batch_evidence": True,
                        }
                    checkpoint["completed_columns"] = sorted(completed)
                    checkpoint["last_completed_column"] = end_column
                    checkpoint["updated_utc"] = utc_now()
                    atomic_json(checkpoint_path, checkpoint)
                    print(
                        f"columns={start_column}..{end_column} complete "
                        f"columns_done={len(completed)}/32",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    failures.append(f"{type(exc).__name__}: {exc}")
                    checkpoint["last_failure"] = {
                        "start_column": start_column,
                        "end_column": end_column,
                        "attempt": attempt,
                        "error": failures[-1],
                        "time_utc": utc_now(),
                    }
                    atomic_json(checkpoint_path, checkpoint)
                    if local_attempt < args.max_attempts_per_column:
                        time.sleep(args.retry_delay_seconds)
            else:
                checkpoint.update(status="blocked", updated_utc=utc_now())
                atomic_json(checkpoint_path, checkpoint)
                raise RuntimeError(
                    f"columns {start_column}..{end_column} failed all attempts: {failures!r}"
                )

    if len(rows) != 1024:
        raise RuntimeError(f"prescreen ended with {len(rows)} rows instead of 1024")
    final_path = output_dir / "prescreen_reads.csv"
    write_reads(final_path, rows)
    threshold = float(args.forming_threshold_us)
    conductance = [float(item["conductance_uS"]) for item in rows]
    summary = {
        "schema": "resumable-full-array-readonly-prescreen-v1",
        "cell_count": 1024,
        "unique_cell_count": 1024,
        "forming_threshold_uS": threshold,
        "unformed_count": sum(value < threshold for value in conductance),
        "formed_count": sum(value >= threshold for value in conductance),
        "minimum_conductance_uS": min(conductance),
        "maximum_conductance_uS": max(conductance),
        "program_markers": 0,
        "job_complete": True,
        "prescreen_csv": final_path.name,
        "prescreen_csv_sha256": sha256(final_path),
        "completed_utc": utc_now(),
    }
    atomic_json(output_dir / "summary.json", summary)
    checkpoint.update(status="complete", updated_utc=utc_now())
    atomic_json(checkpoint_path, checkpoint)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
