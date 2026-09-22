# Publication baseline v1

This directory freezes the digital setup for the 130-nm 32x32 RRAM
characterization campaign. It does not program a cell or change hardware.

The baseline selects the validated v35 BIT/LTX pair, the permanent Caravel
firmware, a 2 MHz scan-debug clock, a 10 MHz WB clock, 0.5 V / 2.5 V read
rails, a 470-ohm shunt, Iref=1.0 V, Vcomp=0.9 V, Bias_comp2=0.6 V,
VBIAS=1.6 V, dc_bias=1.5 V, and VDDIO=4.0 V.

Verify the immutable artifacts and API defaults:

```powershell
python api_v1/publication/verify_baseline.py --artifacts-only
```

Before publication experiments, program/reuse the normal GUI/API runtime,
set the fixed rails, measure each physical DAC output with a DMM or calibrated
analogue input, and fill `measured_rails_v1.csv`. Then run:

```powershell
python api_v1/publication/verify_baseline.py
```

The second command intentionally fails while any measurement is blank or
outside its stated tolerance. Record the chip ID and ambient temperature in
the experiment run metadata; they are required but are not stored in this
shared baseline because they change between sessions.

The v35 binary and probes are release artifacts frozen by SHA-256. The tracked
FPGA RTL/build script predates v35, so do not rebuild over the release artifact
until a rebuilt image has been validated and shown equivalent on hardware.

`hardware_preflight_20260922.json` records the first post-freeze hardware
application and passive Saleae capture. DAC9-DAC13 command acknowledgements and
the connected A0-A5 measurements are preserved separately: an acknowledgement
is not treated as a physical voltage measurement. The publication gate remains
pending until the fixed rails in `measured_rails_v1.csv` are measured directly.

`idle_shunt_noise_20260922.json` records Step 2: three independent, passive
one-second captures of A12-A13 and A14-A15 with no reset, rail write, scan
packet, WB packet, SET, or RESET. Across 100,482 samples, the set/read path
offset was -22.60 uA (-10.62 mV across 470 ohm). Its three-capture mean range
was 2.84 uA and 1,000-sample block-mean standard deviation was 1.62 uA. The
stored 31.25 kS/s read offset differs by only 0.46 uA, so the dated result
validates rather than replaces the existing calibration. Individual samples
remain noisy (6.48 uA standard deviation), and publication reads must retain
window averaging.
