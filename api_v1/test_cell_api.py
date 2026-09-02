import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cell_api import (
    FPGA_RUNTIME_BITSTREAM,
    RESET_PROGRAM_VCC_SET_V,
    RESET_PROGRAM_VCC_SET_SWEEP_V,
    RESET_PROGRAM_VCC_WL_V,
    SET_PROGRAM_VCC_SET_V,
    SET_PROGRAM_VCC_WL_V,
    CommandRunner,
    RailVoltages,
    ScanDebugCellAPI,
    ScanDebugConfig,
)


class CommandRunnerPasswordSshTests(unittest.TestCase):
    def test_windows_password_ssh_uses_paramiko_and_combines_output(self) -> None:
        channel = Mock()
        channel.makefile.return_value.read.return_value = b"remote output\n"
        channel.recv_exit_status.return_value = 7
        transport = Mock()
        transport.is_active.return_value = True
        transport.open_session.return_value = channel
        client = Mock()
        client.get_transport.return_value = transport

        with patch("paramiko.SSHClient", return_value=client):
            result = CommandRunner._ssh_with_paramiko_password(
                "user@example.test",
                "secret",
                "hostname",
                timeout_s=30,
            )

        self.assertEqual(result.args, ["ssh", "user@example.test", "hostname"])
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "remote output\n")
        self.assertEqual(result.stderr, "")
        client.connect.assert_called_once_with(
            hostname="example.test",
            username="user",
            password="secret",
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        channel.set_combine_stderr.assert_called_once_with(True)
        channel.exec_command.assert_called_once_with("hostname")
        channel.close.assert_called_once_with()
        client.close.assert_called_once_with()

    def test_windows_password_ssh_requires_user_at_hostname(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "user@hostname"):
            CommandRunner._ssh_with_paramiko_password("example.test", "secret", "hostname")


class CaptureCopyTests(unittest.TestCase):
    def test_remote_copy_falls_back_to_scp_when_rsync_is_not_on_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(
                ScanDebugConfig(
                    run_dir=Path(temp_dir),
                    saleae_host="user@example.test",
                    dry_run=True,
                )
            )
            api.runner = Mock()
            api.runner.run.return_value.returncode = 0
            api.runner.run.return_value.stdout = ""
            with patch("cell_api.shutil.which", side_effect=lambda name: None if name == "rsync" else "scp.exe"):
                local = api._copy_capture("/remote/capture", 3, "read", RailVoltages(1.0, 2.5))

            api.runner.run.assert_called_once_with(
                ["scp.exe", "-r", "user@example.test:/remote/capture/.", str(local)]
            )


class ReadRailDefaultTests(unittest.TestCase):
    def test_all_read_paths_share_half_volt_default(self) -> None:
        config = ScanDebugConfig()

        self.assertEqual(config.read_rails, RailVoltages(0.5, 2.5))
        self.assertTrue(config.fpga_dac_enabled)
        self.assertTrue(config.persistent_fpga_runtime)
        self.assertFalse(config.capture_program_pulses)
        self.assertFalse(config.enable_adc_monitor)
        self.assertFalse(config.dac_teensy_reflash_enabled)

    def test_half_volt_read_uses_updated_state_thresholds(self) -> None:
        config = ScanDebugConfig()

        self.assertEqual(config.set_sweep.threshold_uA, 70.0)
        self.assertEqual(config.set_sweep.direction, "above")
        self.assertEqual(config.reset_sweep.threshold_uA, 5.0)
        self.assertEqual(config.reset_sweep.direction, "below")

    def test_programming_sweeps_hold_vcc_set_and_use_projected_wl_levels(self) -> None:
        config = ScanDebugConfig()

        self.assertEqual(config.set_sweep.vcc_set_v, (SET_PROGRAM_VCC_SET_V,))
        self.assertEqual(config.set_sweep.vcc_wl_set_v, SET_PROGRAM_VCC_WL_V)
        self.assertEqual(config.reset_sweep.vcc_set_v, RESET_PROGRAM_VCC_SET_SWEEP_V)
        self.assertEqual(config.reset_sweep.vcc_wl_set_v, RESET_PROGRAM_VCC_WL_V)
        self.assertEqual(len(config.set_sweep.vcc_wl_set_v), 32)
        self.assertEqual(len(config.reset_sweep.vcc_wl_set_v), 32)
        self.assertEqual(config.set_sweep.vcc_wl_set_v[0], 0.44)
        self.assertEqual(config.set_sweep.vcc_wl_set_v[-1], 2.37)
        self.assertEqual(config.reset_sweep.vcc_wl_set_v[0], 0.94)
        self.assertEqual(config.reset_sweep.vcc_wl_set_v[-1], 2.88)

    def test_projected_levels_are_within_half_an_lsb_of_dac_codes(self) -> None:
        wl_half_lsb_v = (5.0 / 65535) / 2
        set_half_lsb_v = (10.0 / 65535) / 2

        for voltage in (*SET_PROGRAM_VCC_WL_V, *RESET_PROGRAM_VCC_WL_V):
            code = round(voltage * 65535 / 5.0)
            self.assertLessEqual(abs((code * 5.0 / 65535) - voltage), wl_half_lsb_v + 1e-12)
        for voltage in (SET_PROGRAM_VCC_SET_V, *RESET_PROGRAM_VCC_SET_SWEEP_V):
            code = round(voltage * 65535 / 10.0)
            self.assertLessEqual(abs((code * 10.0 / 65535) - voltage), set_half_lsb_v + 1e-12)

    def test_state_threshold_boundaries_are_strict(self) -> None:
        self.assertFalse(ScanDebugCellAPI._passes(70.0, 70.0, "above"))
        self.assertTrue(ScanDebugCellAPI._passes(70.001, 70.0, "above"))
        self.assertFalse(ScanDebugCellAPI._passes(5.0, 5.0, "below"))
        self.assertTrue(ScanDebugCellAPI._passes(4.999, 5.0, "below"))


class SaleaeRecoveryTests(unittest.TestCase):
    def test_ssh_timeout_is_transport_retry_not_usb_reset(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))
        output = (
            "ssh: connect to host 100.98.132.51 port 22: Connection timed out\n"
            "SALEAE_ARM_TIMEOUT: capture was not armed\n"
        )

        self.assertTrue(api._remote_transport_needs_retry(output))

    def test_capture_read_timeout_triggers_usb_recovery(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))

        self.assertTrue(
            api._usb_needs_recovery(
                "saleae.automation.errors.DeviceError: "
                "Error interacting with device during capture: ReadTimeout."
            )
        )

    def test_missing_trigger_watchdog_triggers_saleae_recovery(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))

        self.assertTrue(api._usb_needs_recovery("SALEAE_CAPTURE_COMPLETION_TIMEOUT"))

    def test_saleae_script_reports_armed_only_after_start_capture(self) -> None:
        script = (
            Path(__file__).parent
            / "prerequisites"
            / "saleae_ubuntu"
            / "run_fpga_scan0000_la12_15_capture.py"
        ).read_text()

        self.assertLess(script.index("with manager.start_capture("), script.index('f"SALEAE_ARMED'))
        self.assertLess(script.index('f"SALEAE_ARMED'), script.index("capture.wait()"))

    def test_restart_requires_a_physical_saleae_device(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(
                ScanDebugConfig(run_dir=Path(temp_dir), saleae_host="user@example.test", dry_run=False)
            )
            api.runner = Mock()
            api.runner.ssh.return_value = subprocess.CompletedProcess([], 0, "[]\n", "")

            api._restart_saleae_automation(1, "read", 1)

            command = api.runner.ssh.call_args.args[1]
            self.assertIn("include_simulation_devices=False", command)
            self.assertIn("if not devices", command)

    def test_usb_recovery_force_restarts_logic_even_when_xhci_reset_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(
                ScanDebugConfig(run_dir=Path(temp_dir), saleae_host="user@example.test", dry_run=False)
            )
            api.runner = Mock()
            api.runner.ssh.return_value = subprocess.CompletedProcess([], 0, "ready\n", "")

            api._recover_saleae_usb(1, "read", 2)

            command = api.runner.ssh.call_args.args[1]
            self.assertIn("FORCE_RESTART_LOGIC", command)
            self.assertIn("pkill -TERM -f '[L]ogic.bin'", command)
            self.assertIn("if not devices", command)
            self.assertIn("VIRTUALBOX_USB_PASSTHROUGH", command)
            self.assertIn("WAIT_FOR_SALEAE_USB", command)


class FpgaDacBitstreamTests(unittest.TestCase):
    def test_runtime_daemon_uses_a_unique_remote_log(self) -> None:
        source = Path(__file__).with_name("cell_api.py").read_text()

        self.assertIn('remote_daemon_log = f"runtime_vio_daemon.{uuid.uuid4().hex}.log"', source)
        self.assertIn("*> '{remote_daemon_log}'", source)

    def test_runtime_daemon_uses_request_scoped_response_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))
            api._ensure_runtime_vio_daemon = Mock()  # type: ignore[method-assign]
            def run_cmd(command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
                if command == "dir /B runtime_vio_response.request-id.txt":
                    return subprocess.CompletedProcess([], 0, "runtime_vio_response.request-id.txt\n", "")
                if command == "type runtime_vio_response.request-id.txt":
                    return subprocess.CompletedProcess([], 0, "request-id OK command=0 status=0\n", "")
                return subprocess.CompletedProcess([], 0, "", "")

            api._run_zynq_cmd = Mock(side_effect=run_cmd)  # type: ignore[method-assign]

            with patch("cell_api.uuid.uuid4") as request_uuid:
                request_uuid.return_value.hex = "request-id"
                api._program_fpga_via_runtime_daemon("0x0000000000000000")

            commands = [call.args[0] for call in api._run_zynq_cmd.call_args_list]
            self.assertIn("dir /B runtime_vio_response.request-id.txt", commands)
            self.assertNotIn("dir /B runtime_vio_response.txt", commands)

    def test_single_cell_bitstream_carries_rail_parameters(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))
        rails = RailVoltages(0.5, 2.5)
        tcl = api._build_tcl(api._array_sweep_cells(18, 0)[0], 0, rails, "test.bit")

        self.assertIn('add_files [file join $script_dir "dac81416_spi.v"]', tcl)
        self.assertIn("DAC_VCC_SET_MV=500", tcl)
        self.assertIn("DAC_VCC_WL_SET_MV=2500", tcl)
        self.assertEqual(rails.bitstream_tag, "vcc0500_wl2500")

    def test_dry_run_uses_one_runtime_bitstream(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))
        cell = api._array_sweep_cells(18, 0)[0]

        name = api._ensure_bitstream(cell, 0, RailVoltages(0.5, 2.5))

        self.assertEqual(name, "caravel_scan_debug_runtime_dac81416_v2.bit")
        self.assertEqual(api._ensure_bitstream(cell, 1, RailVoltages(3.0, 2.0)), name)
        self.assertEqual(api._ensure_array_bitstream(0, 31), name)

    def test_runtime_payload_contains_cell_operation_count_and_dac_codes(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))
        packet = 0x8000 | (18 << 10) | (7 << 5) | 18

        payload = int(api._runtime_command_payload(packet, RailVoltages(0.5, 2.5), 32), 16)

        self.assertEqual((payload >> 63) & 1, 1)
        self.assertEqual((payload >> 62) & 1, 1)
        self.assertEqual((payload >> 57) & 0x1F, 18)
        self.assertEqual((payload >> 52) & 0x1F, 7)
        self.assertEqual((payload >> 41) & 0x7FF, 32)
        self.assertEqual((payload >> 25) & 0xFFFF, round(0.5 * 65535 / 10.0))
        self.assertEqual((payload >> 9) & 0xFFFF, round(2.5 * 65535 / 5.0))

    def test_runtime_programming_uses_universal_bitstream_and_vio_payload(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=False, persistent_fpga_runtime=False))
        api._run_zynq_powershell = Mock(  # type: ignore[method-assign]
            return_value=subprocess.CompletedProcess([], 0, "VIVADO_EXIT=0\n", "")
        )
        packet = 0x8000 | (18 << 10) | 18

        rc = api._program_fpga(
            "caravel_scan_debug_runtime_dac81416_v2.bit",
            packet=packet,
            rails=RailVoltages(2.5, 1.2),
            packet_count=1,
        )

        self.assertEqual(rc, 0)
        command = api._run_zynq_powershell.call_args.args[0]
        self.assertIn("program_and_run_runtime.tcl", command)
        self.assertIn("caravel_scan_debug_runtime_dac81416_v2.bit", command)
        self.assertIn("caravel_scan_debug_runtime_dac81416_v2.ltx", command)
        self.assertIn(api._runtime_command_payload(packet, RailVoltages(2.5, 1.2), 1), command)

    def test_fast_program_pulse_uses_runtime_ack_without_saleae_capture(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))
            api._ensure_bitstream = Mock(return_value=FPGA_RUNTIME_BITSTREAM)  # type: ignore[method-assign]
            api._ensure_runtime_vio_daemon = Mock()  # type: ignore[method-assign]
            api._program_fpga = Mock(return_value=0)  # type: ignore[method-assign]

            result = api._program_pulse(api._array_sweep_cells(18, 0)[0], "set", RailVoltages(2.5, 1.2), "set_pulse")

            self.assertTrue(result.ok)
            self.assertIsNone(result.current_uA)
            self.assertEqual(result.local_output_dir, "FPGA_RUNTIME_ACK_NO_CAPTURE")
            api._program_fpga.assert_called_once()


class SaleaeScriptUploadTests(unittest.TestCase):
    def test_matching_remote_script_skips_transfer(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))
            api.runner = Mock()
            api._run_saleae = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))  # type: ignore[method-assign]

            api._write_remote_saleae_text("run_full_array_burst_capture.py", "script\n")

            api.runner.run.assert_not_called()
            self.assertIn("sha256sum", api._run_saleae.call_args.args[0])

    def test_changed_script_uses_scp_then_atomic_remote_install(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))
            api._run_saleae = Mock(  # type: ignore[method-assign]
                side_effect=[
                    subprocess.CompletedProcess([], 1, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ]
            )
            api.runner = Mock()

            def transfer(cmd: list[str], *, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
                self.assertEqual(Path(cmd[-2]).read_bytes(), b"large script\n")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            api.runner.run.side_effect = transfer
            with patch("cell_api.shutil.which", side_effect=lambda name: "scp.exe" if name == "scp" else None):
                api._write_remote_saleae_text("run_full_array_burst_capture.py", "large script\n")

            transfer_cmd = api.runner.run.call_args.args[0]
            self.assertEqual(transfer_cmd[0], "scp.exe")
            self.assertTrue(transfer_cmd[-1].startswith("ubuntu-24-04@100.98.132.51:/home/ubuntu-24-04/saleae-api/"))
            self.assertIn("chmod 755", api._run_saleae.call_args.args[0])
            self.assertIn("mv -f", api._run_saleae.call_args.args[0])


class HardwareQueueTests(unittest.TestCase):
    def test_acquire_accepts_verified_lock_after_ssh_return_timeout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))
            api.runner = Mock()
            api.runner.ssh.side_effect = [
                subprocess.TimeoutExpired(["ssh"], 20),
                subprocess.CompletedProcess(["ssh"], 0, "queue-token\n", ""),
            ]

            api._acquire_hardware_queue("user@example.test", "queue-token", "owner", "set")

            self.assertEqual(api.runner.ssh.call_count, 2)
            self.assertIn("/token", api.runner.ssh.call_args_list[1].args[1])

    def test_owner_lookup_timeout_is_nonfatal(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))
            api.runner = Mock()
            api.runner.ssh.side_effect = subprocess.TimeoutExpired(["ssh"], 10)

            self.assertEqual(api._hardware_queue_owner("user@example.test"), "")


class DacTeensyRecoveryTests(unittest.TestCase):
    def test_missing_configured_serial_port_triggers_reflash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))
            output = (
                "serial.serialutil.SerialException: [Errno 2] could not open port "
                f"{api.config.adc_dac_port}: [Errno 2] No such file or directory"
            )

            self.assertTrue(api._dac_teensy_needs_reflash(output))

    def test_unrelated_missing_port_does_not_trigger_dac_reflash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=False))

            self.assertFalse(
                api._dac_teensy_needs_reflash(
                    "SerialException: could not open port /dev/ttyACM99: No such file or directory"
                )
            )


if __name__ == "__main__":
    unittest.main()
