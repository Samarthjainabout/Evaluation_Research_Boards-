from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue the PWM-first population run behind a validated full-array prescreen."
    )
    parser.add_argument("--prescreen-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile-check-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args()

    prescreen_dir = args.prescreen_dir.resolve()
    output_dir = args.output_dir.resolve()
    profile_check_dir = args.profile_check_dir.resolve() if args.profile_check_dir else None
    state_path = output_dir.parent / f"{output_dir.name}_queue_state.json"
    log_path = output_dir.parent / f"{output_dir.name}_launcher.log"
    summary_path = prescreen_dir / "summary.json"
    prescreen_csv = prescreen_dir / "prescreen_reads.csv"
    deadline = time.monotonic() + args.timeout_seconds
    atomic_json(
        state_path,
        {
            "status": "waiting_for_prescreen",
            "prescreen_dir": str(prescreen_dir),
            "output_dir": str(output_dir),
            "updated_utc": utc_now(),
        },
    )

    while not summary_path.is_file():
        if time.monotonic() >= deadline:
            atomic_json(
                state_path,
                {
                    "status": "failed",
                    "reason": "prescreen summary was not produced before timeout",
                    "updated_utc": utc_now(),
                },
            )
            raise TimeoutError("prescreen summary timeout")
        time.sleep(5)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if (
        not summary.get("job_complete")
        or int(summary.get("cell_count", 0)) != 1024
        or int(summary.get("unique_cell_count", 0)) != 1024
        or int(summary.get("program_markers", -1)) != 0
        or not prescreen_csv.is_file()
    ):
        atomic_json(
            state_path,
            {
                "status": "failed",
                "reason": "prescreen summary did not pass the 1024-cell read-only gate",
                "prescreen_summary": summary,
                "updated_utc": utc_now(),
            },
        )
        raise RuntimeError("prescreen validation failed")
    if output_dir.exists() and any(output_dir.iterdir()):
        atomic_json(
            state_path,
            {
                "status": "failed",
                "reason": "phase-1 output directory already contains files",
                "updated_utc": utc_now(),
            },
        )
        raise RuntimeError(f"phase-1 output directory is not empty: {output_dir}")

    if profile_check_dir is not None:
        if profile_check_dir.exists() and any(profile_check_dir.iterdir()):
            raise RuntimeError(f"profile-check output directory is not empty: {profile_check_dir}")
        profile_command = [
            sys.executable,
            str(Path(__file__).with_name("run_pwm_skew_profile_check.py")),
            "--output-dir",
            str(profile_check_dir),
            "--row",
            "7",
            "--column",
            "1",
            "--vcc-set-v",
            "3.0",
            "--vcc-wl-v",
            "2.0",
        ]
        atomic_json(
            state_path,
            {
                "status": "running_fast_profile_check",
                "prescreen_summary": summary,
                "command": profile_command,
                "updated_utc": utc_now(),
            },
        )
        profile_result = subprocess.run(
            profile_command,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        with log_path.open("a", encoding="utf-8", newline="\n") as log:
            log.write(profile_result.stdout)
            log.flush()
        profile_summary_path = profile_check_dir / "summary.json"
        profile_summary = (
            json.loads(profile_summary_path.read_text(encoding="utf-8"))
            if profile_summary_path.is_file()
            else {}
        )
        if (
            profile_result.returncode != 0
            or not profile_summary.get("job_complete")
            or int(profile_summary.get("program_packets", -1)) != 0
            or int(profile_summary.get("restoration_read_count", 0)) != 5
        ):
            atomic_json(
                state_path,
                {
                    "status": "failed",
                    "reason": "optimized WB-PWM profile check did not pass",
                    "return_code": profile_result.returncode,
                    "profile_summary": profile_summary,
                    "updated_utc": utc_now(),
                },
            )
            raise RuntimeError("optimized WB-PWM profile check failed")

    command = [
        sys.executable,
        str(Path(__file__).with_name("run_pwm_first_population.py")),
        "--prescreen-csv",
        str(prescreen_csv),
        "--output-dir",
        str(output_dir),
        "--forming-threshold-us",
        "20",
        "--set-voltage-v",
        "1.65",
        "--seed",
        "pwm-then-split-v2",
    ]
    atomic_json(
        state_path,
        {
            "status": "launching_pwm_population",
            "prescreen_summary": summary,
            "command": command,
            "updated_utc": utc_now(),
        },
    )
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write(f"{utc_now()} launching: {command!r}\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        atomic_json(
            state_path,
            {
                "status": "pwm_population_running",
                "pid": process.pid,
                "prescreen_summary": summary,
                "updated_utc": utc_now(),
            },
        )
        return_code = process.wait()
        log.write(f"{utc_now()} phase-1 process exit code: {return_code}\n")
        log.flush()

    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_path.is_file()
        else {}
    )
    final_status = checkpoint.get("status", "unknown")
    atomic_json(
        state_path,
        {
            "status": "phase1_process_exited",
            "return_code": return_code,
            "phase1_status": final_status,
            "updated_utc": utc_now(),
        },
    )
    if return_code != 0:
        raise RuntimeError(f"phase-1 process exited with code {return_code}")


if __name__ == "__main__":
    main()
