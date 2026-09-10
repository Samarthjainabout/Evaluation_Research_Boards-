# Evaluation Research Boards

This repo contains the scan-debug API, FPGA/Saleae setup files, and a local realtime GUI for monitoring and controlling cell read/set/reset experiments.

The main API documentation is in [`api_v1/README.md`](api_v1/README.md). This top-level guide explains the GUI shown at `http://127.0.0.1:8765`.

![Debug Monitor GUI](docs/images/gui-debug-monitor.png)

## Start The GUI

View-only monitor:

```bash
python3 api_v1/gui/server.py --host 127.0.0.1 --port 8765
```

Monitor plus command buttons:

```bash
python3 api_v1/gui/server.py --host 127.0.0.1 --port 8765 --allow-commands
```

Open:

```text
http://127.0.0.1:8765/
```

The GUI reads live data from `api_v1/runs/*/manifest.csv`, `progress.jsonl`, and command logs. It can attach to an already-running API process without stopping it.

## What The GUI Shows

### Top Bar

| UI item | Meaning |
|---|---|
| `Debug Monitor` | The local scan-debug dashboard. |
| Run dropdown | Selects which run directory under `api_v1/runs/` is displayed. |
| `FOLLOWING` | The GUI automatically tracks the newest/active run. |
| `PINNED` | The GUI is locked to the run selected in the dropdown. Change the dropdown to inspect old runs. |

### Summary Boxes

| UI item | Meaning |
|---|---|
| `Read rXX cYY` | Last cell with a recorded read packet. |
| `uA` | Last recorded read current in microamps. Only read packets update this value. |

### 1Kbit Heatmap

The heatmap is a 32 by 32 view of the 1Kbit array.

| UI item | Meaning |
|---|---|
| Each square | One array cell. Rows and columns are zero-indexed. |
| Cell color | Last recorded read current for that cell. |
| Dark cell | No read value has been recorded for that cell in the selected run yet. |
| Blinking/outlined cell | Cell currently being read or programmed. |
| Hover over a cell | Shows the cell address and last read current in the browser tooltip. |
| Click a cell | Copies that row/column into the command controls. |
| `High Resistance(HRS)` | Low current side of the color scale, currently around `100 uA`. |
| `Low Resistance(LRS)` | High current side of the color scale, currently around `400 uA`. |

Read-array behavior:

- `Read array` uses column-by-column burst mode.
- The API reads column `0` through column `31`.
- After each decoded column, the GUI heatmap updates for those 32 cells.
- Existing colored cells stay visible until a newer read for that cell arrives.

### Live Panel

| UI item | Meaning |
|---|---|
| Large `rXX cYY` | Current active cell from the latest API record. |
| Colored dot | Blue means read activity; yellow means programming activity. |
| Operation text | Current operation such as `read`, `set`, `reset`, `cycle`, or `read-array`. |
| Packet text | Scan packet used by the active/latest operation. |

### Control Panel

The control panel starts API commands from the browser when the GUI server was launched with `--allow-commands`.

| UI item | Meaning |
|---|---|
| `Op` | Command to run, including scan-debug operations plus native `WB read` and `WB write`. |
| `Read` | Reads one selected cell. |
| `Read array` | Reads the full 32 by 32 array column-by-column. |
| `Set` | Programs the selected cell toward LRS. Default threshold is `166.7 uA` with read `Vcc_set=1.0 V`. |
| `Reset` | Programs the selected cell toward HRS. Default threshold is `108.3 uA` with read `Vcc_set=1.0 V`. |
| `Cycle` | Runs set/reset cycling on the selected cell. |
| `WB read` | Runs the native setup/read sequence at `0x30000004`; blank defaults to `0x4002AA82`. It collects and displays all 15 FPGA UART/VIO readbacks. |
| `WB write` | Writes the entered 32-bit value to `0x30000004`. Blank input uses `0x500888FF`. |
| `Row` | Selected row, `0` to `31`. Ignored by full `Read array`. |
| `Col` | Selected column, `0` to `31`. Ignored by full `Read array`. |
| `Password` | Zynq SSH password for hardware-backed commands. The GUI sends it to the local API process but does not display it. |
| `Dry run` | If checked, the API simulates commands and does not touch hardware. |
| `Start` | Starts the selected command. For non-dry-run hardware commands, the GUI asks for confirmation first. |
| `Processing...` | A command is already running, so Start is disabled. |
| `Kill` | Sends a stop signal to the currently tracked API command. Use only when you intentionally want to stop the running hardware operation. |
| Status line below buttons | Shows whether the GUI is ready, processing a GUI-started command, or tracking an external API process. |

### API Return Box

| UI item | Meaning |
|---|---|
| Signal dot | Green/normal means API records are returning; red means the latest visible event is an error or needs attention. |
| `API return` | Last two API/progress/log messages in FIFO order. |
| `Saleae capturing column N` | Column burst capture is currently running on the Saleae host. |
| `Programming for column N` | FPGA bitstream for that column is being programmed. |
| `Column N: decoded` | That column has decoded and its heatmap cells can update. |
| `ERROR: ...` | Error extracted from a recent API log. |

### Read Trace

The bottom chart shows read current and programming voltage history for the selected run.

| UI item | Meaning |
|---|---|
| Top plot | Read current in microamps. |
| Cyan line/points | Recorded read current packets. |
| Red dashed line | SET threshold. New default is `166.7 uA`; older runs may show their recorded threshold, such as `200 uA`. |
| Blue dashed line | RESET threshold. New default is `108.3 uA`; older runs may show their recorded threshold, such as `130 uA`. |
| Bottom plot | Programming pulse voltage. |
| Red bars | SET pulses. |
| Blue bars | RESET pulses. |
| `Pulse` axis | Pulse/read event order in the selected run. |

## Current Defaults

| Setting | Default |
|---|---|
| Read `Vcc_set` | `0.5 V` |
| Read `Vcc_wl_set` | `2.5 V` |
| Heatmap HRS side | `100 uA` |
| Heatmap LRS side | `400 uA` |
| SET threshold | `166.7 uA` |
| RESET threshold | `108.3 uA` |
| Read-array mode | Column-by-column burst |
| WB read command | `0x4002AA82` when the field is blank |
| WB write command | `0x500888FF` when the field is blank |
| WB DAC behavior | Apply the WB read-bias profile on DAC4/5 and DAC9-13; keep the Si5351 PLL at 900 MHz and switch its output divider from scan 2 MHz to WB 10 MHz |

## Safety Notes

- Keep `Dry run` checked unless you only want a simulation.
- Unchecking `Dry run` sends commands to the hardware bench after confirmation.
- Do not start a second hardware command while another API process is running.
- Program the full FPGA runtime first so DAC7 powers VDDIO at 4.0 V before any Caravel firmware access. WB operations reuse the permanent Caravel firmware and flash it only when it is absent or does not acknowledge the runtime protocol. Keep `J2` removed for that initial/recovery flash.
- For FPGA UART return, wire Caravel `GPIO6/UART TX` to AX7020 `J10-10` and share ground. Remove Caravel `J2` before flashing and leave it removed; the FPGA captures UART directly, so it does not use the FTDI UART path.
- The GUI can detect and display external `scan_debug_cli.py` processes, but killing them should be deliberate.
- Older runs retain their own recorded thresholds and rails, so the trace may show old values even after defaults change.
