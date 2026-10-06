"""基于标准库的 HTTP 服务：POST /api/vcd/window。

multipart/form-data 字段：

* ``file``：VCD ASCII 文件（<= 4 MiB）
* ``signals``：待选完整层级信号名，可重复提交；也可用单个
  ``signals_json`` 传 JSON 字符串数组
* ``start_fs`` / ``end_fs``：以飞秒表示的半开窗口（兼容 start/end 名）

错误一律返回 JSON：``{"code", "message", 可选 "line"}``，且不产生部分结果。
"""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import VCDServiceError, WindowError
from .parser import MAX_FEMTOSECONDS, MAX_FILE_BYTES, parse_vcd
from .window import build_window

# multipart 开销（boundary、各 part 头）允许的额外字节
_MULTIPART_OVERHEAD = 256 * 1024

logger = logging.getLogger("vcd_service")


class HTTPBadRequest(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def parse_multipart(body, boundary):
    """极简 multipart/form-data 解析器。

    返回 [(name, filename_or_None, data_bytes), ...]，保持字段文本顺序。
    """
    delimiter = b"--" + boundary
    parts = body.split(delimiter)
    # 首段为 preamble（通常为空），末段为 epilogue
    if len(parts) < 3:
        raise HTTPBadRequest("MALFORMED_MULTIPART",
                             "multipart 体中找不到任何表单分段")

    fields = []
    for index, raw in enumerate(parts[1:-1], start=1):
        # 每段以 CRLF 开始、以 CRLF 结束（最后一段为 "--\r\n"）
        if raw == b"--" or raw == b"--\r\n":
            break
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        header_end = raw.find(b"\r\n\r\n")
        if header_end < 0:
            raise HTTPBadRequest("MALFORMED_MULTIPART",
                                 "第 %d 个表单分段缺少头/体分隔" % index)
        header_block = raw[:header_end].decode("latin-1")
        data = raw[header_end + 4:]
        name = None
        filename = None
        for line in header_block.split("\r\n"):
            key, _, value = line.partition(":")
            if key.strip().lower() != "content-disposition":
                continue
            for param in value.split(";"):
                param = param.strip()
                if param.startswith("name="):
                    name = _unquote(param[5:])
                elif param.startswith("filename="):
                    filename = _unquote(param[9:])
        if name is None:
            raise HTTPBadRequest("MALFORMED_MULTIPART",
                                 "第 %d 个表单分段缺少 name" % index)
        fields.append((name, filename, data))
    return fields


def _unquote(value):
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _parse_signals(form):
    """从重复 signals 字段（或 signals_json）提取信号名单。"""
    names = []
    for value in form.get("signals", []):
        value = value.strip()
        if value:
            names.append(value)
    for value in form.get("signals_json", []):
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(
                isinstance(x, str) for x in parsed):
            raise HTTPBadRequest(
                "INVALID_SIGNALS", "signals_json 必须是字符串数组")
        names.extend(parsed)
    return names


def handle_window_request(body, content_type):
    """核心处理：返回 (status_code, response_dict)。"""
    # Content-Type: multipart/form-data; boundary=...
    ctype, _, params = content_type.partition(";")
    if ctype.strip().lower() != "multipart/form-data":
        raise HTTPBadRequest(
            "UNSUPPORTED_CONTENT_TYPE",
            "仅接受 multipart/form-data", status=415)
    boundary = None
    for param in params.split(";"):
        param = param.strip()
        if param.startswith("boundary="):
            boundary = _unquote(param[len("boundary="):])
    if not boundary:
        raise HTTPBadRequest("MALFORMED_MULTIPART", "缺少 boundary 参数")
    boundary = boundary.encode("latin-1")

    fields = parse_multipart(body, boundary)

    file_data = None
    form = {}
    for name, filename, data in fields:
        if name == "file":
            if file_data is not None:
                raise HTTPBadRequest("DUPLICATE_FILE", "file 字段只能出现一次")
            if not filename:
                raise HTTPBadRequest("MISSING_FILENAME", "file 字段缺少文件名")
            file_data = data
            continue
        if len(data) > 64 * 1024:
            raise HTTPBadRequest("FIELD_TOO_LARGE",
                                 "表单字段 %s 过大" % name)
        try:
            text_value = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPBadRequest(
                "INVALID_FIELD", "表单字段 %s 必须为 UTF-8 文本" % name)
        form.setdefault(name, []).append(text_value)

    if file_data is None:
        raise HTTPBadRequest("MISSING_FILE", "缺少 file 字段（VCD 文件）")
    if len(file_data) > MAX_FILE_BYTES:
        raise HTTPBadRequest(
            "FILE_TOO_LARGE",
            "文件大小 %d 字节超过 %d 字节（4 MiB）上限"
            % (len(file_data), MAX_FILE_BYTES), status=413)
    try:
        text = file_data.decode("ascii")
    except UnicodeDecodeError:
        raise HTTPBadRequest("NON_ASCII", "文件必须为 ASCII 编码")

    try:
        signals = _parse_signals(form)
    except json.JSONDecodeError:
        raise HTTPBadRequest(
            "INVALID_SIGNALS", "signals_json 不是合法 JSON 数组")
    if not signals:
        raise HTTPBadRequest(
            "MISSING_SIGNALS", "至少通过 signals 字段选择一个信号")
    if len(signals) != len(set(signals)):
        dupes = sorted({s for s in signals if signals.count(s) > 1})
        raise HTTPBadRequest(
            "DUPLICATE_SIGNAL_IN_REQUEST",
            "请求中存在重复信号名：%s" % ", ".join(dupes))

    start = _parse_int_field(form, ("start_fs", "start"), "窗口起点")
    end = _parse_int_field(form, ("end_fs", "end"), "窗口终点")
    if start < 0 or end <= start or end > MAX_FEMTOSECONDS:
        raise HTTPBadRequest(
            "INVALID_WINDOW",
            "窗口非法：要求 0 <= start < end <= %d fs（得到 start=%d, "
            "end=%d）" % (MAX_FEMTOSECONDS, start, end))

    # 解析：失败则整体失败，无任何部分结果
    doc = parse_vcd(text)
    # 窗口裁决：任何信号无法完整解释则整体失败
    results = build_window(doc, signals, start, end)

    return 200, {
        "timescale_fs": doc.timescale_fs,
        "window": {"start": start, "end": end, "unit": "fs"},
        "signals": results,
    }


def _parse_int_field(form, keys, label):
    for key in keys:
        if key in form:
            raw = form[key][-1].strip()
            digits = raw[1:] if raw[:1] in "+-" else raw
            if not digits.isascii() or not digits.isdigit():
                raise HTTPBadRequest(
                    "INVALID_WINDOW", "%s必须是飞秒整数：%r" % (label, raw))
            return int(raw)
    raise HTTPBadRequest(
        "MISSING_WINDOW", "缺少窗口字段 %s（飞秒整数）" % keys[0])


class VCDRequestHandler(BaseHTTPRequestHandler):
    server_version = "VCDWindow/1.0"

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/health", "/healthz"):
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"code": "NOT_FOUND", "message": "未知路径：%s" % path})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/vcd/window":
            self._send_json(404, {"code": "NOT_FOUND", "message": "未知路径：%s" % path})
            return
        self._handle_window()

    def do_PUT(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def _method_not_allowed(self):
        self._send_json(405, {"code": "METHOD_NOT_ALLOWED",
                              "message": "仅支持 POST /api/vcd/window"})

    def _handle_window(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"code": "BAD_REQUEST",
                                  "message": "非法 Content-Length"})
            return
        if length <= 0:
            self._send_json(400, {"code": "EMPTY_BODY", "message": "请求体为空"})
            return
        if length > MAX_FILE_BYTES + _MULTIPART_OVERHEAD:
            self._send_json(413, {
                "code": "REQUEST_TOO_LARGE",
                "message": "请求体 %d 字节超过上限（文件 4 MiB + 表单开销）"
                           % length})
            return
        body = self._read_exact(length)
        if body is None:
            return  # 连接中断，响应已发送

        try:
            status, payload = handle_window_request(
                body, self.headers.get("Content-Type", ""))
        except HTTPBadRequest as exc:
            self._send_json(exc.status,
                            {"code": exc.code, "message": exc.message})
        except VCDServiceError as exc:
            self._send_json(exc.status, exc.to_dict())
        except WindowError as exc:
            self._send_json(422, exc.to_dict())
        except Exception:  # pragma: no cover - 防御性兜底
            logger.exception("未预期的内部错误")
            self._send_json(500, {"code": "INTERNAL_ERROR",
                                  "message": "服务内部错误"})
        else:
            self._send_json(status, payload)

    def _read_exact(self, length):
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                self._send_json(400, {"code": "TRUNCATED_BODY",
                                      "message": "请求体读取不完整"})
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def build_server(host, port):
    return ThreadingHTTPServer((host, port), VCDRequestHandler)
