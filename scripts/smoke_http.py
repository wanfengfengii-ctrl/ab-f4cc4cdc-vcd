#!/usr/bin/env python3
"""针对运行中服务的 HTTP 冒烟测试。

用法：python3 scripts/smoke_http.py [BASE_URL]

覆盖：
* /health 就绪
* 同刻多次赋值按文本顺序裁决（后者胜）
* 跨时标窗口（10ps VCD，以飞秒给出窗口）
* 相邻同值区间合并
* 时间倒退被拒绝（可定位原因，无部分结果）
* 窗口无法完整解释 => 422
任一断言失败即以非零退出码结束。
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"

BOUNDARY = "----smokeboundary9001"
PASSED = 0


def check(name, condition, detail=""):
    global PASSED
    if not condition:
        print("FAIL: %s %s" % (name, detail))
        sys.exit(1)
    PASSED += 1
    print("ok %d: %s" % (PASSED, name))


def wait_healthy(timeout=30.0):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - 启动期重试
            last_err = exc
        time.sleep(0.5)
    raise SystemExit("健康检查在 %.0f 秒内未就绪：%s" % (timeout, last_err))


def multipart(fields):
    chunks = []
    for name, value in fields:
        chunks.append("--" + BOUNDARY)
        if isinstance(value, tuple):
            filename, data = value
            chunks.append('Content-Disposition: form-data; name="%s"; '
                          'filename="%s"' % (name, filename))
            chunks.append("Content-Type: application/octet-stream")
            chunks.append("")
            chunks.append(data.decode("ascii"))
        else:
            chunks.append('Content-Disposition: form-data; name="%s"' % name)
            chunks.append("")
            chunks.append(value)
    chunks.append("--" + BOUNDARY + "--")
    return "\r\n".join(chunks).encode("ascii")


def post_window(fields):
    req = urllib.request.Request(
        BASE_URL + "/api/vcd/window",
        data=multipart(fields),
        headers={"Content-Type": "multipart/form-data; boundary=" + BOUNDARY},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


VCD_1NS = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! sig $end
$upscope $end
$enddefinitions $end
#0
0!
#10
1!
0!
#20
1!
#30
1!
#40
0!
"""

VCD_10PS = """$timescale 10ps $end
$scope module top $end
$var wire 1 ! s $end
$upscope $end
$enddefinitions $end
#0
0!
#5
1!
#15
0!
"""

VCD_BACKWARDS = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! s $end
$upscope $end
$enddefinitions $end
#10
0!
#9
1!
"""

VCD_NO_INIT = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! s $end
$upscope $end
$enddefinitions $end
#100
1!
"""


def main():
    wait_healthy()
    check("健康检查就绪", True)

    # 1) 同刻变更：#10 处 1! 紧接 0!，文本后者胜；
    #    #20->1、#30->1 连成一段（相邻同值合并）
    status, payload = post_window([
        ("file", ("w.vcd", VCD_1NS.encode())),
        ("signals", "top.sig"),
        ("start_fs", "10000000"),
        ("end_fs", "50000000"),
    ])
    check("同刻裁决 HTTP 200", status == 200, payload)
    intervals = payload["signals"][0]["intervals"]
    check("同刻后者胜 + 同值合并", intervals == [
        {"start": 10_000_000, "end": 20_000_000, "value": "0"},
        {"start": 20_000_000, "end": 40_000_000, "value": "1"},
        {"start": 40_000_000, "end": 50_000_000, "value": "0"},
    ], json.dumps(intervals))

    # 2) 跨时标窗口：10ps（=10000 fs/tick），窗口以飞秒给出
    status, payload = post_window([
        ("file", ("p.vcd", VCD_10PS.encode())),
        ("signals", "top.s"),
        ("start_fs", "40000"),
        ("end_fs", "160000"),
    ])
    check("跨时标窗口 HTTP 200", status == 200, payload)
    check("跨时标换算正确", payload["signals"][0]["intervals"] == [
        {"start": 40_000, "end": 50_000, "value": "0"},
        {"start": 50_000, "end": 150_000, "value": "1"},
        {"start": 150_000, "end": 160_000, "value": "0"},
    ], json.dumps(payload["signals"][0]["intervals"]))

    # 3) 时间倒退：必须 400 且携带行号，不产生结果
    status, payload = post_window([
        ("file", ("b.vcd", VCD_BACKWARDS.encode())),
        ("signals", "top.s"),
        ("start_fs", "0"),
        ("end_fs", "20000000"),
    ])
    check("时间倒退被拒绝", status == 400
          and payload.get("code") == "TIME_GOES_BACKWARDS"
          and "line" in payload, json.dumps(payload))

    # 4) 窗口无法完整解释（起点前无任何赋值）=> 422
    status, payload = post_window([
        ("file", ("n.vcd", VCD_NO_INIT.encode())),
        ("signals", "top.s"),
        ("start_fs", "0"),
        ("end_fs", "50000000"),
    ])
    check("无法完整解释返回 422",
          status == 422 and payload.get("code") == "WINDOW_UNEXPLAINABLE"
          and "signals" not in payload, json.dumps(payload))

    print("\n全部冒烟检查通过：%d 项" % PASSED)


def _merged(intervals):
    return all(a["value"] != b["value"]
               for a, b in zip(intervals, intervals[1:]))


if __name__ == "__main__":
    main()
