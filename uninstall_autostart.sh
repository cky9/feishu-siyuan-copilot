#!/bin/bash
# 一键卸载飞书 Copilot 开机自启服务

PLIST_NAME="com.feishu.copilot.plist"
DEST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
LEGACY_PATH="$HOME/Library/LaunchAgents/com.feishu.copilot.plist"

echo "🛑 正在卸载飞书 AI 秘书开机自启服务..."
launchctl unload -w "$DEST_PATH" 2>/dev/null || true
rm -f "$DEST_PATH"

echo "✅ 开机自启服务已完全卸载。"
