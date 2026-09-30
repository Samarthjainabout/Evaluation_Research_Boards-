#!/usr/bin/env python3
"""Verify the frozen publication baseline without touching hardware."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = Path(__file__).with_name("baseline_v1.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bitstream_structure(path: Path) -> dict[str, object]:
    """Reject truncated .bit files even if a saved hash matches them."""
    data = path.read_bytes()
    position = 0

    def take(size: int) -> bytes:
        nonlocal position
        if position + size > len(data):
            raise ValueError("Truncated bitstream header")
        value = data[position:position + size]
        position += size
        return value

    try:
        magic_size = int.from_bytes(take(2), "big")
        if take(magic_size) != bytes.fromhex("0ff00ff00ff00ff000"):
            raise ValueError("Invalid .bit header magic")
        if take(2) != b"\x00\x01":
            raise ValueError("Invalid .bit header marker")
        for tag in b"abcd":
            if take(1) != bytes([tag]):
                raise ValueError("Unexpected .bit header field")
            take(int.from_bytes(take(2), "big"))
        if take(1) != b"e":
            raise ValueError("Missing .bit payload field")
        declared = int.from_bytes(take(4), "big")
        actual = len(data) - position
        if declared != actual:
            raise ValueError(f"Bitstream payload length mismatch: declared {declared}, actual {actual}")
        payload = data[position:]
        sync = payload.find(bytes.fromhex("aa995566"))
        if sync < 0 or (len(payload) - sync) % 4:
            raise ValueError("Missing sync word or misaligned configuration words")
        return {"ok": True, "payload_bytes": actual, "file_bytes": len(data)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "file_bytes": len(data)}


def verify_artifacts(baseline: dict[str, object]) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for artifact in baseline["artifacts"]:  # type: ignore[index]
        item = dict(artifact)
        path = ROOT / str(item["path"])
        expected = str(item["sha256"]).lower()
        actual = sha256(path) if path.is_file() else None
        structure = verify_bitstream_structure(path) if path.is_file() and path.suffix == ".bit" else None
        results.append({
            "role": item["role"],
            "path": item["path"],
            "expected_sha256": expected,
            "actual_sha256": actual,
            "structure": structure,
            "ok": actual == expected and (structure is None or structure["ok"]),
        })
    return results


def verify_api_defaults(baseline: dict[str, object]) -> list[dict[str, object]]:
    sys.path.insert(0, str(ROOT))
    from api_v1.cell_api import (  # pylint: disable=import-outside-toplevel
        FPGA_RUNTIME_BITSTREAM,
        FPGA_RUNTIME_PROBES,
        WB_BIAS_SKEWS,
        ScanDebugConfig,
    )

    artifacts = {item["role"]: Path(item["path"]).name for item in baseline["artifacts"]}  # type: ignore[index]
    fixed = {item["signal"].lower(): item["commanded_v"] for item in baseline["fixed_rails"]}  # type: ignore[index]
    config = ScanDebugConfig()
    checks = {
        "fpga_bitstream": (FPGA_RUNTIME_BITSTREAM, artifacts["fpga_bitstream"]),
        "fpga_debug_probes": (FPGA_RUNTIME_PROBES, artifacts["fpga_debug_probes"]),
        "read_vcc": (config.read_rails.vcc_set_v, fixed["vcc_read"]),
        "read_wordline": (config.read_rails.vcc_wl_set_v, fixed["vcc_wl_read"]),
        "shunt_ohms": (config.shunt_ohms, baseline["readout"]["shunt_ohms"]),  # type: ignore[index]
        "iref": (WB_BIAS_SKEWS["iref"]["nominal_v"], fixed["iref"]),
        "vcomp": (WB_BIAS_SKEWS["vcomp"]["nominal_v"], fixed["vcomp"]),
        "bias_comp2": (WB_BIAS_SKEWS["bias_comp2"]["nominal_v"], fixed["bias_comp2"]),
        "vbias": (WB_BIAS_SKEWS["vbias"]["nominal_v"], fixed["vbias"]),
        "dc_bias": (WB_BIAS_SKEWS["dc_bias"]["nominal_v"], fixed["dc_bias"]),
    }
    return [
        {"name": name, "actual": actual, "expected": expected, "ok": actual == expected}
        for name, (actual, expected) in checks.items()
    ]


def verify_rail_measurements(baseline: dict[str, object]) -> list[dict[str, object]]:
    path = ROOT / baseline["measurement_requirements"]["rail_measurements_csv"]  # type: ignore[index]
    results: list[dict[str, object]] = []
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            measured_text = row["measured_v"].strip()
            if not measured_text:
                results.append({"dac": int(row["dac"]), "signal": row["signal"], "status": "pending", "ok": False})
                continue
            commanded = float(row["commanded_v"])
            measured = float(measured_text)
            tolerance = float(row["tolerance_v"])
            error = abs(measured - commanded)
            results.append({
                "dac": int(row["dac"]),
                "signal": row["signal"],
                "commanded_v": commanded,
                "measured_v": measured,
                "error_v": error,
                "tolerance_v": tolerance,
                "status": "pass" if error <= tolerance else "fail",
                "ok": error <= tolerance,
            })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--artifacts-only", action="store_true", help="do not require completed rail measurements")
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    artifacts = verify_artifacts(baseline)
    api_defaults = verify_api_defaults(baseline)
    rails = [] if args.artifacts_only else verify_rail_measurements(baseline)
    digital_ok = all(item["ok"] for item in artifacts + api_defaults)
    electrical_ok = args.artifacts_only or (bool(rails) and all(item["ok"] for item in rails))
    report = {
        "baseline_id": baseline["baseline_id"],
        "digital_baseline_ok": digital_ok,
        "electrical_baseline_ok": electrical_ok if not args.artifacts_only else None,
        "publication_ready": digital_ok and electrical_ok and not args.artifacts_only,
        "artifacts": artifacts,
        "api_defaults": api_defaults,
        "rail_measurements": rails,
    }
    print(json.dumps(report, indent=2))
    return 0 if digital_ok and electrical_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
