"""半开窗口裁决。

给定解析后的 :class:`~vcd_service.parser.VCDocument` 与一组选中信号，
把每条信号时间线裁剪为连续覆盖 ``[start, end)`` 的 0/1/x/z 半开区间：

* 窗口起点的有效值 = 时间戳 ``<= start`` 的最后一次赋值；不存在则
  “窗口无法完整解释”，整体失败且不产生任何部分结果。
* 同一时刻的多次赋值严格按 VCD 文本顺序裁决——后者胜。
* 相邻同值区间合并；区间以飞秒整数表示，保证不受时标单位影响。
"""

from .errors import WindowError
from .parser import MAX_FEMTOSECONDS


def _collapse(seq):
    """把 (time, value) 序列折叠为每刻最终值（同刻后者胜）。"""
    collapsed = []
    for t, v in seq:
        if collapsed and collapsed[-1][0] == t:
            collapsed[-1] = (t, v)
        else:
            collapsed.append((t, v))
    return collapsed


def validate_window(start, end):
    """校验以飞秒表示的半开窗口参数。"""
    if not isinstance(start, int) or not isinstance(end, int):
        raise WindowError("窗口起止必须是飞秒整数")
    if start < 0:
        raise WindowError("窗口起点不能为负（%d fs）" % start)
    if end <= start:
        raise WindowError(
            "半开窗口要求 end > start（start=%d, end=%d fs）" % (start, end))
    if end > MAX_FEMTOSECONDS:
        raise WindowError(
            "窗口终点换算为飞秒后越界（%d fs）" % end)


def build_window(doc, selected_names, start, end):
    """为选中信号构建覆盖 ``[start, end)`` 的区间序列。

    返回 list[{"name", "intervals": [{"start","end","value"}]}]；
    任何信号在窗口起点没有有效值时抛 :class:`WindowError`，不返回部分结果。
    """
    validate_window(start, end)

    if not selected_names:
        raise WindowError("至少选择一个信号")

    # 先完成全部前置校验，避免给出部分结果
    collapsed = {}
    initial = {}
    for name in selected_names:
        signal = doc.signals.get(name)
        if signal is None:
            raise WindowError(
                "选中的信号未在 VCD 中声明：%s" % name, signal=name)
        seq = _collapse(doc.changes.get(signal.id_code, []))
        collapsed[name] = seq

        # 起点有效值：t <= start 的最后一次赋值（seq 已按时间排序）
        v0 = None
        for t, v in seq:
            if t <= start:
                v0 = v
            else:
                break
        if v0 is None:
            raise WindowError(
                "信号 %s 在窗口起点 %d fs 处没有有效值（缺少不晚于起点的"
                "显式赋值，窗口无法完整解释）" % (name, start), signal=name)
        initial[name] = v0

    results = []
    for name in selected_names:
        seq = collapsed[name]
        intervals = []
        seg_start = start
        seg_value = initial[name]

        for t, v in seq:
            if t <= start:
                continue            # 已用于确定起点有效值
            if t >= end:
                break               # 半开窗口：end 处赋值不影响窗口
            if v == seg_value:
                continue            # 相邻同值合并
            intervals.append(_interval(seg_start, t, seg_value))
            seg_start, seg_value = t, v
        intervals.append(_interval(seg_start, end, seg_value))

        results.append({"name": name, "intervals": intervals})
    return results


def _interval(start, end, value):
    return {"start": start, "end": end, "value": value}
