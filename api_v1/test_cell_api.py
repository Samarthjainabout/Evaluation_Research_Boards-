import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cell_api import FPGA_RUNTIME_BITSTREAM, CommandRunner, RailVoltages, ScanDebugCellAPI, ScanDebugConfig


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

    def test_state_threshold_boundaries_are_strict(self) -> None:
        self.assertFalse(ScanDebugCellAPI._passes(70.0, 70.0, "above"))
        self.assertTrue(ScanDebugCellAPI._passes(70.001, 70.0, "above"))
        self.assertFalse(ScanDebugCellAPI._passes(5.0, 5.0, "below"))
        self.assertTrue(ScanDebugCellAPI._passes(4.999, 5.0, "below"))


class SaleaeRecoveryTests(unittest.TestCase):
    def test_capture_read_timeout_triggers_usb_recovery(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))

        self.assertTrue(
            api._usb_needs_recovery(
                "saleae.automation.errors.DeviceError: "
                "Error interacting with device during capture: ReadTimeout."
            )
        )


class FpgaDacBitstreamTests(unittest.TestCase):
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
