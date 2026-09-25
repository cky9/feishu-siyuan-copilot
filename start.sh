#!/bin/bash
# 启动飞书点滴监控服务

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PLIST_PATH="$HOME/Library/LaunchAgents/com.feishu.copilot.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.feishu.copilot.plist"

# 检查是否已安装开机自启
if [ -f "$PLIST_PATH" ]; then
    echo "🚀 检测到已配置开机自启 (LaunchAgent)，正在加载启动..."
    "$DIR/install_autostart.sh"
    exit 0
fi

# 手动后台启动模式
echo "🚀 正在启动飞书点滴监控服务 (后台模式)..."
PIDS=$(pgrep -f "bot_service.py" || true)
if [ -n "$PIDS" ]; then
    echo "⚠️ 服务已在运行中 (PID: $PIDS)，无需重复启动。"
    exit 0
fi

nohup /usr/bin/python3 "$DIR/bot_service.py" > "$DIR/bot.log" 2>&1 &
PID=$!
echo $PID > "$DIR/bot.pid"
echo "✅ 服务启动成功！进程 PID: $PID"
echo "📋 日志文件: $DIR/bot.log"
