from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


SIGNALS = [
    ("rst_b", "Channel 7", "#66727d"),
    ("ready", "Channel 6", "#66727d"),
    ("wb_clk_i", "Channel 8", "#51606d"),
    ("TM", "Channel 9", "#f08b45"),
    ("ScanInDR", "Channel 11", "#35a66f"),
    ("ScanInDL", "Channel 10", "#8058e8"),
]


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "arialbd.ttf" if bold else "arial.ttf"
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size)


def read_csv(path: Path) -> dict[str, np.ndarray]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    return {
        key: np.asarray([float(row[key]) for row in rows], dtype=float)
        for key in rows[0]
    }


def dashed_vline(draw: ImageDraw.ImageDraw, x: int, y0: int, y1: int, color: str) -> None:
    for y in range(y0, y1, 9):
        draw.line((x, y, x, min(y + 5, y1)), fill=color, width=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot an archived RESET Saleae packet capture")
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--vdda1", type=float, help="externally supplied VDDA1 voltage, for plot annotation")
    parser.add_argument("--note", default="measured Saleae capture")
    args = parser.parse_args()

    capture = args.capture_dir.resolve()
    digital = read_csv(capture / "digital.csv")
    analog = read_csv(capture / "analog.csv")
    summary = json.loads((capture / "capture_summary.json").read_text())
    analysis = json.loads((capture / "analysis.json").read_text())

    tm_rise_s = float(summary["decoded"]["tm_rise_s"])
    tm_fall_s = float(summary["decoded"]["tm_fall_s"])
    dr_fall_s = float(summary["decoded"]["dr_fall_s"])
    dr_rise_s = float(summary["decoded"]["dr_rise_s"])
    clk_period_s = float(summary["decoded"]["clock_period_s_median"])
    t_d = (digital["Time [s]"] - tm_rise_s) * 1e6
    t_a = (analog["Time [s]"] - tm_rise_s) * 1e6
    tm_fall_us = (tm_fall_s - tm_rise_s) * 1e6
    dr_fall_us = (dr_fall_s - tm_rise_s) * 1e6
    dr_rise_us = (dr_rise_s - tm_rise_s) * 1e6
    mean_stop_us = tm_fall_us - 3 * clk_period_s * 1e6
    x_min, x_max = -1.0, max(35.0, tm_fall_us + 10.0)

    set_current = (analog["Channel 12"] - analog["Channel 13"]) / 470.0 * 1e6
    reset_current = (analog["Channel 14"] - analog["Channel 15"]) / 470.0 * 1e6
    mean_mask = (t_a >= dr_rise_us) & (t_a <= mean_stop_us)
    set_mean = float(np.mean(set_current[mean_mask]))
    reset_mean = float(np.mean(reset_current[mean_mask]))

    width, height = 1600, 1000
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    left, right = 155, 1540
    plot_w = right - left
    panels = [(95, 260), (390, 230), (665, 230)]

    cell_match = re.search(r"r(\d{2})c(\d{2})", capture.name)
    cell_label = f"({int(cell_match.group(1))},{int(cell_match.group(2))})" if cell_match else "(unknown)"
    vdda1_label = f", VDDA1={args.vdda1:.2f} V" if args.vdda1 is not None else ""
    title = f"RESET Packet Cell {cell_label}, packet {summary['packet']}: {args.note}{vdda1_label}"
    draw.text((width // 2, 25), title, fill="#111111", font=font(26, bold=True), anchor="ma")

    def x_px(value: float) -> int:
        return round(left + (value - x_min) / (x_max - x_min) * plot_w)

    tick_step = 10
    ticks = np.arange(0, x_max + 0.1, tick_step)

    # Digital timing panel.
    top, panel_h = panels[0]
    draw.text((left, top - 27), "RESET packet timing from Saleae capture", fill="#111111", font=font(20, bold=True))
    draw.rectangle((left, top, right, top + panel_h), outline="#333333", width=2)
    row_h = panel_h / len(SIGNALS)
    for tick in ticks:
        x = x_px(float(tick))
        draw.line((x, top, x, top + panel_h), fill="#e5e7eb", width=1)
    for i, (label, channel, color) in enumerate(SIGNALS):
        y0 = top + i * row_h
        draw.line((left, round(y0), right, round(y0)), fill="#9ba3aa", width=1)
        draw.text((left - 18, y0 + row_h / 2), label, fill=color, font=font(14), anchor="rm")
        values = digital[channel]
        points: list[tuple[int, int]] = []
        for j, (time_us, value) in enumerate(zip(t_d, values)):
            if not x_min <= time_us <= x_max:
                continue
            y = round(y0 + row_h * (0.72 if value == 0 else 0.28))
            x = x_px(float(time_us))
            if points and j > 0:
                points.append((x, points[-1][1]))
            points.append((x, y))
        if len(points) >= 2:
            draw.line(points, fill=color, width=2)

    markers = [
        (0.0, "TM rise", "#f08b45"),
        (dr_fall_us, "DR fall", "#35a66f"),
        (dr_rise_us, "DR rise", "#35a66f"),
        (mean_stop_us, "mean stop", "#6b7280"),
        (tm_fall_us, "TM fall", "#f08b45"),
    ]
    for index, (value, label, color) in enumerate(markers):
        x = x_px(value)
        dashed_vline(draw, x, top - 12, panels[-1][0] + panels[-1][1], color)
        draw.text((x + 3, top - 15 - (index % 2) * 16), label, fill=color, font=font(11), anchor="ls")

    def analog_panel(panel: tuple[int, int], values: np.ndarray, heading: str, color: str, mean: float) -> None:
        y_top, h = panel
        draw.text((left, y_top - 25), heading, fill="#111111", font=font(19, bold=True))
        draw.rectangle((left, y_top, right, y_top + h), outline="#333333", width=2)
        in_view = (t_a >= x_min) & (t_a <= x_max)
        visible = values[in_view]
        low = float(np.floor((np.min(visible) - 10) / 25) * 25)
        high = float(np.ceil((np.max(visible) + 10) / 25) * 25)
        if high <= low:
            high = low + 1

        def y_px(value: float) -> int:
            return round(y_top + h - (value - low) / (high - low) * h)

        draw.rectangle(
            (x_px(dr_rise_us), y_top + 1, x_px(mean_stop_us), y_top + h - 1),
            fill="#eef4fb" if color == "#2878d0" else "#fceff0",
        )
        for tick in ticks:
            x = x_px(float(tick))
            draw.line((x, y_top, x, y_top + h), fill="#e5e7eb", width=1)
        for value in np.linspace(low, high, 5):
            y = y_px(float(value))
            draw.line((left, y, right, y), fill="#e5e7eb", width=1)
            draw.text((left - 12, y), f"{value:.0f}", fill="#30343b", font=font(12), anchor="rm")
        points = [
            (x_px(float(time_us)), y_px(float(value)))
            for time_us, value in zip(t_a[in_view], visible)
        ]
        draw.line(points, fill=color, width=2)
        draw.text((27, y_top + h / 2), "current (µA)", fill="#30343b", font=font(13), anchor="mm")
        legend = f"mean window = {mean:.2f} µA"
        draw.rounded_rectangle((630, y_top + 88, 925, y_top + 128), radius=5, fill="#ffffff", outline="#d0d4da")
        draw.line((650, y_top + 108, 690, y_top + 108), fill=color, width=3)
        draw.text((705, y_top + 108), legend, fill="#30343b", font=font(13), anchor="lm")

    analog_panel(panels[1], set_current, "Set shunt current, LA A12-A13 / 470 ohm", "#2878d0", set_mean)
    analog_panel(panels[2], reset_current, "Reset shunt current, LA A14-A15 / 470 ohm", "#df3f3f", reset_mean)

    axis_y = panels[-1][0] + panels[-1][1]
    for tick in ticks:
        x = x_px(float(tick))
        draw.text((x, axis_y + 11), f"{tick:.0f}", fill="#30343b", font=font(12), anchor="ma")
    draw.text((width // 2, axis_y + 38), "time from TM rise (µs)", fill="#30343b", font=font(14), anchor="ma")

    footer = (
        f"Decoded expected={summary['packet']} captured={summary['decoded_packet']}; bits(lsb-first)={summary['bits_lsb_first']}; "
        f"TM hold after DR rise={tm_fall_us - dr_rise_us:.2f} µs; clock={clk_period_s * 1e6:.2f} µs; "
        f"Vcc_set={summary['vcc_set_V']:.2f} V; Vcc_wl_set={summary['vcc_wl_set_V']:.2f} V; "
        f"VDDA1={'external ' + format(args.vdda1, '.2f') + ' V' if args.vdda1 is not None else 'not recorded'}; "
        f"source={args.note}"
    )
    draw.text((left, 955), footer, fill="#333333", font=font(11))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output, quality=95)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
