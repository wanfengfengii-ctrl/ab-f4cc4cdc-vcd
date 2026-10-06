"""HTTP 端到端测试：真实监听临时端口，覆盖健康检查与各类错误。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from vcd_service.parser import MAX_FILE_BYTES
from vcd_service.server import build_server

from .helpers import multipart, signal, vcd


GOOD_VCD = vcd(
    signals="""\
        $var wire 1 ! clk $end
        $var wire 1 " rst $end
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
        0"
    """)


class ServerFixture:
    def __init__(self):
        self.server = build_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        return False

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


def post_window(fixture, fields, raw_content_type=None):
    body, boundary = multipart(fields)
    ctype = raw_content_type or (
        "multipart/form-data; boundary=%s" % boundary.decode())
    req = urllib.request.Request(
        fixture.url("/api/vcd/window"), data=body,
        headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(fixture, path):
    try:
        with urllib.request.urlopen(fixture.url(path), timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class HealthTests(unittest.TestCase):
    def test_health_ok(self):
        with ServerFixture() as fx:
            status, payload = get(fx, "/health")
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "ok")


class WindowHttpTests(unittest.TestCase):
    def test_happy_path_timeline(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals", "top.dut.clk"),
                ("start_fs", "0"),
                ("end_fs", "12000000"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["timescale_fs"], 1_000_000)
            clk = payload["signals"][0]
            self.assertEqual(clk["name"], "top.dut.clk")
            self.assertEqual(clk["intervals"], [
                {"start": 0, "end": 5_000_000, "value": "0"},
                {"start": 5_000_000, "end": 12_000_000, "value": "1"},
            ])

    def test_repeated_signals_fields(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals", "top.dut.clk"),
                ("signals", "top.dut.rst"),
                ("start_fs", "0"),
                ("end_fs", "1000000"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 200, payload)
            self.assertEqual([s["name"] for s in payload["signals"]],
                             ["top.dut.clk", "top.dut.rst"])

    def test_signals_json_field(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals_json", json.dumps(["top.dut.rst"])),
                ("start_fs", "0"),
                ("end_fs", "10000000"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["signals"][0]["intervals"],
                             [{"start": 0, "end": 10_000_000, "value": "1"}])

    def test_duplicate_signal_names_rejected(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals", "top.dut.clk"),
                ("signals", "top.dut.clk"),
                ("start_fs", "0"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "DUPLICATE_SIGNAL_IN_REQUEST")

    def test_undeclared_selected_signal_is_422(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals", "top.dut.ghost"),
                ("start_fs", "0"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 422)
            self.assertEqual(payload["code"], "WINDOW_UNEXPLAINABLE")
            self.assertEqual(payload["signal"], "top.dut.ghost")

    def test_syntax_error_reports_line_and_no_partial_result(self):
        bad = GOOD_VCD + "\n!!!oops\n"
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", bad.encode())),
                ("signals", "top.dut.clk"),
                ("start_fs", "0"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "SYNTAX_ERROR")
            self.assertIn("line", payload)

    def test_time_backwards_rejected(self):
        bad = vcd(signals=signal("!", "a"),
                  sim="#10\n0!\n#9\n1!\n")
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", bad.encode())),
                ("signals", "top.dut.a"),
                ("start_fs", "0"),
                ("end_fs", "20000000"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "TIME_GOES_BACKWARDS")
            self.assertIn("line", payload)

    def test_non_ascii_rejected(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode() + "字".encode())),
                ("signals", "top.dut.clk"),
                ("start_fs", "0"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "NON_ASCII")

    def test_file_too_large(self):
        big = GOOD_VCD.encode() + b" " * (MAX_FILE_BYTES + 1)
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", big)),
                ("signals", "top.dut.clk"),
                ("start_fs", "0"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 413)
            self.assertEqual(payload["code"], "FILE_TOO_LARGE")

    def test_missing_file(self):
        with ServerFixture() as fx:
            fields = [
                ("signals", "top.dut.clk"),
                ("start_fs", "0"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "MISSING_FILE")

    def test_missing_window_field(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals", "top.dut.clk"),
                ("start_fs", "0"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "MISSING_WINDOW")

    def test_invalid_window_rejected_before_parsing(self):
        for start, end in (("10", "10"), ("20", "10"), ("-1", "10")):
            with ServerFixture() as fx:
                fields = [
                    ("file", ("wave.vcd", GOOD_VCD.encode())),
                    ("signals", "top.dut.clk"),
                    ("start_fs", start),
                    ("end_fs", end),
                ]
                status, payload = post_window(fx, fields)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["code"], "INVALID_WINDOW")

    def test_window_not_integer(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals", "top.dut.clk"),
                ("start_fs", "soon"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "INVALID_WINDOW")

    def test_bad_signals_json(self):
        with ServerFixture() as fx:
            fields = [
                ("file", ("wave.vcd", GOOD_VCD.encode())),
                ("signals_json", "not-json"),
                ("start_fs", "0"),
                ("end_fs", "10"),
            ]
            status, payload = post_window(fx, fields)
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], "INVALID_SIGNALS")

    def test_non_utf8_field_rejected(self):
        boundary = b"----vcdtestboundary42"
        # 手工构造含非法 UTF-8 的 signals 字段
        raw = (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="file"; '
            b'filename="w.vcd"\r\n\r\n' + GOOD_VCD.encode() + b"\r\n"
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="signals"\r\n\r\n'
            b"\xff\xfe\r\n"
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="start_fs"\r\n\r\n0\r\n'
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="end_fs"\r\n\r\n10\r\n'
            b"--" + boundary + b"--\r\n"
        )
        with ServerFixture() as fx:
            req = urllib.request.Request(
                fx.url("/api/vcd/window"), data=raw,
                headers={"Content-Type":
                         "multipart/form-data; boundary=%s"
                         % boundary.decode()}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("应当 400")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                self.assertEqual(json.loads(exc.read())["code"],
                                 "INVALID_FIELD")

    def test_bad_content_type(self):
        with ServerFixture() as fx:
            req = urllib.request.Request(
                fx.url("/api/vcd/window"), data=b"x",
                headers={"Content-Type": "text/plain"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("应当返回 4xx")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 415)
                payload = json.loads(exc.read())
                self.assertEqual(payload["code"], "UNSUPPORTED_CONTENT_TYPE")

    def test_unknown_path_404(self):
        with ServerFixture() as fx:
            status, _ = get(fx, "/api/vcd/other")
            self.assertEqual(status, 404)


class StabilityTests(unittest.TestCase):
    """合法波形必须不受空白与声明顺序影响，产生稳定时间线。"""

    sim = """\
        #0
        0!
        0"
        #10
        1!
        #10
        0!
        #20
        1"
    """

    def _request(self, fx, text):
        fields = [
            ("file", ("w.vcd", text.encode())),
            ("signals", "top.dut.a"),
            ("signals", "top.dut.b"),
            ("start_fs", "0"),
            ("end_fs", "30000000"),
        ]
        status, payload = post_window(fx, fields)
        self.assertEqual(status, 200, payload)
        return payload["signals"]

    def test_whitespace_and_declaration_order_invariant(self):
        compact = (
            "$timescale 1ns $end $scope module top $end"
            " $scope module dut $end"
            " $var wire 1 ! a $end $var wire 1 \" b $end"
            " $upscope $end $upscope $end"
            " $enddefinitions $end " + self.sim.replace("\n", "  "))
        spaced = vcd(
            signals="""\
                $var wire 1 " b $end
                $var wire 1 ! a $end
            """, sim=self.sim)
        with ServerFixture() as fx:
            t1 = self._request(fx, compact)
            t2 = self._request(fx, spaced)
        # 输出按选择顺序（a, b），与声明顺序无关；同刻 1 后立即 0
        # 在 #10 裁决为 0，整段保持 0
        self.assertEqual([s["name"] for s in t1], ["top.dut.a", "top.dut.b"])
        self.assertEqual(t1, t2)
        self.assertEqual(t1[0]["intervals"], [
            {"start": 0, "end": 30_000_000, "value": "0"},
        ])
        self.assertEqual(t1[1]["intervals"], [
            {"start": 0, "end": 20_000_000, "value": "0"},
            {"start": 20_000_000, "end": 30_000_000, "value": "1"},
        ])


if __name__ == "__main__":
    unittest.main()
