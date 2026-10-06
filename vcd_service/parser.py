"""VCD (IEEE 1364) 词法与语义解析器。

仅接受需求规定的安全子集：

* ``$timescale``：1/10/100 × fs|ps|ns
* ``$var``：``wire`` 且位宽必须为 1
* 作用域：``$scope module <规范名>``，名字为 ``[A-Za-z_][A-Za-z0-9_$]*``
* 短标识符：可打印 ASCII（33-126）、非空白（``#`` 仅在值变更中作为
  标识符字符出现，行首的 ``#<数字>`` 才是时间戳，词法无歧义）
* 标量值变更：``0!`` / ``1!`` / ``x!`` / ``z!``
* 时间戳非递减，时间一律换算为飞秒（int）
* 拒绝：重复完整层级名、向量/实数/事件赋值、对未声明标识的赋值、
  除 ``$comment/$date/$version/$dumpvars`` 之外的一切指令体隐式赋值

解析结果为 :class:`VCDocument`。除空白与注释外，解析对声明顺序不做假设；
同一短标识符被多个完整名引用（别名）时，它们共享同一条变更时间线。
"""

from .errors import VCDServiceError

# 单次请求最多处理的值变更次数（含 $dumpvars 初值）
MAX_CHANGES = 50_000
# 上传文件大小上限 4 MiB
MAX_FILE_BYTES = 4 * 1024 * 1024
# 换算后飞秒绝对上限（约 64 位有符号范围），超出即“越界换算”
MAX_FEMTOSECONDS = 9_000_000_000_000_000_000

_TIME_MULTIPLIERS = {"fs": 1, "ps": 1_000, "ns": 1_000_000}
_VALID_TIME_UNITS = ("1fs", "10fs", "100fs",
                     "1ps", "10ps", "100ps",
                     "1ns", "10ns", "100ns")
_SCALAR_VALUES = frozenset("01xz")
_NAME_START = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME_CHARS = _NAME_START | frozenset("0123456789$")
# 合法短标识符：可打印 ASCII（33-126）非空白。'#' 也允许（Icarus 第 3
# 个标识符即为 '#'）；词法上只有行首的 # 是时间戳，值变更形如 '1#' 无歧义。
_ID_CHARS = {chr(c) for c in range(33, 127)}
_WHITESPACE = frozenset(" \t\r\n\v\f")

# 定义区允许出现、且无副作用的指令（体内容直接忽略）
_PASSTHROUGH_DIRECTIVES = frozenset(("$comment", "$date", "$version"))


class Signal:
    """一个 1 位 wire 信号。"""

    __slots__ = ("id_code", "name", "line")

    def __init__(self, id_code, name, line):
        self.id_code = id_code
        self.name = name          # 完整层级名，如 top.dut.signal
        self.line = line          # $var 声明所在行（1 起）


class VCDocument:
    """解析后的 VCD 文档。"""

    def __init__(self, timescale_fs, signals, changes):
        self.timescale_fs = timescale_fs      # 时标单位（飞秒）
        self.signals = signals                # full name -> Signal
        # changes: id_code -> [(time_fs, value)]，全局文本顺序追加
        self.changes = changes


def _is_valid_name(name):
    return bool(name) and name[0] in _NAME_START \
        and all(ch in _NAME_CHARS for ch in name)


def _is_valid_id(id_code):
    return bool(id_code) and all(ch in _ID_CHARS for ch in id_code)


def tokenize(text):
    """把 VCD 文本切为 token 流。

    产出三元组：
      ("$指令名", [体词...], lineno)
      ("#",       int_time,  lineno)
      ("0|1|x|z", id_code,   lineno)
    除空白（空格/制表/换行等）外不忽略任何字符；无法识别即报错。
    """
    tokens = []
    length = len(text)
    pos = 0
    line = 1

    def skip_ws():
        nonlocal pos, line
        while pos < length and text[pos] in _WHITESPACE:
            if text[pos] == "\n":
                line += 1
            pos += 1

    while pos < length:
        skip_ws()
        if pos >= length:
            break
        start_line = line
        ch = text[pos]

        if ch == "$":
            # 指令：读到独立的 $end token 为止
            words = []
            pos += 1
            while True:
                skip_ws()
                start = pos
                while pos < length and text[pos] not in _WHITESPACE:
                    pos += 1
                word = text[start:pos]
                if word == "$end":
                    break
                if pos >= length:
                    raise VCDServiceError(
                        "UNTERMINATED_DIRECTIVE",
                        "指令 $%s 缺少 $end" % (words[0] if words else ""),
                        start_line)
                words.append(word)
            if not words:
                raise VCDServiceError(
                    "SYNTAX_ERROR", "空的 VCD 指令", start_line)
            tokens.append(("$" + words[0], words[1:], start_line))
            continue

        if ch == "#":
            pos += 1
            start = pos
            while pos < length and text[pos] not in _WHITESPACE:
                pos += 1
            num = text[start:pos]
            if not num or not num.isdigit():
                raise VCDServiceError(
                    "INVALID_TIMESTAMP",
                    "时间戳必须是非负整数：#%s" % num, start_line)
            tokens.append(("#", int(num), start_line))
            continue

        # 普通 token：值变更
        start = pos
        while pos < length and text[pos] not in _WHITESPACE:
            pos += 1
        word = text[start:pos]
        if word[0] in _SCALAR_VALUES:
            if len(word) < 2:
                raise VCDServiceError(
                    "SYNTAX_ERROR",
                    "标量值变更缺少标识符：%r" % word, start_line)
            id_code = word[1:]
            if not _is_valid_id(id_code):
                raise VCDServiceError(
                    "INVALID_IDENTIFIER",
                    "值变更中的非法标识符：%r" % word, start_line)
            tokens.append((word[0], id_code, start_line))
        elif word[0] in "bBrRpP":
            raise VCDServiceError(
                "UNSUPPORTED_VALUE_FORMAT",
                "仅支持 1 位标量值变更（0/1/x/z），不支持向量/实数/事件："
                "%r" % word, start_line)
        else:
            raise VCDServiceError(
                "SYNTAX_ERROR", "无法识别的 VCD 语法：%r" % word, start_line)

    return tokens


def parse_vcd(text):
    """解析 VCD 文本，返回 :class:`VCDocument`。

    任何违例抛 :class:`VCDServiceError`，附带源码行号；解析要么完整成功，
    要么失败，不产生部分结果。
    """
    if not isinstance(text, str):
        raise VCDServiceError("INVALID_INPUT", "VCD 内容必须为 ASCII 文本")
    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        line = text.count("\n", 0, exc.start) + 1
        raise VCDServiceError(
            "NON_ASCII", "文件必须为 ASCII 编码（首个非 ASCII 字符位于第 %d 行）"
            % line, line=line)

    timescale_fs = None
    scope_stack = []
    name_to_signal = {}              # 完整层级名 -> Signal
    id_to_names = {}                 # id_code -> [完整名]
    changes = {}                     # id_code -> [(time_fs, value)]
    change_count = 0
    current_time = 0
    in_definitions = True

    for tok in tokenize(text):
        kind = tok[0]
        lineno = tok[-1]

        if kind.startswith("$"):
            directive = kind
            body = tok[1]

            if in_definitions:
                if directive == "$timescale":
                    if timescale_fs is not None:
                        raise VCDServiceError(
                            "DUPLICATE_TIMESCALE",
                            "$timescale 只能声明一次", lineno)
                    num, unit = _split_timescale(body, lineno)
                    timescale_fs = int(num) * _TIME_MULTIPLIERS[unit]

                elif directive == "$scope":
                    if len(body) < 2 or body[0] != "module":
                        raise VCDServiceError(
                            "INVALID_SCOPE",
                            "仅支持 module 作用域：$scope module <name> $end",
                            lineno)
                    if len(body) != 2:
                        raise VCDServiceError(
                            "INVALID_SCOPE",
                            "$scope module 仅接受一个名字", lineno)
                    name = body[1]
                    if not _is_valid_name(name):
                        raise VCDServiceError(
                            "INVALID_SCOPE_NAME",
                            "作用域名不合法（须以字母/下划线开头，"
                            "仅含字母数字与 _ $）：%r" % name, lineno)
                    scope_stack.append(name)

                elif directive == "$upscope":
                    if body:
                        raise VCDServiceError(
                            "SYNTAX_ERROR", "$upscope 不接受参数", lineno)
                    if not scope_stack:
                        raise VCDServiceError(
                            "UNBALANCED_UPSCOPE",
                            "$upscope 与 $scope 不匹配", lineno)
                    scope_stack.pop()

                elif directive == "$var":
                    # $var wire 1 ! name [msb:lsb] $end
                    if len(body) not in (4, 5):
                        raise VCDServiceError(
                            "INVALID_VAR",
                            "$var 格式应为：$var wire <width> <id> <name>"
                            " [范围] $end", lineno)
                    var_kind, width, id_code, ref = body[:4]
                    if var_kind != "wire":
                        raise VCDServiceError(
                            "UNSUPPORTED_VAR_KIND",
                            "仅接受 1 位 wire 信号，发现 %s" % var_kind,
                            lineno)
                    if width != "1":
                        raise VCDServiceError(
                            "UNSUPPORTED_VAR_WIDTH",
                            "仅接受 1 位信号，发现位宽 %s" % width, lineno)
                    if len(body) == 5 and not _looks_like_range(body[4]):
                        raise VCDServiceError(
                            "INVALID_VAR",
                            "$var 第 5 个参数只能是 [msb:lsb] 范围", lineno)
                    if not _is_valid_id(id_code):
                        raise VCDServiceError(
                            "INVALID_IDENTIFIER",
                            "非法短标识符 %r（须为可打印 ASCII 且不含空白）"
                            % id_code, lineno)
                    if not _is_valid_name(ref):
                        raise VCDServiceError(
                            "INVALID_SIGNAL_NAME",
                            "信号名不合法：%r" % ref, lineno)
                    if not scope_stack:
                        raise VCDServiceError(
                            "VAR_OUTSIDE_SCOPE",
                            "$var 必须位于 module 作用域内", lineno)
                    full_name = ".".join(scope_stack + [ref])
                    if full_name in name_to_signal:
                        first = name_to_signal[full_name].line
                        raise VCDServiceError(
                            "DUPLICATE_SIGNAL",
                            "重复的完整层级信号名：%s（首次声明于第 %d 行）"
                            % (full_name, first), lineno)
                    sig = Signal(id_code, full_name, lineno)
                    name_to_signal[full_name] = sig
                    id_to_names.setdefault(id_code, []).append(full_name)
                    changes.setdefault(id_code, [])

                elif directive == "$enddefinitions":
                    if body:
                        raise VCDServiceError(
                            "SYNTAX_ERROR",
                            "$enddefinitions 不接受参数", lineno)
                    if scope_stack:
                        raise VCDServiceError(
                            "UNBALANCED_SCOPE",
                            "$enddefinitions 前仍有未关闭的作用域：%s"
                            % " > ".join(scope_stack), lineno)
                    if timescale_fs is None:
                        raise VCDServiceError(
                            "MISSING_TIMESCALE",
                            "$enddefinitions 前必须声明 $timescale", lineno)
                    in_definitions = False

                elif directive in _PASSTHROUGH_DIRECTIVES:
                    pass
                else:
                    raise VCDServiceError(
                        "UNSUPPORTED_DIRECTIVE",
                        "定义区不支持的 VCD 指令：%s" % directive, lineno)

            else:
                # 仿真区
                if directive == "$dumpvars":
                    _consume_initial_values(
                        body, current_time, id_to_names, changes, lineno)
                    change_count += len(body)
                    if change_count > MAX_CHANGES:
                        raise VCDServiceError(
                            "TOO_MANY_CHANGES",
                            "值变更超过 %d 次上限" % MAX_CHANGES, lineno,
                            status=422)
                elif directive in _PASSTHROUGH_DIRECTIVES:
                    pass
                else:
                    raise VCDServiceError(
                        "UNSUPPORTED_DIRECTIVE",
                        "仿真区不支持指令 %s（本服务不模拟 $dumpoff/$dumpon "
                        "等隐式赋值，请提供仅含显式值变更的 VCD）"
                        % directive, lineno)
            continue

        if in_definitions:
            raise VCDServiceError(
                "SYNTAX_ERROR",
                "$enddefinitions 之前只允许声明区指令，却发现：%r"
                % ("#%d" % tok[1] if kind == "#" else kind + str(tok[1])),
                lineno)

        if kind == "#":
            sim_time = tok[1]
            new_time = sim_time * timescale_fs
            if new_time > MAX_FEMTOSECONDS:
                raise VCDServiceError(
                    "TIME_OUT_OF_RANGE",
                    "时间戳换算为飞秒后越界（#%d × %dfs）"
                    % (sim_time, timescale_fs), lineno)
            if new_time < current_time:
                raise VCDServiceError(
                    "TIME_GOES_BACKWARDS",
                    "时间戳非递减：#%d（%d fs）早于上一时刻 %d fs"
                    % (sim_time, new_time, current_time), lineno)
            current_time = new_time
            continue

        # 标量值变更
        value, id_code = kind, tok[1]
        if id_code not in id_to_names:
            raise VCDServiceError(
                "UNDECLARED_IDENTIFIER",
                "对未声明标识符 %r 赋值" % id_code, lineno)
        changes[id_code].append((current_time, value))
        change_count += 1
        if change_count > MAX_CHANGES:
            raise VCDServiceError(
                "TOO_MANY_CHANGES",
                "值变更超过 %d 次上限" % MAX_CHANGES, lineno, status=422)

    if timescale_fs is None:
        raise VCDServiceError(
            "MISSING_TIMESCALE", "VCD 缺少 $timescale 声明",
            line=text.count("\n") + 1)

    return VCDocument(timescale_fs, name_to_signal, changes)


def _looks_like_range(token):
    return len(token) >= 3 and token[0] == "[" and token[-1] == "]" \
        and ":" in token


def _split_timescale(body, lineno):
    """解析时标体，兼容 ``10 ns`` 与 ``10ns`` 两种写法。"""
    if len(body) == 1:
        token = body[0]
        num, unit = "", ""
        for candidate in ("fs", "ps", "ns"):
            if token.endswith(candidate):
                num, unit = token[:-len(candidate)], candidate
                break
    elif len(body) == 2:
        num, unit = body
    else:
        num, unit = "", ""
    if num not in ("1", "10", "100") or unit not in _TIME_MULTIPLIERS:
        raise VCDServiceError(
            "UNSUPPORTED_TIMESCALE",
            "仅支持 1/10/100 倍的 fs、ps、ns 时标"
            "（合法值：%s）" % "、".join(_VALID_TIME_UNITS), lineno)
    return num, unit


def _consume_initial_values(body, current_time, id_to_names, changes, lineno):
    """解析 $dumpvars 体中的标量初值 token（directive body 是字符串列表）。"""
    i = 0
    while i < len(body):
        word = body[i]
        if not word or word[0] not in _SCALAR_VALUES or len(word) < 2:
            raise VCDServiceError(
                "UNSUPPORTED_VALUE_FORMAT",
                "$dumpvars 中仅支持标量初值（0/1/x/z + 标识符）：%r"
                % word, lineno)
        value, id_code = word[0], word[1:]
        if not _is_valid_id(id_code):
            raise VCDServiceError(
                "INVALID_IDENTIFIER",
                "$dumpvars 中的非法标识符：%r" % word, lineno)
        if id_code not in id_to_names:
            raise VCDServiceError(
                "UNDECLARED_IDENTIFIER",
                "$dumpvars 引用未声明标识符 %r" % id_code, lineno)
        changes[id_code].append((current_time, value))
        i += 1
