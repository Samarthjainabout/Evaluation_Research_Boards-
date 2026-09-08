"""Build and apply the complete 16-channel WB DAC profile."""
from datetime import datetime
from pathlib import Path
import json
import shutil

from cell_api import ScanDebugCellAPI, ScanDebugConfig

base = Path(__file__).resolve().parent
source = base / "prerequisites/fpga_dac_full_wb"
run = base / "runs" / ("dac_full_wb_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
run.mkdir()
api = ScanDebugCellAPI(ScanDebugConfig(run_dir=run, hardware_queue_timeout_seconds=30))
remote_fpga = api.config.zynq_dir + "/" + run.name
remote_sim = "/tmp/" + run.name + "_sim"


def checked(proc, name):
    output = proc.stdout or ""
    (run / name).write_text(output)
    if proc.returncode or any(marker in output for marker in ("FAILED", "ERROR:", "FATAL:")):
        raise RuntimeError(f"{name} failed: {output[-3000:]}")
    return output


targets = {
    0: ("Vcc_read", 0.5, "0x199A"),
    1: ("Vcc_wl_read", 2.5, "0x8000"),
    2: ("Vcc_set", 2.3, "0x75C3"),
    5: ("VDDA2", 2.3, "0x75C3"),
    7: ("VDDIO", 4.0, "0xCCCC"),
    9: ("Iref", 0.5, "0x199A"),
    10: ("Vcomp", 0.9, "0x2E14"),
    11: ("Bias_comp2", 0.6, "0x1EB8"),
    12: ("Vbias", 1.6, "0x51EC"),
    13: ("dc_bias", 1.0, "0x3333"),
    15: ("VCCD2", 2.1, "0x6B85"),
}

print(f"RUN={run}", flush=True)
with api.hardware_queue("dac-full-wb"):
    checked(api._run_saleae(f"mkdir {remote_sim}", timeout_s=30), "sim_mkdir.log")
    for filename in ("dac_full_wb_top.v", "tb_dac_full_wb.v"):
        checked(api.runner.run(["scp", str(source / filename),
            f"{api.config.saleae_host}:{remote_sim}/{filename}"], timeout_s=60),
            f"sim_upload_{filename}.log")
    simulation = checked(api._run_saleae(
        f"cd {remote_sim} && iverilog -g2012 -s tb_dac_full_wb -o test.vvp "
        "tb_dac_full_wb.v dac_full_wb_top.v && vvp test.vvp", timeout_s=60),
        "simulation.log")
    if "PASS: complete 16-channel WB DAC profile" not in simulation:
        raise RuntimeError("Simulation pass marker missing")
    print(simulation, flush=True)

    checked(api._run_zynq_powershell(
        f"New-Item -ItemType Directory -Path '{remote_fpga}' -ErrorAction Stop | Out-Null",
        timeout_s=30), "fpga_mkdir.log")
    for item in source.iterdir():
        if item.is_file():
            shutil.copy2(item, run / item.name)
            checked(api.runner.run(["scp", str(item),
                f"{api.config.zynq_host}:{remote_fpga}/{item.name}"], timeout_s=60),
                f"upload_{item.name}.log")

    print("SIMULATION_OK; BUILDING_FULL_WB_DAC_BITSTREAM", flush=True)
    build = checked(api._run_zynq_powershell(
        f"Set-Location '{remote_fpga}'; & '{api.config.vivado_cmd}' -mode batch "
        "-source build.tcl *> build.log; $rc=$LASTEXITCODE; Get-Content build.log; exit $rc",
        timeout_s=900), "build.log")
    if "BUILT dac_full_wb.bit" not in build:
        raise RuntimeError("Bitstream completion marker missing")

    print("BITSTREAM_BUILD_OK; APPLYING_COMPLETE_DAC_PROFILE", flush=True)
    programmed = checked(api._run_zynq_powershell(
        f"Set-Location '{remote_fpga}'; & '{api.config.vivado_cmd}' -mode batch "
        "-source program.tcl *> program.log; $rc=$LASTEXITCODE; Get-Content program.log; exit $rc",
        timeout_s=180), "program.log")
    if "DAC_FULL_WB_PROGRAMMED" not in programmed:
        raise RuntimeError("Programming completion marker missing")

    channels = {}
    for index in range(16):
        if index in targets:
            name, voltage, code = targets[index]
            channels[str(index)] = {
                "state": "active", "name": name, "target_V": voltage, "code": code,
            }
        else:
            channels[str(index)] = {
                "state": "powered_down", "target_V": 0.0,
                "output_connection": "10-kohm internal pull-down to ground",
            }
    result = {
        "complete": True,
        "range_V": [0.0, 5.0],
        "final_powerdown_register": "0x4158",
        "channels": channels,
        "caravel_reset_changed": False,
        "pll_or_clock_changed": False,
        "fpga_image": remote_fpga + "/dac_full_wb.bit",
        "voltage_measured": False,
    }
    (run / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)
