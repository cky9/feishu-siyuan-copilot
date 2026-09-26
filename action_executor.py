#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 动作执行网关 (Action Tools Gateway - 增强版)
功能:
1. 细粒度意图识别: 精准区分【仅清理飞书日程/待办】与【清理思源笔记点滴】或【全量清理】
2. 飞书官方待办中心真实联动: 调用飞书 Task v2 API 物理删除任务
3. 预演扫描 (Dry-Run): 精准盘点受影响的笔记块、飞书待办、本地文件
4. 安全二次确认状态机: 需用户二次回复【确认清除】才真正执行物理删除
"""

import os
import re
import json
import time
import logging
from datetime import datetime
import requests
from typing import Optional, List, Dict, Any

logger = logging.getLogger("FeishuCopilot.ActionGateway")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEISHU_TASKS_FILE = os.path.join(BASE_DIR, "feishu_tasks.json")

def record_created_task(guid: str, summary: str, tasks_file: str = None):
    """记录已创建的飞书任务，便于后续精准清理"""
    if not guid:
        return
    fpath = tasks_file or FEISHU_TASKS_FILE
    try:
        tasks = []
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                tasks = json.load(f)
        tasks.append({
            "guid": guid,
            "summary": summary,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 已登记飞书任务账本: {guid} ({summary})")
    except Exception as e:
        logger.error(f"记录飞书任务失败: {e}")

def cancel_last_created_task(lark_client=None, tasks_file: str = None) -> dict:
    """用于在日程纠错时废弃上一条误建的飞书待办"""
    fpath = tasks_file or FEISHU_TASKS_FILE
    if not os.path.exists(fpath):
        return None
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            tasks = json.load(f)
        if not tasks:
            return None
        last_task = tasks.pop()
        guid = last_task.get("guid")
        if guid and lark_client:
            from lark_oapi.api.task.v2 import DeleteTaskRequest
            req = DeleteTaskRequest.builder().task_guid(guid).build()
            resp = lark_client.task.v2.task.delete(req)
            if resp.success():
                logger.info(f"🔄 纠错联动：已自动从飞书物理删除上一条旧待办: {guid} ({last_task.get('summary')})")
            else:
                logger.warning(f"从飞书删除旧待办返回: {resp.code} {resp.msg}")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        return last_task
    except Exception as e:
        logger.error(f"废弃上一条飞书待办异常: {e}")
        return None

class ActionGateway:
    def __init__(self, siyuan_client, reminders_file: str, lark_client=None, assets_dir: Optional[str] = None, feishu_tasks_file: str = None):
        self.siyuan = siyuan_client
        self.reminders_file = reminders_file
        self.lark_client = lark_client
        self.assets_dir = assets_dir or os.getenv("SIYUAN_ASSETS_DIR")
        self.feishu_tasks_file = feishu_tasks_file or FEISHU_TASKS_FILE
        self.pending_confirmations = {}  # {chat_id: pending_action_dict}

    def detect_action_intent(self, text: str, now: datetime, llm_caller=None) -> dict:
        """检测输入是否为系统级执行操作 (如清理日程、清理点滴等)"""
        text_clean = text.strip()

        # 快速前置规则过滤：必须包含核心动作词
        action_verbs = ["清除", "清空", "删除", "撤销", "清理", "作废", "取消全部"]
        if not any(v in text_clean for v in action_verbs):
            return None

        # 核心过滤 A: 目标对象必须属于机器人系统管辖的业务数据！
        # 如果用户是在说外部事物（Mac、后台、内存、APP、电脑、手机、桌面、房间、抽屉、衣服、缓存、邮件、微信等），绝非机器人管理操作！
        external_targets = ["mac", "app", "后台", "内存", "电脑", "手机", "桌面", "缓存", "垃圾", "微信", "邮件", "房间", "抽屉", "衣服", "标签页", "浏览器"]
        bot_targets = ["日程", "待办", "闹钟", "提醒", "点滴", "笔记", "思源", "随笔", "附件", "文件", "所有东西", "全部数据", "全部清除", "全清", "清空所有", "飞书"]
        has_bot_target = any(t in text_clean.lower() for t in bot_targets)
        has_external_target = any(t in text_clean.lower() for t in external_targets)
        if has_external_target and not has_bot_target:
            logger.info(f"💡 检测到清理外部对象/个人日记，非系统指令: {text_clean}")
            return None

        # 核心过滤 B: 日记/状态陈述与感想（非祈使命令语气）
        diary_feelings = ["感觉", "觉得", "以前", "快了一点", "清爽", "体验", "心得", "舒服", "占用", "省出", "变好", "好像"]
        command_imperatives = ["帮我", "请", "立即", "马上", "替我", "麻烦", "把", "给我"]
        if any(f in text_clean for f in diary_feelings) and not any(cmd in text_clean for cmd in command_imperatives):
            logger.info(f"💡 检测到用户感想/日志叙述，非系统指令: {text_clean}")
            return None

        # 1. 优先调用大模型进行意图理解与时间提取
        data = None
        if llm_caller:
            now_str = now.strftime("%Y-%m-%d %H:%M:%S (%A)")
            prompt = f"""当前系统时间是：{now_str}。
请分析用户输入是否包含「指示助手执行系统级清理/删除/管理操作」的指令意图：
用户输入："{text_clean}"

极重要判断规则：
1. 【个人日记/经验心得】（必须输出 is_action: false）：
   - 用户在叙述自己做过的事、设备维护或生活感想（例如："今天清理了一些mac上面的后台和APP，感觉速度快了一点"、"刚才删除了手机里的废照片，省出5G空间"、"清理了桌面感觉清爽"）。
   - 凡是叙述个人行动、心得体验、或者清理对象非机器人数据（如Mac、APP、内存、电脑、房间）的，一律判定为 is_action: false！
2. 【机器人系统管理指令】（输出 is_action: true）：
   - 用户明确指示机器人清理本系统的数据（飞书日程、飞书待办、闹钟提醒、思源笔记点滴、附件文件）。
   - 必须具有明确的祈使/命令语气（如："帮我清理今天在飞书创建的所有日程"、"清空闹钟"、"删除今天8点后的点滴和日程"）。
3. 严格分析用户的【清理目标范围】：
   - 若用户提到"点滴"、"笔记"、"随笔"、"思源"：clean_memos 必须为 true。
   - 若用户未提点滴/笔记，只说"日程"、"待办"、"闹钟"、"飞书"：clean_memos 必须为 false。
   - 若用户提到"日程"、"待办"、"闹钟"、"飞书"：clean_reminders 与 clean_feishu_tasks 必须为 true。
   - 若用户提到"文件"、"附件"、"资源"：clean_assets 必须为 true。
   - 若用户提到"所有东西"、"全部清除"、"全清"：所有清理项全部为 true。
4. 推算起始时间 start_time：
   - "今天创建的所有日程" -> 今天的 00:00:00
   - "今天8点之后" -> 今天的 08:00:00
   - "刚才"、"今天" -> 今天的 00:00:00

请以纯 JSON 格式输出：
{{"is_action": true, "action": "cleanup_records", "target_desc": "清理飞书日程与待办", "start_time": "YYYY-MM-DD HH:MM:SS", "clean_memos": false, "clean_reminders": true, "clean_feishu_tasks": true, "clean_assets": false}}
若不是操作指令，输出：
{{"is_action": false}}
只输出纯 JSON，不要有任何多余文字或 markdown。"""
            try:
                res = llm_caller(prompt, system_prompt="你是一个极度严谨的系统管理指令意图路由器。只输出 JSON。")
                if res:
                    json_str = re.sub(r"```(?:json)?", "", res).strip()
                    parsed = json.loads(json_str)
                    if parsed.get("is_action") and parsed.get("action") == "cleanup_records":
                        data = parsed
            except Exception as e:
                logger.warning(f"大模型解析 Action 意图异常: {e}")

        # 2. 如果大模型未返回，使用本地规则兜底
        if not data:
            clean_memos = any(k in text_clean for k in ["点滴", "笔记", "所有东西", "所有资源", "全部", "随笔", "思源"])
            clean_reminders = any(k in text_clean for k in ["日程", "闹钟", "设置", "所有东西", "全部", "待办", "飞书"])
            clean_assets = any(k in text_clean for k in ["文件", "附件", "资源", "所有东西", "全部", "图片", "录音"])
            clean_feishu = clean_reminders

            start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
            m_hour = re.search(r"([一二两三四五六七八九十\d]{1,2})\s*[点:：]\s*(半|[一二两三四五六七八九十\d]{1,2})?", text_clean)
            if m_hour:
                h_str, m_str = m_hour.groups()
                CN = {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}
                h = int(h_str) if h_str.isdigit() else CN.get(h_str, 0)
                if any(p in text_clean for p in ["下午", "晚上"]) and h < 12:
                    h += 12
                m = 30 if m_str == "半" else (int(m_str) if m_str and m_str.isdigit() else 0)
                start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)

            data = {
                "is_action": True,
                "action": "cleanup_records",
                "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "clean_memos": clean_memos,
                "clean_reminders": clean_reminders,
                "clean_feishu_tasks": clean_feishu,
                "clean_assets": clean_assets
            }

        # 3. 强规则纠偏与安全隔离 (确定性防误删，禁止大模型幻觉)
        if any(k in text_clean for k in ["点滴", "笔记", "思源", "随笔"]):
            data["clean_memos"] = True
        if any(k in text_clean for k in ["文件", "附件", "资源", "图片", "录音", "语音"]):
            data["clean_assets"] = True
        if any(k in text_clean for k in ["日程", "闹钟", "待办", "提醒", "飞书"]):
            data["clean_reminders"] = True
            data["clean_feishu_tasks"] = True
        if any(k in text_clean for k in ["所有东西", "全部清除", "全清", "清空所有"]):
            data["clean_memos"] = True
            data["clean_reminders"] = True
            data["clean_feishu_tasks"] = True
            data["clean_assets"] = True

        # 核心保护：若用户只提到了日程/飞书/闹钟，严禁触碰笔记点滴与附件文件！
        if ("日程" in text_clean or "飞书" in text_clean or "闹钟" in text_clean) and not any(k in text_clean for k in ["点滴", "笔记", "思源", "文件", "资源", "所有东西", "全部"]):
            data["clean_memos"] = False
            data["clean_assets"] = False

        # 4. 终极安全校验：如果所有清理目标都为 False，说明用户未要求清理任何机器人数据，绝不能触发管理指令！
        if not data.get("clean_memos") and not data.get("clean_reminders") and not data.get("clean_feishu_tasks") and not data.get("clean_assets"):
            logger.info(f"💡 所有清理标志均为 False，非机器人数据清理指令: {text_clean}")
            return None

        # 生成精准的目标描述
        if data.get("clean_memos") and data.get("clean_reminders") and data.get("clean_assets"):
            data["target_desc"] = "全量清理点滴、日程与文件"
        elif data.get("clean_memos") and not data.get("clean_reminders"):
            data["target_desc"] = "清理思源点滴与笔记"
        elif data.get("clean_reminders") and not data.get("clean_memos"):
            data["target_desc"] = "清理飞书日程与待办"
        else:
            data["target_desc"] = "清理相关数据记录"

        data["raw_text"] = text_clean
        logger.info(f"🎯 最终生效的管理指令参数: {data}")
        return data

    def prepare_cleanup(self, chat_id: str, action_params: dict) -> str:
        """预演清点要清理的目标，生成二次确认卡片"""
        start_time_str = action_params.get("start_time", "")
        clean_memos = action_params.get("clean_memos", False)
        clean_reminders = action_params.get("clean_reminders", True)
        clean_feishu = action_params.get("clean_feishu_tasks", True)
        clean_assets = action_params.get("clean_assets", False)
        target_desc = action_params.get("target_desc", "数据清理")

        clean_start_ts = start_time_str.replace("-", "").replace(":", "").replace(" ", "")

        blocks_to_delete = []
        files_to_delete = []
        rems_to_delete = []
        feishu_tasks_to_delete = []

        # 1. 扫描思源笔记块 (仅当明确要求清理点滴/笔记时)
        if clean_memos and self.siyuan and self.siyuan.is_active():
            doc_id = self.siyuan.get_or_create_today_doc()
            if doc_id:
                try:
                    sql = f"SELECT id, content, markdown FROM blocks WHERE root_id='{doc_id}' AND updated >= '{clean_start_ts}' AND type IN ('p', 'i', 'l', 'h')"
                    res = requests.post(f"{self.siyuan.api_url}/api/query/sql", headers=self.siyuan._headers(), json={"stmt": sql}, timeout=6).json()
                    raw_blocks = res.get("data", [])
                    for b in raw_blocks:
                        bid = b.get("id")
                        if bid and bid != doc_id:
                            blocks_to_delete.append(bid)
                            if clean_assets and self.assets_dir and os.path.exists(self.assets_dir):
                                full_text = str(b.get("content", "")) + " " + str(b.get("markdown", ""))
                                matches = re.findall(r"assets/([a-zA-Z0-9_\-\.]+\.(?:opus|mp4|png|jpg|jpeg|txt|pdf|zip|docx|xlsx))", full_text)
                                for m in matches:
                                    fpath = os.path.join(self.assets_dir, m)
                                    if fpath not in files_to_delete and os.path.exists(fpath):
                                        files_to_delete.append(fpath)
                except Exception as e:
                    logger.error(f"扫描思源清理块异常: {e}")

        # 2. 扫描本地待办与闹钟
        if clean_reminders and os.path.exists(self.reminders_file):
            try:
                with open(self.reminders_file, "r", encoding="utf-8") as f:
                    rems = json.load(f)
                for r in rems:
                    if r.get("created_at", "") >= start_time_str:
                        rems_to_delete.append(r)
            except Exception as e:
                logger.error(f"扫描 reminders 异常: {e}")

        # 3. 扫描已创建的飞书官方待办中心任务
        if clean_feishu and os.path.exists(self.feishu_tasks_file):
            try:
                with open(self.feishu_tasks_file, "r", encoding="utf-8") as f:
                    ftasks = json.load(f)
                for t in ftasks:
                    if t.get("created_at", "") >= start_time_str:
                        feishu_tasks_to_delete.append(t)
            except Exception as e:
                logger.error(f"扫描 feishu_tasks 异常: {e}")

        # 若没有任何可清理的目标
        if not blocks_to_delete and not files_to_delete and not rems_to_delete and not feishu_tasks_to_delete:
            return f"ℹ️ 扫描完毕：在【{start_time_str} 之后】未找到任何匹配的{target_desc}（未产生未清理的数据）。"

        # 记录待确认会话 (120 秒有效期)
        self.pending_confirmations[chat_id] = {
            "action": "cleanup_records",
            "created_at": time.time(),
            "start_time_str": start_time_str,
            "target_desc": target_desc,
            "clean_memos": clean_memos,
            "clean_reminders": clean_reminders,
            "clean_feishu_tasks": clean_feishu,
            "clean_assets": clean_assets,
            "blocks_to_delete": blocks_to_delete,
            "files_to_delete": files_to_delete,
            "rems_to_delete": rems_to_delete,
            "feishu_tasks_to_delete": feishu_tasks_to_delete
        }

        # 联动思源笔记：记录用户原始指令与系统预演请求
        if self.siyuan and self.siyuan.is_active():
            try:
                raw_text = action_params.get("raw_text", "")
                user_msg = f"📋 【用户管理指令】{raw_text}\n\n" if raw_text else ""
                self.siyuan.append_memo(f"{user_msg}⚠️ 预演盘点「{target_desc}」（范围：【{start_time_str} 之后】），等待二次确认。", summary_tag="#指令预演")
            except Exception as e:
                logger.error(f"记录思源预演日志失败: {e}")

        # 格式化清单
        rems_summary = "、".join([r.get("task", "未命名") for r in rems_to_delete[:3]])
        if len(rems_to_delete) > 3:
            rems_summary += f" 等 {len(rems_to_delete)} 个"

        feishu_summary = "、".join([t.get("summary", "待办") for t in feishu_tasks_to_delete[:3]])
        if len(feishu_tasks_to_delete) > 3:
            feishu_summary += f" 等 {len(feishu_tasks_to_delete)} 项"

        memos_line = f"• 📝 **思源笔记点滴**：共 {len(blocks_to_delete)} 个块" if clean_memos else "• 📝 **思源笔记点滴**：✅ 保持不动（不清理随笔灵感）"
        feishu_line = f"• 📋 **飞书官方待办中心**：共 {len(feishu_tasks_to_delete)} 项待办 ({feishu_summary or '已识别'})" if clean_feishu else ""
        rems_line = f"• ⏰ **本地备忘闹钟**：共 {len(rems_to_delete)} 项 ({rems_summary or '已识别'})" if clean_reminders else ""
        files_line = f"• 📁 **本地关联文件**：共 {len(files_to_delete)} 个" if clean_assets and files_to_delete else ""

        card = [
            "⚠️ ════【高危系统操作 · 二次确认】════",
            f"目标：**{target_desc}**（范围：【{start_time_str} 之后】）",
            "",
            "已为您精准盘点受影响目标：",
            memos_line,
        ]
        if feishu_line:
            card.append(feishu_line)
        if rems_line:
            card.append(rems_line)
        if files_line:
            card.append(files_line)

        card.extend([
            "━━━━━━━━━━━━━━━━━━━━",
            "⚠️ 飞书待办中心任务将同步从飞书服务器彻底删除！",
            "👉 请在 2 分钟内回复【确认清除】立即执行",
            "👉 或回复【取消】放弃本次清理操作"
        ])
        return "\n".join(card)

    def handle_pending_confirmation(self, chat_id: str, user_text: str) -> str:
        """检查并处理用户的确认/取消反馈"""
        pending = self.pending_confirmations.get(chat_id)
        if not pending:
            return None

        # 检查是否超时 (120 秒)
        if time.time() - pending["created_at"] > 120:
            del self.pending_confirmations[chat_id]
            if self.siyuan and self.siyuan.is_active():
                try:
                    self.siyuan.append_memo(f"⏳ 【系统管理日志】清理操作（{pending.get('target_desc')}）已超时自动取消。", summary_tag="#系统取消")
                except Exception as e:
                    pass
            return "⏳ 操作确认已超时，已自动取消本次清理计划。"

        clean_text = user_text.strip()

        # 确认分支
        if clean_text in ["确认清除", "确认", "确定", "执行", "立即清除", "清除", "清理"]:
            del self.pending_confirmations[chat_id]
            return self._execute_cleanup(pending)

        # 取消分支
        if clean_text in ["取消", "算了", "算啦", "放弃", "不要了", "保留"]:
            del self.pending_confirmations[chat_id]
            if self.siyuan and self.siyuan.is_active():
                try:
                    self.siyuan.append_memo(f"✅ 【系统管理日志】用户取消了「{pending.get('target_desc')}」操作，数据完好保留。", summary_tag="#系统取消")
                except Exception as e:
                    pass
            return "✅ 已为您取消本次清理操作，所有日程、待办与笔记完好保留。"

        return None

    def _execute_cleanup(self, pending: dict) -> str:
        """执行真正的物理删除操作 (含飞书官方待办中心真正删除)"""
        blocks = pending.get("blocks_to_delete", [])
        files = pending.get("files_to_delete", [])
        rems = pending.get("rems_to_delete", [])
        feishu_tasks = pending.get("feishu_tasks_to_delete", [])
        start_time_str = pending.get("start_time_str", "")

        deleted_blocks_count = 0
        deleted_files_count = 0
        deleted_rems_count = 0
        deleted_feishu_count = 0

        # 1. 物理删除飞书官方待办中心任务 (调用 Task v2 API)
        if self.lark_client and feishu_tasks:
            from lark_oapi.api.task.v2 import DeleteTaskRequest
            del_guids = set()
            for t in feishu_tasks:
                guid = t.get("guid")
                if guid and guid not in del_guids:
                    try:
                        req = DeleteTaskRequest.builder().task_guid(guid).build()
                        resp = self.lark_client.task.v2.task.delete(req)
                        if resp.success():
                            deleted_feishu_count += 1
                            del_guids.add(guid)
                            logger.info(f"🗑️ 成功从飞书服务器物理删除待办: {guid} ({t.get('summary')})")
                    except Exception as e:
                        logger.error(f"删除飞书待办 {guid} 异常: {e}")

            # 更新 feishu_tasks.json
            if os.path.exists(self.feishu_tasks_file):
                try:
                    with open(self.feishu_tasks_file, "r", encoding="utf-8") as f:
                        current_ftasks = json.load(f)
                    remaining = [x for x in current_ftasks if x.get("guid") not in del_guids]
                    with open(self.feishu_tasks_file, "w", encoding="utf-8") as f:
                        json.dump(remaining, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.error(f"更新 feishu_tasks.json 异常: {e}")

        # 2. 删除思源笔记块
        if blocks and self.siyuan and self.siyuan.is_active():
            for bid in blocks:
                try:
                    res = requests.post(f"{self.siyuan.api_url}/api/block/deleteBlock", headers=self.siyuan._headers(), json={"id": bid}, timeout=5).json()
                    if res.get("code") == 0:
                        deleted_blocks_count += 1
                except Exception as e:
                    logger.error(f"删除思源块 {bid} 失败: {e}")

        # 3. 物理删除 assets 文件
        for fpath in files:
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
                    deleted_files_count += 1
                    logger.info(f"🗑️ 成功物理删除本地附件: {fpath}")
            except Exception as e:
                logger.error(f"删除文件 {fpath} 异常: {e}")

        # 4. 更新 reminders.json
        if os.path.exists(self.reminders_file) and rems:
            try:
                rem_ids_to_del = set(r["id"] for r in rems)
                with open(self.reminders_file, "r", encoding="utf-8") as f:
                    current_rems = json.load(f)
                new_rems = [r for r in current_rems if r["id"] not in rem_ids_to_del]
                with open(self.reminders_file, "w", encoding="utf-8") as f:
                    json.dump(new_rems, f, ensure_ascii=False, indent=2)
                deleted_rems_count = len(rems)
            except Exception as e:
                logger.error(f"清理 reminders.json 异常: {e}")

        report = [
            "✅ ════【清理操作执行完毕】════",
            f"已彻底物理清除【{start_time_str} 之后】的目标：",
        ]
        if deleted_feishu_count > 0:
            report.append(f"• 📋 **飞书待办中心**：已同步从飞书服务器抹除 {deleted_feishu_count} 项任务")
        if deleted_rems_count > 0:
            report.append(f"• ⏰ **本地备忘闹钟**：已注销 {deleted_rems_count} 条闹钟")
        if deleted_blocks_count > 0:
            report.append(f"• 📝 **思源笔记点滴**：已删除 {deleted_blocks_count} 条记录")
        if deleted_files_count > 0:
            report.append(f"• 📁 **本地磁盘文件**：已物理释放 {deleted_files_count} 个附件")

        report.extend([
            "━━━━━━━━━━━━━━━━━━━━",
            "💾 飞书官方待办中心与系统已恢复清爽！"
        ])

        # 联动思源笔记：记录物理删除执行审计日志
        if self.siyuan and self.siyuan.is_active():
            try:
                report_body = "\n".join(report[1:])
                self.siyuan.append_memo(f"🛠️ 【系统维护执行日志】\n{report_body}", summary_tag="#系统执行")
            except Exception as e:
                logger.error(f"记录思源维护执行日志失败: {e}")

        return "\n".join(report)
