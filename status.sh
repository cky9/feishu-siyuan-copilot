#!/bin/bash
# 查看服务状态与最新日志
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PLIST_PATH="$HOME/Library/LaunchAgents/com.feishu.copilot.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.feishu.copilot.plist"
LOG_FILE="$DIR/bot.log"

echo "=================================================="
echo "📊 飞书 24h AI 秘书服务状态"
echo "=================================================="

# 1. 检查开机自启配置
if [ -f "$PLIST_PATH" ]; then
    echo "⚙️ 开机自启 (LaunchAgent): 已开启"
else
    echo "⚙️ 开机自启 (LaunchAgent): 未开启 (如需开启请运行 ./install_autostart.sh)"
fi

# 2. 检查进程运行状态
PIDS=$(pgrep -f "bot_service.py" || true)
if [ -n "$PIDS" ]; then
    echo "🟢 运行状态: 正在运行 (PID: $PIDS)"
else
    echo "🔴 运行状态: 已停止"
fi

echo "--------------------------------------------------"
echo "📋 最近 20 行运行日志:"
echo "--------------------------------------------------"
if [ -f "$LOG_FILE" ]; then
    tail -n 20 "$LOG_FILE"
elif [ -f "$HOME/.feishu_copilot_app/bot.log" ]; then
    tail -n 20 "$HOME/.feishu_copilot_app/bot.log"
else
    echo "(日志文件尚不存在)"
fi
echo "=================================================="
