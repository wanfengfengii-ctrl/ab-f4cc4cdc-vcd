"""窗口裁决测试：同刻后者胜、同值合并、跨时标、无法完整解释。"""

import unittest

from vcd_service.errors import WindowError
from vcd_service.parser import parse_vcd
from vcd_service.window import build_window

from .helpers import signal, vcd


def intervals(doc_text, names, start, end):
    doc = parse_vcd(doc_text)
    return build_window(doc, names, start, end)


NS = 1_000_000


class WindowBasicTests(unittest.TestCase):
    def test_constant_signal_covers_full_window(self):
        text = vcd(signals=signal("!", "a"), sim="#0\n1!\n")
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        self.assertEqual(out["intervals"],
                         [{"start": 10 * NS, "end": 20 * NS, "value": "1"}])

    def test_half_open_boundaries(self):
        # 赋值恰好在窗口起点/终点
        text = vcd(signals=signal("!", "a"),
                   sim="#0\n0!\n#10\n1!\n#20\n0!\n")
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        # 起点取值为 <=start 的最后赋值（#10 的 1）；
        # #20 的 0 落在窗口外（半开）
        self.assertEqual(out["intervals"],
                         [{"start": 10 * NS, "end": 20 * NS, "value": "1"}])

    def test_start_value_from_before_window(self):
        text = vcd(signals=signal("!", "a"),
                   sim="#0\n0!\n#5\n1!\n#15\n0!\n")
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        self.assertEqual(out["intervals"], [
            {"start": 10 * NS, "end": 15 * NS, "value": "1"},
            {"start": 15 * NS, "end": 20 * NS, "value": "0"},
        ])

    def test_merge_adjacent_equal_segments(self):
        # 1 -> 0 -> 0（第二处 0 与前一段同值）-> 1 应合并中间同值
        text = vcd(signals=signal("!", "a"),
                   sim="#0\n1!\n#12\n0!\n#14\n0!\n#18\n1!\n")
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        self.assertEqual(out["intervals"], [
            {"start": 10 * NS, "end": 12 * NS, "value": "1"},
            {"start": 12 * NS, "end": 18 * NS, "value": "0"},
            {"start": 18 * NS, "end": 20 * NS, "value": "1"},
        ])

    def test_intervals_are_continuous(self):
        text = vcd(signals=signal("!", "a"),
                   sim="#0\n0!\n#11\n1!\n#13\nx!\n#17\nz!\n")
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        segs = out["intervals"]
        self.assertEqual(segs[0]["start"], 10 * NS)
        self.assertEqual(segs[-1]["end"], 20 * NS)
        for prev, nxt in zip(segs, segs[1:]):
            self.assertEqual(prev["end"], nxt["start"])
            self.assertNotEqual(prev["value"], nxt["value"],
                                "相邻同值区间必须合并")
        self.assertEqual([s["value"] for s in segs], ["0", "1", "x", "z"])


class SameTimeArbitrationTests(unittest.TestCase):
    def test_last_textual_assignment_wins(self):
        # #12 同刻按文本顺序：1,0,x,1 => 最终 1
        text = vcd(signals=signal("!", "a"), sim="""\
            #0
            0!
            #12
            1!
            0!
            x!
            1!
            #18
            0!
        """)
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        self.assertEqual(out["intervals"], [
            {"start": 10 * NS, "end": 12 * NS, "value": "0"},
            {"start": 12 * NS, "end": 18 * NS, "value": "1"},
            {"start": 18 * NS, "end": 20 * NS, "value": "0"},
        ])

    def test_same_time_at_window_start_last_wins(self):
        # 窗口起点恰好有同刻多次赋值
        text = vcd(signals=signal("!", "a"), sim="""\
            #0
            0!
            #10
            1!
            0!
            #20
            1!
        """)
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        self.assertEqual(out["intervals"], [
            {"start": 10 * NS, "end": 20 * NS, "value": "0"},
        ])

    def test_same_time_glitch_values_merge_away(self):
        # 同刻 0->1->0 在裁决后整体消失
        text = vcd(signals=signal("!", "a"), sim="""\
            #0
            0!
            #15
            1!
            0!
        """)
        out = intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)[0]
        self.assertEqual(out["intervals"], [
            {"start": 10 * NS, "end": 20 * NS, "value": "0"},
        ])


class CrossTimescaleTests(unittest.TestCase):
    def test_picosecond_timescale_window_in_femtoseconds(self):
        text = vcd(signals=signal("!", "a"),
                   sim="#0\n0!\n#5\n1!\n#15\n0!\n",
                   timescale="10ps")  # 每 tick = 10000 fs
        out = intervals(text, ["top.dut.a"], 40_000, 160_000)[0]
        # #5 = 50000fs, #15 = 150000fs
        self.assertEqual(out["intervals"], [
            {"start": 40_000, "end": 50_000, "value": "0"},
            {"start": 50_000, "end": 150_000, "value": "1"},
            {"start": 150_000, "end": 160_000, "value": "0"},
        ])

    def test_femtosecond_timescale_exact(self):
        text = vcd(signals=signal("!", "a"),
                   sim="#0\n1!\n#100\n0!\n", timescale="1fs")
        out = intervals(text, ["top.dut.a"], 0, 1000)[0]
        self.assertEqual(out["intervals"], [
            {"start": 0, "end": 100, "value": "1"},
            {"start": 100, "end": 1000, "value": "0"},
        ])

    def test_nanosecond_timescale_large_window(self):
        text = vcd(signals=signal("!", "a"),
                   sim="#0\nx!\n#2\n0!\n#3\n1!\n", timescale="100ns")
        out = intervals(text, ["top.dut.a"], 0, 500_000_000)[0]
        self.assertEqual(out["intervals"], [
            {"start": 0, "end": 200_000_000, "value": "x"},
            {"start": 200_000_000, "end": 300_000_000, "value": "0"},
            {"start": 300_000_000, "end": 500_000_000, "value": "1"},
        ])


class WindowFailureTests(unittest.TestCase):
    def test_no_value_at_start_rejected(self):
        # 首次赋值晚于窗口起点 => 无法完整解释
        text = vcd(signals=signal("!", "a"), sim="#15\n1!\n")
        with self.assertRaises(WindowError) as cm:
            intervals(text, ["top.dut.a"], 10 * NS, 20 * NS)
        self.assertIn("top.dut.a", cm.exception.message)
        self.assertEqual(cm.exception.signal, "top.dut.a")

    def test_never_driven_signal_rejected(self):
        text = vcd(signals=signal("!", "a") + "\n" + signal('"', "b"),
                   sim="#0\n0!\n")
        with self.assertRaises(WindowError):
            intervals(text, ["top.dut.b"], 0, 10 * NS)

    def test_undeclared_selected_signal_rejected(self):
        text = vcd(signals=signal("!", "a"), sim="#0\n0!\n")
        with self.assertRaises(WindowError):
            intervals(text, ["top.dut.missing"], 0, 10 * NS)

    def test_no_partial_results_on_failure(self):
        # a 合法、b 无法解释：必须整体失败，不能只返回 a
        text = vcd(signals=signal("!", "a") + "\n" + signal('"', "b"),
                   sim="#0\n0!\n#5\n1!\n")
        with self.assertRaises(WindowError):
            intervals(text, ["top.dut.a", "top.dut.b"], 0, 10 * NS)

    def test_invalid_window_arguments(self):
        text = vcd(signals=signal("!", "a"), sim="#0\n0!\n")
        doc = parse_vcd(text)
        with self.assertRaises(WindowError):
            build_window(doc, ["top.dut.a"], -1, 10)
        with self.assertRaises(WindowError):
            build_window(doc, ["top.dut.a"], 10, 10)
        with self.assertRaises(WindowError):
            build_window(doc, ["top.dut.a"], 20, 10)
        with self.assertRaises(WindowError):
            build_window(doc, [], 0, 10)

    def test_dumpvars_initial_value_counts(self):
        # $dumpvars 提供 #0 初值，窗口可解释
        text = vcd(signals=signal("!", "a"),
                   sim="#0\n$dumpvars\nz!\n$end\n#5\n1!\n")
        out = intervals(text, ["top.dut.a"], 0, 10 * NS)[0]
        self.assertEqual(out["intervals"], [
            {"start": 0, "end": 5 * NS, "value": "z"},
            {"start": 5 * NS, "end": 10 * NS, "value": "1"},
        ])


class MultiSignalOrderTests(unittest.TestCase):
    def test_selection_order_preserved(self):
        text = vcd(signals=signal("!", "a") + "\n" + signal('"', "b"),
                   sim="#0\n0!\n0\"\n")
        doc = parse_vcd(text)
        out = build_window(doc, ["top.dut.b", "top.dut.a"], 0, 10 * NS)
        self.assertEqual([s["name"] for s in out],
                         ["top.dut.b", "top.dut.a"])


if __name__ == "__main__":
    unittest.main()
