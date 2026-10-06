"""解析器测试：时标、作用域、同刻裁决、时间倒退、空白稳定性等。"""

import unittest

from vcd_service.errors import VCDServiceError
from vcd_service.parser import MAX_CHANGES, parse_vcd

from .helpers import raw_ts, signal, vcd


VALID_WAVE = vcd(
    signals="""\
        $var wire 1 ! clk $end
        $var wire 1 " data $end
    """,
    sim="""\
        #0
        $dumpvars
        0!
        1"
        $end
        #5
        1!
        #10
        0!
        1"
        #10
        x"
        #15
        z!
    """)


class ParseHappyPathTests(unittest.TestCase):
    def test_basic_parse_and_timescale_conversion(self):
        doc = parse_vcd(VALID_WAVE)
        self.assertEqual(doc.timescale_fs, 1_000_000)  # 1ns
        self.assertEqual(set(doc.signals),
                         {"top.dut.clk", "top.dut.data"})
        self.assertEqual(
            doc.changes["!"],
            [(0, "0"), (5_000_000, "1"), (10_000_000, "0"),
             (15_000_000, "z")])
        # 同刻对 data 的两次赋值都保留（文本顺序由窗口阶段裁决）
        self.assertEqual(
            doc.changes['"'],
            [(0, "1"), (10_000_000, "1"), (10_000_000, "x")])

    def test_all_supported_timescales(self):
        for ts, expected in (
                ("1fs", 1), ("10fs", 10), ("100fs", 100),
                ("1ps", 1_000), ("10ps", 10_000), ("100ps", 100_000),
                ("1ns", 1_000_000), ("10ns", 10_000_000),
                ("100ns", 100_000_000)):
            doc = parse_vcd(vcd(signals=signal("!", "a"),
                                sim="#0\n0!\n#1\n1!\n", timescale=ts))
            self.assertEqual(doc.timescale_fs, expected, ts)
            self.assertEqual(doc.changes["!"][1][0], expected)

    def test_timescale_no_space_form(self):
        # Icarus 风格：$timescale 10ps $end（数字与单位无空格）
        text = VALID_WAVE.replace("$timescale 1ns $end",
                                  "$timescale 10ps $end")
        self.assertEqual(parse_vcd(text).timescale_fs, 10_000)

    def test_identifier_and_scope_name_rules(self):
        doc = parse_vcd(vcd(
            signals="""\
                $var wire 1 ! a_1$ $end
                $upscope $end
                $scope module b_2$ $end
                $var wire 1 " ok $end
            """,
            sim="#0\n0!\n0\"\n"))
        self.assertEqual(set(doc.signals),
                         {"top.dut.a_1$", "top.b_2$.ok"})

    def test_dumpvars_at_nonzero_time(self):
        doc = parse_vcd(vcd(
            signals=signal("!", "a"),
            sim="""\
                #3
                $dumpvars
                x!
                $end
                #4
                1!
            """))
        self.assertEqual(doc.changes["!"],
                         [(3_000_000, "x"), (4_000_000, "1")])

    def test_comment_and_version_date_ignored(self):
        doc = parse_vcd(vcd(
            signals="""\
                $comment
                multi
                line
                $end
                $var wire 1 ! a $end
            """,
            sim="#0\n0!\n"))
        self.assertEqual(doc.changes["!"], [(0, "0")])

    def test_multiline_directives(self):
        doc = parse_vcd(vcd(
            signals="""\
                $var
                  wire 1 ! a $end
                $scope
                  module inner $end
                $var wire 1 " b $end
                $upscope $end
            """,
            sim="#0\n0!\n0\"\n"))
        self.assertEqual(set(doc.signals),
                         {"top.dut.a", "top.dut.inner.b"})

    def test_duplicate_full_name_rejected(self):
        with self.assertRaises(VCDServiceError) as cm:
            parse_vcd(vcd(
                signals="""\
                    $var wire 1 ! a $end
                    $var wire 1 " a $end
                """,
                sim="#0\n0!\n"))
        self.assertEqual(cm.exception.code, "DUPLICATE_SIGNAL")
        self.assertIsNotNone(cm.exception.line)

    def test_alias_shares_timeline(self):
        doc = parse_vcd(vcd(
            signals="""\
                $var wire 1 ! a $end
                $scope module inner $end
                $var wire 1 ! a2 $end
                $upscope $end
            """,
            sim="#0\n0!\n#1\n1!\n"))
        self.assertEqual(set(doc.signals),
                         {"top.dut.a", "top.dut.inner.a2"})
        self.assertEqual(doc.changes["!"],
                         [(0, "0"), (1_000_000, "1")])

    def test_hash_identifier_supported(self):
        # Icarus 标识符序列为 !、"、#，'#' 作为标识符必须可用
        doc = parse_vcd(vcd(signals=signal("#", "a"), sim="#0\n0#\n#1\n1#\n"))
        self.assertEqual(doc.changes["#"],
                         [(0, "0"), (1_000_000, "1")])


class ParseRejectionTests(unittest.TestCase):
    def assert_error(self, text, code):
        with self.assertRaises(VCDServiceError) as cm:
            parse_vcd(text)
        self.assertEqual(cm.exception.code, code)
        self.assertIsNotNone(cm.exception.line, "错误必须携带行号")

    def test_non_ascii(self):
        text = VALID_WAVE + "字\n"
        self.assert_error(text, "NON_ASCII")

    def test_missing_timescale(self):
        self.assert_error(
            "$scope module top $end\n$var wire 1 ! a $end\n"
            "$upscope $end\n$enddefinitions $end\n#0\n0!\n",
            "MISSING_TIMESCALE")

    def test_unsupported_timescale_units_and_multipliers(self):
        for bad in ("1us", "2ns", "0ns", "1ms", "1000ns", "1 s", "10us"):
            self.assert_error(raw_ts(bad), "UNSUPPORTED_TIMESCALE")

    def test_duplicate_timescale(self):
        text = VALID_WAVE.replace(
            "$timescale 1ns $end",
            "$timescale 1ns $end\n$comment x $end\n"
            "$timescale 10ns $end", 1)
        self.assert_error(text, "DUPLICATE_TIMESCALE")

    def test_reg_not_accepted(self):
        self.assert_error(
            vcd(signals=signal("!", "a").replace("wire", "reg"),
                sim="#0\n0!\n"),
            "UNSUPPORTED_VAR_KIND")

    def test_multibit_rejected(self):
        self.assert_error(
            vcd(signals="$var wire 8 ! bus $end", sim="#0\n0!\n"),
            "UNSUPPORTED_VAR_WIDTH")

    def test_invalid_scope_name(self):
        text = vcd(signals="").replace("module dut", "module 0bad")
        self.assert_error(text, "INVALID_SCOPE_NAME")

    def test_var_outside_scope(self):
        self.assert_error(
            "$timescale 1ns $end\n$var wire 1 ! a $end\n"
            "$scope module top $end\n$upscope $end\n"
            "$enddefinitions $end\n#0\n0!\n",
            "VAR_OUTSIDE_SCOPE")

    def test_unbalanced_upscope(self):
        manual = ("$timescale 1ns $end\n$scope module top $end\n"
                  "$var wire 1 ! a $end\n$upscope $end\n$upscope $end\n"
                  "$enddefinitions $end\n#0\n0!\n")
        self.assert_error(manual, "UNBALANCED_UPSCOPE")

    def test_unclosed_scope_rejected(self):
        text = ("$timescale 1ns $end\n$scope module top $end\n"
                "$scope module inner $end\n$var wire 1 ! a $end\n"
                "$upscope $end\n$enddefinitions $end\n#0\n0!\n")
        self.assert_error(text, "UNBALANCED_SCOPE")

    def test_undeclared_identifier_assignment(self):
        self.assert_error(
            vcd(signals=signal("!", "a"), sim="#0\n0@\n"),
            "UNDECLARED_IDENTIFIER")

    def test_vector_and_real_and_event_values_rejected(self):
        for bad in ("b1010!", "r3.14!", "p!", "B1!", "R1!"):
            self.assert_error(
                vcd(signals=signal("!", "a"), sim="#0\n%s\n" % bad),
                "UNSUPPORTED_VALUE_FORMAT")

    def test_time_goes_backwards(self):
        self.assert_error(
            vcd(signals=signal("!", "a"),
                sim="#10\n0!\n#5\n1!\n"),
            "TIME_GOES_BACKWARDS")

    def test_equal_timestamps_allowed(self):
        doc = parse_vcd(vcd(
            signals=signal("!", "a"),
            sim="#0\n0!\n#0\n1!\n#1\n0!\n"))
        self.assertEqual(doc.changes["!"],
                         [(0, "0"), (0, "1"), (1_000_000, "0")])

    def test_same_timestamp_multiple_assignments_text_order(self):
        doc = parse_vcd(vcd(
            signals=signal("!", "a"),
            sim="""\
                #0
                0!
                #7
                0!
                1!
                x!
                1!
                #8
                0!
            """))
        self.assertEqual(
            doc.changes["!"],
            [(0, "0"), (7_000_000, "0"), (7_000_000, "1"),
             (7_000_000, "x"), (7_000_000, "1"), (8_000_000, "0")])

    def test_change_limit_boundary(self):
        sim_lines = ["#0", "0!"]
        for k in range(1, MAX_CHANGES):
            sim_lines.append("#%d" % k)
            sim_lines.append("1!" if k % 2 else "0!")
        # 1 + (MAX_CHANGES-1) = 50000 次，应通过
        doc = parse_vcd(vcd(signals=signal("!", "a"),
                            sim="\n".join(sim_lines) + "\n"))
        self.assertEqual(len(doc.changes["!"]), MAX_CHANGES)

        # 再追加一次，第 50001 次必须失败
        sim_lines += ["#%d" % (MAX_CHANGES + 1), "1!"]
        with self.assertRaises(VCDServiceError) as cm:
            parse_vcd(vcd(signals=signal("!", "a"),
                          sim="\n".join(sim_lines) + "\n"))
        self.assertEqual(cm.exception.code, "TOO_MANY_CHANGES")

    def test_time_out_of_range(self):
        self.assert_error(
            vcd(signals=signal("!", "a"),
                sim="#9007199254740993\n0!\n", timescale="1ns"),
            "TIME_OUT_OF_RANGE")

    def test_syntax_error_points_line(self):
        self.assert_error(
            vcd(signals=signal("!", "a"),
                sim="#0\n0!\n!!!garbage\n"),
            "SYNTAX_ERROR")

    def test_unterminated_directive(self):
        text = ("$timescale 1ns $end\n$scope module top $end\n"
                "$var wire 1 ! a\n")
        self.assert_error(text, "UNTERMINATED_DIRECTIVE")

    def test_dumpvars_rejects_undeclared(self):
        self.assert_error(
            vcd(signals=signal("!", "a"),
                sim="#0\n$dumpvars\n0@\n$end\n"),
            "UNDECLARED_IDENTIFIER")

    def test_dumpoff_rejected(self):
        self.assert_error(
            vcd(signals=signal("!", "a"),
                sim="#0\n0!\n#1\n$dumpoff\nx!\n$end\n"),
            "UNSUPPORTED_DIRECTIVE")


if __name__ == "__main__":
    unittest.main()
