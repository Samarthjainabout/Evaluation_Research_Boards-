"""Streaming parser and strict evidence accumulator for 4-bit array runs.

The hardware log is the immutable source of truth.  Parsing therefore keeps the
raw line and raw FPGA words even when a frame is invalid; validation is reported
by :class:`AttemptAccumulator` rather than silently dropping evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import math
import re
import statistics
from typing import Any, Iterable, Iterator, Sequence


READ_VOLTAGE_V = 0.5
NOMINAL_GAIN = 7.0
SHUNT_OHMS = 56.0
BASELINE_COUNT_RANGE = (120, 200)
ACTIVE_COUNT_RANGE = (11_960, 12_040)
SENTINEL_LIMIT_UA = 10.0
PRE_READS = 5
DEFAULT_POST_READS = 5


_CELL_RE = re.compile(
    r"^CELL_RESULT\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+"
    r"([0-9A-Fa-f]+)\s+([0-9A-Fa-f]+)\s*$"
)
_WB_SHUNT_RE = re.compile(
    r"^WB_SHUNT\s+r(\d+)/c(\d+)\s+op=(set|reset|read)\s*(.*)$",
    re.IGNORECASE,
)
_WB_ACK_RE = re.compile(
    r"^EVENT\s+WB_ACK\s+(set|reset|read)\s+r(\d+)/c(\d+)\s+"
    r"pwm=(0x[0-9A-Fa-f]{2})\s*(.*)$",
    re.IGNORECASE,
)
_WB_WORD_RE = re.compile(
    r"^WB_WORD\s+(\d+)\s+(\d+)\s+(set|reset|read)\s+(\d+)\s+"
    r"([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]+)\s+([0-9A-Fa-f]+)\s*$",
    re.IGNORECASE,
)
_TDC_GATE_RE = re.compile(
    r"^WB_TDC_RETURN_GATE\s+r(\d+)/c(\d+)\s+op=(set|reset|read)\s*(.*)$",
    re.IGNORECASE,
)
_PWM_PREFIX_RE = re.compile(
    r"^(PWM_METRIC|PWM_PRE_PULSE_SAFETY_STOP)\s+(\d+)\s+(\d+)\s+"
    r"(set|reset)\s+(0x[0-9A-Fa-f]{2})\s+([-+0-9.eE]+)\s*(.*)$",
    re.IGNORECASE,
)
_PWM_SHUNT_GATE_RE = re.compile(
    r"^PWM_SHUNT_GATE\s+(\d+)\s+(\d+)\s+(set|reset)\s*(.*)$",
    re.IGNORECASE,
)
_PWM_COMPLETE_RE = re.compile(
    r"^PWM_DOE_COMPLETE\s+(\d+)\s+(\d+)\s+(set|reset)\s+"
    r"(0x[0-9A-Fa-f]{2})\s+([-+0-9.eE]+)\s+(\d+)\s+(\d+)\s+"
    r"(\d+)\s+(\d+)\s+(\S+)\s*$",
    re.IGNORECASE,
)
_PWM_SKIPPED_RE = re.compile(
    r"^PWM_DOE_SKIPPED\s+(\d+)\s+(\d+)\s+(set|reset)\s+"
    r"(0x[0-9A-Fa-f]{2})\s+([-+0-9.eE]+)\s+(\S+)\s*$",
    re.IGNORECASE,
)
_DAC_PROFILE_RE = re.compile(r"^DAC_PROFILE\s+(\S+)\s*(.*)$", re.IGNORECASE)
_JOB_COMPLETE_RE = re.compile(r"^JOB_COMPLETE\s+(\S+)\s*$", re.IGNORECASE)
_RAMP_COMPLETE_RE = re.compile(
    r"^RAMP_COMPLETE\s+(set|reset)\s+(\d+)\s+([01])\s+([-+0-9.eE]+)\s*$",
    re.IGNORECASE,
)
_RESTORE_COMPLETE_RE = re.compile(r"^RESTORE_SCAN_COMPLETE\s*$", re.IGNORECASE)
_RESTORE_FAILED_RE = re.compile(r"^RESTORE_FAILED(?:\s+(.*))?$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")
_INTEGER_RE = re.compile(r"^[-+]?\d+$")
_PRE_LABEL_RE = re.compile(r"^phase_p_pre_(set|reset)_(\d+)$")
_POST_LABEL_RE = re.compile(r"^phase_p_post_(set|reset)_(\d+)$")
_SENTINEL_PRE_RE = re.compile(r"^phase_p_sentinel_pre_(set|reset)$")
_SENTINEL_POST_RE = re.compile(r"^phase_p_sentinel_(set|reset)$")


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return value
    if _INTEGER_RE.fullmatch(value):
        try:
            return int(value, 10)
        except ValueError:
            return value
    if _NUMBER_RE.fullmatch(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else value
        except ValueError:
            return value
    return value


def _key_values(text: str) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    fields: dict[str, Any] = {}
    raw_fields: dict[str, str] = {}
    flags: list[str] = []
    for token in text.split():
        if "=" not in token:
            flags.append(token)
            continue
        key, value = token.split("=", 1)
        raw_fields[key] = value
        fields[key] = _coerce(value)
    return fields, raw_fields, flags


def _base(marker: str, line: str, timestamp_utc: Any, line_number: int | None) -> dict[str, Any]:
    return {
        "marker": marker,
        "timestamp_utc": _timestamp(timestamp_utc),
        "source_line": line_number,
        "raw_line": line.rstrip("\r\n"),
    }


def _identity(event: dict[str, Any], row: int, col: int, operation: str) -> None:
    event.update(row=row, col=col, operation=operation.lower())


def decode_scan_frame(
    h0: str,
    h1: str,
    row: int,
    col: int,
    label: str,
    hold_clocks: int,
) -> dict[str, Any]:
    """Decode one deployed ``SCR2`` scan frame without discarding bad frames."""

    word0 = int(h0, 16)
    word1 = int(h1, 16)
    packet = (word0 >> 16) & 0xFFFF
    set_packet = label == "set_pulse"
    expected_packet = (int(set_packet) << 15) | (row << 10) | (col << 5) | row
    baseline_count = (word0 >> 64) & 0xFFFF
    active_count = (word0 >> 80) & 0xFFFF
    signature = word1 >> 224
    done = bool(word0 & (1 << 9))
    busy = bool(word0 & (1 << 8))
    fault = bool(word0 & (1 << 10))
    read_clipped = bool((word0 >> 37) & 1)

    errors: list[str] = []
    if signature != 0x53435232:
        errors.append(f"signature=0x{signature:X}, expected SCR2")
    if packet != expected_packet:
        errors.append(f"packet=0x{packet:04X}, expected=0x{expected_packet:04X}")
    if not done or busy or fault:
        errors.append(f"status done={int(done)} busy={int(busy)} fault={int(fault)}")
    if not BASELINE_COUNT_RANGE[0] <= baseline_count <= BASELINE_COUNT_RANGE[1]:
        errors.append(f"baseline_count={baseline_count}")
    if not ACTIVE_COUNT_RANGE[0] <= active_count <= ACTIVE_COUNT_RANGE[1]:
        errors.append(f"active_count={active_count}")
    if read_clipped:
        errors.append("read_clipped=1")

    channels: list[dict[str, Any]] = []
    for channel in range(5):
        baseline_sum = (word0 >> (96 + 32 * channel)) & 0xFFFFFFFF
        active_sum = (word1 >> (32 * channel)) & 0xFFFFFFFF
        baseline_mean = baseline_sum / baseline_count if baseline_count else None
        active_mean = active_sum / active_count if active_count else None
        channel_clipped = bool((word0 >> (39 + channel)) & 1)
        delta_code = (
            active_mean - baseline_mean
            if baseline_mean is not None and active_mean is not None
            else None
        )
        current_ua = (
            delta_code / 2048.0 / (NOMINAL_GAIN * SHUNT_OHMS) * 1.0e6
            if channel < 3 and delta_code is not None and not channel_clipped
            else None
        )
        if baseline_mean is not None and not 0 <= baseline_mean <= 4095:
            errors.append(f"channel{channel + 1}_baseline_mean={baseline_mean}")
        if active_mean is not None and not 0 <= active_mean <= 4095:
            errors.append(f"channel{channel + 1}_active_mean={active_mean}")
        channels.append(
            {
                "channel": channel + 1,
                "baseline_sum": baseline_sum,
                "active_sum": active_sum,
                "baseline_mean_code": baseline_mean,
                "active_mean_code": active_mean,
                "delta_code": delta_code,
                "clipped": channel_clipped,
                "current_uA": current_ua,
                "conductance_uS": current_ua / READ_VOLTAGE_V if current_ua is not None else None,
            }
        )

    # The validated scan-debug outcome is ADC3/R10, channel index 2.
    outcome = channels[2]
    if outcome["clipped"]:
        errors.append("outcome_channel_clipped=1")
    return {
        "raw_h0": h0.upper(),
        "raw_h1": h1.upper(),
        "raw_h0_int": word0,
        "raw_h1_int": word1,
        "row": row,
        "col": col,
        "label": label,
        "hold_clocks": hold_clocks,
        "packet": packet,
        "packet_hex": f"0x{packet:04X}",
        "expected_packet": expected_packet,
        "expected_packet_hex": f"0x{expected_packet:04X}",
        "signature": signature,
        "signature_hex": f"0x{signature:08X}",
        "done": done,
        "busy": busy,
        "fault": fault,
        "baseline_count": baseline_count,
        "active_count": active_count,
        "read_clipped": read_clipped,
        "channels": channels,
        "outcome_channel": 3,
        "current_uA": outcome["current_uA"],
        "conductance_uS": outcome["conductance_uS"],
        "read_voltage_V": READ_VOLTAGE_V,
        "frame_valid": not errors,
        "frame_errors": errors,
    }


def parse_marker(
    line: str,
    timestamp_utc: Any = None,
    line_number: int | None = None,
) -> dict[str, Any] | None:
    """Parse one supported hardware marker, returning ``None`` for other lines."""

    text = line.strip()
    if not text:
        return None

    match = _CELL_RE.match(text)
    if match:
        row, col, hold = int(match[1]), int(match[2]), int(match[4])
        event = _base("CELL_RESULT", line, timestamp_utc, line_number)
        event.update(decode_scan_frame(match[5], match[6], row, col, match[3], hold))
        return event

    match = _WB_SHUNT_RE.match(text)
    if match:
        fields, raw_fields, flags = _key_values(match[4])
        event = _base("WB_SHUNT", line, timestamp_utc, line_number)
        _identity(event, int(match[1]), int(match[2]), match[3])
        event.update(fields=fields, raw_fields=raw_fields, flags=flags)
        return event

    match = _WB_ACK_RE.match(text)
    if match:
        fields, raw_fields, flags = _key_values(match[5])
        event = _base("WB_ACK", line, timestamp_utc, line_number)
        _identity(event, int(match[2]), int(match[3]), match[1])
        event.update(
            pwm_code=int(match[4], 16),
            pwm_hex=f"0x{int(match[4], 16):02X}",
            fields=fields,
            raw_fields=raw_fields,
            flags=flags,
            completed="completed" in {item.lower() for item in flags},
        )
        return event

    match = _WB_WORD_RE.match(text)
    if match:
        h0, h1 = match[6].upper(), match[7].upper()
        word0, word1 = int(h0, 16), int(h1, 16)
        event = _base("WB_WORD", line, timestamp_utc, line_number)
        _identity(event, int(match[1]), int(match[2]), match[3])
        event.update(
            index=int(match[4]),
            packet=int(match[5], 16),
            packet_hex=f"0x{int(match[5], 16):08X}",
            raw_h0=h0,
            raw_h1=h1,
            raw_h0_int=word0,
            raw_h1_int=word1,
            tdc_word=word1 & 0xFFFFFFFF,
            tdc_word_hex=f"0x{word1 & 0xFFFFFFFF:08X}",
            nonzero=bool(word1 & 0xFFFFFFFF),
            wb_signature=(word0 >> 224) & 0xFFFFFFFF,
            scan_signature=(word1 >> 224) & 0xFFFFFFFF,
        )
        return event

    match = _TDC_GATE_RE.match(text)
    if match:
        fields, raw_fields, flags = _key_values(match[4])
        event = _base("WB_TDC_RETURN_GATE", line, timestamp_utc, line_number)
        _identity(event, int(match[1]), int(match[2]), match[3])
        event.update(fields=fields, raw_fields=raw_fields, flags=flags)
        return event

    match = _PWM_PREFIX_RE.match(text)
    if match:
        marker = match[1].upper()
        fields, raw_fields, flags = _key_values(match[7])
        event = _base(marker, line, timestamp_utc, line_number)
        _identity(event, int(match[2]), int(match[3]), match[4])
        event.update(
            pwm_code=int(match[5], 16),
            pwm_hex=f"0x{int(match[5], 16):02X}",
            voltage_V=float(match[6]),
            fields=fields,
            raw_fields=raw_fields,
            flags=flags,
        )
        if marker == "PWM_METRIC":
            for source, target in (
                ("pre_median_uA", "pre_median_uS"),
                ("pre_min_uA", "pre_min_uS"),
                ("pre_max_uA", "pre_max_uS"),
                ("post_median_uA", "post_median_uS"),
                ("post_min_uA", "post_min_uS"),
                ("post_max_uA", "post_max_uS"),
                ("delta_uA", "delta_uS"),
            ):
                value = fields.get(source)
                if isinstance(value, (int, float)) and target not in fields:
                    fields[target] = 2.0 * value
        return event

    match = _PWM_SHUNT_GATE_RE.match(text)
    if match:
        fields, raw_fields, flags = _key_values(match[4])
        event = _base("PWM_SHUNT_GATE", line, timestamp_utc, line_number)
        _identity(event, int(match[1]), int(match[2]), match[3])
        event.update(fields=fields, raw_fields=raw_fields, flags=flags)
        return event

    match = _PWM_COMPLETE_RE.match(text)
    if match:
        event = _base("PWM_DOE_COMPLETE", line, timestamp_utc, line_number)
        _identity(event, int(match[1]), int(match[2]), match[3])
        event.update(
            pwm_code=int(match[4], 16),
            pwm_hex=f"0x{int(match[4], 16):02X}",
            voltage_V=float(match[5]),
            firmware_transactions=int(match[6]),
            firmware_packet_issues=int(match[7]),
            pre_reads=int(match[8]),
            post_reads=int(match[9]),
            outcome_source=match[10],
        )
        return event

    match = _PWM_SKIPPED_RE.match(text)
    if match:
        event = _base("PWM_DOE_SKIPPED", line, timestamp_utc, line_number)
        _identity(event, int(match[1]), int(match[2]), match[3])
        event.update(
            pwm_code=int(match[4], 16),
            pwm_hex=f"0x{int(match[4], 16):02X}",
            voltage_V=float(match[5]),
            reason=match[6],
        )
        return event

    match = _DAC_PROFILE_RE.match(text)
    if match:
        fields, raw_fields, flags = _key_values(match[2])
        event = _base("DAC_PROFILE", line, timestamp_utc, line_number)
        event.update(
            profile=match[1], fields=fields, raw_fields=raw_fields, flags=flags
        )
        return event

    match = _JOB_COMPLETE_RE.match(text)
    if match:
        event = _base("JOB_COMPLETE", line, timestamp_utc, line_number)
        event["action"] = match[1]
        return event

    match = _RAMP_COMPLETE_RE.match(text)
    if match:
        current_ua = float(match[4])
        event = _base("RAMP_COMPLETE", line, timestamp_utc, line_number)
        event.update(
            operation=match[1].lower(),
            pulses=int(match[2]),
            target_met=bool(int(match[3])),
            current_uA=current_ua,
            conductance_uS=current_ua / READ_VOLTAGE_V,
        )
        return event

    if _RESTORE_COMPLETE_RE.match(text):
        return _base("RESTORE_SCAN_COMPLETE", line, timestamp_utc, line_number)

    match = _RESTORE_FAILED_RE.match(text)
    if match:
        event = _base("RESTORE_FAILED", line, timestamp_utc, line_number)
        event["message"] = match[1] or ""
        return event

    return None


def decode_cell_result(
    line: str,
    timestamp_utc: Any = None,
    line_number: int | None = None,
) -> dict[str, Any]:
    """Return one ``CELL_RESULT`` as a flat, CSV-ready measurement row."""

    marker = parse_marker(line, timestamp_utc, line_number)
    if marker is None or marker.get("marker") != "CELL_RESULT":
        raise ValueError("line is not a CELL_RESULT marker")
    outcome = marker["channels"][marker["outcome_channel"] - 1]
    clipped = bool(marker["read_clipped"] or outcome["clipped"])
    return {
        "row": marker["row"],
        "column": marker["col"],
        "label": marker["label"],
        "raw_h0_hex": f"0x{marker['raw_h0']}",
        "raw_h1_hex": f"0x{marker['raw_h1']}",
        "baseline_count": marker["baseline_count"],
        "baseline_sum": outcome["baseline_sum"],
        "baseline_mean": outcome["baseline_mean_code"],
        "baseline_mean_code": outcome["baseline_mean_code"],
        "active_count": marker["active_count"],
        "active_sum": outcome["active_sum"],
        "active_mean": outcome["active_mean_code"],
        "active_mean_code": outcome["active_mean_code"],
        "current_uA": marker["current_uA"],
        "conductance_uS": marker["conductance_uS"],
        "read_clipped": clipped,
        "qc_flags": "; ".join(marker["frame_errors"]),
        "timestamp_utc": marker["timestamp_utc"],
        "source_line": marker["source_line"],
    }


def iter_markers(
    lines: Iterable[str],
    *,
    timestamps_utc: Iterable[Any] | None = None,
    first_line_number: int = 1,
) -> Iterator[dict[str, Any]]:
    """Yield supported markers from an existing or live line iterable."""

    if timestamps_utc is None:
        for line_number, line in enumerate(lines, first_line_number):
            marker = parse_marker(line, None, line_number)
            if marker is not None:
                yield marker
        return

    for line_number, pair in enumerate(zip(lines, timestamps_utc), first_line_number):
        line, timestamp_utc = pair
        marker = parse_marker(line, timestamp_utc, line_number)
        if marker is not None:
            yield marker


def parse_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse all supported markers from an after-run line iterable."""

    return list(iter_markers(lines))


def parse_file(path: str | Path) -> list[dict[str, Any]]:
    """Parse a completed text log while retaining its one-based source lines."""

    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        return parse_lines(handle)


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _range(values: Sequence[float]) -> list[float] | None:
    return [min(values), max(values)] if values else None


def _gate_value(event: dict[str, Any] | None, key: str) -> Any:
    return event.get("fields", {}).get(key) if event else None


def _flat_metric(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    result = {
        key: value
        for key, value in event.items()
        if key not in {"fields", "raw_fields", "flags"}
    }
    result.update(event.get("fields", {}))
    if event.get("flags"):
        result["flags"] = " ".join(event["flags"])
    return result


@dataclass
class AttemptAccumulator:
    """Accumulate and validate one WB-PWM programming attempt.

    A caller should create one accumulator per hardware attempt and feed it the
    attempt's full process output, including final cleanup and ``JOB_COMPLETE``.
    Completed attempts default to exactly five post-write scan reads, matching
    the completed r7/c1 reference experiment.  Pass
    ``expected_post_reads=None`` to accept the controller-declared 5..10 range
    when parsing older evidence.
    """

    row: int
    col: int
    operation: str
    expected_post_reads: int | None = DEFAULT_POST_READS
    markers: list[dict[str, Any]] = field(default_factory=list, init=False)
    scan_reads: list[dict[str, Any]] = field(default_factory=list, init=False)
    sentinel_reads: list[dict[str, Any]] = field(default_factory=list, init=False)
    tdc_words: list[dict[str, Any]] = field(default_factory=list, init=False)
    wb_shunts: list[dict[str, Any]] = field(default_factory=list, init=False)
    wb_acks: list[dict[str, Any]] = field(default_factory=list, init=False)
    tdc_gates: list[dict[str, Any]] = field(default_factory=list, init=False)
    pwm_metrics: list[dict[str, Any]] = field(default_factory=list, init=False)
    shunt_gates: list[dict[str, Any]] = field(default_factory=list, init=False)
    completions: list[dict[str, Any]] = field(default_factory=list, init=False)
    safety_stops: list[dict[str, Any]] = field(default_factory=list, init=False)
    skipped_markers: list[dict[str, Any]] = field(default_factory=list, init=False)
    dac_profiles: list[dict[str, Any]] = field(default_factory=list, init=False)
    job_markers: list[dict[str, Any]] = field(default_factory=list, init=False)
    ramp_completions: list[dict[str, Any]] = field(default_factory=list, init=False)
    restore_failures: list[dict[str, Any]] = field(default_factory=list, init=False)
    _program_evidence_seen: bool = field(default=False, init=False)
    _scan_profile_seen: bool = field(default=False, init=False)
    _profile_restored: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not 0 <= self.row <= 31 or not 0 <= self.col <= 31:
            raise ValueError("row and col must be in 0..31")
        self.operation = self.operation.lower()
        if self.operation not in {"set", "reset"}:
            raise ValueError("operation must be 'set' or 'reset'")
        if self.expected_post_reads is not None and not 5 <= self.expected_post_reads <= 10:
            raise ValueError("expected_post_reads must be None or an integer in 5..10")

    def _belongs(self, marker: dict[str, Any]) -> bool:
        if "row" not in marker:
            return True
        if marker["marker"] == "CELL_RESULT":
            label = marker["label"]
            target = marker["row"] == self.row and marker["col"] == self.col
            if target and (
                _PRE_LABEL_RE.match(label)
                or _POST_LABEL_RE.match(label)
                or label.startswith("before_")
                or label.startswith("after_")
                or label.startswith("confirm_")
            ):
                return True
            return bool(
                (_SENTINEL_PRE_RE.match(label) or _SENTINEL_POST_RE.match(label))
                and marker.get("operation", self.operation) == self.operation
            )
        return (
            marker["row"] == self.row
            and marker["col"] == self.col
            and marker.get("operation", self.operation) == self.operation
        )

    def feed(
        self,
        line: str,
        timestamp_utc: Any,
        line_number: int,
    ) -> dict[str, Any] | None:
        """Parse and accumulate one process-output line."""

        marker = parse_marker(line, timestamp_utc, line_number)
        if marker is None:
            return None
        marker["ordinal"] = len(self.markers) + 1
        self.markers.append(marker)

        kind = marker["marker"]
        if kind == "DAC_PROFILE":
            self.dac_profiles.append(marker)
            if marker["profile"].lower() == "scan_read":
                self._scan_profile_seen = True
                if self._program_evidence_seen:
                    self._profile_restored = True
            return marker
        if kind == "RESTORE_SCAN_COMPLETE":
            self._profile_restored = True
            return marker
        if kind == "RESTORE_FAILED":
            self.restore_failures.append(marker)
            return marker
        if kind == "JOB_COMPLETE":
            self.job_markers.append(marker)
            return marker
        if kind == "RAMP_COMPLETE":
            self.ramp_completions.append(marker)
            return marker
        if not self._belongs(marker):
            return marker

        if kind == "CELL_RESULT":
            label = marker["label"]
            if _SENTINEL_PRE_RE.match(label) or _SENTINEL_POST_RE.match(label):
                self.sentinel_reads.append(marker)
            else:
                self.scan_reads.append(marker)
        elif kind == "WB_SHUNT":
            self.wb_shunts.append(marker)
            self._program_evidence_seen = True
        elif kind == "WB_ACK":
            self.wb_acks.append(marker)
            self._program_evidence_seen = True
        elif kind == "WB_WORD":
            self.tdc_words.append(marker)
            self._program_evidence_seen = True
        elif kind == "WB_TDC_RETURN_GATE":
            self.tdc_gates.append(marker)
        elif kind == "PWM_METRIC":
            self.pwm_metrics.append(marker)
        elif kind == "PWM_SHUNT_GATE":
            self.shunt_gates.append(marker)
        elif kind == "PWM_DOE_COMPLETE":
            self.completions.append(marker)
        elif kind == "PWM_PRE_PULSE_SAFETY_STOP":
            self.safety_stops.append(marker)
        elif kind == "PWM_DOE_SKIPPED":
            self.skipped_markers.append(marker)
        return marker

    def _target_scan_groups(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pre: list[dict[str, Any]] = []
        post: list[dict[str, Any]] = []
        for marker in self.scan_reads:
            pre_match = _PRE_LABEL_RE.match(marker["label"])
            post_match = _POST_LABEL_RE.match(marker["label"])
            if pre_match and pre_match[1] == self.operation:
                marker["read_index"] = int(pre_match[2])
                pre.append(marker)
            elif post_match and post_match[1] == self.operation:
                marker["read_index"] = int(post_match[2])
                post.append(marker)
        return (
            sorted(pre, key=lambda item: item["read_index"]),
            sorted(post, key=lambda item: item["read_index"]),
        )

    def _sentinel_drift(self) -> tuple[float | None, list[dict[str, Any]], list[str]]:
        before: dict[tuple[int, int], dict[str, Any]] = {}
        after: dict[tuple[int, int], dict[str, Any]] = {}
        errors: list[str] = []
        for marker in self.sentinel_reads:
            key = (marker["row"], marker["col"])
            if _SENTINEL_PRE_RE.match(marker["label"]):
                if key in before:
                    errors.append(f"duplicate sentinel pre-read r{key[0]}/c{key[1]}")
                before[key] = marker
            elif _SENTINEL_POST_RE.match(marker["label"]):
                if key in after:
                    errors.append(f"duplicate sentinel post-read r{key[0]}/c{key[1]}")
                after[key] = marker
        if len(before) != 4 or len(after) != 4:
            errors.append(f"expected 4/4 sentinel pre/post reads, got {len(before)}/{len(after)}")
        if set(before) != set(after):
            errors.append("sentinel pre/post cell sets differ")
        rows: list[dict[str, Any]] = []
        for key in sorted(set(before) & set(after)):
            pre_current = before[key].get("current_uA")
            post_current = after[key].get("current_uA")
            drift = (
                post_current - pre_current
                if isinstance(pre_current, (int, float)) and isinstance(post_current, (int, float))
                else None
            )
            rows.append(
                {
                    "row": key[0],
                    "col": key[1],
                    "pre_current_uA": pre_current,
                    "post_current_uA": post_current,
                    "drift_uA": drift,
                    "abs_drift_uA": abs(drift) if drift is not None else None,
                }
            )
        valid = [row["abs_drift_uA"] for row in rows if row["abs_drift_uA"] is not None]
        return (max(valid) if valid else None), rows, errors

    def finalize(self, returncode: int | None) -> dict[str, Any]:
        """Return the complete attempt record and all acceptance gates."""

        pre_reads, post_reads = self._target_scan_groups()
        pre_ua = [item["current_uA"] for item in pre_reads if item.get("current_uA") is not None]
        post_ua = [item["current_uA"] for item in post_reads if item.get("current_uA") is not None]
        pre_us = [item["conductance_uS"] for item in pre_reads if item.get("conductance_uS") is not None]
        post_us = [item["conductance_uS"] for item in post_reads if item.get("conductance_uS") is not None]
        completion = self.completions[-1] if len(self.completions) == 1 else None
        metric = self.pwm_metrics[-1] if len(self.pwm_metrics) == 1 else None
        shunt_gate = self.shunt_gates[-1] if len(self.shunt_gates) == 1 else None
        wb_shunt = self.wb_shunts[-1] if len(self.wb_shunts) == 1 else None
        tdc_gate = self.tdc_gates[-1] if len(self.tdc_gates) == 1 else None
        ack = self.wb_acks[-1] if len(self.wb_acks) == 1 else None
        skipped = bool(self.skipped_markers or self.safety_stops)

        indices = [item["index"] for item in self.tdc_words]
        unique_indices = sorted(set(indices))
        duplicate_indices = sorted({index for index in indices if indices.count(index) > 1})
        nonzero_indices = sorted(item["index"] for item in self.tdc_words if item["nonzero"])
        tdc_signatures_valid = all(
            item["wb_signature"] == 0x57424131 and item["scan_signature"] == 0x53435232
            for item in self.tdc_words
        )
        tdc_marker_valid = bool(
            tdc_gate
            and _gate_value(tdc_gate, "pass") == 1
            and _gate_value(tdc_gate, "first_nonzero_index") == 11
            and _gate_value(tdc_gate, "last_nonzero_index") == 14
            and _gate_value(tdc_gate, "nonzero_count") == 4
        )
        ack_valid = bool(
            ack
            and ack.get("completed")
            and _gate_value(ack, "firmware_transaction") == 1
            and _gate_value(ack, "firmware_packet_issues") == 2
        )
        analog_confirmed = _gate_value(shunt_gate, "analog_confirmed") == 1
        no_clip = bool(
            shunt_gate
            and _gate_value(shunt_gate, "clip") == 0
            and wb_shunt
            and _gate_value(wb_shunt, "baseline_clip") == 0
            and _gate_value(wb_shunt, "active_clip") == 0
            and all(item.get("frame_valid") and not item.get("read_clipped") for item in pre_reads + post_reads)
            and all(item.get("frame_valid") and not item.get("read_clipped") for item in self.sentinel_reads)
        )
        sentinel_metric = _gate_value(metric, "sentinel_max_abs_drift_uA")
        sentinel_computed, sentinel_drift_rows, sentinel_errors = self._sentinel_drift()
        sentinel_valid = bool(
            isinstance(sentinel_metric, (int, float))
            and sentinel_metric <= SENTINEL_LIMIT_UA
            and sentinel_computed is not None
            and sentinel_computed <= SENTINEL_LIMIT_UA
            and not sentinel_errors
        )
        controller_post_reads = completion.get("post_reads") if completion else None
        declared_post_valid = bool(
            isinstance(controller_post_reads, int) and 5 <= controller_post_reads <= 10
        )
        expected_post = (
            self.expected_post_reads
            if self.expected_post_reads is not None
            else controller_post_reads
        )
        pre_indices = [item["read_index"] for item in pre_reads]
        post_indices = [item["read_index"] for item in post_reads]
        scan_count_valid = bool(
            pre_indices == list(range(1, PRE_READS + 1))
            and isinstance(expected_post, int)
            and post_indices == list(range(1, expected_post + 1))
            and controller_post_reads == expected_post
        )
        profile_restored = self._profile_restored and not self.restore_failures
        job_complete = len(self.job_markers) == 1

        gates = {
            "returncode_zero": returncode == 0,
            "ack": ack_valid,
            "tdc_15_unique_words": (
                len(self.tdc_words) == 15
                and unique_indices == list(range(15))
                and not duplicate_indices
            ),
            "tdc_exact_nonzero_indices_11_14": nonzero_indices == [11, 12, 13, 14],
            "tdc_raw_signatures": tdc_signatures_valid and len(self.tdc_words) == 15,
            "tdc_gate_marker": tdc_marker_valid,
            "analog_confirmed": analog_confirmed,
            "no_clip": no_clip,
            "sentinel_within_10_uA": sentinel_valid,
            "pwm_metric": metric is not None,
            "completion": completion is not None,
            "declared_post_reads_5_to_10": declared_post_valid,
            "scan_read_counts": scan_count_valid,
            "profile_restored": profile_restored,
            "job_complete": job_complete,
        }

        validation_errors: list[str] = []
        if skipped:
            pulse_issued = _gate_value(self.safety_stops[-1], "pulse_issued") if self.safety_stops else None
            safe_skip = bool(
                len(self.safety_stops) == 1
                and len(self.skipped_markers) == 1
                and pulse_issued == 0
                and not self.wb_acks
                and not self.tdc_words
                and not self.completions
                and pre_indices == list(range(1, PRE_READS + 1))
                and all(item.get("frame_valid") for item in pre_reads)
                and self._scan_profile_seen
                and job_complete
                and returncode == 0
                and not self.restore_failures
            )
            gates["safe_pre_pulse_skip"] = safe_skip
            if not safe_skip:
                validation_errors.append("incomplete or contradictory pre-pulse safety skip evidence")
            accepted = False
            status = "skipped" if safe_skip else "invalid"
        else:
            for name, passed in gates.items():
                if not passed:
                    validation_errors.append(name)
            validation_errors.extend(sentinel_errors)
            accepted = not validation_errors
            status = "accepted" if accepted else "invalid"

        summary = {
            "pwm_code": (
                completion.get("pwm_code")
                if completion
                else self.skipped_markers[-1].get("pwm_code") if self.skipped_markers else None
            ),
            "pwm_hex": (
                completion.get("pwm_hex")
                if completion
                else self.skipped_markers[-1].get("pwm_hex") if self.skipped_markers else None
            ),
            "voltage_V": (
                completion.get("voltage_V")
                if completion
                else self.skipped_markers[-1].get("voltage_V") if self.skipped_markers else None
            ),
            "pre_read_count": len(pre_reads),
            "post_read_count": len(post_reads),
            "expected_post_reads": expected_post,
            "pre_median_uA": _median(pre_ua),
            "pre_range_uA": _range(pre_ua),
            "post_median_uA": _median(post_ua),
            "post_range_uA": _range(post_ua),
            "delta_median_uA": (
                _median(post_ua) - _median(pre_ua) if pre_ua and post_ua else None
            ),
            "pre_median_uS": _median(pre_us),
            "pre_range_uS": _range(pre_us),
            "post_median_uS": _median(post_us),
            "post_range_uS": _range(post_us),
            "delta_median_uS": (
                _median(post_us) - _median(pre_us) if pre_us and post_us else None
            ),
            "sentinel_max_abs_drift_uA_reported": sentinel_metric,
            "sentinel_max_abs_drift_uA_computed": sentinel_computed,
            "nonzero_tdc_indices": nonzero_indices,
            "tdc_nonzero_count": len(nonzero_indices),
            "tdc_last_word_hex": (
                next(
                    (item["tdc_word_hex"] for item in self.tdc_words if item["index"] == 14),
                    None,
                )
            ),
        }

        tdc_pass = bool(
            gates["tdc_15_unique_words"]
            and gates["tdc_exact_nonzero_indices_11_14"]
            and gates["tdc_raw_signatures"]
            and gates["tdc_gate_marker"]
        )
        safety_stop = bool(
            skipped
            or _gate_value(shunt_gate, "safety_stop") == 1
            or _gate_value(shunt_gate, "set_absolute_stop") == 1
        )
        result_class = (
            "accepted"
            if accepted
            else "pre_pulse_safety_skip"
            if status == "skipped"
            else "gate_rejected"
        )

        return {
            "row": self.row,
            "col": self.col,
            "operation": self.operation,
            "returncode": returncode,
            "status": status,
            "result_class": result_class,
            "accepted": accepted,
            "pre_pulse_skipped": skipped,
            "wb_ack": ack_valid,
            "tdc_pass": tdc_pass,
            "analog_confirmed": analog_confirmed,
            "clip": False if status == "skipped" else not no_clip,
            "sentinel_pass": sentinel_valid,
            "safety_stop": safety_stop,
            "pre_median_uA": summary["pre_median_uA"],
            "pre_min_uA": summary["pre_range_uA"][0] if summary["pre_range_uA"] else None,
            "pre_max_uA": summary["pre_range_uA"][1] if summary["pre_range_uA"] else None,
            "post_median_uA": summary["post_median_uA"],
            "post_min_uA": summary["post_range_uA"][0] if summary["post_range_uA"] else None,
            "post_max_uA": summary["post_range_uA"][1] if summary["post_range_uA"] else None,
            "pre_median_uS": summary["pre_median_uS"],
            "pre_min_uS": summary["pre_range_uS"][0] if summary["pre_range_uS"] else None,
            "pre_max_uS": summary["pre_range_uS"][1] if summary["pre_range_uS"] else None,
            "post_median_uS": summary["post_median_uS"],
            "post_min_uS": summary["post_range_uS"][0] if summary["post_range_uS"] else None,
            "post_max_uS": summary["post_range_uS"][1] if summary["post_range_uS"] else None,
            "sentinel_max_abs_drift_uA": sentinel_metric,
            "job_complete": job_complete,
            "profile_restored": profile_restored if not skipped else self._scan_profile_seen,
            "scan_reads": self.scan_reads,
            "sentinel_reads": self.sentinel_reads,
            "sentinel_drift": sentinel_drift_rows,
            "tdc_words": sorted(self.tdc_words, key=lambda item: item["index"]),
            "shunt_metrics": [
                item
                for item in (_flat_metric(wb_shunt), _flat_metric(shunt_gate))
                if item is not None
            ],
            "shunt_evidence": {
                "observation": wb_shunt,
                "gate": shunt_gate,
            },
            "ack": ack,
            "tdc_gate": tdc_gate,
            "pwm_metric": metric,
            "completion": completion,
            "safety_stop": self.safety_stops[-1] if self.safety_stops else None,
            "skipped_marker": self.skipped_markers[-1] if self.skipped_markers else None,
            "ramp_completions": self.ramp_completions,
            "summary": summary,
            "gates": gates,
            "validation_errors": validation_errors,
            "markers": self.markers,
        }


def parse_attempt(
    lines: Iterable[str],
    *,
    row: int,
    col: int,
    operation: str,
    returncode: int | None,
    expected_post_reads: int | None = DEFAULT_POST_READS,
    timestamps_utc: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Convenience after-run parser for a single-attempt process log."""

    accumulator = AttemptAccumulator(row, col, operation, expected_post_reads)
    if timestamps_utc is None:
        for line_number, line in enumerate(lines, 1):
            accumulator.feed(line, None, line_number)
    else:
        for line_number, (line, timestamp_utc) in enumerate(zip(lines, timestamps_utc), 1):
            accumulator.feed(line, timestamp_utc, line_number)
    return accumulator.finalize(returncode)


__all__ = [
    "ACTIVE_COUNT_RANGE",
    "AttemptAccumulator",
    "BASELINE_COUNT_RANGE",
    "DEFAULT_POST_READS",
    "decode_cell_result",
    "decode_scan_frame",
    "iter_markers",
    "parse_attempt",
    "parse_file",
    "parse_lines",
    "parse_marker",
]
