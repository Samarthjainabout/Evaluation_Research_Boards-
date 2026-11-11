from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config import GLOBAL_HARDWARE_LOCK  # noqa: E402
from parser import decode_cell_result  # noqa: E402
from runner import HardwareLock, VivadoRunner  # noqa: E402


PROGRAM_MARKER = re.compile(
    r"^(?:PWM_DOE_COMPLETE|PWM_BATCH_CELL_START|WB_(?:SET|RESET|SHUNT)|"
    r"RAMP_COMPLETE\s+(?:set|reset)|SCAN_SET_FORM_RESULT|SCAN_RESET_SENTINEL_GATE)\b"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prove the independent WB-PWM SET rail profile without issuing a program packet."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--row", type=int, default=7)
    parser.add_argument("--column", type=int, default=1)
    parser.add_argument("--vcc-set-v", type=float, default=3.0)
    parser.add_argument("--vcc-wl-v", type=float, default=2.0)
    args = parser.parse_args()

    if not (0 <= args.row < 32 and 0 <= args.column < 32):
        raise RuntimeError("profile-check cell must be within the 32x32 array")
    if not (0.0 <= args.vcc_set_v <= 3.0 and 0.0 <= args.vcc_wl_v <= 2.0):
        raise RuntimeError("WB-PWM profile check requires VCC_SET <=3.0 V and VCC_WL <=2.0 V")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output_dir}")

    job_id = uuid.uuid4().hex
    observed_lines: list[tuple[str, str, int]] = []

    def on_line(text: str, timestamp_utc: str, line_number: int) -> None:
        observed_lines.append((text, timestamp_utc, line_number))

    with HardwareLock(GLOBAL_HARDWARE_LOCK, f"readonly-pwm-skew-check-{job_id}"):
        return_code, vivado_log, console_log, elapsed_seconds = VivadoRunner(None).run(
            job_id,
            [
                "pwm_skew_profile_check",
                str(args.row),
                str(args.column),
                f"{args.vcc_set_v:g}",
                f"{args.vcc_wl_v:g}",
                "0",
                "0",
                "2400",
                "1",
                job_id,
            ],
            output_dir,
            on_line,
        )

    if return_code != 0:
        raise RuntimeError(f"read-only PWM skew profile check failed with exit code {return_code}")

    text_lines = [text for text, _, _ in observed_lines]
    completion = (
        f"PWM_SKEW_PROFILE_CHECK_COMPLETE set_v={args.vcc_set_v:g} "
        f"wl_v={args.vcc_wl_v:g} program_packets=0"
    )
    if sum(text == completion for text in text_lines) != 1:
        raise RuntimeError("PWM skew profile check lacks its exact completion marker")
    if sum(text == "JOB_COMPLETE pwm_skew_profile_check" for text in text_lines) != 1:
        raise RuntimeError("PWM skew profile check lacks exactly one job-complete marker")
    program_markers = [text for text in text_lines if PROGRAM_MARKER.match(text)]
    if program_markers:
        raise RuntimeError(f"programming marker found in read-only profile check: {program_markers[:3]!r}")

    decoded = []
    for text, timestamp_utc, line_number in observed_lines:
        if text.startswith("CELL_RESULT ") and " 2400 " in text:
            item = decode_cell_result(text, timestamp_utc, line_number)
            if item["read_clipped"] or item["qc_flags"]:
                raise RuntimeError(
                    f"invalid scan restoration read at r{item['row']}/c{item['column']}: {item['qc_flags']}"
                )
            decoded.append(item)
    if len(decoded) != 5:
        raise RuntimeError(f"expected five restoration reads, observed {len(decoded)}")

    summary = {
        "schema": "readonly-wb-pwm-skew-profile-check-v1",
        "job_id": job_id,
        "target": {"row": args.row, "column": args.column},
        "vcc_set_V": args.vcc_set_v,
        "vcc_wl_V": args.vcc_wl_v,
        "program_packets": 0,
        "restoration_read_count": len(decoded),
        "job_complete": True,
        "elapsed_seconds": elapsed_seconds,
        "vivado_log": vivado_log.name,
        "vivado_log_sha256": sha256(vivado_log),
        "console_log": console_log.name,
        "console_log_sha256": sha256(console_log),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
