"""测试共享工具。"""

import textwrap


def vcd(signals="", sim="", timescale="1ns"):
    """构造一个标准结构的 VCD：top.dut 作用域 + 仿真段。

    signals 中可自行插入 ``$scope/$upscope`` 改变嵌套（须配平，
    helper 最后会补两个 ``$upscope`` 关闭 dut 与 top）。
    """
    return textwrap.dedent("""\
        $date Mon Jan 1 00:00:00 2024 $end
        $version test-emu 1.0 $end
        $timescale {ts} $end
        $scope module top $end
        $scope module dut $end
        {signals}
        $upscope $end
        $upscope $end
        $enddefinitions $end
        {sim}""").format(
        ts=timescale,
        signals=textwrap.dedent(signals).rstrip("\n"),
        sim=textwrap.dedent(sim).rstrip("\n"))


def signal(mark, name):
    return "$var wire 1 %s %s $end" % (mark, name)


def raw_ts(timescale, sim="#0\n0!\n", signals=None):
    if signals is None:
        signals = "$var wire 1 ! a $end\n"
    return ("$timescale %s $end\n"
            "$scope module top $end\n%s"
            "$upscope $end\n$enddefinitions $end\n%s") % (
                timescale, signals, sim)


def multipart(fields):
    """构造 multipart/form-data 请求体。

    fields: [(name, value)]；value 为 (filename, bytes) 时作为文件段。
    """
    boundary = b"----vcdtestboundary42"
    chunks = []
    for name, value in fields:
        chunks.append(b"--" + boundary)
        if isinstance(value, tuple):
            filename, data = value
            chunks.append(
                ('Content-Disposition: form-data; name="%s"; filename="%s"'
                 % (name, filename)).encode("utf-8"))
            chunks.append(b"Content-Type: application/octet-stream")
            chunks.append(b"")
            chunks.append(data)
        else:
            chunks.append(
                ('Content-Disposition: form-data; name="%s"' % name)
                .encode("utf-8"))
            chunks.append(b"")
            chunks.append(value.encode("utf-8"))
    chunks.append(b"--" + boundary + b"--")
    return b"\r\n".join(chunks), boundary
