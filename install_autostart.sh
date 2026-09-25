#!/bin/bash
# 一键安装飞书 Copilot 开机自启服务 (macOS LaunchAgent)

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.feishu_copilot_app"
PLIST_NAME="com.feishu.copilot.plist"
DEST_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "🚀 正在安装飞书 24h AI 秘书开机自启服务..."

# 1. 先卸载可能正在运行的旧服务
launchctl unload "$DEST_PLIST" 2>/dev/null || true
launchctl unload "$HOME/Library/LaunchAgents/com.feishu.copilot.plist" 2>/dev/null || true

# 2. 同步项目文件到安全的家目录运行环境 (避开 macOS 对 Desktop 的沙盒拦截)
mkdir -p "$APP_DIR"
rsync -av --exclude="*.log*" --exclude="*.pid" --exclude="reminders.json" --exclude="feishu_tasks.json" "$SRC_DIR/" "$APP_DIR/"

# 确保数据账本存在
if [ ! -f "$APP_DIR/reminders.json" ]; then
    echo "[]" > "$APP_DIR/reminders.json"
fi
if [ ! -f "$APP_DIR/feishu_tasks.json" ]; then
    echo "[]" > "$APP_DIR/feishu_tasks.json"
fi

# 3. 动态生成并安装 plist 文件
cat << EOF > "$DEST_PLIST"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.feishu.copilot</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$APP_DIR/run_service.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$APP_DIR/bot.log</string>
    <key>StandardErrorPath</key>
    <string>$APP_DIR/bot_err.log</string>
    <key>WorkingDirectory</key>
    <string>$APP_DIR</string>
</dict>
</plist>
EOF
chmod 644 "$DEST_PLIST"
chmod +x "$APP_DIR/run_service.sh"

# 4. 把运行日志与数据账本软链接回当前目录，方便随时查看
if [ "$SRC_DIR" != "$APP_DIR" ]; then
    ln -sfn "$APP_DIR/bot.log" "$SRC_DIR/bot.log"
    ln -sfn "$APP_DIR/bot_err.log" "$SRC_DIR/bot_err.log"
    rm -f "$SRC_DIR/reminders.json"
    ln -sfn "$APP_DIR/reminders.json" "$SRC_DIR/reminders.json"
    rm -f "$SRC_DIR/feishu_tasks.json"
    ln -sfn "$APP_DIR/feishu_tasks.json" "$SRC_DIR/feishu_tasks.json"
fi

# 5. 加载 LaunchAgent 启动服务
launchctl load -w "$DEST_PLIST"

echo "✅ 开机自启服务安装并启动完成！"
echo "💡 当前状态："
launchctl list | grep "feishu.copilot" || echo "正在启动中..."
