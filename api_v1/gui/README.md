# Scan-Debug GUI

Local web GUI for watching `api_v1/runs/*/manifest.csv` while the cell API reads or programs cells. The GUI can be opened while an API command is already running in the background; it polls the manifest files and updates as new rows are written.

Start in view-only mode:

```bash
python api_v1/gui/server.py
```

Open:

```text
http://127.0.0.1:8765
```

The default mode only polls run manifests and does not start, stop, or alter any hardware process.

The GUI shows:

- latest API rows from the selected run;
- a 32 by 32 1Kbit heatmap of last recorded read-packet current values;
- the last recorded read-packet cell and current reading;
- a blinking marker for the cell currently being read or programmed;
- rising/falling current trends from read packets as records arrive.

To enable browser buttons that launch new `read`, `set`, `reset`, or `cycle` commands through `scan_debug_cli.py`, start with:

```bash
python api_v1/gui/server.py --allow-commands
```

The heatmap is a 32 by 32 1Kbit grid. Each cell displays the last recorded current from the selected run, and the latest read/program cell blinks while new manifest rows arrive.

The GUI's `Read array` command uses column-burst mode: one FPGA sequence sends a full column with reset before every packet, and one continuous Saleae capture is decoded into 32 manifest rows. The separate `Burst mode read` command uses one FPGA full-array stream and one Saleae capture. Full-array burst intentionally reduces Saleae sampling to avoid a multi-GB capture, but it keeps the single-cell FPGA reset/scan/hold timing and the full ScanInDR-rise-through-TM-fall measurement window. Use the CLI directly with `--array-mode serial` for the old one-cell-at-a-time loop, or `--burst-capture-strategy per-cell --burst-repeat-cycles 10000000` for the old Saleae rearm/export loop.

Existing background API processes are not stopped by the GUI. Start new commands from the browser only when the bench is ready for another operation.
