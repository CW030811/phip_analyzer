#!/usr/bin/env bash
# 单次运行（手动跑一次完整流程）
# 用法：bash scripts/run_once.sh
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

python main.py run --max-items 10
