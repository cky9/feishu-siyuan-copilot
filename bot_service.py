#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书点滴监控与个人智能顾问 (Feishu Copilot + 思源笔记 + 微软日程)
核心功能:
1. 飞书 WebSocket 长连接 (免公网 IP，实时双向交互)
2. 本地思源笔记直连 (自动归入当天日记，完全私密本地化)
3. 随时呼唤大模型分析 (发送「总结今天」或「/分析」，大模型自动检索思源笔记进行全天深度复盘)
4. 微软 Outlook 日程联动 (自动比对今日计划与实际精力)
5. flomo 闪念笔记自动双写同步
"""

import os
import sys
import re
import json
import time
import logging
import warnings
from datetime import datetime, timedelta
from collections import OrderedDict
import requests
import base64
import io

# 屏蔽 macOS 自带 Python 的 LibreSSL 告警
warnings.filterwarnings("ignore", category=Warning, module="urllib3")

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    GetMessageResourceRequest
)



try:
    import keyring
except ImportError:
    keyring = None

def _resolve_secret(service: str, account: str, env_var: str = None, default: str = "") -> str:
    """
    凭据级联安全解析：
    1. 优先从 macOS Keychain 钥匙串读取
    2. 回退从系统集中环境变量读取 (~/.config/secrets/tokens.env)
    3. 若均未找到则返回 default
    """
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

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 动态安全注入 Keychain / 环境变量凭据
    if "feishu" in cfg:
        cfg["feishu"]["app_id"] = _resolve_secret("feishu_copilot", "app_id", "FEISHU_APP_ID", cfg["feishu"].get("app_id", ""))
        cfg["feishu"]["app_secret"] = _resolve_secret("feishu_copilot", "app_secret", "FEISHU_APP_SECRET", cfg["feishu"].get("app_secret", ""))

    if "siyuan" in cfg:
        cfg["siyuan"]["token"] = _resolve_secret("siyuan", "api_token", "SIYUAN_TOKEN", cfg["siyuan"].get("token", ""))

    sf_key = _resolve_secret("siliconflow", "api_key", "SILICONFLOW_API_KEY")
    ds_key = _resolve_secret("deepseek", "api_key", "DEEPSEEK_API_KEY")
    zp_key = _resolve_secret("zhipu", "api_key", "ZHIPU_API_KEY")

    def _fill_provider_key(p_dict):
        prov = p_dict.get("provider", "").lower()
        if not p_dict.get("api_key"):
            if "siliconflow" in prov and sf_key:
                p_dict["api_key"] = sf_key
            elif "deepseek" in prov and ds_key:
                p_dict["api_key"] = ds_key
            elif "zhipu" in prov and zp_key:
                p_dict["api_key"] = zp_key

    if "llm" in cfg:
        _fill_provider_key(cfg["llm"])

    if "vlm" in cfg and not cfg["vlm"].get("api_key") and zp_key:
        cfg["vlm"]["api_key"] = zp_key

    if "model_providers" in cfg:
        for p_name, p_data in cfg["model_providers"].items():
            _fill_provider_key(p_data)

    return cfg

CONFIG = load_config()

# 配置日志
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("FeishuCopilot")

# 消息去重缓存 (记录最近 1000 条消息 ID)
class MessageDeduplicator:
    def __init__(self, max_size=1000):
        self.seen = OrderedDict()
        self.max_size = max_size

    def is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        if msg_id in self.seen:
            return True
        self.seen[msg_id] = time.time()
        if len(self.seen) > self.max_size:
            self.seen.popitem(last=False)
        return False

dedup = MessageDeduplicator()

# ================= 本地思源笔记接口封装 =================
class SiYuanClient:
    def __init__(self, conf: dict):
        self.enabled = conf.get("enabled", False)
        self.api_url = conf.get("api_url", "http://127.0.0.1:6806").rstrip("/")
        self.token = conf.get("token", "")
        self.notebook = conf.get("notebook", "")
        self.doc_prefix = conf.get("doc_prefix", "每日点滴与灵感")
        self.today_doc_id = None
        self.today_date_str = ""

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Token {self.token}"
        return h

    def is_active(self):
        return self.enabled and bool(self.notebook)

    def get_or_create_today_doc(self) -> str:
        """获取或创建今天的日记文档"""
        if not self.is_active():
            return ""

        today = datetime.now().strftime("%Y-%m-%d")
        if self.today_doc_id and self.today_date_str == today:
            return self.today_doc_id

        doc_title = f"{today} {self.doc_prefix}"
        doc_path = f"/{doc_title}"

        # 1. 尝试通过 SQL 查询是否已存在该文档
        try:
            sql = f"SELECT id FROM blocks WHERE type='d' AND root_id=id AND box='{self.notebook}' AND content='{doc_title}' LIMIT 1"
            res = requests.post(f"{self.api_url}/api/query/sql", headers=self._headers(), json={"stmt": sql}, timeout=5).json()
            if res.get("data") and len(res["data"]) > 0:
                self.today_doc_id = res["data"][0]["id"]
                self.today_date_str = today
                return self.today_doc_id
        except Exception as e:
            logger.warning(f"思源 SQL 查询文档失败: {e}")

        # 2. 不存在则创建
        try:
            init_md = f"# {doc_title}\n\n> 🤖 由飞书智能助理自动同步与整理\n\n---\n"
            create_res = requests.post(f"{self.api_url}/api/filetree/createDocWithMd", headers=self._headers(), json={
                "notebook": self.notebook,
                "path": doc_path,
                "markdown": init_md
            }, timeout=6).json()

            if create_res.get("code") == 0:
                self.today_doc_id = create_res.get("data")
                self.today_date_str = today
                logger.info(f"✅ 在思源笔记创建今日文档成功: {doc_title} (ID: {self.today_doc_id})")
                return self.today_doc_id
            else:
                logger.error(f"思源创建文档失败: {create_res}")
        except Exception as e:
            logger.error(f"思源创建文档请求异常: {e}")

        return ""

    def append_memo(self, raw_content: str, summary_tag: str = "") -> bool:
        """追加一条点滴到今天的思源文档"""
        if not self.is_active():
            return False

        doc_id = self.get_or_create_today_doc()
        if not doc_id:
            return False

        now_time = datetime.now().strftime("%H:%M:%S")
        tag_part = f" `{summary_tag}`" if summary_tag else ""
        block_md = f"* **[{now_time}]**{tag_part} {raw_content}"

        try:
            res = requests.post(f"{self.api_url}/api/block/appendBlock", headers=self._headers(), json={
                "dataType": "markdown",
                "data": block_md,
                "parentID": doc_id
            }, timeout=5).json()
            if res.get("code") == 0:
                logger.info("✅ 成功写入思源笔记")
                return True
            else:
                logger.warning(f"追加思源块失败: {res}")
        except Exception as e:
            logger.error(f"写入思源笔记异常: {e}")
        return False

    def query_today_memos(self) -> list:
        """使用 SQL 获取今天记录的所有点滴内容"""
        if not self.is_active():
            return []

        doc_id = self.get_or_create_today_doc()
        if not doc_id:
            return []

        # 查询该文档下的所有段落和列表块
        sql = f"SELECT content, updated FROM blocks WHERE root_id='{doc_id}' AND type IN ('p', 'i') ORDER BY updated ASC"
        try:
            res = requests.post(f"{self.api_url}/api/query/sql", headers=self._headers(), json={"stmt": sql}, timeout=6).json()
            if res.get("code") == 0:
                records = []
                for item in res.get("data", []):
                    c = item.get("content", "").strip()
                    if c and "由飞书智能助理自动同步" not in c:
                        records.append(c)
                return records
        except Exception as e:
            logger.error(f"从思源笔记拉取点滴记录失败: {e}")
        return []

    def upload_asset(self, filename: str, file_bytes: bytes) -> str:
        """上传静态资源至思源笔记的 assets 目录，返回 relative asset path (如 assets/xxx.png)"""
        if not self.is_active():
            return ""
        try:
            import mimetypes
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            files = {
                "file[]": (filename, file_bytes, mime_type)
            }
            data = {"assetsDirPath": "/assets/"}
            
            # multipart 上传必须仅保留 Authorization 头，不能带 Content-Type: application/json
            upload_headers = {}
            if self.token:
                upload_headers["Authorization"] = f"Token {self.token}"

            resp = requests.post(f"{self.api_url}/api/asset/upload", headers=upload_headers, files=files, data=data, timeout=20)
            if resp.status_code == 200:
                res_json = resp.json()
                if res_json.get("code") == 0:
                    succ_map = res_json.get("data", {}).get("succMap", {})
                    if filename in succ_map:
                        return succ_map[filename]
                    elif succ_map:
                        return list(succ_map.values())[0]
            logger.warning(f"上传思源资源失败: {resp.text}")
        except Exception as e:
            logger.error(f"上传思源资源异常: {e}")
        return ""

    def append_daily_review(self, review_content: str):
        """将全天复盘报告追加进当天的思源文档"""
        doc_id = self.get_or_create_today_doc()
        if not doc_id:
            return
        review_md = f"\n\n---\n### 🧠 【今日智能复盘与认知归纳】\n\n{review_content}\n"
        try:
            requests.post(f"{self.api_url}/api/block/appendBlock", headers=self._headers(), json={
                "dataType": "markdown",
                "data": review_md,
                "parentID": doc_id
            }, timeout=6)
        except Exception as e:
            logger.error(f"复盘报告回写思源失败: {e}")

siyuan = SiYuanClient(CONFIG.get("siyuan", {}))

# ================= 本地统一向量引擎 (Apple M4 硬件优化) =================
try:
    from vector_engine import VectorEngine
    vector_engine = VectorEngine()
    logger.info(f"🧠 本地向量引擎已就绪 (知识库记录: {vector_engine.count_records()} 条)")
except Exception as e:
    logger.warning(f"本地向量引擎挂载失败: {e}")
    vector_engine = None



# ================= 大模型分析与调用 =================
CURRENT_EXECUTION_TAG = "- 本地"

def get_current_llm_brand():
    llm_conf = CONFIG.get("llm", {})
    model = llm_conf.get("model", "")
    base_url = llm_conf.get("base_url", "")
    m_lower = model.lower()
    b_lower = base_url.lower()
    if "siliconflow" in b_lower:
        if "deepseek" in m_lower:
            return "硅基·DeepSeek"
        elif "qwen" in m_lower:
            return "硅基·Qwen"
        return f"硅基·{model.split('/')[-1]}"
    if "glm" in m_lower or "bigmodel" in b_lower:
        return "智谱"
    elif "qwen" in m_lower:
        return "Qwen"
    elif "deepseek" in m_lower:
        return "DeepSeek"
    elif "llama" in m_lower:
        return "Llama"
    elif "gemini" in m_lower:
        return "Gemini"
    return model.split("/")[-1] if model else "AI"

def call_llm(prompt: str, system_prompt: str = "你是一个高认知、行文极简的顾问助手。语言风格精炼有力、直击本质，严禁使用过多emoji表情符号，不要有多余废话。") -> str:
    global CURRENT_EXECUTION_TAG
    llm_conf = CONFIG.get("llm", {})
    api_key = llm_conf.get("api_key", "").strip()
    base_url = llm_conf.get("base_url", "https://api.siliconflow.cn/v1").rstrip("/")
    model = llm_conf.get("model", "deepseek-ai/DeepSeek-V3")
    brand = get_current_llm_brand()

    # 1. 尝试主模型调用
    primary_failed = False
    if api_key:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3
        }
        try:
            url = f"{base_url}/chat/completions"
            resp = requests.post(url, headers=headers, json=payload, timeout=25)
            if resp.status_code == 200:
                CURRENT_EXECUTION_TAG = f"- {brand}"
                return resp.json()["choices"][0]["message"]["content"].strip()
            else:
                logger.warning(f"主模型 [{brand}] 调用失败 (HTTP {resp.status_code}: {resp.text[:200]})，即将触发智谱自动保底...")
                primary_failed = True
        except Exception as e:
            logger.warning(f"主模型 [{brand}] 请求超时或异常 ({e})，即将触发智谱自动保底...")
            primary_failed = True
    else:
        primary_failed = True

    # 2. 若主模型未配置、欠费(402/429)、报错或超时，触发智谱 GLM-4-Flash 永久免费保底
    fallback_conf = CONFIG.get("model_providers", {}).get("zhipu-free", {})
    fb_key = fallback_conf.get("api_key", "").strip() or CONFIG.get("vlm", {}).get("api_key", "").strip() or os.getenv("ZHIPU_API_KEY", "").strip()
    fb_url = fallback_conf.get("base_url", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    fb_model = fallback_conf.get("model", "glm-4-flash")

    if fb_key:
        try:
            logger.info("🛡️ 触发智谱 GLM-4-Flash 永久免费自动保底降级...")
            fb_headers = {"Authorization": f"Bearer {fb_key}", "Content-Type": "application/json"}
            fb_payload = {
                "model": fb_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3
            }
            fb_resp = requests.post(f"{fb_url}/chat/completions", headers=fb_headers, json=fb_payload, timeout=25)
            if fb_resp.status_code == 200:
                tag_reason = "硅基额度耗尽保底" if "siliconflow" in base_url.lower() else "自动保底"
                CURRENT_EXECUTION_TAG = f"- 智谱 ({tag_reason})"
                return fb_resp.json()["choices"][0]["message"]["content"].strip()
            else:
                logger.error(f"智谱保底调用失败: HTTP {fb_resp.status_code} - {fb_resp.text}")
        except Exception as fb_err:
            logger.error(f"智谱保底请求异常: {fb_err}")

    CURRENT_EXECUTION_TAG = "- 出错"
    return "⚠️ 大模型调用异常 (已尝试自动保底，请稍后重试)"

def analyze_image_with_vlm(img_bytes: bytes) -> str:
    """使用视觉大模型（优先专用的 vlm 配置，保证主对话切到硅基/DeepSeek 时 OCR 仍走免费智谱）"""
    vlm_conf = CONFIG.get("vlm", {})
    api_key = vlm_conf.get("api_key", "").strip() or CONFIG.get("llm", {}).get("api_key", "").strip()
    if not api_key:
        return ""

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        max_dim = 1600
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=85)
        compressed_bytes = out_buf.getvalue()
        b64_str = base64.b64encode(compressed_bytes).decode("utf-8")
        mime_type = "image/jpeg"
    except Exception as e:
        logger.warning(f"PIL 图片预处理失败，使用原字节: {e}")
        b64_str = base64.b64encode(img_bytes).decode("utf-8")
        mime_type = "image/png"

    prompt = """这是一张用户截取的书籍页面、文章划线、笔记或随手拍图。请对其进行高精度的 OCR 识字与结构化知识萃取：

1. 【如果包含文字 / 是读书/文章截图】：
   请严格按以下格式输出（重点突出、信息高密度）：
   📌 **核心摘录**：（提炼图中关键原文段落或金句）
   💡 **认知洞见**：（从本质逻辑、认知模型或商业/生活视角深度解读）
   🏷️ **分类标签**：（打上 1-3 个标签，如 #读书笔记、#认知、#方法论）
   ⚡ **行动启发**：（提取 1 条具备杠杆效应的落地建议；若纯理论可写「无直接行动」）

2. 【如果是普通照片 / 无明显文本】：
   📷 **画面概览**：（1-2 句话描述核心内容）
   💡 **细节与联想**：（提炼场景背后的灵感线索）
   🏷️ **场景标签**：（1-2 个标签）

请以精炼、美观的 Markdown 格式输出。不要添加任何多余的寒暄或开场白。"""

    base_url = vlm_conf.get("base_url", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    model = vlm_conf.get("model", "glm-4v-flash")
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_str}"}}
                ]
            }
        ],
        "temperature": 0.2
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"].strip()
            logger.info(f"✅ 视觉模型 {model} 识图提炼成功")
            return content
        else:
            logger.error(f"视觉模型 {model} 调用失败: HTTP {resp.status_code} - {resp.text}")
            return ""
    except Exception as e:
        logger.error(f"视觉模型 {model} 请求异常: {e}")
        return ""

# ================= 飞书 Client 与闹钟日程调度引擎 =================
_feishu_app_id = _resolve_secret("feishu", "app_id", "FEISHU_APP_ID", CONFIG.get("feishu", {}).get("app_id", ""))
_feishu_app_secret = _resolve_secret("feishu", "app_secret", "FEISHU_APP_SECRET", CONFIG.get("feishu", {}).get("app_secret", ""))
client = lark.Client.builder() \
    .app_id(_feishu_app_id) \
    .app_secret(_feishu_app_secret) \
    .build()

from reminder_manager import ReminderScheduler
reminder_scheduler = ReminderScheduler(client, siyuan_client=siyuan, llm_caller=call_llm)

# ================= 飞书原生待办任务集成 (Task v2) =================
DEFAULT_ASSIGNEE_OPEN_ID = "ou_25c777fb5f69eece63075f245d89e324"

def create_feishu_task(summary: str, due_dt: datetime = None, open_id: str = "", description: str = "") -> dict:
    """创建飞书官方原生待办任务 (Task v2)，返回包含 guid 和 url 的字典"""
    try:
        from lark_oapi.api.task.v2 import CreateTaskRequest, InputTask, Member, Due
        builder = InputTask.builder().summary(summary)
        if description:
            builder.description(description)
        else:
            builder.description("🤖 由飞书 24h AI 秘书自动创建")

        assignee_id = open_id or DEFAULT_ASSIGNEE_OPEN_ID
        if assignee_id:
            try:
                builder.members([Member.builder().id(assignee_id).type("user").role("assignee").build()])
            except Exception as e:
                logger.warning(f"构建任务成员失败: {e}")

        if due_dt:
            due_ms = int(due_dt.timestamp() * 1000)
            builder.due(Due.builder().timestamp(str(due_ms)).is_all_day(False).build())

        req = CreateTaskRequest.builder().request_body(builder.build()).build()
        resp = client.task.v2.task.create(req)
        if resp.success():
            task_obj = resp.data.task
            url = getattr(task_obj, "url", "") or f"https://applink.feishu.cn/client/todo/detail?guid={task_obj.guid}"
            logger.info(f"✅ 成功创建飞书官方待办: {summary} (GUID: {task_obj.guid})")
            from action_executor import record_created_task
            record_created_task(task_obj.guid, summary)
            return {"guid": task_obj.guid, "url": url}
        else:
            logger.warning(f"创建飞书待办返回失败: {resp.code} - {resp.msg}")
    except Exception as e:
        logger.error(f"创建飞书待办异常: {e}")
    return {}

def complete_feishu_task(guid: str) -> bool:
    """在飞书 Task v2 中标记任务已完成"""
    if not guid:
        return False
    try:
        from lark_oapi.api.task.v2 import PatchTaskRequest, PatchTaskRequestBody, InputTask
        now_ms = str(int(time.time() * 1000))
        input_task = InputTask.builder().completed_at(now_ms).build()
        req_body = PatchTaskRequestBody.builder().task(input_task).update_fields(["completed_at"]).build()
        req = PatchTaskRequest.builder().task_guid(guid).request_body(req_body).build()
        resp = client.task.v2.task.patch(req)
        if resp.success() or (resp.code == 1470400 and "completed" in str(resp.msg).lower()):
            logger.info(f"✅ 飞书待办任务已完成/处于完成状态: {guid}")
            _update_local_task_status(guid, "completed")
            return True
        else:
            logger.warning(f"完成飞书待办返回错误: {resp.code} - {resp.msg}")
    except Exception as e:
        logger.error(f"完成飞书待办异常: {e}")
    return False

def delete_feishu_task(guid: str) -> bool:
    """在飞书 Task v2 中物理删除任务"""
    if not guid:
        return False
    try:
        from lark_oapi.api.task.v2 import DeleteTaskRequest
        req = DeleteTaskRequest.builder().task_guid(guid).build()
        resp = client.task.v2.task.delete(req)
        if resp.success():
            logger.info(f"✅ 成功从飞书物理删除待办: {guid}")
            _update_local_task_status(guid, "deleted")
            return True
        else:
            logger.warning(f"删除飞书待办返回错误: {resp.code} - {resp.msg}")
    except Exception as e:
        logger.error(f"删除飞书待办异常: {e}")
    return False

def update_feishu_task_due(guid: str, due_dt: datetime) -> bool:
    """在飞书 Task v2 中更新任务截止时间"""
    if not guid or not due_dt:
        return False
    try:
        from lark_oapi.api.task.v2 import PatchTaskRequest, PatchTaskRequestBody, InputTask, Due
        due_ms = int(due_dt.timestamp() * 1000)
        due_obj = Due.builder().timestamp(str(due_ms)).is_all_day(False).build()
        input_task = InputTask.builder().due(due_obj).build()
        req_body = PatchTaskRequestBody.builder().task(input_task).update_fields(["due"]).build()
        req = PatchTaskRequest.builder().task_guid(guid).request_body(req_body).build()
        resp = client.task.v2.task.patch(req)
        if resp.success():
            logger.info(f"✅ 成功更新飞书待办截止时间: {guid} -> {due_dt}")
            return True
        else:
            logger.warning(f"更新飞书待办截止时间返回错误: {resp.code} - {resp.msg}")
    except Exception as e:
        logger.error(f"更新飞书待办截止时间异常: {e}")
    return False

def _update_local_task_status(guid: str, status: str):
    """更新本地 feishu_tasks.json 中的任务状态"""
    try:
        fpath = os.path.join(BASE_DIR, "feishu_tasks.json")
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            for t in tasks:
                if t.get("guid") == guid:
                    t["status"] = status
                    t["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"更新本地任务状态异常: {e}")

def find_task_guid_by_summary(task_summary: str) -> str:
    """根据任务标题在 feishu_tasks.json 中查找最近对应的任务 GUID"""
    try:
        fpath = os.path.join(BASE_DIR, "feishu_tasks.json")
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            for t in reversed(tasks):
                if t.get("summary") == task_summary:
                    return t.get("guid", "")
    except Exception as e:
        logger.warning(f"查找本地任务 GUID 异常: {e}")
    return ""

# ================= 飞书官方日历日程集成 (Calendar v4 手机日历联动) =================
def create_feishu_calendar_event(summary: str, start_dt: datetime, open_id: str = "", duration_minutes: int = 15, description: str = "") -> dict:
    """创建飞书官方日历日程 (Calendar Event v4)，并添加用户为参与人，实现手机系统日历与闹钟无缝联动"""
    try:
        from lark_oapi.api.calendar.v4 import (
            CreateCalendarEventRequest, CreateCalendarEventAttendeeRequest,
            CreateCalendarEventAttendeeRequestBody, CalendarEvent, CalendarEventAttendee,
            TimeInfo, Reminder
        )
        st = int(start_dt.timestamp())
        et = st + duration_minutes * 60

        event_builder = CalendarEvent.builder() \
            .summary(summary) \
            .description(description or "🤖 飞书 24h AI 秘书创建（已联动手机系统日历与闹钟）") \
            .start_time(TimeInfo.builder().timestamp(str(st)).build()) \
            .end_time(TimeInfo.builder().timestamp(str(et)).build()) \
            .need_notification(True) \
            .reminders([
                Reminder.builder().minutes(0).build(),  # 事件发生时准时响铃
                Reminder.builder().minutes(5).build()   # 提前 5 分钟预警
            ])

        req = CreateCalendarEventRequest.builder() \
            .calendar_id("primary") \
            .request_body(event_builder.build()) \
            .build()

        resp = client.calendar.v4.calendar_event.create(req)
        if resp.success():
            event_id = resp.data.event.event_id
            app_link = getattr(resp.data.event, "app_link", "")
            logger.info(f"📅 成功创建飞书官方日历日程: {summary} (Event ID: {event_id})")

            # 邀请用户作为参与人，使其立即出现在用户的个人主日历及手机同步日历中
            attendee_id = open_id or DEFAULT_ASSIGNEE_OPEN_ID
            if attendee_id:
                try:
                    att_req = CreateCalendarEventAttendeeRequest.builder() \
                        .calendar_id("primary") \
                        .event_id(event_id) \
                        .user_id_type("open_id") \
                        .request_body(CreateCalendarEventAttendeeRequestBody.builder()
                                      .attendees([CalendarEventAttendee.builder()
                                                  .type("user")
                                                  .user_id(attendee_id)
                                                  .build()])
                                      .need_notification(False)
                                      .build()) \
                        .build()
                    client.calendar.v4.calendar_event_attendee.create(att_req)
                    logger.info(f"👥 已邀请用户加入日历日程: {attendee_id}")
                except Exception as e:
                    logger.warning(f"添加日历参与人异常: {e}")

            return {"event_id": event_id, "app_link": app_link}
        else:
            logger.warning(f"创建飞书日历日程失败: {resp.code} - {resp.msg}")
    except Exception as e:
        logger.error(f"创建飞书日历日程异常: {e}")
    return {}

def update_feishu_calendar_event(event_id: str, new_start_dt: datetime = None, duration_minutes: int = 15, new_summary: str = None) -> bool:
    """更新飞书官方日历日程时间或标题（用于延期或标记完成）"""
    if not event_id:
        return False
    try:
        from lark_oapi.api.calendar.v4 import PatchCalendarEventRequest, CalendarEvent, TimeInfo
        builder = CalendarEvent.builder()
        if new_start_dt:
            st = int(new_start_dt.timestamp())
            et = st + duration_minutes * 60
            builder.start_time(TimeInfo.builder().timestamp(str(st)).build())
            builder.end_time(TimeInfo.builder().timestamp(str(et)).build())
        if new_summary:
            builder.summary(new_summary)

        req = PatchCalendarEventRequest.builder() \
            .calendar_id("primary") \
            .event_id(event_id) \
            .request_body(builder.build()) \
            .build()
        resp = client.calendar.v4.calendar_event.patch(req)
        if resp.success():
            logger.info(f"📅 成功更新飞书日历日程: {event_id}")
            return True
        else:
            logger.warning(f"更新飞书日历日程返回: {resp.code} - {resp.msg}")
    except Exception as e:
        logger.warning(f"更新飞书日历日程异常: {e}")
    return False

def delete_feishu_calendar_event(event_id: str) -> bool:
    """删除飞书官方日历日程（用于取消提醒）"""
    if not event_id:
        return False
    try:
        from lark_oapi.api.calendar.v4 import DeleteCalendarEventRequest
        req = DeleteCalendarEventRequest.builder().calendar_id("primary").event_id(event_id).build()
        resp = client.calendar.v4.calendar_event.delete(req)
        if resp.success():
            logger.info(f"📅 成功删除飞书日历日程: {event_id}")
            return True
    except Exception as e:
        logger.warning(f"删除飞书日历日程异常: {e}")
    return False

def handle_reminder_feedback_intent(text_clean: str, chat_id: str, open_id: str):
    """
    上下文感知：闭环/延期/取消闹钟提醒
    处理用户在收到到期提醒（或设定提醒后）发送的反馈：「确认」、「完成」、「延期 10分钟」、「取消」等
    0 Token 本地即时响应，自动同步飞书待办中心与思源笔记
    """
    text_lower = text_clean.lower().strip()

    # 动作 A: 确认完成 (闭环)
    complete_words = {
        "确认", "已确认", "确认完成", "收到", "收到啦", "已收到",
        "完成", "已完成", "搞定", "已搞定", "办了", "已办",
        "done", "ok", "知道了", "明白", "弄好了", "处理了"
    }
    is_complete = text_lower in complete_words or bool(re.match(r"^(?:确认|已确认|已完成|已搞定|搞定|完成|收到)(?:了|啦)?$", text_clean))

    # 动作 B: 延期推迟 (Snooze)
    m_snooze = re.match(r"^(?:延期|推迟|延后|稍后|待会|等会儿)(?:提醒)?(?:\s*(\d+|半|[一二两三四五六七八九十]+)\s*(?:个)?(分钟|小时|分|h|min|m)?)?$", text_clean, re.IGNORECASE)
    m_snooze2 = re.match(r"^(?:再过|等)\s*(\d+|半|[一二两三四五六七八九十]+)\s*(?:个)?(分钟|小时|分|h|min|m)?(?:后再提醒我?|提醒我?)$", text_clean)

    # 动作 C: 取消作废 (Cancel)
    cancel_words = {"取消", "不用了", "不用提醒", "不用提醒了", "取消该提醒", "取消闹钟", "取消待办", "算了", "作废"}
    is_cancel = text_lower in cancel_words

    # 如果有待确认的 triggered 闹钟（24小时内），用户回复「好」或「好的」也视为确认闭环
    # 但如果只有 pending 闹钟，回复「好」不触发，避免误触
    is_gentle_affirm = text_clean in ["好", "好的"]

    if not (is_complete or m_snooze or m_snooze2 or is_cancel or is_gentle_affirm):
        return None

    # 检索最近一条可操作的提醒
    target_rem, rem_type = reminder_scheduler.get_latest_actionable_reminder(chat_id)
    if not target_rem:
        # 没有可操作提醒时：如果是显式延期或取消指令，提示用户；如果是普通词「好/收到/确认」，返回 None 让其正常流转
        if m_snooze or m_snooze2:
            return "⏰ 当前没有任何待触发或刚刚提醒的闹钟，无需延期。"
        if is_cancel and text_clean in ["取消该提醒", "取消闹钟"]:
            return "⏰ 当前没有任何活跃闹钟。"
        return None

    # 如果命中「好/好的」，但最近的提醒并不是 triggered（已响铃），则不拦截
    if is_gentle_affirm and rem_type != "triggered":
        return None

    task_name = target_rem.get("task", "未知事项")
    rem_id = target_rem.get("id")
    guid = target_rem.get("feishu_task_guid") or find_task_guid_by_summary(task_name)
    cal_event_id = target_rem.get("calendar_event_id")

    # 执行具体动作
    if is_complete or is_gentle_affirm:
        reminder_scheduler.complete_reminder(rem_id)
        if guid:
            complete_feishu_task(guid)
        if cal_event_id:
            update_feishu_calendar_event(cal_event_id, new_summary=f"✅ {task_name}")
        if siyuan and siyuan.is_active():
            siyuan.append_memo(f"✅ 事项已完成闭环：{task_name}", summary_tag="#已完成")
        logger.info(f"🎯 提醒闭环成功: {task_name} (ID: {rem_id})")
        return (
            f"✅ 备忘已闭环\n"
            f"事项：{task_name}\n"
            f"状态：已标记完成\n"
            f"思源与待办已同步"
        )

    elif m_snooze or m_snooze2:
        m = m_snooze or m_snooze2
        num_str = m.group(1)
        unit = m.group(2) if len(m.groups()) > 1 else ""

        delay_minutes = 10  # 默认推迟 10 分钟
        if num_str:
            CN_NUM = {"一":1, "二":2, "两":2, "三":3, "四":4, "五":5, "六":6, "七":7, "八":8, "九":9, "十":10}
            if num_str == "半":
                delay_minutes = 30
            elif num_str.isdigit():
                val = int(num_str)
                delay_minutes = val * 60 if unit in ["小时", "h"] else val
            elif num_str in CN_NUM:
                val = CN_NUM[num_str]
                delay_minutes = val * 60 if unit in ["小时", "h"] else val

        delay_minutes = max(1, delay_minutes)
        res = reminder_scheduler.snooze_reminder(rem_id, delay_minutes)
        if res:
            item, new_dt = res
            new_time_str = new_dt.strftime("%H:%M") if new_dt.date() == datetime.now().date() else new_dt.strftime("%Y-%m-%d %H:%M")
            if guid:
                update_feishu_task_due(guid, new_dt)
            if cal_event_id:
                update_feishu_calendar_event(cal_event_id, new_start_dt=new_dt)
            if siyuan and siyuan.is_active():
                siyuan.append_memo(f"⏰ 事项已延期 {delay_minutes} 分钟至 [{new_time_str}]：{task_name}", summary_tag="#待办闹钟")
            logger.info(f"🎯 提醒延期成功: {task_name} -> {new_time_str}")
            return (
                f"⏰ 提醒已推迟\n"
                f"事项：{task_name}\n"
                f"新时间：{new_time_str} (约 {delay_minutes} 分钟后)\n"
                f"思源与待办已同步"
            )

    elif is_cancel:
        reminder_scheduler.cancel_reminder_by_id(rem_id)
        if guid:
            delete_feishu_task(guid)
        if cal_event_id:
            delete_feishu_calendar_event(cal_event_id)
        if siyuan and siyuan.is_active():
            siyuan.append_memo(f"🗑️ 取消提醒：{task_name}", summary_tag="#注销闹钟")
        logger.info(f"🎯 提醒取消成功: {task_name} (ID: {rem_id})")
        return (
            f"🗑️ 提醒已取消\n"
            f"事项：{task_name}\n"
            f"思源与待办已同步"
        )

    return None

def detect_sleep_rhythm_log(text_clean: str, now: datetime):
    """
    生理节律与作息打卡智能拦截器 (0-Token 本地秒级响应)
    精准识别深夜至清晨（及日常）的「醒」、「起夜」、「睡了」、「午休」等生理状态记录
    自动归入 #作息 #睡眠 体系，杜绝大模型无关的玄学说教（如冥想建议）
    """
    clean = text_clean.strip()
    if len(clean) > 15:
        return None

    # 排除显式的指令、待办或提醒
    if any(clean.startswith(prefix) for prefix in ["待办", "TODO", "todo", "- [ ]", "提醒", "取消", "查看", "切换", "我的"]):
        return None

    hour = now.hour
    now_hm = now.strftime("%H:%M")

    # 1. 晨起 / 唤醒模式
    m_wake = re.match(r"^(?:刚?醒(?:了|来)?|早醒|早起|起(?:了|床(?:了)?)?)$", clean)

    # 2. 夜醒 / 起夜 / 睡眠障碍模式
    m_night_wake = re.match(r"^(?:起夜|夜醒|半夜醒|睡不着|失眠|翻来覆去|恶梦醒了|做噩梦醒了)$", clean)

    # 3. 就寝 / 入睡模式
    m_sleep = re.match(r"^(?:睡(?:了|觉(?:了)?)?|去睡(?:了|觉)?|准备睡(?:了|觉)?|入睡|晚安|熄灯)$", clean)

    # 4. 午间作息模式
    m_nap = re.match(r"^(?:午休(?:了)?|午睡(?:了)?|小憩|眯(?:了)?会儿|眯一会|睡个午觉)$", clean)
    m_nap_wake = re.match(r"^(?:午睡醒(?:了)?|午休醒(?:了)?|午觉醒了)$", clean)

    matched_type = None
    subtag = ""
    title = ""
    status_desc = ""

    if m_night_wake:
        matched_type = "night_wake"
        subtag = "作息/夜醒"
        title = "🌙 夜间作息"
        status_desc = "已记录夜醒状态"
    elif m_wake:
        if 0 <= hour < 5:
            matched_type = "night_wake"
            subtag = "作息/夜醒"
            title = "🌙 夜间作息"
            status_desc = "已记录夜醒状态"
        elif 5 <= hour < 12:
            matched_type = "morning_wake"
            subtag = "作息/晨起"
            title = "🌅 晨起作息"
            status_desc = "已记录清醒打卡"
        else:
            matched_type = "wake"
            subtag = "作息/清醒"
            title = "☀️ 作息打卡"
            status_desc = "已记录清醒状态"
    elif m_sleep:
        matched_type = "sleep"
        subtag = "作息/就寝"
        title = "🌙 就寝作息"
        status_desc = "已记录就寝打卡"
    elif m_nap:
        matched_type = "nap"
        subtag = "作息/午休"
        title = "☀️ 午间作息"
        status_desc = "已记录午休小憩"
    elif m_nap_wake:
        matched_type = "nap_wake"
        subtag = "作息/午休唤醒"
        title = "☀️ 午间作息"
        status_desc = "已记录午休唤醒"

    if not matched_type:
        return None

    if siyuan and siyuan.is_active():
        memo_content = f"【{subtag}】{clean}"
        siyuan.append_memo(memo_content, summary_tag=f"#{subtag} #睡眠")

    logger.info(f"🛌 命中生理作息记录: [{subtag}] {clean} ({now_hm})")

    return (
        f"{title}\n"
        f"时间：{now_hm}\n"
        f"状态：{status_desc} (#{subtag})\n"
        f"思源已同步"
    )

# ================= Agent 动作执行网关 (Action Tools Gateway) =================
from action_executor import ActionGateway, record_created_task, cancel_last_created_task
REMINDERS_FILE = os.path.join(BASE_DIR, "reminders.json")
action_gateway = ActionGateway(siyuan_client=siyuan, reminders_file=REMINDERS_FILE, lark_client=client)

# ================= 躯体直觉预警雷达与本地智能引擎 (0 Token 毫秒级) =================
WORKSPACE_LEDGER = CONFIG.get("custom_hooks", {}).get("somatic_ledger_path") or os.getenv("WORKSPACE_LEDGER")
AUDIT_SCRIPT = CONFIG.get("custom_hooks", {}).get("somatic_audit_script") or os.getenv("AUDIT_SCRIPT")

SOMATIC_ORGAN_RULES = [
    (r"(胃|肠|腹|拉肚子|恶心|抽搐)", "胃部/肠道紧缩 (典型下行风险/拒斥)"),
    (r"(胸|憋|闷|呼吸|喘|心慌|心跳|发虚)", "胸膈气滞/心慌发虚 (边界超载/系统负荷)"),
    (r"(颈|项|肩|背|腰|脊椎)", "颈肩腰背紧绷 (防御激活/对抗预期)"),
    (r"(头|脑|晕|头疼|头沉|偏头痛)", "头部沉重/前额叶过载 (认知超负荷)"),
    (r"(睡|醒|失眠|做梦|梦境|夜里|半夜)", "睡眠干扰/DMN离线活跃 (潜意识警报)"),
    (r"(焦躁|焦虑|烦躁|郁闷|沮丧|内耗)", "情绪气机阻滞 (多巴胺/内耗消耗)"),
]

PHYSIOLOGICAL_CAUSES = [
    "浓茶", "喝茶", "茶叶", "咖啡", "美式", "拿铁", "咖啡因",
    "喝多了酒", "喝酒", "啤酒", "白酒", "宿醉",
    "吃辣", "辣椒", "火锅", "感冒", "发烧", "咳嗽", "腹泻", "拉肚子",
    "受凉", "着凉", "吹空调", "剧烈运动", "深蹲", "跑步", "健身", "通宵", "熬夜", "晕车", "晕船", "中暑"
]

def detect_somatic_radar(text: str, llm_caller=None):
    """检测是否含有躯体标记、预感、情绪异常等信号 (Qwen 智能研判 + 生理诱因精准排除)"""
    text_lower = text.lower()
    explicit_tags = ["#预警", "#躯体", "#直觉", "#躯体预警"]
    has_explicit_tag = any(t in text_lower for t in explicit_tags)

    # 1. 外部生理/生活诱因过滤：若包含浓茶/咖啡/熬夜等明确生理原因且未打显式预警标签，直接判定为日常生理记录
    if not has_explicit_tag and any(c in text_lower for c in PHYSIOLOGICAL_CAUSES):
        logger.info(f"💡 识别为已知外部生理/生活反应 (如咖啡因/饮食/作息)，流转至日常健康顾问: {text}")
        return None

    # 2. 检查是否有身体/直觉关键词或器官匹配
    triggers = ["预感", "身体", "直觉", "不对劲", "不安", "担心", "既视感", "deja vu", "先赚后亏", "套牢", "心神不宁", "发慌", "郁闷", "心跳", "胸闷", "胃", "前额", "失眠", "后背"]
    matched_organs = []
    for pattern, desc in SOMATIC_ORGAN_RULES:
        if re.search(pattern, text):
            matched_organs.append(desc)

    is_triggered = has_explicit_tag or any(t in text_lower for t in triggers) or bool(matched_organs)
    if not is_triggered:
        return None

    # 3. 优先调用大模型进行深层语境研判 (区分单纯生理不适与决策潜意识警报)
    if llm_caller:
        prompt = f"""用户在随笔中记录了一条涉及身体体感或心理状态的内容：
"{text}"

请深入研判该记录的本质属性：
1. 【已知外部生理/生活诱因】（is_somatic_radar: false）：
   - 明确由饮食、作息、外部环境、生化物质、体力活动引起的生理反应。例如：“吃坏肚子胃痛”、“感冒发烧咳嗽”、“吹空调脖子酸”、“跑步完心跳快”、“昨晚通宵改代码今天头晕”、“喝茶失眠”。
   - 这种情况下，属于【日常健康/生理状态】，绝不是潜意识隐性预警！必须设为 false，交给日常顾问提供健康与代谢建议。
2. 【无明确诱因的潜意识预警 / 躯体标记】（is_somatic_radar: true）：
   - 在交易、决策、人际或日常中，身体无明确生理原因地出现紧缩、心悸、发慌、既视感、睡眠异常、直觉强烈不安。例如：“总觉得这只票哪里不对劲，心慌”、“没有任何理由，突然胃部痉挛”、“今晚心里莫名发毛睡不着”、“看着盘面有强烈的既视感和套牢恐惧”。
   - 这种情况下，属于【躯体直觉预警雷达】。请提炼：
     - organ: 核心受累器官（如：胸膈气滞/心慌、胃部紧缩等）
     - decoded: 潜意识验尸译码（潜意识感知到的潜在风险或矛盾）
     - action: 最小阻断/防御动作（给神经开具收据的具体微行动）

请以纯 JSON 输出：
若为预警雷达：
{{"is_somatic_radar": true, "organ": "器官标记", "decoded": "潜意识译码", "action": "最小防御动作"}}
若为生理/日常诱因或普通记录：
{{"is_somatic_radar": false}}
只输出纯 JSON，不要有任何额外文字或 markdown。"""
        try:
            res = llm_caller(prompt, system_prompt="你是一个专业的身心医学与潜意识直觉预警评估专家。只输出纯 JSON。")
            if res:
                json_str = re.sub(r"```(?:json)?", "", res).strip()
                data = json.loads(json_str)
                if data.get("is_somatic_radar"):
                    organ_str = data.get("organ", "") or ("、".join(matched_organs) if matched_organs else "全身体感/心理直觉预警")
                    return {
                        "organ": organ_str,
                        "decoded": data.get("decoded", "潜意识捕捉到异常信号"),
                        "action": data.get("action", "执行最小确认动作，设立防守边界")
                    }
                else:
                    return None
        except Exception as e:
            logger.warning(f"大模型判别躯体雷达异常，回退本地规则: {e}")

    # 4. 本地规则兜底 (仅当显式含有 #预警、#直觉 或 强直觉词时才触发)
    if has_explicit_tag or any(t in text_lower for t in ["预感", "直觉", "不对劲", "先赚后亏", "套牢", "既视感"]):
        organ_str = "、".join(matched_organs) if matched_organs else "全身体感/心理直觉预警"
        if any(k in text for k in ["胃", "套牢", "下行", "跌", "先赚后亏", "买断"]):
            decoded = "潜意识正敏锐扫描非对称下行风险，防范再次滑入被动套牢剧本"
            action = "设紧防守底线，10:30前不看分时，排期1个客观核验动作开具神经收据"
        elif any(k in text for k in ["胸", "闷", "郁闷", "呼吸", "喘"]):
            decoded = "气机受阻于胸膈，微观走势或多任务撕裂引发认知失调"
            action = "立刻离开屏幕，做3次极深长叹气式深呼吸宣肺，喝一杯温水阻断内耗"
        elif any(k in text for k in ["睡", "梦", "醒", "半夜", "夜里"]):
            decoded = "DMN默认模式网络在后台离线匹配到未解决的异常模式或人际线索"
            action = "文字已成功外显卸载，给下丘脑发送安全收据，安心休息"
        else:
            decoded = "潜意识嗅到隐藏的环境变量与认知偏差，生物警报系统已正常唤醒"
            action = "今日保持观察防守姿态，不盲目追加暴露，排期一个微小确认动作"

        return {
            "organ": organ_str,
            "decoded": decoded,
            "action": action
        }

    return None

def sync_to_somatic_ledger(raw_text: str, organ: str, decoded: str, action: str) -> str:
    """自动将躯体预感追加写入本地预警底册 (数字游民个人发展系统)"""
    if not WORKSPACE_LEDGER or not os.path.exists(WORKSPACE_LEDGER):
        logger.debug(f"未配置预警底册或文件不存在: {WORKSPACE_LEDGER}")
        return ""
    try:
        with open(WORKSPACE_LEDGER, "r", encoding="utf-8") as f:
            content = f.read()

        ids = [int(x) for x in re.findall(r"#(\d{3})", content)]
        next_id = max(ids) + 1 if ids else 1
        id_str = f"#{next_id:03d}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        # 摘要清洗
        clean_thought = raw_text.replace("\n", " ").replace("|", "/").strip()[:32]
        clean_organ = organ.split()[0] if organ else "全身体感"
        clean_decoded = decoded.replace("|", "/")[:30]
        clean_action = action.replace("|", "/")[:30]

        new_row = f"| {id_str} | {now_str} | {clean_organ} | {clean_thought} | {clean_decoded} | {clean_action} | 待定 | ⏳待验证 |\n"

        # 插入到活跃清单表格最后一行
        last_row_match = list(re.finditer(r"(\| #\d{3} \|.*?\n)", content))
        if last_row_match:
            last_span = last_row_match[-1].span()
            new_content = content[:last_span[1]] + new_row + content[last_span[1]:]
        elif "| *暂无待验证记录*" in content:
            new_content = content.replace("| *暂无待验证记录* | - | - | - | - | - | - | - |\n", new_row)
        else:
            target = "## 📁 三、 历史结算与回测归档"
            new_content = content.replace(target, new_row + "\n---\n\n" + target)

        with open(WORKSPACE_LEDGER, "w", encoding="utf-8") as f:
            f.write(new_content)

        if AUDIT_SCRIPT and os.path.exists(AUDIT_SCRIPT):
            os.system(f"python3 \"{AUDIT_SCRIPT}\" >/dev/null 2>&1 &")

        logger.info(f"✅ 成功自动同步至本地预警底册: {id_str}")
        return id_str
    except Exception as e:
        logger.error(f"同步躯体预警底册异常: {e}")
        return ""

# ================= 自然语言标签智能识别器 =================
COMMON_TAG_KEYWORDS = [
    # 认知思考类
    '思考', '反思', '心得', '随想', '灵感', '金句', '复盘',
    # 动作留存类
    '存档', '保存', '归档', '备忘', '摘录', '记录', '日记', '资料',
    # 专属实体/项目类
    '孩子教育', '孩子', '教育', '初二', '五年级',
    '八部金刚功', '金刚功', '练功', '健康', '身体', '运动',
    '言三交易', '交易', '股市', '盯盘'
]
KW_PATTERN = '|'.join(sorted(COMMON_TAG_KEYWORDS, key=len, reverse=True))

def parse_natural_tags(text: str):
    """自动识别句首、句尾自然语言标签（如'思考：'、'存档'、'【孩子教育】'），实现 0 门槛随手打标"""
    tags = []
    clean_text = text.strip()

    # 1. 显式 #标签
    hash_tags = re.findall(r'#([\w\u4e00-\u9fa5/]+)', clean_text)
    if hash_tags:
        for ht in hash_tags:
            if ht not in tags:
                tags.append(ht)

    # 2. 句首识别：【标签】、[标签]、标签：、标签:、标签 + 空格
    prefix_match = re.match(rf'^(?:[【\[\(（]({KW_PATTERN})[】\]\)）]|({KW_PATTERN})[:：\s\-\/])\s*(.*)$', clean_text, re.DOTALL | re.IGNORECASE)
    if prefix_match:
        tag = prefix_match.group(1) or prefix_match.group(2)
        if tag not in tags:
            tags.append(tag)
        rem = prefix_match.group(3).strip()
        if rem:
            clean_text = rem

    # 3. 句尾识别：内容 存档、内容 (思考)、内容 - 孩子教育
    suffix_match = re.search(rf'^(.*?)\s*[-——\s\(\[（【]+({KW_PATTERN})[】）\]\)]*$', clean_text, re.DOTALL | re.IGNORECASE)
    if suffix_match:
        body = suffix_match.group(1).strip()
        tag = suffix_match.group(2)
        if body and len(body) >= 2:
            clean_text = body
            if tag not in tags:
                tags.append(tag)

    return clean_text, tags

# 1. 单条即时分析 (支持 0 Token 本地高速解析 与 大模型深度提取)
def analyze_single_memo(raw_text: str, user_tags: list = None) -> str:
    llm_conf = CONFIG.get("llm", {})

    # 若未配置 API Key，启动 0-Token 本地结构化提取引擎 (完全免费、毫秒级响应)
    if not llm_conf.get("api_key"):
        todos = []
        for line in raw_text.split("\n"):
            line_clean = line.strip()
            if re.search(r"(- \[ \]|TODO|todo|买|办|去|搞|联系|安排|提醒|写|改|提交|核对|出差|约|确认)", line_clean):
                todos.append(f"  - [ ] {line_clean[:35]}")
        
        todo_section = "⚡ **提取待办**：\n" + "\n".join(todos) if todos else "⚡ **提取待办**：无明确待办项"
        
        lines = [
            "📝 【已捕获并存入本地思源笔记】",
            f"📌 **内容**：{raw_text}",
            todo_section,
            "",
            "💡 **提示**：",
            "• 记录已实时写入本地思源笔记（闲笔 -> 今日日记）。",
            "• 随时发「总结今天」或「/分析」，大模型将汇总全天点滴做深度复盘。",
            "• 若含身体不适或预感信号，系统已自动分流至专属心智雷达。"
        ]
        return "\n".join(lines)

    tag_instruction = ""
    if user_tags:
        tags_display = " ".join([f"#{t}" for t in user_tags])
        tag_instruction = f"\n用户意图标签为：【{tags_display}】。请务必在【归纳】中包含此标签并作为首要归类。"

    prompt = f"""用户刚刚在飞书中发来一条随时记录的点滴。请对其进行精炼结构化提炼：

【刚记录的点滴】：
"{raw_text}"{tag_instruction}

请严格按照以下格式输出（极简、高密度、严禁多余emoji和无用废话）：
归纳：（1句话提炼本质，打上 1-3 个标签，包含用户指定标签及延伸标签）
待办：（如含明确TODO列出「- [ ] 事项内容」，无则写「无」）
洞见：（从认知视角、执行力或心智模式给出的精炼建议，60字内。注意：若为生理体感、日常作息或纯生活陈述，保持客观共情或简要提炼，严禁居高临下说教或强推打卡/冥想）
"""
    res = call_llm(prompt)
    if res:
        return f"{res}\n\n思源已同步"
    return f"思源已同步：\n{raw_text}"

# 2. 全天深度复盘（用户呼唤「分析今天」「复盘」时触发）
def perform_daily_review() -> str:
    memos = siyuan.query_today_memos()
    if not memos:
        return "ℹ️ 今天在思源笔记中还没有找到记录的点滴。先给我发几条你的随想或待办，再来呼唤分析吧！"

    memos_text = "\n".join([f"{i+1}. {m}" for i, m in enumerate(memos)])

    prompt = f"""
你是一名严谨、深刻的首席执行顾问。以下是用户今天在思源笔记记录的所有点滴碎片。请对用户今天的全天表现进行深度综合复盘。

【今日记录的所有点滴与随想（共 {len(memos)} 条）】：
{memos_text}

请按照以下结构生成一份高质量复盘报告：
🎯 **一、 今日全景与认知主题**（概括今天用户的核心精力投向与重点关注领域）
⚡ **二、 待办与行动推进清单**（汇总所有记录中产生、完成或遗漏的 Action Items，用清晰的 `- [ ]` 列出）
🔋 **三、 精力与执行力反思**（分析记录节奏与精力消耗点，指出心智模式或时间黑洞）
🚀 **四、 明日建议与关键推进**（提出 1-2 条最具杠杆效应的高价值建议）
"""
    llm_res = call_llm(prompt)
    if not llm_res:
        summary = [
            f"📊 【今日思源笔记记录汇总】（共 {len(memos)} 条）",
            "",
            memos_text,
            "",
            "ℹ️ 提示：配置大模型 API Key 后，呼唤「总结今天」将由大模型自动生成多维认知复盘与行动清单。"
        ]
        return "\n".join(summary)

    # 将复盘报告同步回写到思源笔记
    siyuan.append_daily_review(llm_res)

    # 同步增量向量化今日复盘
    if vector_engine and llm_res:
        try:
            today_date = datetime.now().strftime("%Y-%m-%d")
            vector_engine.add_record(
                record_id=f"sy_review_{today_date}",
                source="siyuan",
                ref_id=siyuan.today_doc_id or "today_doc",
                content=llm_res,
                title=f"{today_date} 今日综合复盘",
                tags="#今日复盘"
            )
        except Exception as e:
            logger.warning(f"今日复盘向量化异常: {e}")

    return f"🧠 【今日综合复盘报告】\n\n{llm_res}\n\n*(已自动归档至思源笔记今日文档)*"

# ================= 意图识别与消息处理 =================
REVIEW_KEYWORDS = ["总结今天", "分析今天", "复盘", "总结", "今日复盘", "/分析", "/总结", "/review"]

def handle_incoming_text(text: str, chat_id: str, open_id: str = "") -> str:
    global CURRENT_EXECUTION_TAG
    CURRENT_EXECUTION_TAG = "- 本地"
    text_clean = text.strip()
    now = datetime.now()

    # 0. 优先检查是否正在回复二次确认 (如用户刚发了清除指令，现在回复【确认清除】或【取消】)
    confirm_reply = action_gateway.handle_pending_confirmation(chat_id, text_clean)
    if confirm_reply:
        logger.info(f"🔄 处理用户确认反馈: {text_clean}")
        siyuan.append_memo(f"⚙️ 用户确认反馈：{text_clean}", summary_tag="#系统指令")
        return confirm_reply

    # 0.2 检查是否是闹钟/待办的反馈闭环指令 (如用户回复「确认」、「完成」、「延期 10分钟」、「取消」)
    feedback_reply = handle_reminder_feedback_intent(text_clean, chat_id, open_id)
    if feedback_reply:
        logger.info(f"⏰ 闹钟操作闭环: {text_clean}")
        return feedback_reply

    # 0.3 检查是否是作息/睡眠等生理节律打卡 (如「醒」、「起夜」、「睡了」、「午休」)
    sleep_reply = detect_sleep_rhythm_log(text_clean, now)
    if sleep_reply:
        logger.info(f"🛌 生理节律打卡: {text_clean}")
        return sleep_reply

    # 0.5 检查是否是系统级管理操作指令 (如 "帮我清除掉今天8点之后的所有点滴设置和日程设置，包括对应的文件")
    action_intent = action_gateway.detect_action_intent(text_clean, now, llm_caller=call_llm)
    if action_intent:
        logger.info(f"⚡ 触发系统管理指令预演: {action_intent}")
        siyuan.append_memo(f"⚠️ 系统管理指令请求：{text_clean}", summary_tag="#系统指令")
        return action_gateway.prepare_cleanup(chat_id, action_intent)

    # 0.8 检查是否是查看模型指令
    if text_clean.lower() in ["/model", "/models", "查看模型", "模型列表", "当前模型", "大模型"]:
        current_llm = CONFIG.get("llm", {})
        current_vlm = CONFIG.get("vlm", {})
        providers = CONFIG.get("model_providers", {})
        lines = [
            "大模型状态",
            f"主模型：{get_current_llm_brand()} (`{current_llm.get('model', '未配置')}`)",
            f"端点：`{current_llm.get('base_url', '')}`",
            f"视觉OCR：智谱 (`{current_vlm.get('model', 'glm-4v-flash')}`) (免费)",
            "",
            "可用切换通道：",
        ]
        alias_map = {
            "siliconflow-deepseek": "切换模型 硅基ds",
            "siliconflow-qwen": "切换模型 硅基qwen",
            "deepseek-official": "切换模型 官方ds",
            "zhipu-free": "切换模型 智谱"
        }
        for k, p in providers.items():
            cmd_hint = alias_map.get(k, f"切换模型 {k}")
            lines.append(f"• {p.get('name', k)} (`{p.get('model', '')}`) ➔ `{cmd_hint}`")

        lines.append("\n发送对应指令即可即时切换。")
        return "\n".join(lines)

    # 0.9 检查是否是切换模型指令
    m_switch = re.match(r"^切换模型\s*(.+)$", text_clean, re.IGNORECASE)
    if m_switch:
        target = m_switch.group(1).strip().lower()
        providers = CONFIG.get("model_providers", {})
        matched_key = None

        if target in ["硅基ds", "硅基deepseek", "deepseek", "ds", "deepseek-v3", "v3"]:
            matched_key = "siliconflow-deepseek"
        elif target in ["硅基qwen", "qwen", "通义千问", "qwen-72b", "千问"]:
            matched_key = "siliconflow-qwen"
        elif target in ["官方ds", "官方deepseek", "deepseek官方"]:
            matched_key = "deepseek-official"
        elif target in ["智谱", "glm", "glm4", "免费"]:
            matched_key = "zhipu-free"
        elif target in providers:
            matched_key = target

        if matched_key and matched_key in providers:
            # 内存中更新活跃配置
            CONFIG["llm"] = {
                "provider": chosen.get("provider", "custom"),
                "api_key": chosen.get("api_key", ""),
                "base_url": chosen.get("base_url", ""),
                "model": chosen.get("model", ""),
                "notes": f"由指令动态切换至 {chosen.get('name', matched_key)}"
            }
            # 磁盘持久化时执行严格脱敏：清除明文 Key，依靠 Keychain / 环境变量动态提供
            import copy
            sanitized_cfg = copy.deepcopy(CONFIG)
            if "feishu" in sanitized_cfg: sanitized_cfg["feishu"]["app_secret"] = ""
            if "siyuan" in sanitized_cfg: sanitized_cfg["siyuan"]["token"] = ""
            if "llm" in sanitized_cfg: sanitized_cfg["llm"]["api_key"] = ""
            if "vlm" in sanitized_cfg: sanitized_cfg["vlm"]["api_key"] = ""
            if "model_providers" in sanitized_cfg:
                for p in sanitized_cfg["model_providers"].values():
                    p["api_key"] = ""

            for cfg_path in [
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
                os.path.expanduser("~/.feishu_copilot_app/config.json")
            ]:
                try:
                    if os.path.exists(cfg_path):
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            json.dump(sanitized_cfg, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.error(f"持久化保存配置到 {cfg_path} 失败: {e}")

            siyuan.append_memo(f"⚙️ 切换大模型：{chosen.get('name', matched_key)} ({chosen.get('model')})", summary_tag="#系统指令")
            return (
                f"✅ **大模型通道切换成功！**\n\n"
                f"📌 当前生效引擎：**{chosen.get('name', matched_key)}**\n"
                f"🏷️ 模型代号：`{chosen.get('model')}`\n"
                f"🔗 端点地址：`{chosen.get('base_url')}`\n\n"
                f"即刻生效，无需重启服务。"
            )
        else:
            avail = ["硅基ds", "硅基qwen", "官方ds", "智谱"]
            return f"⚠️ 未识别的目标模型通道「{target}」。\n支持的快捷名称：{'、'.join(avail)}。\n可发「查看模型」查看详情。"

    # 1. 检查是否是查看/管理提醒指令
    if text_clean in ["我的提醒", "查看提醒", "提醒列表", "闹钟列表", "/reminders", "/remind"]:
        siyuan.append_memo(f"🔍 查询待触发提醒列表", summary_tag="#查询")
        active = reminder_scheduler.get_active_reminders()
        if not active:
            return "⏰ 当前没有任何待触发的提醒闹钟。\n💡 你可以随手发：「半小时后提醒我拿外卖」或「下午3点提醒我开会」来定闹钟。"
        now = datetime.now()
        lines = [f"⏰ 【待触发备忘与闹钟】（共 {len(active)} 条）："]
        for idx, item in enumerate(active):
            target_dt = datetime.strptime(item["remind_at"], "%Y-%m-%d %H:%M:%S")
            diff = target_dt - now
            mins = int(diff.total_seconds() // 60)
            if mins < 60:
                left_str = f"约 {max(mins, 1)} 分钟后"
            else:
                left_str = f"约 {mins//60} 小时 {mins%60} 分后"
            lines.append(f"{idx+1}. [{item['remind_at'][11:16]}] {item['task']} ({left_str})")
        lines.append("\n💡 发送「取消提醒 序号」可删除指定闹钟（如：取消提醒 1）")
        return "\n".join(lines)

    # 2. 检查是否是取消提醒指令
    m_cancel = re.match(r"(?:取消提醒|删除提醒|取消闹钟)\s*(\d+)", text_clean)
    if m_cancel:
        idx = int(m_cancel.group(1)) - 1
        if reminder_scheduler.cancel_reminder(idx):
            siyuan.append_memo(f"🗑️ 取消提醒：第 {idx+1} 条闹钟", summary_tag="#注销闹钟")
            return f"✅ 已成功取消第 {idx+1} 条提醒事项。"
        else:
            return f"⚠️ 未找到序号为 {idx+1} 的待触发提醒，请发「我的提醒」核对列表。"

    # 3. 检查是否是设定提醒闹钟
    parsed = reminder_scheduler.parse_intent(text_clean)
    if parsed and parsed[0] and parsed[1]:
        remind_dt, remind_task = parsed[0], parsed[1]
        is_correction = parsed[2] if len(parsed) > 2 else False

        item = reminder_scheduler.add_reminder(chat_id, remind_dt, remind_task, is_correction=is_correction, raw_text=text_clean)
        now = datetime.now()
        diff = remind_dt - now
        total_mins = int(diff.total_seconds() // 60)
        if total_mins < 60:
            remaining_str = f"{max(total_mins, 1)} 分钟"
        else:
            remaining_str = f"{total_mins//60} 小时 {total_mins%60} 分钟"

        time_display = remind_dt.strftime("%H:%M") if remind_dt.date() == now.date() else remind_dt.strftime("%Y-%m-%d %H:%M")

        # 若是纠错模式，联动从飞书待办中心物理删除上一条旧任务
        if is_correction:
            cancel_last_created_task(client)

        # 同步在飞书创建官方原生待办任务
        task_info = create_feishu_task(remind_task, due_dt=remind_dt, open_id=open_id, description=f"⏰ 智能提醒: 设定于 {time_display} 触发")
        if task_info.get("guid"):
            reminder_scheduler.update_reminder_feishu_guid(item["id"], task_info["guid"])

        # 同步创建飞书官方日历日程 (带准时与提前5分钟闹钟提醒，秒级同步至手机系统日历)
        cal_info = create_feishu_calendar_event(remind_task, start_dt=remind_dt, open_id=open_id, description=f"⏰ 由飞书 24h AI 秘书创建\n• 事项：{remind_task}\n• 时间：{time_display}")
        if cal_info.get("event_id"):
            reminder_scheduler.update_reminder_calendar_event(item["id"], cal_info["event_id"])

        task_link_part = ""
        if task_info.get("url"):
            task_link_part = f"\n📋 飞书待办：[点击进入待办详情]({task_info['url']})"
        cal_part = "\n📅 手机日历：已同步至飞书与手机系统日历 (含闹钟提醒)"

        title_header = "提醒已更新" if is_correction else "提醒已设定"
        correction_tip = "已废弃上一条旧日程。\n" if is_correction else ""

        return (
            f"{title_header}\n"
            f"事项：{remind_task}\n"
            f"时间：{time_display} (约 {remaining_str} 后)"
            f"{task_link_part}"
            f"{cal_part}\n"
            f"{correction_tip}"
            "思源、待办与手机日历已同步"
        )

    # 3.5 检查是否是显式待办事项指令 (以 - [ ], TODO, 待办 开头)
    m_todo = re.match(r"^(?:-\s*\[\s*\]|TODO[:：]?|todo[:：]?|待办[:：]?)\s*(.+)$", text_clean, re.IGNORECASE)
    if m_todo:
        todo_content = m_todo.group(1).strip()
        task_info = create_feishu_task(todo_content, open_id=open_id, description="由飞书点滴消息自动转为待办")
        siyuan.append_memo(f"- [ ] {todo_content}", summary_tag="#待办")

        task_url = task_info.get("url", "")
        url_part = f"\n飞书待办：[直达]({task_url})" if task_url else ""
        return (
            f"待办已建立\n"
            f"事项：{todo_content}"
            f"{url_part}\n"
            f"思源已同步"
        )

    # 4. 检查是否是呼唤大模型全面分析（按需使用 API，杜绝日常浪费）
    for kw in REVIEW_KEYWORDS:
        if kw in text_clean:
            logger.info(f"🔍 触发全天综合复盘分析 (关键词: {kw})")
            siyuan.append_memo(f"📊 呼唤今日综合复盘：{text_clean}", summary_tag="#复盘请求")
            return perform_daily_review()

    # 5. 检查是否触发躯体直觉预警雷达 (Qwen 智能研判 + 生理诱因精准排除)
    somatic_res = detect_somatic_radar(text_clean, llm_caller=call_llm)
    if somatic_res:
        logger.info(f"🧠 触发躯体直觉雷达捕获: {somatic_res['organ']}")
        # 写入思源笔记（打上 #躯体预警 标签）
        siyuan.append_memo(text_clean, summary_tag="#躯体预警")
        # 自动同步至本地预警底册
        assigned_id = sync_to_somatic_ledger(text_clean, somatic_res["organ"], somatic_res["decoded"], somatic_res["action"])
        id_display = f"（已自动入册 {assigned_id}，待验证）" if assigned_id else "（已存入思源）"
        
        now_hm = datetime.now().strftime("%H:%M")
        card_lines = [
            "🧠 【飞书即时回执 · 躯体直觉雷达】",
            f"• 捕获时间：{now_hm}",
            f"• 躯体标记：{somatic_res['organ']}",
            f"• 潜意识验尸译码：{somatic_res['decoded']}",
            f"• 🛡️ 最小防御动作：{somatic_res['action']}",
            f"• 状态：{id_display}，给神经开具收据，安心专注当下"
        ]
        return "\n".join(card_lines)

    # 6. 普通单条记录：自然语言标签智能解析与分流
    clean_content, extracted_tags = parse_natural_tags(text_clean)

    tag_str = " ".join([f"#{t}" for t in extracted_tags]) if extracted_tags else "#闲笔"
    primary_tag = extracted_tags[0] if extracted_tags else "随想"

    # 6.1 特殊动作分流：如果用户明确以 "存档"、"保存"、"归档" 标记，纯粹资料留存，无需大模型说教
    if any(t in ['存档', '保存', '归档'] for t in extracted_tags):
        siyuan.append_memo(f"{tag_str} {clean_content}", summary_tag=f"#{primary_tag}")
        if vector_engine:
            try:
                vector_engine.add_record(
                    record_id=f"sy_memo_{int(time.time()*1000)}",
                    source="siyuan",
                    ref_id=siyuan.today_doc_id or "today_doc",
                    content=clean_content,
                    title=f"资料存档 ({primary_tag})",
                    tags=tag_str
                )
            except Exception as e:
                logger.warning(f"存档向量化异常: {e}")
        return (
            f"已归档 ({tag_str})\n"
            f"内容：{clean_content}\n\n"
            f"思源已同步 · 本地"
        )

    # 6.2 普通记录 / 思考 / 实体项目：写入思源 + 向量化 + 跨时空灵感回响 + DeepSeek 结构化洞见
    siyuan_text = f"{tag_str} {clean_content}" if extracted_tags else text_clean
    siyuan.append_memo(siyuan_text, summary_tag=f"#{primary_tag}")

    reflection_hint = ""
    if vector_engine:
        try:
            # 1. 查找历史跨时空灵感回响 (相似度 >= 0.62)
            reflection_hint = vector_engine.format_reflection_hint(clean_content, threshold=0.62)
            # 2. 边记边向量化存入本地 knowledge.db
            vector_engine.add_record(
                record_id=f"sy_memo_{int(time.time()*1000)}",
                source="siyuan",
                ref_id=siyuan.today_doc_id or "today_doc",
                content=clean_content,
                title=f"每日随想 ({primary_tag})",
                tags=tag_str
            )
        except Exception as e:
            logger.warning(f"随想向量化处理异常: {e}")

    memo_res = analyze_single_memo(clean_content, user_tags=extracted_tags)
    if reflection_hint:
        return f"{reflection_hint}\n\n{memo_res}"
    return memo_res

# ================= 飞书长连接事件回调 =================
def create_event_handler(client: lark.Client):
    def on_message_received(data: P2ImMessageReceiveV1) -> None:
        try:
            msg = data.event.message
            msg_id = msg.message_id
            
            if dedup.is_duplicate(msg_id):
                return

            msg_type = msg.message_type
            chat_id = msg.chat_id
            logger.info(f"收到飞书消息 [ID={msg_id}, Type={msg_type}]")

            user_text = ""
            if msg_type == "text":
                content_json = json.loads(msg.content)
                user_text = content_json.get("text", "").strip()
            elif msg_type == "image":
                content_json = json.loads(msg.content)
                image_key = content_json.get("image_key", "")
                logger.info(f"📷 收到图片消息，image_key: {image_key}")

                # 从飞书下载原图
                res_req = GetMessageResourceRequest.builder() \
                    .message_id(msg_id) \
                    .file_key(image_key) \
                    .type("image") \
                    .build()
                res_resp = client.im.v1.message_resource.get(res_req)

                if res_resp.success():
                    img_bytes = res_resp.file.read()
                    filename = f"feishu_{image_key[:16]}.png"
                    asset_path = siyuan.upload_asset(filename, img_bytes)
                    if asset_path:
                        logger.info(f"✅ 图片已成功存入思源笔记: {asset_path}")
                        # 尝试调用视觉大模型 (GLM-4V-Flash) 进行识图与知识/读书提炼
                        vision_summary = analyze_image_with_vlm(img_bytes)
                        if vision_summary:
                            siyuan_content = (
                                f"📖 【读书与图文提炼】\n\n"
                                f"![图片]({asset_path})\n\n"
                                f"{vision_summary}"
                            )
                            siyuan.append_memo(siyuan_content, summary_tag="#读书笔记")

                            # 增量向量化存入本地知识库
                            if vector_engine:
                                try:
                                    vector_engine.add_record(
                                        record_id=f"sy_book_{int(time.time()*1000)}",
                                        source="siyuan",
                                        ref_id=asset_path,
                                        content=vision_summary,
                                        title="读书与图文提炼",
                                        tags="#读书笔记"
                                    )
                                except Exception as e:
                                    logger.warning(f"读书笔记向量化写入异常: {e}")

                            reply_text = (
                                f"读书笔记提炼\n\n"
                                f"{vision_summary}\n\n"
                                f"原图已存思源：`{asset_path}`\n\n"
                                f"思源已同步 · 智谱"
                            )
                        else:
                            siyuan.append_memo(f"收到图片：\n\n![图片]({asset_path})", summary_tag="#图片")
                            reply_text = (
                                f"图片已存入思源：`{asset_path}`\n\n"
                                f"思源已同步 · 本地"
                            )
                    else:
                        reply_text = "⚠️ 图片已从飞书拉取，但上传至思源笔记失败，请确认思源笔记客户端是否正常运行。\n\n- 本地"
                else:
                    logger.error(f"飞书下载图片失败: {res_resp.code} - {res_resp.msg}")
                    if res_resp.code == 99991672 or "scope" in str(res_resp.msg).lower() or "resource" in str(res_resp.msg).lower():
                        reply_text = "⚠️ 收到图片，但机器人缺少「获取消息资源」权限。\n请在飞书开放平台「权限管理」开通 `im:resource` 权限并发布新版本。\n\n- 本地"
                    else:
                        reply_text = f"⚠️ 从飞书下载图片失败 ({res_resp.code}: {res_resp.msg})\n\n- 本地"

                # 回复图片处理结果
                reply_req = ReplyMessageRequest.builder() \
                    .message_id(msg_id) \
                    .request_body(ReplyMessageRequestBody.builder()
                                  .msg_type("text")
                                  .content(json.dumps({"text": reply_text}))
                                  .build()) \
                    .build()
                client.im.v1.message.reply(reply_req)
                return

            elif msg_type == "file":
                content_json = json.loads(msg.content)
                file_key = content_json.get("file_key", "")
                file_name = content_json.get("file_name", "未命名文件")
                logger.info(f"📎 收到文件消息，file_name: {file_name}, file_key: {file_key}")

                # 从飞书下载原文件
                res_req = GetMessageResourceRequest.builder() \
                    .message_id(msg_id) \
                    .file_key(file_key) \
                    .type("file") \
                    .build()
                res_resp = client.im.v1.message_resource.get(res_req)

                if res_resp.success():
                    file_bytes = res_resp.file.read()
                    file_size = len(file_bytes)
                    if file_size < 1024:
                        size_str = f"{file_size} B"
                    elif file_size < 1024 * 1024:
                        size_str = f"{file_size / 1024:.1f} KB"
                    else:
                        size_str = f"{file_size / (1024 * 1024):.2f} MB"

                    asset_path = siyuan.upload_asset(file_name, file_bytes)
                    if asset_path:
                        logger.info(f"✅ 文件已成功存入思源笔记: {asset_path} ({size_str})")
                        
                        # 检查是否为文本类文件，尝试提取预览
                        preview_text = ""
                        ext = os.path.splitext(file_name)[1].lower()
                        if ext in [".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml", ".sql", ".sh", ".html"] and file_size <= 200 * 1024:
                            try:
                                text_sample = file_bytes.decode("utf-8", errors="ignore").strip()
                                if text_sample:
                                    snippet = text_sample[:300].replace("\r", "").strip()
                                    preview_text = f"\n\n📄 **文本摘要/预览**：\n```text\n{snippet[:200]}\n```"
                            except Exception:
                                pass

                        siyuan_memo = f"📎 收到附件文件：[{file_name}]({asset_path}) `({size_str})`"
                        if preview_text:
                            siyuan_memo += preview_text
                        siyuan.append_memo(siyuan_memo, summary_tag="#文件附件")

                        reply_text = f"文件已归档：{file_name} ({size_str})\n路径：`{asset_path}`"
                        if preview_text:
                            reply_text += preview_text
                    else:
                        reply_text = f"⚠️ 文件 `{file_name}` 已从飞书拉取，但上传至思源笔记失败，请确认思源笔记客户端是否运行正常。"
                else:
                    logger.error(f"飞书下载文件失败: {res_resp.code} - {res_resp.msg}")
                    if res_resp.code == 99991672 or "scope" in str(res_resp.msg).lower() or "resource" in str(res_resp.msg).lower():
                        reply_text = f"⚠️ 收到文件 `{file_name}`，但机器人缺少「获取消息资源」权限。\n请在飞书开放平台「权限管理」开通 `im:resource` 权限并发布新版本。"
                    else:
                        reply_text = f"⚠️ 从飞书下载文件 `{file_name}` 失败 ({res_resp.code}: {res_resp.msg})"

                # 回复文件处理结果
                reply_text = f"{reply_text.strip()}\n\n思源已同步 · 本地"
                reply_req = ReplyMessageRequest.builder() \
                    .message_id(msg_id) \
                    .request_body(ReplyMessageRequestBody.builder()
                                  .msg_type("text")
                                  .content(json.dumps({"text": reply_text}))
                                  .build()) \
                    .build()
                client.im.v1.message.reply(reply_req)
                return

            elif msg_type == "audio":
                content_json = json.loads(msg.content)
                file_key = content_json.get("file_key", "")
                duration_ms = content_json.get("duration", 0)
                duration_sec = round(duration_ms / 1000.0, 1)
                logger.info(f"🎙️ 收到语音消息，时长: {duration_sec}s, file_key: {file_key}")

                # 下载语音文件
                res_req = GetMessageResourceRequest.builder() \
                    .message_id(msg_id) \
                    .file_key(file_key) \
                    .type("file") \
                    .build()
                res_resp = client.im.v1.message_resource.get(res_req)

                if res_resp.success():
                    audio_bytes = res_resp.file.read()
                    filename = f"feishu_voice_{datetime.now().strftime('%H%M%S')}.opus"
                    asset_path = siyuan.upload_asset(filename, audio_bytes)
                    if asset_path:
                        siyuan.append_memo(f"🎙️ 收到语音条：[{filename}]({asset_path}) `({duration_sec}秒)`", summary_tag="#语音")
                        reply_text = f"语音已归档 ({duration_sec}秒)\n路径：`{asset_path}`\n\n思源已同步 · 本地"
                    else:
                        reply_text = "⚠️ 语音拉取成功，但写入思源 assets 失败。\n\n思源已同步 · 本地"
                else:
                    reply_text = "⚠️ 收到语音，但拉取音频流失败（需开通 `im:resource` 权限）。\n\n思源已同步 · 本地"

                reply_req = ReplyMessageRequest.builder() \
                    .message_id(msg_id) \
                    .request_body(ReplyMessageRequestBody.builder()
                                  .msg_type("text")
                                  .content(json.dumps({"text": reply_text}))
                                  .build()) \
                    .build()
                client.im.v1.message.reply(reply_req)
                return

            elif msg_type == "media":
                content_json = json.loads(msg.content)
                file_key = content_json.get("file_key", "")
                file_name = content_json.get("file_name", f"feishu_video_{datetime.now().strftime('%H%M%S')}.mp4")
                logger.info(f"🎬 收到视频消息，file_name: {file_name}, file_key: {file_key}")

                res_req = GetMessageResourceRequest.builder() \
                    .message_id(msg_id) \
                    .file_key(file_key) \
                    .type("file") \
                    .build()
                res_resp = client.im.v1.message_resource.get(res_req)

                if res_resp.success():
                    video_bytes = res_resp.file.read()
                    file_size = len(video_bytes)
                    size_str = f"{file_size / (1024 * 1024):.2f} MB" if file_size >= 1024 * 1024 else f"{file_size / 1024:.1f} KB"
                    asset_path = siyuan.upload_asset(file_name, video_bytes)
                    if asset_path:
                        siyuan.append_memo(f"🎬 收到视频附件：[{file_name}]({asset_path}) `({size_str})`", summary_tag="#视频")
                        reply_text = f"视频已归档：{file_name} ({size_str})\n路径：`{asset_path}`\n\n思源已同步 · 本地"
                    else:
                        reply_text = f"⚠️ 视频 `{file_name}` 下载成功，但存入思源 assets 失败。\n\n思源已同步 · 本地"
                else:
                    reply_text = f"⚠️ 下载视频失败: {res_resp.code} - {res_resp.msg}\n\n思源已同步 · 本地"
                reply_req = ReplyMessageRequest.builder() \
                    .message_id(msg_id) \
                    .request_body(ReplyMessageRequestBody.builder()
                                  .msg_type("text")
                                  .content(json.dumps({"text": reply_text}))
                                  .build()) \
                    .build()
                client.im.v1.message.reply(reply_req)
                return

            elif msg_type == "post":
                content_json = json.loads(msg.content)
                user_text = str(content_json)
            else:
                user_text = f"[{msg_type} 格式消息]"

            if not user_text:
                return

            logger.info(f"📝 捕获内容: {user_text}")

            # 提取发送者 open_id 用于指派待办等个性化联动
            open_id = ""
            if hasattr(data.event, "sender") and data.event.sender:
                sender_id_obj = getattr(data.event.sender, "sender_id", None)
                if sender_id_obj:
                    open_id = getattr(sender_id_obj, "open_id", "") or ""

            # 核心业务路由 (带入 chat_id 与 open_id)
            reply_text = handle_incoming_text(user_text, chat_id, open_id)

            if not reply_text:
                return

            reply_text = reply_text.strip()
            known_tags = ["- 智谱", "- Qwen", "- DeepSeek", "- 硅基", "- Llama", "- 本地", "- 出错", "(保底)", "思源已同步"]
            if reply_text.endswith("思源已同步"):
                model_tag = CURRENT_EXECUTION_TAG.lstrip("- ").strip()
                reply_text = f"{reply_text} · {model_tag}"
            elif not any(reply_text.endswith(tag) or tag in reply_text[-30:] for tag in known_tags):
                model_tag = CURRENT_EXECUTION_TAG.lstrip("- ").strip()
                reply_text = f"{reply_text}\n\n思源已同步 · {model_tag}"

            # 回复飞书消息
            reply_req = ReplyMessageRequest.builder() \
                .message_id(msg_id) \
                .request_body(ReplyMessageRequestBody.builder()
                              .msg_type("text")
                              .content(json.dumps({"text": reply_text}))
                              .build()) \
                .build()

            resp = client.im.v1.message.reply(reply_req)
            if resp.success():
                logger.info(f"✅ 成功回复消息: {msg_id}")
            else:
                logger.error(f"回复消息失败: {resp.code} - {resp.msg}")

        except Exception as e:
            logger.error(f"处理飞书消息未捕获异常: {e}", exc_info=True)

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message_received) \
        .build()
    return handler

# ================= 启动入口 =================
def main():
    logger.info("=" * 60)
    logger.info("🚀 启动飞书点滴监控与智能顾问 (已集成思源笔记与飞书待办)")
    logger.info(f"飞书 App ID: {CONFIG['feishu']['app_id']}")
    logger.info(f"思源笔记状态: {'已就绪 (端口 6806, 闲笔笔记本)' if siyuan.is_active() else '未启用'}")
    brand = get_current_llm_brand()
    logger.info(f"大模型状态: {brand} ({CONFIG['llm'].get('model', '未配置')})")
    if vector_engine:
        logger.info(f"本地向量底座: 已挂载 (ADATA / 知识记录: {vector_engine.count_records()} 条)")
    else:
        logger.info("本地向量底座: 未挂载")
    logger.info("=" * 60)

    client = lark.Client.builder() \
        .app_id(CONFIG["feishu"]["app_id"]) \
        .app_secret(CONFIG["feishu"]["app_secret"]) \
        .build()

    handler = create_event_handler(client)

    ws_client = lark.ws.Client(
        CONFIG["feishu"]["app_id"],
        CONFIG["feishu"]["app_secret"],
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )

    try:
        ws_client.start()
    except KeyboardInterrupt:
        logger.info("👋 服务已手动停止")
    except Exception as e:
        logger.error(f"WebSocket 长连接异常退出: {e}", exc_info=True)

if __name__ == "__main__":
    main()
