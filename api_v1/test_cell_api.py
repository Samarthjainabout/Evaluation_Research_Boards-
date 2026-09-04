import subprocess
import sys
import base64
import hashlib
import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cell_api import (
    FPGA_RUNTIME_BITSTREAM,
    WINDOWS_REMOTE_COMMAND_LIMIT,
    RESET_PROGRAM_VCC_SET_V,
    RESET_PROGRAM_VCC_SET_SWEEP_V,
    RESET_PROGRAM_VCC_WL_V,
    SET_PROGRAM_VCC_SET_V,
    SET_PROGRAM_VCC_WL_V,
    CellAddress,
    CellOperationResult,
    CommandRunner,
    PasswordSSHProcess,
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

    def test_password_process_drains_output_and_preserves_exit_code(self) -> None:
        channel = Mock()
        channel.recv.side_effect = [b"\xe2", b"\x82\xac output\n", b""]
        channel.recv_exit_status.return_value = 7
        client = Mock()
        output = io.StringIO()
        with patch.object(CommandRunner, "_open_password_ssh_channel", return_value=(client, channel)) as open_channel:
            proc = CommandRunner().start_password_ssh_process(
                "user@example.test", "private-password", "daemon", log=output, timeout_s=15,
            )
            self.assertEqual(proc.wait(timeout=2), 7)
        self.assertEqual(proc.poll(), 7)
        self.assertEqual(output.getvalue(), "€ output\n")
        self.assertNotIn("private-password", repr(proc.args) + output.getvalue())
        open_channel.assert_called_once_with("user@example.test", "private-password", "daemon", timeout_s=15)
        channel.settimeout.assert_called_once_with(None)
        client.get_transport().set_keepalive.assert_called_once_with(30)
        channel.close.assert_called_once()
        client.close.assert_called_once()

    def test_password_process_wait_timeout_and_terminate_close_session(self) -> None:
        released = threading.Event()
        channel = Mock()
        def recv(size):
            if not released.wait(2):
                raise AssertionError("test channel was not closed")
            return b""
        channel.recv.side_effect = recv
        channel.close.side_effect = released.set
        channel.recv_exit_status.return_value = -1
        client = Mock()
        proc = PasswordSSHProcess(client, channel, "user@example.test", io.StringIO())
        try:
            self.assertIsNone(proc.poll())
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.wait(timeout=0.01)
            proc.terminate()
            self.assertEqual(proc.wait(timeout=2), -15)
            self.assertTrue(client.close.called)
        finally:
            released.set()
            proc.wait(timeout=2)

    def test_password_process_failure_does_not_log_exception_secrets(self) -> None:
        channel = Mock()
        channel.recv.side_effect = OSError("sensitive details")
        client = Mock()
        output = io.StringIO()
        proc = PasswordSSHProcess(client, channel, "user@example.test", output)
        self.assertEqual(proc.wait(timeout=2), -1)
        self.assertIn("OSError", output.getvalue())
        self.assertNotIn("sensitive details", output.getvalue())
        client.close.assert_called_once()

    def test_password_process_cancel_tolerates_disconnected_close(self) -> None:
        released = threading.Event()
        channel = Mock()
        def recv(_size):
            released.wait(2)
            return b""
        def disconnected_close():
            released.set()
            raise EOFError()
        channel.recv.side_effect = recv
        channel.close.side_effect = disconnected_close
        channel.recv_exit_status.return_value = -1
        client = Mock()
        client.close.side_effect = OSError("already disconnected")
        proc = PasswordSSHProcess(client, channel, "user@example.test", io.StringIO())
        proc.terminate()
        self.assertEqual(proc.wait(timeout=2), -15)
        proc.terminate()  # Repeated cleanup is harmless.

    def test_password_process_normal_exit_survives_close_eof(self) -> None:
        channel = Mock()
        channel.recv.return_value = b""
        channel.recv_exit_status.return_value = 0
        channel.close.side_effect = EOFError()
        client = Mock()
        proc = PasswordSSHProcess(client, channel, "user@example.test", io.StringIO())
        self.assertEqual(proc.wait(timeout=2), 0)
        client.close.assert_called_once()

    def test_password_session_is_closed_if_exec_fails(self) -> None:
        channel = Mock()
        channel.exec_command.side_effect = RuntimeError("exec failed")
        client = Mock()
        client.get_transport().open_session.return_value = channel
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "exec failed"):
                CommandRunner._open_password_ssh_channel("user@example.test", "secret", "daemon", timeout_s=15)
        channel.close.assert_called_once()
        client.close.assert_called_once()


class RemoteUploadTests(unittest.TestCase):
    def test_password_uploads_small_empty_and_large_files_via_sftp(self) -> None:
        for data in (b"", b"source" * 2200, bytes(range(256)) * 16384):
            with self.subTest(size=len(data)), TemporaryDirectory() as temp_dir:
                api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), zynq_password="test-secret"))
                client, sftp = Mock(), Mock()
                client.open_sftp.return_value = sftp
                api.runner._open_password_ssh_client = Mock(return_value=client)
                api._promote_verified_windows_upload = Mock()
                api._run_zynq_powershell = Mock(side_effect=AssertionError("Payload must not enter the command line"))
                api._write_remote_binary("test.bin", data)
                api.runner._open_password_ssh_client.assert_called_once_with(api.config.zynq_host, "test-secret", timeout_s=30)
                transfer = sftp.putfo.call_args
                self.assertEqual(transfer.args[0].getvalue(), data)
                self.assertTrue(transfer.args[1].startswith(api.config.zynq_dir + "/.test.bin."))
                self.assertEqual(transfer.kwargs, {"file_size": len(data), "confirm": True})
                self.assertEqual(api._promote_verified_windows_upload.call_args.args[1:], ("test.bin", hashlib.sha256(data).hexdigest()))
                sftp.remove.assert_not_called()
                sftp.close.assert_called_once()
                client.close.assert_called_once()

    def test_failed_sftp_transfer_preserves_destination_and_cleans_own_stage(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), zynq_password="secret"))
            client, sftp = Mock(), Mock()
            client.open_sftp.return_value = sftp
            sftp.putfo.side_effect = OSError("transfer failed")
            api.runner._open_password_ssh_client = Mock(return_value=client)
            api._promote_verified_windows_upload = Mock()
            with self.assertRaisesRegex(OSError, "transfer failed"):
                api._write_remote_binary("existing.bit", b"new contents")
            api._promote_verified_windows_upload.assert_not_called()
            self.assertEqual(sftp.remove.call_args.args[0], sftp.putfo.call_args.args[1])
            self.assertNotEqual(sftp.remove.call_args.args[0], api.config.zynq_dir + "/existing.bit")
            client.close.assert_called_once()

    def test_hash_or_install_failure_cleans_stage_and_propagates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), zynq_password="secret"))
            client, sftp = Mock(), Mock()
            client.open_sftp.return_value = sftp
            api.runner._open_password_ssh_client = Mock(return_value=client)
            api._promote_verified_windows_upload = Mock(side_effect=RuntimeError("SHA256 mismatch"))
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                api._write_remote_binary("existing.bit", b"new contents")
            self.assertEqual(sftp.remove.call_args.args[0], sftp.putfo.call_args.args[1])
            sftp.close.assert_called_once()
            client.close.assert_called_once()

    def test_large_unicode_text_uses_the_binary_transfer_path(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))
        api._write_remote_binary = Mock()
        text = "# volts μS 中文\n" * 10000
        api._write_remote_text("script.tcl", text)
        api._write_remote_binary.assert_called_once_with("script.tcl", text.encode("utf-8"))

    def test_existing_key_auth_upload_keeps_scp(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir)))
            api.runner.run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
            api._write_remote_binary_sftp = Mock()
            with patch("cell_api.shutil.which", return_value="scp.exe"):
                api._write_remote_binary("source.v", b"contents")
            self.assertEqual(api.runner.run.call_args.args[0][0], "scp.exe")
            api._write_remote_binary_sftp.assert_not_called()
            self.assertFalse((Path(temp_dir) / ".source.v.upload").exists())

    def test_windows_fallback_bounds_final_encoded_commands_and_roundtrips(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir)))
            api._run_zynq = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
            data = bytes(range(256)) * 100
            with patch("cell_api.shutil.which", return_value=None):
                api._write_remote_binary("source.v", data)
            decoded = []
            for call in api._run_zynq.call_args_list:
                command = call.args[0]
                full = f"cd {api.config.zynq_dir} && {command}"
                self.assertLess(len(full.encode("utf-16le")) // 2, WINDOWS_REMOTE_COMMAND_LIMIT)
                decoded.append(base64.b64decode(command.split("-EncodedCommand ", 1)[1]).decode("utf-16le"))
            chunks = []
            for script in decoded:
                if "AppendAllText" in script:
                    chunks.append(script.split(", '", 1)[1].split("'", 1)[0])
            self.assertGreater(len(chunks), 1)
            self.assertTrue(all(len(chunk) <= 2000 for chunk in chunks))
            self.assertEqual(base64.b64decode("".join(chunks)), data)
            promote = next(script for script in decoded if "[IO.File]::Replace" in script)
            self.assertIn(hashlib.sha256(data).hexdigest(), promote)
            self.assertLess(promote.index("Get-FileHash"), promote.index("[IO.File]::Replace"))

    def test_final_command_guard_rejects_double_encoding_before_ssh(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))
        api._run_zynq = Mock()
        # The old inner-base64 20k threshold missed this 13KB source file.
        source = (Path(__file__).parent / "prerequisites/fpga_zynq7020/caravel_scan_debug_fpga.v").read_bytes()
        inner = base64.b64encode(source).decode()
        self.assertLess(len(inner), 20000)
        with self.assertRaisesRegex(ValueError, "command-line limit"):
            api._run_zynq_powershell(f"$b='{inner}'; Write-Output 'test'")
        api._run_zynq.assert_not_called()

    def test_dry_run_and_bad_filenames_never_connect(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True, zynq_password="secret"))
        api.runner._open_password_ssh_client = Mock()
        api._write_remote_binary("dry.bin", b"data")
        for name in ("../bad", "C:bad", "bad\\file", "", ".", ".."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                api._write_remote_binary(name, b"data")
        api.runner._open_password_ssh_client.assert_not_called()

    def test_windows_install_quotes_names_and_propagates_failure(self) -> None:
        api = ScanDebugCellAPI(ScanDebugConfig(dry_run=True))
        api._run_zynq_powershell = Mock(return_value=subprocess.CompletedProcess([], 1, "mismatch", ""))
        with self.assertRaisesRegex(RuntimeError, "mismatch"):
            api._promote_verified_windows_upload(".it's.tmp", "it's.v", "abc123")
        script = api._run_zynq_powershell.call_args.args[0]
        self.assertIn("'.it''s.tmp'", script)
        self.assertIn("'it''s.v'", script)
        self.assertIn("[IO.File]::Replace($stage, $dest, [NullString]::Value)", script)
        self.assertNotIn("Remove-Item", script)


class CaptureCopyTests(unittest.TestCase):
    def test_long_windows_capture_names_are_short_and_deterministic(self):
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=True))
            kind = 'r00c00_t027_preparation_initial_read_' + 'long_label_'*20
            with patch('cell_api.platform.system', return_value='Windows'):
                path = api._capture_local_path('/remote/capture', 1032, kind, RailVoltages(.5,2.5))
                again = api._capture_local_path('/remote/capture', 1032, kind, RailVoltages(.5,2.5))
                other = api._capture_local_path('/remote/other', 1032, kind, RailVoltages(.5,2.5))
            self.assertEqual(path, again)
            self.assertNotEqual(path, other)
            self.assertLessEqual(len(str(path.resolve())),180)
            self.assertRegex(path.name,r'^1032_[0-9a-f]{12}$')
            self.assertEqual(path.parent,Path(temp_dir)/'raw')

    def test_short_capture_names_are_unchanged(self):
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=True))
            with patch('cell_api.platform.system', return_value='Windows'):
                path=api._capture_local_path('/remote/capture',3,'read',RailVoltages(.5,2.5))
            self.assertEqual(path.name,'3_read_wl2500_capture')

    def test_excessively_long_capture_root_fails_clearly(self):
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), dry_run=True))
            api.config.run_dir=Path(temp_dir)/('deep_'*40)
            with patch('cell_api.platform.system', return_value='Windows'):
                with self.assertRaisesRegex(RuntimeError,'shorter --run-dir'):
                    api._capture_local_path('/remote/capture',3,'read',RailVoltages(.5,2.5))

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
        self.assertEqual(config.set_sweep.vcc_set_v, (2.3,))
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


class BurstWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(self.temp.name)))
        self.api._append_progress = Mock()

    def test_capture_ssh_is_noninteractive_and_detects_dead_peer(self):
        with patch("cell_api.subprocess.Popen") as popen:
            self.api._popen_saleae("capture")
        args = popen.call_args.args[0]
        for option in ("BatchMode=yes", "ConnectTimeout=10", "ServerAliveInterval=10", "ServerAliveCountMax=3"):
            self.assertIn(option, args)
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_missing_capture_completion_is_not_a_fifteen_minute_wait(self):
        proc = Mock()
        proc.poll.return_value = None
        lines = []
        with patch("cell_api.time.monotonic", side_effect=[0, 61]):
            self.assertTrue(self.api._wait_burst_capture(proc, lines, 900, 60))
        proc.kill.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5)
        self.assertIn("capture completion timeout", lines[-1])

    def test_export_gets_longer_budget_and_reports_elapsed_time(self):
        proc = Mock()
        proc.poll.side_effect = [None, 0]
        lines = ["BURST_STAGE Capture complete; exporting waveform data\n"]
        with patch("cell_api.time.monotonic", side_effect=[0, 61]):
            self.assertFalse(self.api._wait_burst_capture(proc, lines, 900, 60))
        proc.kill.assert_not_called()
        self.assertIn("export/analysis", self.api._append_progress.call_args.args[1])

    def test_export_still_has_a_hard_deadline(self):
        proc = Mock()
        proc.poll.return_value = None
        with patch("cell_api.time.monotonic", side_effect=[0, 901]):
            self.assertTrue(self.api._wait_burst_capture(proc, ["BURST_STAGE exported\n"], 900, 60))
        proc.kill.assert_called_once()

    def test_ssh_exit_does_not_wait_for_watchdog(self):
        proc = Mock()
        proc.poll.return_value = 255
        self.assertFalse(self.api._wait_burst_capture(proc, [], 900, 60))
        proc.wait.assert_not_called()

    def test_watchdog_terminates_a_real_silent_local_child(self):
        # No SSH or hardware: exercise real process wait/kill/cleanup.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(self.api._wait_burst_capture(proc, [], 2, 0.05))
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_lost_lease_skips_runtime_cleanup(self):
        self.api._acquire_hardware_queue = Mock()
        self.api._release_hardware_queue = Mock()
        self.api._wait_for_pending_capture_copies = Mock()
        self.api._stop_runtime_vio_daemon = Mock()
        with self.api.hardware_queue("read-array"):
            self.assertIsNotNone(self.api._hardware_queue_lease)
            self.api._hardware_queue_ownership_lost = True
        self.api._stop_runtime_vio_daemon.assert_not_called()
        self.assertIsNone(self.api._hardware_queue_lease)

    def test_output_is_written_before_reader_finishes(self):
        log = Path(self.temp.name) / "capture.log"
        def output():
            yield "SINGLE_CAPTURE_ARMED\n"
            self.assertIn("SINGLE_CAPTURE_ARMED", log.read_text())
            yield "BURST_STAGE Capture complete; exporting waveform data\n"
            yield "Timeout, server test not responding.\n"
        proc = Mock(stdout=output())
        done = threading.Event()
        lines = []
        self.api._stream_burst_output(proc, lines, done, log)
        self.assertTrue(done.is_set())
        self.assertEqual(len(lines), 3)
        self.assertFalse(self.api._append_progress.call_args.kwargs["ok"])

    def test_reconnect_waits_then_checks_lock_before_restart(self):
        self.api._run_saleae = Mock(side_effect=[subprocess.TimeoutExpired([], 10), subprocess.CompletedProcess([], 0)])
        sequence = Mock()
        self.api._restore_burst_queue_ownership = sequence.lock
        self.api._restart_saleae_automation = sequence.restart
        with patch("cell_api.time.sleep"):
            self.api._reconnect_burst_capture(0, 1)
        self.assertEqual([c[0] for c in sequence.mock_calls], ["lock", "restart"])

    def test_unreachable_vm_fails_bounded_without_restart(self):
        self.api._run_saleae = Mock(return_value=subprocess.CompletedProcess([], 255))
        self.api._restart_saleae_automation = Mock()
        with patch("cell_api.time.monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(RuntimeError, "unreachable"):
                self.api._reconnect_burst_capture(0, 1)
        self.api._restart_saleae_automation.assert_not_called()

    def test_lost_queue_cannot_restart_another_workers_capture(self):
        self.api._hardware_queue_lease = ("user@host", "token", "owner", "read-array")
        self.api.runner.ssh = Mock(return_value=subprocess.CompletedProcess([], 1))
        self.api._run_saleae = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.api._restart_saleae_automation = Mock()
        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            self.api._reconnect_burst_capture(0, 1)
        self.api._restart_saleae_automation.assert_not_called()
        self.assertTrue(self.api._hardware_queue_ownership_lost)
        self.assertNotIn("rm ", self.api.runner.ssh.call_args.args[1])

    def test_queue_can_be_confirmed_or_recreated_without_stealing(self):
        self.api._hardware_queue_lease = ("user@host", "token", "owner", "read-array")
        self.api.runner.ssh = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.api._restore_burst_queue_ownership()
        command = self.api.runner.ssh.call_args.args[1]
        self.assertIn('mkdir "$lock_dir"', command)
        self.assertIn('"$token"', command)
        self.assertNotIn("rm ", command)
        self.assertFalse(self.api._hardware_queue_ownership_lost)

    def test_restart_failure_is_not_reported_as_recovered(self):
        self.api.runner.ssh = Mock(return_value=subprocess.CompletedProcess([], 1, "no physical device"))
        with self.assertRaisesRegex(RuntimeError, "restart/device check failed"):
            self.api._restart_saleae_automation(0, "read_array_burst", 1)

    def test_burst_retries_transport_failure_and_preserves_attempt_logs(self):
        first = Mock(stdout=io.StringIO("SINGLE_CAPTURE_ARMED\nTimeout, server test not responding.\n"), returncode=255)
        first.poll.return_value = 255
        second = Mock(stdout=io.StringIO("OUTPUT_ROOT=/test/capture\nSINGLE_CAPTURE_ARMED\nDONE output_root=/test/capture\n"), returncode=0)
        second.poll.return_value = 0
        self.api._popen_saleae = Mock(side_effect=[first, second])
        self.api._program_fpga = Mock(return_value=0)
        self.api._reconnect_burst_capture = Mock()
        def thread(*, target, args, daemon):
            return Mock(start=lambda: target(*args))
        with patch("cell_api.threading.Thread", side_effect=thread), patch("cell_api.time.sleep"):
            output = self.api._capture_array_burst(0, self.api.config.read_rails, "test.bit", 0, 1024, 0, 0)
        self.assertEqual(output, "/test/capture")
        self.api._reconnect_burst_capture.assert_called_once_with(0, 1)
        self.assertEqual(self.api._program_fpga.call_count, 2)
        self.assertIn("ERROR: Capture SSH connection lost", (Path(self.temp.name) / "capture_0_read_array_burst_attempt1.log").read_text())

    def test_burst_retries_are_bounded(self):
        self.api.config.attempts = 1
        def proc(*args):
            result = Mock(stdout=io.StringIO("Connection reset by peer\n"), returncode=255)
            result.poll.return_value = 255
            return result
        self.api._popen_saleae = Mock(side_effect=proc)
        self.api._program_fpga = Mock()
        self.api._reconnect_burst_capture = Mock()
        def thread(*, target, args, daemon):
            return Mock(start=lambda: target(*args))
        with patch("cell_api.threading.Thread", side_effect=thread), patch("cell_api.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 2 attempts"):
                self.api._capture_array_burst(0, self.api.config.read_rails, "test.bit", 0, 1024, 0, 0)
        self.assertEqual(self.api._popen_saleae.call_count, 2)
        self.api._program_fpga.assert_not_called()


class FpgaDacBitstreamTests(unittest.TestCase):
    def test_runtime_password_startup_uses_owned_paramiko_session(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), zynq_password="private-password"))
            api._ensure_remote_fpga_sources = Mock()
            api._run_zynq_cmd = Mock(return_value=subprocess.CompletedProcess([], 0, "runtime_vio_daemon.heartbeat", ""))
            process = Mock()
            process.poll.return_value = None
            api.runner.start_password_ssh_process = Mock(return_value=process)
            with patch("cell_api.subprocess.Popen") as popen:
                try:
                    api._ensure_runtime_vio_daemon()
                    self.assertTrue(api._runtime_daemon_ready)
                    popen.assert_not_called()
                    call = api.runner.start_password_ssh_process.call_args
                    self.assertEqual(call.args[:2], (api.config.zynq_host, "private-password"))
                    self.assertNotIn("private-password", call.args[2])
                    api._ensure_runtime_vio_daemon()
                    api.runner.start_password_ssh_process.assert_called_once()
                    api._run_zynq_cmd.return_value = subprocess.CompletedProcess([], 1, "", "")
                    api._stop_runtime_vio_daemon()
                    process.wait.assert_called_once_with(timeout=5)
                    self.assertIsNone(api._runtime_daemon_process)
                    self.assertIsNone(api._runtime_daemon_log_handle)
                finally:
                    if api._runtime_daemon_log_handle:
                        api._runtime_daemon_log_handle.close()

    def test_runtime_key_auth_still_uses_noninteractive_openssh(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir)))
            api._ensure_remote_fpga_sources = Mock()
            api._run_zynq_cmd = Mock(return_value=subprocess.CompletedProcess([], 0, "runtime_vio_daemon.heartbeat", ""))
            api.runner.start_password_ssh_process = Mock()
            process = Mock()
            process.poll.return_value = None
            with patch("cell_api.subprocess.Popen", return_value=process) as popen:
                try:
                    api._ensure_runtime_vio_daemon()
                    self.assertTrue(api._runtime_daemon_ready)
                    self.assertIn("BatchMode=yes", popen.call_args.args[0])
                    api.runner.start_password_ssh_process.assert_not_called()
                finally:
                    if api._runtime_daemon_log_handle:
                        api._runtime_daemon_log_handle.close()

    def test_runtime_password_launch_failure_closes_log(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), zynq_password="secret"))
            api._ensure_remote_fpga_sources = Mock()
            api._run_zynq_cmd = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
            api.runner.start_password_ssh_process = Mock(side_effect=RuntimeError("authentication failed"))
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                api._ensure_runtime_vio_daemon()
            self.assertFalse(api._runtime_daemon_ready)
            self.assertIsNone(api._runtime_daemon_log_handle)

    def test_runtime_password_early_exit_is_not_reported_ready(self) -> None:
        with TemporaryDirectory() as temp_dir:
            api = ScanDebugCellAPI(ScanDebugConfig(run_dir=Path(temp_dir), zynq_password="secret"))
            api._ensure_remote_fpga_sources = Mock()
            api._run_zynq_cmd = Mock(return_value=subprocess.CompletedProcess([], 0, "daemon exited", ""))
            process = Mock()
            process.poll.return_value = 1
            api.runner.start_password_ssh_process = Mock(return_value=process)
            with self.assertRaisesRegex(RuntimeError, "daemon exited"):
                api._ensure_runtime_vio_daemon()
            self.assertFalse(api._runtime_daemon_ready)
            self.assertIsNone(api._runtime_daemon_process)
            self.assertIsNone(api._runtime_daemon_log_handle)

    def test_fpga_rails_match_user_requested_legacy_bench_profile(self) -> None:
        runtime_spi = (
            Path(__file__).parent
            / "prerequisites"
            / "fpga_zynq7020"
            / "dac81416_runtime_spi.v"
        ).read_text()
        compile_time_spi = (
            Path(__file__).parent
            / "prerequisites"
            / "fpga_zynq7020"
            / "dac81416_spi.v"
        ).read_text()

        # Explicit bench rollback requested on 2026-09-03, not a nominal core
        # voltage recommendation: DAC7=4.0 V and DAC15~=2.1 V.
        self.assertIn("DEFAULT_VDDIO_CODE   = 16'hCCCC", runtime_spi)
        self.assertIn("{8'h17, DEFAULT_VDDIO_CODE}", runtime_spi)
        self.assertIn("24'h17CCCC", compile_time_spi)
        self.assertIn("24'h1F6B85", runtime_spi)
        self.assertIn("24'h1F6B85", compile_time_spi)
        self.assertIn("VCCD2 is normally 1.8 V", runtime_spi)
        self.assertIn("VCCD2 is normally 1.8 V", compile_time_spi)
        self.assertNotIn("24'h1F8000", runtime_spi)
        self.assertNotIn("24'h1F8000", compile_time_spi)
        self.assertNotIn("24'h1F5C29", runtime_spi)
        self.assertNotIn("24'h1F5C29", compile_time_spi)

    def test_runtime_daemon_timeout_scales_with_packet_count(self) -> None:
        source = (
            Path(__file__).parent
            / "prerequisites"
            / "fpga_zynq7020"
            / "runtime_vio_daemon.tcl"
        ).read_text()

        self.assertIn("set packet_count [expr {($command_value >> 41) & 0x7FF}]", source)
        self.assertIn("set command_timeout_ms [expr {10000 + ($packet_count * 20)}]", source)
        self.assertIn("[clock milliseconds] + $command_timeout_ms", source)

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


class InvalidReadFeedbackTests(unittest.TestCase):
    def test_runtime_timeout_retries_read_only(self):
        with TemporaryDirectory() as directory:
            api = ScanDebugCellAPI(ScanDebugConfig(
                run_dir=Path(directory), persistent_fpga_runtime=False,
                read_feedback_attempts=3,
            ))
            recovered = CellOperationResult(
                cell=CellAddress(0, 0), operation="read", packet="0x0000",
                rails=api.config.read_rails, current_uA=35.0,
                decoded_packet="0x0000", ok=True, local_output_dir="test",
            )
            api._pulse_and_capture_once = Mock(side_effect=[
                RuntimeError("persistent FPGA runtime command timed out"), recovered,
            ])
            api._program_pulse = Mock()

            result = api.read(0, 0)

            self.assertEqual(result.current_uA, 35.0)
            self.assertEqual(result.feedback_attempts, 2)
            self.assertEqual(api._pulse_and_capture_once.call_count, 2)
            api._program_pulse.assert_not_called()

    def test_zero_sample_capture_retries_read_only(self):
        with TemporaryDirectory() as directory:
            api = ScanDebugCellAPI(ScanDebugConfig(
                run_dir=Path(directory), persistent_fpga_runtime=False,
                defer_capture_copy=False, attempts=1, read_feedback_attempts=3,
            ))
            api._ensure_saleae_capture_script = Mock()
            api._ensure_bitstream = Mock(return_value="test.bit")
            api._capture_remote = Mock(side_effect=["remote-1", "remote-2"])
            api._copy_capture = Mock(return_value=Path(directory))
            zero_samples = RuntimeError("samples=0")
            api._summarize_capture = Mock(side_effect=[
                zero_samples, zero_samples,
                {"ok": True, "decoded_packet": "0x0000", "la_set_window_mean_uA": 35.0},
            ])
            api._program_pulse = Mock()

            result = api.read(0, 0)

            self.assertTrue(result.ok)
            self.assertEqual(result.current_uA, 35.0)
            self.assertEqual(result.feedback_attempts, 2)
            self.assertEqual(api._capture_remote.call_count, 2)
            api._program_pulse.assert_not_called()

    def test_invalid_initial_read_stops_cycle_and_records_failure(self):
        import csv
        for current in (-16.49, float("nan"), float("inf"), None):
            with self.subTest(current=current), TemporaryDirectory() as directory:
                api = ScanDebugCellAPI(ScanDebugConfig(
                    run_dir=Path(directory), persistent_fpga_runtime=False, defer_capture_copy=False,
                ))
                api._ensure_saleae_capture_script = Mock()
                api._ensure_bitstream = Mock(return_value="test.bit")
                api._capture_remote = Mock(return_value="remote")
                api._copy_capture = Mock(return_value=Path(directory))
                api._summarize_capture = Mock(return_value={
                    "ok": True, "decoded_packet": "0x0000", "la_set_window_mean_uA": current,
                })
                api._program_pulse = Mock()
                with self.assertRaisesRegex(RuntimeError, "Invalid read feedback"):
                    api.cycle_cell(0, 0)
                api._program_pulse.assert_not_called()
                with api.manifest.open(newline="") as handle:
                    row = list(csv.DictReader(handle))[-1]
                self.assertEqual(row["ok"], "False")
                self.assertIn("Invalid read feedback", row["error"])

    def test_nonnegative_read_remains_available(self):
        for current in (0.0, 25.0):
            with self.subTest(current=current), TemporaryDirectory() as directory:
                api = ScanDebugCellAPI(ScanDebugConfig(
                    run_dir=Path(directory), persistent_fpga_runtime=False, defer_capture_copy=False,
                ))
                api._ensure_saleae_capture_script = Mock()
                api._ensure_bitstream = Mock(return_value="test.bit")
                api._capture_remote = Mock(return_value="remote")
                api._copy_capture = Mock(return_value=Path(directory))
                api._summarize_capture = Mock(return_value={
                    "ok": True, "decoded_packet": "0x0000", "la_set_window_mean_uA": current,
                })
                result = api.read(0, 0)
                self.assertTrue(result.ok)
                self.assertEqual(result.current_uA, current)


if __name__ == "__main__":
    unittest.main()
