# Averaged TDC-to-conductance fit — 2026-09-30

## Result and physical interpretation

This is an **empirical linear fit**, not a validated capacitor/frontend model:

\[
T_i=100C_i+99-F_i,\quad \bar T=\mathrm{mean}(T_i)
\]
\[
G_{31}-G_{30}\;[\mu S]=-46.6936130271+0.0270300459284\bar T.
\]

For the opposite reporting sign, negate the complete equation:
`G30-G31 = 46.6936130271 - 0.0270300459284*T`.
`C` is the coarse count, not capacitance. `T` is a candidate combined count,
not a verified time in seconds. No new coefficients were installed in the GUI.

| Validation on held-out physical rows | Error |
|---|---:|
| Combined-count model RMSE | 4.678912 uS |
| Combined-count model MAE | 3.302574 uS |
| Worst held-out row RMSE | 10.460916 uS |
| Coarse-only RMSE | 4.745417 uS |

The small 0.0665 uS RMSE improvement does **not** establish a useful fine-count
contribution. Allowing a free fine coefficient instead worsens RMSE to
5.144305 uS. Row-cluster bootstrap 95% intervals for the combined-count fit are
intercept `[-59.66824, -25.13171]` and slope `[0.01623494, 0.03195830]`.
These are coefficient intervals, not prediction error bars or device accuracy.

### Why the physical relationship may be nonlinear

A capacitor obeys `Ccap*dV/dt = I`. At constant current and fixed voltage swing,
`t = Ccap*deltaV/I`. Thus, even a voltage ramp linear in time can produce an
inverse relationship between threshold-crossing time and input current.
Voltage-dependent charging current instead requires integrating
`t = integral(Ccap/I(V, G31, G30, biases)) dV`; frontend offsets, leakage and
threshold behavior matter. The basic capacitor relationship is described in
[Analog Devices AN-1373, Capacitive Integration Measurement](https://www.analog.com/en/resources/app-notes/an-1373.html).

Those are general models, **not evidence that this chip follows a specific
inverse or exponential transfer**. The frontend schematic, time-code scale,
offset, comparator thresholds and current polarity must establish that model.
The observed positive slope must not be forced to match an assumed topology.
This fit is only a local approximation over the measured range. A future
comparison of a physically justified reciprocal model, quadratic diagnostic or
monotone lookup must use held-out rows, not just a higher training R-squared.
No nonlinear fit is claimed in this release.

## Exact data and acquisition

- Main run: `tdc_averaged_wrap_20260930_154600` (acquisition complete).
- Seed averaging run: `tdc_averaging_r13_20260930_152600`.
- Eight eligible checkpoints across **seven independent rows**: 13 (seed),
  30, 6, 2, 0, 4, 9, 13 (later repeat). Row 24 failed scan validation and is excluded.
- Seed has 20 independent WB conversions; every later checkpoint has 10.
  Total: 90 accepted independent conversions. The 15 UART polls within each
  transaction are not 15 independent samples. One row-0 C=51/F=0 return was
  rejected as a timeout candidate and replaced within a bounded retry budget.
- Each checkpoint has three scan-pair reads before and three after WB.
  Actual measured read voltage is used for conductance, with the archived
  A12-A13 offset profile and 470-ohm shunt. Raw signed near-zero values are
  not clipped. The profile is dated September 3; it is not a fresh zero check.
- Response is the mean of the before/after mean `G31-G30` values. The fit uses
  the mean of pair-preserved `100*C+99-F`, with equal total weight per row.
  Validation leaves an entire row out, including all visits to that row.
- Old three-read survey points are **not** included in this fit.
- Measured checkpoint delta range: approximately +2.22 to +43.71 uS;
  extrapolation to negative deltas or other chips/biases is not validated.
- Drift filters reject absolute differential or individual-cell before/after
  mean changes above 12 uS. This gate does not establish low measurement noise.

Row 13 demonstrates the remaining disagreement: averaged T changed from
2147.9 to 2153.6 while scan delta changed from 12.6228 to 18.1241 uS. Averaging
WB noise does not remove this scan/reference or cross-path uncertainty.

### Conditions

| Setting | Value |
|---|---:|
| Scan / WB clock | 2 MHz / 10 MHz |
| Scan read hold after DR rise | 50 us |
| Read / read-WL commanded rails | 0.5 V / 2.5 V |
| Iref / Vcomp | 1.0 V / 0.9 V |
| VBIAS / Bias_comp2 / dc_bias | 1.6 V / 0.6 V / 1.5 V |
| VDDIO existing profile | 4.0 V |

Only the connected read rails have measurement records; commanded biases are
not proof of independent measured voltages. No SET/RESET was sent in these two
read-only runs. WB commands use the existing API/firmware reset and setup path.

## Files and reproducibility

- `data/report.json`: exact completed main report, all accepted coarse/fine
  values, UART frames, scan measurements and validation results (includes seed).
- `data/seed_report.json`: original 20-conversion averaging report.
- `data/tdc_raw.jsonl`, `data/seed_tdc_raw.jsonl`: all attempted WB conversions,
  including the rejected return; `scan_pairs.jsonl` and seed counterpart retain
  each scan measurement. `failures.jsonl` retains row 24's failure.
- `data/plan.json`, seed plan and read-calibration profile preserve settings.
- `fit_inputs.json`: small exact checkpoint-level fitting table.
- `artifact_manifest.json`: SHA-256, byte size, code provenance and source hashes.
- `verify_archive.py`: offline data/model/hash checks; **never accesses hardware**.

Large raw Saleae waveform CSVs, captures, machine logs, credentials and unrelated
runs are not included. The scan-derived readings, rails, currents, decoder
results and original local capture paths remain in the report. This package
reproduces the fit, not a fresh waveform-level shunt calibration.

From repository root with Python and NumPy installed:

```text
python api_v1/tdc_averaged_wrap_fit_20260930/verify_archive.py
python api_v1/publication/analyze_existing_pair_calibration.py api_v1/tdc_averaged_wrap_fit_20260930/data/report.json
```

The acquisition sources are retained under `api_v1/publication/`; executing them
is not required for fitting and sends real hardware commands. Their bench paths
and source-run dependencies are historical, not a portable one-command setup.

### Verification status of this snapshot

Exact archived coefficient/RMSE reproduction, artifact hashes and bitstream
structure checks pass. JavaScript syntax and all 14 pulse-series tests pass.
The Python API/GUI/decoder/integrity suite ran 123 tests: 116 passed and 7 failed,
with no import/runtime errors after setting the documented module search paths.
`validation.json` lists the failed assertions: threshold/ramp defaults, operation
names, an old GUI source-string expectation, and sweep-resume expectations.
These failures have not been resolved or dismissed as harmless. This commit is
an archival snapshot of the code present for the run, **not an all-tests-green
software release**. No hardware operation was performed by these mocked tests.

## Hardware and API/GUI snapshot

The same commit contains the current `cell_api.py`, CLI, GUI server/static assets,
runtime VIO helper and calibration decoder. Acquisition used the API directly;
this is not a claim that every GUI mode was hardware-tested in this run.

| Artifact | Repository location |
|---|---|
| Full v35 BIT and matching LTX | `api_v1/prerequisites/fpga_zynq7020/bitstreams/caravel_scan_debug_runtime_dac81416_uart_wb_highz_v35_wb_read_repair.*` |
| Shared Caravel HEX and C source | `api_v1/prerequisites/caravel_wishbone/gui_wb_mode.*` |
| API / CLI | `api_v1/cell_api.py`, `api_v1/scan_debug_cli.py` |
| GUI | `api_v1/gui/server.py`, `api_v1/gui/static/` |

The BIT is 4,045,686 bytes, SHA-256
`378a707afff12d503dc81fa419cd7ad579ee67f1fe61875925c9579326da16b9`.
The shared HEX SHA-256 is
`0998ad922f6a982a113c43b696e03f73651c2cb9ea6e475a06f1b24ac7c3abae`.
Remote deployment BIT/LTX and Caravel C/HEX file hashes were checked read-only
against these local artifacts before this backup. This is a file-identity check,
not a new FPGA configuration/flash readback. No reflash was performed.

The prior committed BIT was truncated; this release includes the preserved full
v35 binary used for the recovered bench. Firmware code/payload was already tracked
and is unchanged. HEX/LTX line endings are now preserved byte-for-byte using
`.gitattributes` so Git checkout conversion does not change deployment hashes;
their textual diff is line-ending preservation, not a firmware/probe redesign.
FPGA RTL rebuild equivalence remains **unverified**: existing build
sources predate v35, so use the archived release binary for reproduction.
