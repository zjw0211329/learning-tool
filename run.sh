#!/usr/bin/env bash
# 一键启动 study tools（macOS / Linux）
cd "$(dirname "$0")"
# 多数发行版只有 python3（NFR3 跨平台）；两个都在时优先 python3
PY="$(command -v python3 || command -v python)"
exec "$PY" app/main.py
