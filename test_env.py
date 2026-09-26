#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环境与连通性自检工具
"""
import os
import sys
import json
import requests
import lark_oapi as lark
from lark_oapi.api.auth.v3 import InternalTenantAccessTokenRequest, InternalTenantAccessTokenRequestBody

try:
    import keyring
except ImportError:
    keyring = None

def _resolve_secret(service: str, account: str, env_var: str = None, default: str = "") -> str:
    if keyring:
        try:
            val = keyring.get_password(service, account)
            if val:
                return val.strip()
        except Exception:
            pass
    if env_var:
        env_val = os.getenv(env_var)
        if env_val:
            return env_val.strip()
    return default

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

def run_checks():
    print("=" * 60)
    print("🔍 飞书智能点滴顾问 - 环境与配置连通性自检")
    print("=" * 60)

    if not os.path.exists(CONFIG_PATH):
        print(f"❌ 找不到配置文件: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        conf = json.load(f)

    # 1. 飞书凭据验证
    app_id = conf["feishu"].get("app_id", "") or _resolve_secret("feishu_copilot", "app_id", "FEISHU_APP_ID")
    app_secret = conf["feishu"].get("app_secret", "") or _resolve_secret("feishu_copilot", "app_secret", "FEISHU_APP_SECRET")
    print(f"\n1. 检查飞书应用凭证 (App ID: {app_id})...")
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    req = InternalTenantAccessTokenRequest.builder().request_body(
        InternalTenantAccessTokenRequestBody.builder().app_id(app_id).app_secret(app_secret).build()
    ).build()
    resp = client.auth.v3.tenant_access_token.internal(req)
    if resp.success():
        print("   ✅ 飞书鉴权成功！通信正常。")
    else:
        print(f"   ❌ 飞书鉴权失败: 错误码={resp.code}, 消息={resp.msg}")

    # 2. 检查大模型配置
    llm_conf = conf.get("llm", {})
    provider = llm_conf.get("provider", "siliconflow")
    api_key = llm_conf.get("api_key", "").strip() or _resolve_secret(provider, "api_key", f"{provider.upper()}_API_KEY")
    base_url = llm_conf.get("base_url", "")
    model = llm_conf.get("model", "")
    print(f"\n2. 检查大模型配置...")
    if not api_key:
        print("   ℹ️ 尚未配置 LLM api_key（当前可使用基础捕获模式，配置后可开启深度智能分析）")
    else:
        print(f"   测试大模型接口 ({model} @ {base_url})...")
        try:
            r = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "测试连接：请回复「OK」"}],
                    "max_tokens": 10
                },
                timeout=10
            )
            if r.status_code == 200:
                print(f"   ✅ 大模型连接成功！响应: {r.json()['choices'][0]['message']['content'].strip()}")
            else:
                print(f"   ❌ 大模型响应异常: HTTP {r.status_code} - {r.text}")
        except Exception as e:
            print(f"   ❌ 请求大模型超时或出错: {e}")

    # 3. 检查思源笔记
    sy_conf = conf.get("siyuan", {})
    sy_enabled = sy_conf.get("enabled", False)
    sy_url = sy_conf.get("api_url", "http://127.0.0.1:6806").rstrip("/")
    sy_token = sy_conf.get("token", "") or _resolve_secret("siyuan", "api_token", "SIYUAN_TOKEN")
    print(f"\n3. 检查思源笔记本地联动...")
    if not sy_enabled:
        print("   ℹ️ 思源笔记联动未启用")
    else:
        try:
            h = {"Content-Type": "application/json"}
            if sy_token:
                h["Authorization"] = f"Token {sy_token}"
            r = requests.post(f"{sy_url}/api/system/version", headers=h, timeout=3)
            if r.status_code == 200:
                ver = r.json().get("data", "未知版本")
                print(f"   ✅ 思源笔记连接成功！内核版本: v{ver} (目标笔记本: {sy_conf.get('notebook_name')})")
            else:
                print(f"   ⚠️ 思源笔记接口响应: HTTP {r.status_code}")
        except Exception as e:
            print(f"   ❌ 连接思源笔记失败 (请确认思源笔记客户端是否已启动): {e}")

    print("\n" + "=" * 60)
    print("自检完成！如果飞书鉴权成功，即可运行 ./start.sh 启动后台常驻。")
    print("=" * 60)

if __name__ == "__main__":
    run_checks()
