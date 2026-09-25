#!/bin/bash
export LANG="zh_CN.UTF-8"
export LC_ALL="zh_CN.UTF-8"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

exec /usr/bin/python3 "$DIR/bot_service.py"
