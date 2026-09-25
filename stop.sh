#!/bin/bash
# 停止飞书点滴监控服务

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PLIST_PATH="$HOME/Library/LaunchAgents/com.feishu.copilot.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.feishu.copilot.plist"

echo "🛑 正在停止飞书点滴监控服务..."

# 如果是 launchd 托管模式，先 unload
launchctl unload "$PLIST_PATH" 2>/dev/null || true

# 杀死所有 bot_service.py 进程
PIDS=$(pgrep -f "bot_service.py" || true)
if [ -n "$PIDS" ]; then
    kill $PIDS 2>/dev/null || true
    sleep 1
    kill -9 $PIDS 2>/dev/null || true
    echo "✅ 服务已成功停止。"
else
    echo "ℹ️ 服务未在运行。"
fi

rm -f "$DIR/bot.pid"
