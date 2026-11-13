#!/usr/bin/env bash
# 启动 SQL 数据空间分析台（零依赖，仅需 Python 3.11+）
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

exec python3 -m app.server --host "$HOST" --port "$PORT"
