#!/usr/bin/env bash
# 配置 cron 定时任务
# 用法：bash scripts/setup_cron.sh
#
# 默认每天 8:00 和 18:00 跑一次（HKEX 通常下午发布 PHIP）

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LOG_FILE="$PROJECT_DIR/logs/cron.log"

if [ ! -f "$PYTHON" ]; then
    echo "未找到虚拟环境，请先创建: python3 -m venv .venv && pip install -r requirements.txt"
    exit 1
fi

# cron 行（每天 08:00 和 18:00）
CRON_LINE="0 8,18 * * * cd $PROJECT_DIR && $PYTHON main.py run --max-items 5 >> $LOG_FILE 2>&1"

echo "建议的 crontab 配置："
echo ""
echo "  $CRON_LINE"
echo ""
echo "执行 'crontab -e' 后追加上述行。或运行下面命令一键追加："
echo ""
echo "  (crontab -l 2>/dev/null; echo \"$CRON_LINE\") | crontab -"
echo ""
echo "查看日志: tail -f $LOG_FILE"
