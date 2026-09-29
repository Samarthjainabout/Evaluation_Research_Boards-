"""Rebuild the preliminary TDC-to-differential-conductance calibration."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import statistics

import matplotlib.pyplot as plt
import numpy as np


HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "runs" / "fine_count_calibration_20260928_225726" / "fine_count_calibration.csv"
MAX_SCAN_RANGE_US = 8.0


def fit(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    pred = x @ beta
    rmse = float(np.sqrt(np.mean((y - pred) ** 2)))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - float(np.sum((y - pred) ** 2)) / ss_tot
    return beta, pred, rmse, r2


with SOURCE.open(newline="") as handle:
    source_rows = list(csv.DictReader(handle))

rows = [r for r in source_rows if r["fit_eligible"] == "True" and float(r["delta_range_uS"]) <= MAX_SCAN_RANGE_US]
for r in rows:
    repeats = [tuple(map(int, item)) for item in json.loads(r["wb_coarse_fine_repeats"])]
    coarse_counts = {value: sum(c == value for c, _ in repeats) for value in {c for c, _ in repeats}}
    coarse = max(coarse_counts, key=lambda value: (coarse_counts[value], value))
    fine = statistics.median(f for c, f in repeats if c == coarse)
    r["first_coarse"] = r["coarse"]
    r["first_fine"] = r["fine"]
    r["coarse"] = str(coarse)
    r["fine"] = str(fine)
    r["tdc_wrap_aware"] = str(100 * coarse + 99 - fine)
    r["source_file"] = str(SOURCE.relative_to(HERE.parent.parent)).replace("\\", "/")

fields = [
    "row", "state_index", "phase", "vcc_V", "wl_V", "G30_mean_uS", "G31_mean_uS",
    "delta_mean_uS", "delta_range_uS", "first_coarse", "first_fine", "coarse", "fine", "tdc_wrap_aware",
    "scan_repeat_count", "wb_repeat_count", "wb_coarse_fine_repeats", "source_file",
]
with (HERE / "measured_points_used.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

y = np.array([float(r["delta_mean_uS"]) for r in rows])
c = np.array([float(r["coarse"]) for r in rows])
f = np.array([float(r["fine"]) for r in rows])
t = np.array([float(r["tdc_wrap_aware"]) for r in rows])

b_coarse, p_coarse, rmse_coarse, r2_coarse = fit(np.column_stack([np.ones(len(y)), c]), y)
b_cf, p_cf, rmse_cf, r2_cf = fit(np.column_stack([np.ones(len(y)), c, f]), y)
b_wrap, p_wrap, rmse_wrap, r2_wrap = fit(np.column_stack([np.ones(len(y)), t]), y)

results = {
    "selection": {"source_states": len(source_rows), "used_states": len(rows), "maximum_scan_pair_range_uS": MAX_SCAN_RANGE_US},
    "coarse_only": {
        "equation": f"G31-G30 = {b_coarse[0]:.6f} + {b_coarse[1]:.6f}*coarse [uS]",
        "intercept_uS": float(b_coarse[0]), "coarse_uS_per_count": float(b_coarse[1]),
        "rmse_uS": rmse_coarse, "r_squared": r2_coarse,
    },
    "coarse_plus_fine_diagnostic": {
        "equation": f"G31-G30 = {b_cf[0]:.6f} + {b_cf[1]:.6f}*coarse + {b_cf[2]:.6f}*fine [uS]",
        "intercept_uS": float(b_cf[0]), "coarse_uS_per_count": float(b_cf[1]),
        "fine_uS_per_count": float(b_cf[2]), "rmse_uS": rmse_cf, "r_squared": r2_cf,
    },
    "wrap_aware_diagnostic": {
        "tdc_definition": "T = 100*coarse + 99 - fine",
        "equation": f"G31-G30 = {b_wrap[0]:.6f} + {b_wrap[1]:.6f}*T [uS]",
        "intercept_uS": float(b_wrap[0]), "uS_per_tdc_count": float(b_wrap[1]),
        "rmse_uS": rmse_wrap, "r_squared": r2_wrap,
    },
}
(HERE / "fit_results.json").write_text(json.dumps(results, indent=2) + "\n")

plt.style.use("seaborn-v0_8-whitegrid")
fig, axes = plt.subplots(1, 3, figsize=(16, 4.8))
axes[0].scatter(c, y, s=65, color="#2563eb")
xx = np.linspace(c.min() - 0.3, c.max() + 0.3, 200)
axes[0].plot(xx, b_coarse[0] + b_coarse[1] * xx, color="#dc2626", lw=2.2)
axes[0].set(title="Coarse-only fit", xlabel="Coarse count", ylabel="Measured G31-G30 (µS)")
axes[0].text(.03, .97, results["coarse_only"]["equation"] + f"\nR²={r2_coarse:.3f}, RMSE={rmse_coarse:.2f} µS", transform=axes[0].transAxes, va="top", fontsize=9)

axes[1].scatter(t, y, s=65, color="#0f766e")
tt = np.linspace(t.min() - 10, t.max() + 10, 200)
axes[1].plot(tt, b_wrap[0] + b_wrap[1] * tt, color="#dc2626", lw=2.2)
axes[1].set(title="Wrap-aware diagnostic", xlabel="T = 100×coarse + 99 − fine", ylabel="Measured G31-G30 (µS)")
axes[1].text(.03, .97, results["wrap_aware_diagnostic"]["equation"] + f"\nR²={r2_wrap:.3f}, RMSE={rmse_wrap:.2f} µS", transform=axes[1].transAxes, va="top", fontsize=9)

axes[2].scatter(y, p_coarse, s=65, label="Coarse only", color="#2563eb")
axes[2].scatter(y, p_wrap, s=65, label="Wrap-aware", color="#0f766e", marker="s")
lims = [min(y.min(), p_coarse.min(), p_wrap.min()) - 4, max(y.max(), p_coarse.max(), p_wrap.max()) + 4]
axes[2].plot(lims, lims, color="#111827", ls="--", lw=1.5)
axes[2].set(xlim=lims, ylim=lims, title="Predicted versus measured", xlabel="Measured G31-G30 (µS)", ylabel="Predicted G31-G30 (µS)")
axes[2].legend()

fig.suptitle("Preliminary TDC-to-conductance calibration — stable historical states", fontsize=15, weight="bold")
fig.tight_layout()
fig.savefig(HERE / "tdc_conductance_preliminary_fit.png", dpi=180, bbox_inches="tight")
plt.close(fig)

print(json.dumps(results, indent=2))
