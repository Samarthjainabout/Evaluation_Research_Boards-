from __future__ import annotations

import struct
import unittest
import zlib
from tkinter import Tcl

from array_4bit_live.config import CONTROLLER_ARTIFACTS


class BulkControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CONTROLLER_ARTIFACTS["control_tcl"].read_text(encoding="utf-8")
        start = cls.source.index("proc bulk_header_decode {hex} {")
        end = cls.source.index("# Establish page zero explicitly", start)
        cls.helpers = cls.source[start:end]
        scan_start = cls.source.index("proc bulk_scan_block {")
        scan_end = cls.source.index("\nproc bulk_tdc_snapshot", scan_start)
        cls.scan = cls.source[scan_start:scan_end]
        tdc_start = scan_end + 1
        tdc_end = cls.source.index("\nproc bulk_profile_check_run", tdc_start)
        cls.tdc = cls.source[tdc_start:tdc_end]
        preflight_start = tdc_end + 1
        preflight_end = cls.source.index("\nproc scan_reset_guarded_run", preflight_start)
        cls.preflight = cls.source[preflight_start:preflight_end]

    def test_crc32_matches_iso_hdlc_little_endian_word(self) -> None:
        tcl = Tcl()
        tcl.eval(self.helpers)
        actual = int(
            tcl.eval(
                "expr {[bulk_crc32_word 0xFFFFFFFF 0x12345678]^0xFFFFFFFF}"
            )
        )
        expected = zlib.crc32(struct.pack("<I", 0x12345678)) & 0xFFFFFFFF
        self.assertEqual(actual, expected)

    def test_job_tag_xor_folds_all_eight_chunks(self) -> None:
        tcl = Tcl()
        tcl.eval(self.helpers)
        job_id = "0123456789abcdeffedcba9876543210"
        expected = 0
        for offset in range(0, 32, 4):
            expected ^= int(job_id[offset : offset + 4], 16)
        self.assertEqual(int(tcl.eval(f"bulk_job_tag {job_id}")), expected & 0xFFFF)

    def test_header_v2_bit_map_decodes_exactly(self) -> None:
        tcl = Tcl()
        tcl.eval(self.helpers)
        header = (
            (0x42554C4B << 224)
            | (2 << 216)
            | (0b01000111 << 208)
            | (0x89ABCDEF << 176)
            | (0x1234 << 160)
            | (0x11111111 << 128)
            | (0x22222222 << 96)
            | (0x33333333 << 64)
            | (0x1FF << 55)
            | (0x155 << 46)
            | (0x7FFF << 31)
            | (0x101 << 22)
            | (0x055 << 13)
            | (1 << 12)
            | (0 << 11)
            | (7 << 6)
            | (1 << 1)
            | 1
        )
        decoded = tcl.splitlist(tcl.eval(f"bulk_header_decode {header:064X}"))
        fields = dict(zip(decoded[0::2], decoded[1::2], strict=True))
        self.assertEqual(fields["signature"], str(0x42554C4B))
        self.assertEqual(fields["version"], "2")
        self.assertEqual(fields["sequence"], str(0x89ABCDEF))
        self.assertEqual(fields["job_tag"], str(0x1234))
        self.assertEqual(fields["pre_valid"], str(0x1FF))
        self.assertEqual(fields["post_valid"], str(0x155))
        self.assertEqual(fields["tdc_valid"], str(0x7FFF))
        self.assertEqual(fields["pre_sentinel_first"], "1")
        self.assertEqual(fields["post_sentinel_first"], "0")
        self.assertEqual(fields["row"], "7")
        self.assertEqual(fields["column"], "1")
        self.assertEqual(fields["valid"], "1")

    def test_exact_probe_capability_and_no_legacy_fallback(self) -> None:
        self.assertIn(
            "BULK_TRANSPORT_CAPABILITY signature=BPG2 version=2 "
            "input_probes=12 output_probes=1 page_count=4 page_lanes=10 "
            "selector_bits=3 keepalive_bit=3 selector_cycles=4 refreshes=5 "
            "scan_frames=18 tdc_words=15 "
            "job16=0x%04X initial_sequence=0x%08X pass=1",
            self.source,
        )
        self.assertIn("if {[llength $bulk_inputs]!=12}", self.source)
        self.assertIn("if {[llength $bulk_outputs]!=1}", self.source)
        self.assertIn("Bulk evidence VIO must expose exactly 12 input probes", self.source)
        self.assertIn(
            "Bulk evidence VIO must expose exactly one page-control output probe",
            self.source,
        )
        self.assertIn("set bulk_header_probe [bulk_probe bulk_header]", self.source)
        self.assertIn(
            "set bulk_page_descriptor_probe [bulk_probe bulk_page_descriptor]",
            self.source,
        )
        self.assertIn("for {set i 0} {$i<10} {incr i}", self.source)
        self.assertIn('bulk_probe "bulk_page_lane${i}"', self.source)
        self.assertIn(
            "set bulk_page_control_probe [lindex $bulk_outputs 0]", self.source
        )
        self.assertIn(
            "set_property OUTPUT_VALUE 0 $bulk_page_control_probe", self.source
        )
        self.assertIn("commit_hw_vio $bulk_page_control_probe", self.source)
        self.assertEqual(self.source.count("[bulk_page_read startup 0 0]"), 1)
        self.assertNotIn("bulk_page_read startup 1", self.source)
        self.assertNotIn("lassign [command 5 $i]", self.source)

    def test_page_descriptor_v2_bit_map_decodes_exactly(self) -> None:
        tcl = Tcl()
        tcl.eval(self.helpers)
        descriptor = (
            (0x42504732 << 224)
            | (2 << 216)
            | (3 << 208)
            | (0x89ABCDEF << 176)
            | (0x1234 << 160)
            | (2 << 152)
            | (1 << 151)
            | (1 << 150)
            | (0x3FF << 140)
            | (7 << 135)
            | (1 << 130)
            | (0x1FF << 121)
            | (0x155 << 112)
            | (0x7FFF << 97)
            | (1 << 96)
            | (1 << 95)
            | (1 << 94)
            | (1 << 90)
            | (1 << 71)
        )
        decoded = tcl.splitlist(
            tcl.eval(f"bulk_page_descriptor_decode {descriptor:064X}")
        )
        fields = dict(zip(decoded[0::2], decoded[1::2], strict=True))
        self.assertEqual(fields["signature"], str(0x42504732))
        self.assertEqual(fields["version"], "2")
        self.assertEqual(fields["page"], "3")
        self.assertEqual(fields["sequence"], str(0x89ABCDEF))
        self.assertEqual(fields["job_tag"], str(0x1234))
        self.assertEqual(fields["header_version"], "2")
        self.assertEqual(fields["header_valid"], "1")
        self.assertEqual(fields["page_valid"], "1")
        self.assertEqual(fields["lane_valid"], str(0x3FF))
        self.assertEqual(fields["reserved"], "0")

    def test_scan_block_fails_closed_on_every_identity_field(self) -> None:
        for evidence in (
            "header_valid",
            "job_tag",
            "address",
            "job_mismatch",
            "wb_protocol_error",
            "any_fault",
            "any_clip",
            "clip_mask_disagrees",
            "clipped_frame",
            "crc",
            "completeness",
            "order",
            "sequence",
            "epoch_not_cleared",
            "pre_association",
            "wb_association",
            "missing_wb_echo",
            "unexpected_wb_echo",
        ):
            self.assertIn(evidence, self.scan)
        self.assertIn("scan_guard_current_ua $pair $r $c", self.scan)
        self.assertIn("phase_p_sentinels $r $c", self.scan)
        self.assertIn("set target_pairs [lrange $pairs 0 4]", self.scan)
        self.assertIn("set sentinel_pairs [lrange $pairs 5 8]", self.scan)
        self.assertIn("for {set slot 0} {$slot<9} {incr slot}", self.scan)
        self.assertIn("set first_page [bulk_page_read pre 0]", self.scan)
        self.assertIn("set second_page [bulk_page_read pre 1]", self.scan)
        self.assertIn("set first_page [bulk_page_read post 2]", self.scan)
        self.assertIn("set second_page [bulk_page_read post 3]", self.scan)
        self.assertIn("bulk_pre_token_publish", self.scan)

    def test_tdc_bank_requires_current_epoch_mask_crc_and_padding(self) -> None:
        for evidence in (
            "header_valid",
            "job_tag",
            "address",
            "job_mismatch",
            "wb_protocol_error",
            "wb_echo",
            "any_fault",
            "any_clip",
            "completeness",
            "high_padding",
            "crc",
            "sequence",
            "epoch_not_cleared",
            "sequence_or_target",
            "pre_page1_changed",
        ):
            self.assertIn(evidence, self.tdc)
        self.assertIn("[dict get $header tdc_valid]!=0x7FFF", self.tdc)
        self.assertIn("for {set i 0} {$i<8} {incr i}", self.tdc)
        self.assertIn("for {set i 0} {$i<7} {incr i}", self.tdc)
        self.assertIn("bulk_page_read wb 1 0", self.tdc)
        self.assertIn("$bulk_committed_pre_valid", self.tdc)
        self.assertIn(
            "[lrange $wb_page_lanes 0 7] ne $bulk_committed_pre_tail",
            self.tdc,
        )
        self.assertIn("packet_echo=0x%08X", self.source)
        self.assertIn(
            "$bulk_job_tag16 [dict get $bulk_tdc sequence] $r $c $op $value",
            self.source,
        )

    def test_read_only_preflight_exercises_both_banks_and_wb_read(self) -> None:
        self.assertIn("bulk_scan_block $r $c pre 1", self.preflight)
        self.assertIn("wb_capture $r $c read 0xFF 1", self.preflight)
        self.assertIn("bulk_scan_block $r $c post 0", self.preflight)
        self.assertIn("target_pre=5 target_post=5", self.preflight)
        self.assertIn("sentinel_pre=4 sentinel_post=4", self.preflight)
        self.assertIn("wb_read_packets=1 program_packets=0 program_pulses=0", self.preflight)
        self.assertNotIn("program_step", self.preflight)
        self.assertNotIn("wb_capture $r $c set", self.preflight)
        self.assertNotIn("wb_capture $r $c reset", self.preflight)

    def test_only_complete_prebank_paths_request_a_matched_wb_epoch(self) -> None:
        self.assertIn(
            "proc wb_capture {r c op {pwm 0xFF} {match_pre_epoch 0}} {",
            self.source,
        )
        self.assertIn("set open_epoch [expr {!$match_pre_epoch}]", self.source)
        self.assertIn(
            'error "Matched WB request has no current bulk pre epoch for r$r/c$c"',
            self.source,
        )
        self.assertIn("wb_capture $r $c read 0xFF 1", self.preflight)
        self.assertIn("wb_capture $r $c $op $pwm 1", self.source)
        self.assertIn(
            "wb_capture $row $col read; wb_capture $row $col set; "
            "wb_capture $row $col read",
            self.source,
        )
        self.assertNotIn("wb_capture $row $col read 0xFF 1", self.source)
        self.assertNotIn("wb_capture $row $col set 0xFF 1", self.source)

    def test_stale_fault_is_recoverable_but_keepalive_still_fails_closed(self) -> None:
        gate_start = self.source.index("if {[dict get $bulk_initial_header signature]")
        capability_gate = self.source[
            gate_start : self.source.index("set bulk_job_tag16", gate_start)
        ]
        self.assertNotIn("version]!=1 ||", capability_gate)
        self.assertIn("version]!=2", capability_gate)
        self.assertIn("BULK_STALE_FAULT_RECOVERY_REQUIRED", capability_gate)
        self.assertNotIn("bulk_initial_tdc_high", capability_gate)
        page_start = self.source.index("proc bulk_page_read")
        page_end = self.source.index("proc bulk_job_tag", page_start)
        page_read = self.source[page_start:page_end]
        self.assertIn("&0xF)!=$page", page_read)
        self.assertIn("![dict get $descriptor page_valid]", page_read)
        self.assertIn("[dict get $descriptor lane_valid]!=0x3FF", page_read)

    def test_legacy_wb_word_view_advances_tag_and_selected_index(self) -> None:
        self.assertIn(
            "set word_view [string replace $base_h1 31 31 [format %X $i]]",
            self.source,
        )
        self.assertIn(
            "set word_view [string replace $word_view 54 55 "
            "[format %02X [expr {0x80+$i}]]]",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
