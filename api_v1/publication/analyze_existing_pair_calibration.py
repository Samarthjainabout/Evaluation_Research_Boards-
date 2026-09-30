"""Row-grouped, wrap-aware model comparison for fixed-bias read-only data."""
import argparse
import json
from pathlib import Path

import numpy as np


def design(c, f, name):
    columns = {"coarse": [c], "coarse_fine": [c, f],
               "wrap100_minus": [100*c+99-f], "wrap100_plus": [100*c+f]}[name]
    return np.column_stack([np.ones(len(c)), *columns])


def solve(x, y, weights):
    weighted = x * np.sqrt(weights)[:, None]
    if np.linalg.matrix_rank(weighted) < x.shape[1]:
        return None
    return np.linalg.lstsq(weighted, y*np.sqrt(weights), rcond=None)[0]


def analyze(states, bootstrap=300):
    eligible = [s for s in states if s["summary"]["fit_eligible"]]
    summary = {"target": "G31-G30 (uS)", "all_observations": len(states),
               "eligible_observations": len(eligible), "independent_rows": len({s['row'] for s in eligible}),
               "validation": "Leave one entire row out; equal total weight per row",
               "status": "collecting", "models": {}}
    if len(eligible) < 6 or summary["independent_rows"] < 6:
        return summary
    y = np.array([s["summary"]["G31_minus_G30_uS"] for s in eligible])
    groups = np.array([s["row"] for s in eligible])
    c = np.array([np.mean([w["selected_matching"]["coarse_cnt"] for w in s["wb"]]) for s in eligible])
    f = np.array([np.mean([w["selected_matching"]["fine_cnt"] for w in s["wb"]]) for s in eligible])
    rows = np.unique(groups)
    weights = np.array([1 / np.sum(groups == row) for row in groups])
    summary.update(span_uS=float(np.ptp(y)), coarse_span=[float(c.min()), float(c.max())],
                   coarse_bins=sorted({int(s['summary']['modal_coarse']) for s in eligible}))
    if np.ptp(y) < 25:
        summary["status"] = "insufficient_conductance_span"
        return summary
    rng = np.random.default_rng(20260930)
    bootstrap_rows = [rng.choice(rows, size=len(rows), replace=True) for _ in range(bootstrap)]
    for name in ("coarse", "coarse_fine", "wrap100_minus", "wrap100_plus"):
        x = design(c, f, name)
        beta = solve(x, y, weights)
        if beta is None:
            continue
        prediction = np.full(len(y), np.nan)
        for row in rows:
            train = groups != row
            b = solve(x[train], y[train], weights[train])
            if b is not None:
                prediction[~train] = x[~train] @ b
        if not np.all(np.isfinite(prediction)):
            continue
        residual = prediction-y
        boot = []
        for sampled in bootstrap_rows:
            indices = np.concatenate([np.flatnonzero(groups == row) for row in sampled])
            b = solve(x[indices], y[indices], weights[indices])
            if b is not None:
                boot.append(b)
        errors_by_row = {str(row): float(np.mean(residual[groups == row]**2)) for row in rows}
        model = {"coefficients": beta.tolist(),
                 "coefficient_95pct_row_bootstrap_interval": np.percentile(boot, [2.5,97.5], axis=0).tolist() if boot else None,
                 "grouped_cv_rmse_uS": float(np.sqrt(np.average(residual**2, weights=weights))),
                 "grouped_cv_mae_uS": float(np.average(abs(residual), weights=weights)),
                 "grouped_cv_worst_row_rmse_uS": float(np.sqrt(max(errors_by_row.values()))),
                 "row_mse": errors_by_row,
                 "held_out_predictions": [{"row":int(row), "visit":int(s.get('visit',0)),
                    "measured_uS":float(actual), "predicted_uS":float(pred), "residual_uS":float(error)}
                    for row,s,actual,pred,error in zip(groups,eligible,y,prediction,residual)]}
        # Train only the first visit of each row. Test later time-separated
        # repeats independently of the row-held-out comparison above.
        read_only = np.array([s.get("state_kind", "read_only") == "read_only" for s in eligible])
        train = np.array([s.get("visit",0) == 0 for s in eligible]) & read_only
        validation = read_only & ~train
        if np.any(validation) and len(np.unique(groups[train])) >= 6:
            b = solve(x[train], y[train], weights[train])
            if b is not None:
                model["later_visit_rmse_uS"] = float(np.sqrt(np.average(
                    (x[validation] @ b-y[validation])**2, weights=weights[validation])))
        summary["models"][name] = model
    if not summary["models"]:
        summary["status"] = "rank_deficient"
        return summary
    summary["lowest_cv_model"] = min(summary["models"], key=lambda key:summary["models"][key]["grouped_cv_rmse_uS"])
    if "coarse" in summary["models"] and "coarse_fine" in summary["models"]:
        baseline = summary["models"]["coarse"]
        fine = summary["models"]["coarse_fine"]
        improvements = np.array([baseline["row_mse"][str(row)]-fine["row_mse"][str(row)] for row in rows])
        # Row-level resampling of the paired held-out errors measures how
        # certain the claimed improvement is without splitting repeated rows.
        means = [np.mean(rng.choice(improvements, len(rows), replace=True)) for _ in range(bootstrap)]
        improvement_ci = np.percentile(means,[2.5,97.5])
        summary["fine_comparison"] = {
            "coarse_rmse_uS": baseline["grouped_cv_rmse_uS"],
            "coarse_fine_rmse_uS": fine["grouped_cv_rmse_uS"],
            "mean_squared_error_improvement_95pct_interval": improvement_ci.tolist(),
            "supported": bool(improvement_ci[0] > 0 and fine["grouped_cv_rmse_uS"] < .9*baseline["grouped_cv_rmse_uS"]
                              and baseline["grouped_cv_rmse_uS"]-fine["grouped_cv_rmse_uS"] >= 1),
            "criterion": "95% row-resampled improvement interval above zero, >=10% and >=1 uS RMSE reduction"}
    repeated_rows = [int(row) for row in rows if len({s.get('visit',0) for s in eligible
                     if s['row']==row and s.get('state_kind','read_only')=='read_only'}) >= 3]
    summary["rows_with_three_visits"] = repeated_rows
    best = summary["models"][summary["lowest_cv_model"]]
    summary["engineering_accuracy_screen"] = {
        "minimum_independent_rows": 12, "minimum_rows_with_three_visits": 6,
        "maximum_cv_mae_uS":5, "maximum_cv_worst_row_rmse_uS":15,
        "passed":bool(len(rows)>=12 and len(repeated_rows)>=6 and best['grouped_cv_mae_uS']<=5
                      and best['grouped_cv_worst_row_rmse_uS']<=15),
        "note":"Screening targets, not a certified measurement uncertainty or proof of transfer across biases/chips."}
    summary["status"] = "candidate_passes_screen" if summary['engineering_accuracy_screen']['passed'] else "provisional_more_validation_needed"
    summary["fit_scope"] = "This chip, column pair 30/31, fixed read rails and current frontend biases only"
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report",type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(json.loads(args.report.read_text())["states"]),indent=2))
