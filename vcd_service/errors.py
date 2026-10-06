"""错误类型：所有可预期错误均携带机器可读 code 与可定位行号。"""


class VCDServiceError(Exception):
    """解析/校验阶段的错误。

    Attributes:
        code:    稳定的机器可读错误码
        message: 人类可读原因（中文）
        line:    VCD 源码中的行号（1 起），无法定位时为 None
        status:  建议的 HTTP 状态码
    """

    def __init__(self, code, message, line=None, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.line = line
        self.status = status

    def to_dict(self):
        data = {"code": self.code, "message": self.message}
        if self.line is not None:
            data["line"] = self.line
        return data


class WindowError(Exception):
    """窗口无法被完整解释时抛出（不产生部分结果）。"""

    def __init__(self, message, signal=None):
        super().__init__(message)
        self.message = message
        self.signal = signal

    def to_dict(self):
        data = {"code": "WINDOW_UNEXPLAINABLE", "message": self.message}
        if self.signal is not None:
            data["signal"] = self.signal
        return data
