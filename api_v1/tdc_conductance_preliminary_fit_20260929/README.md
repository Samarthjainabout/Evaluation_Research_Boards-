# Preliminary TDC-to-conductance calibration

This folder preserves the measured points and reproducible fits used for the preliminary relationship between the column-pair differential conductance and the Wishbone TDC return.

## Quantity and TDC direction

The measured quantity is:

`delta_G = G31 - G30`, in µS.

For increasing current, the coarse count increases while the fine count decreases. Fine is a modulo-100 count (`0..99`). The continuous candidate coordinate is therefore:

`T = 100 * coarse + 99 - fine`

The `+99` only sets the origin. It makes the expected boundary continuous: `(C, F) = (21, 0)` maps to 2199 and `(22, 99)` maps to 2200.

## Data selection

The source run contains 12 programmed states across rows 4 and 6. The fit uses the eight states whose repeated scan-pair range was at most 8 µS. This is the same stability filter used in the 2026-09-29 preliminary findings. For repeated WB transactions, the TDC representative is the modal coarse count followed by the median fine count among returns in that coarse bin. The selected measured states are copied into `measured_points_used.csv`, including first-return and robust coarse/fine counts, conductance, repeat counts, programming rails and source path.

Run `python build_fit.py` from this folder to regenerate the CSV, JSON fit summary and PNG plot from the archived source CSV.

## Interpretation

The coarse-only relationship is the current preliminary estimator:

`G31 - G30 = a + b * coarse`

The exact fitted coefficients and metrics are in `fit_results.json`. The coarse-plus-fine and wrap-aware fits are diagnostic comparisons, not deployed calibration equations.

The wrap-aware model is:

`G31 - G30 = a + b * (100 * coarse + 99 - fine)`

It enforces the known fine direction and rollover behavior. It should only replace the coarse-only estimator if additional stable levels demonstrate lower held-out error. Eight states from two cells are not enough to claim a production calibration, and repeated WB reads at one programmed state must not be counted as independent conductance levels.

## Files

- `measured_points_used.csv`: all and only measured states used by the fits.
- `fit_results.json`: equations, RMSE, R² and selection counts.
- `tdc_conductance_preliminary_fit.png`: measured points and fit comparison.
- `build_fit.py`: reproducible extraction, fitting and plotting code.
