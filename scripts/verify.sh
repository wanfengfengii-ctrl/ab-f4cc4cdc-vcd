#!/bin/sh
# 一次性校验：构建检查 + 代码测试 + HTTP 冒烟（含同刻变更与跨时标窗口）。
# 以退出码报告结果：全部通过为 0，任一失败为非零。
set -e

# 容器内代码位于 /app；容器外（本地）以脚本所在目录的上级为仓库根
if [ -d /app ]; then
  cd /app
else
  cd "$(dirname "$0")/.."
fi

echo "== 构建检查（字节码编译） =="
python3 -m compileall -q vcd_service scripts tests

echo "== 代码测试（unittest） =="
python3 -m unittest discover -s . -p 'test_*.py' -v

echo "== HTTP 冒烟（同刻变更 / 跨时标窗口） =="
python3 scripts/smoke_http.py "${VCD_URL:-http://127.0.0.1:8080}"

echo "== verify 全部通过 =="
