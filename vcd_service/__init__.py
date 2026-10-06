"""VCD 波形窗口裁剪服务（仅依赖 Python 标准库）。"""

from .errors import VCDServiceError
from .parser import MAX_FEMTOSECONDS, parse_vcd
from .window import WindowError, build_window

__all__ = [
    "VCDServiceError",
    "WindowError",
    "MAX_FEMTOSECONDS",
    "parse_vcd",
    "build_window",
]
