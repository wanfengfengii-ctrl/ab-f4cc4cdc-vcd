# VCD 波形窗口裁剪服务

把仿真器生成的 VCD 波形裁成**与声明层级、时间单位、同刻赋值顺序无关**的可比较
信号窗口，供芯片回归系统比较电平持续时间。纯 Python 标准库实现，无第三方依赖。

## 接口

### `POST /api/vcd/window`（`multipart/form-data`）

| 字段 | 说明 |
| --- | --- |
| `file` | VCD ASCII 文件，不超过 **4 MiB** |
| `signals` | 待选**完整层级**信号名；可重复提交多个；也可用 `signals_json` 传 JSON 字符串数组 |
| `start_fs` | 半开窗口起点（飞秒，整数；兼容字段名 `start`） |
| `end_fs` | 半开窗口终点（飞秒，整数，须大于起点；兼容 `end`） |

响应：

```json
{
  "timescale_fs": 10000,
  "window": {"start": 40000, "end": 160000, "unit": "fs"},
  "signals": [
    {"name": "tb.q", "intervals": [
      {"start": 40000, "end": 50000, "value": "0"},
      {"start": 50000, "end": 150000, "value": "1"},
      {"start": 150000, "end": 160000, "value": "0"}
    ]}
  ]
}
```

区间为左闭右开 `[start, end)`，连续完整覆盖窗口，取值仅 `0/1/x/z`，
**相邻同值区间已合并**。

### 裁决与校验规则

- 每个选中信号从**窗口起点的有效值**（时间戳 `<= start` 的最后一次赋值）开始；
  起点前没有任何显式赋值的信号会令整个请求失败（HTTP 422），**不产生部分结果**。
- 同一时刻多次赋值严格按 **VCD 文本顺序**裁决（后者胜），同刻毛刺折叠后消失。
- 时标只接受 `1/10/100 × fs|ps|ns`，全部换算为飞秒整数后比较。
- 只接受 1 位 `wire`、规范 module 作用域、非递减时间戳。
- 拒绝重复完整层级信号名、重复信号选择、未声明标识符、向量/实数/事件赋值、
  语法错误与越界换算；错误返回 JSON `{"code","message","line"?}`，可定位行号。
- 单次请求最多处理 **50,000** 次值变更。

### `GET /health`

返回 `{"status":"ok"}`，供容器健康检查与 `verify` 服务就绪门控。

## 本地运行

```bash
python3 -m vcd_service            # 默认 0.0.0.0:8080
VCD_PORT=9090 python3 -m vcd_service
```

请求示例：

```bash
curl -s http://127.0.0.1:8080/api/vcd/window \
  -F file=@wave.vcd \
  -F signals=tb.clk -F signals=tb.rst_n \
  -F start_fs=0 -F end_fs=50000000
```

## Docker

宿主机端口可配置（默认 8080）：

```bash
VCD_HOST_PORT=9090 docker compose up --build -d
```

一次性校验服务 `verify`：健康检查就绪后执行**字节码构建检查 + unittest 代码
测试 + HTTP 冒烟**（含同刻变更裁决与跨时标窗口），并以退出码报告结果：

```bash
docker compose build
docker compose up --abort-on-container-exit verify
# 或： docker compose run --rm verify
echo $?   # 0 表示全部通过
```

## 不使用 Docker 的等价校验

```bash
python3 -m compileall -q vcd_service scripts tests
python3 -m unittest discover -s . -p 'test_*.py'
python3 -m vcd_service &          # 启动后
python3 scripts/smoke_http.py http://127.0.0.1:8080
```

## 目录结构

```
vcd_service/
  parser.py    # VCD 词法/语义解析（fs 整数换算）
  window.py    # 半开窗口裁决（同刻后者胜、同值合并、完整性校验）
  server.py    # 标准库 HTTP 服务与 multipart 解析
tests/         # parser / window / HTTP 共 60+ 个 unittest
scripts/
  smoke_http.py  # HTTP 冒烟（同刻变更、跨时标窗口、错误路径）
  verify.sh      # 容器内一次性校验入口
```
