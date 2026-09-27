#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书日程与备忘闹钟调度管理器 (Reminder Manager)
功能:
1. 自然语言时间解析 (相对时间/绝对时间/大模型增强)
2. 任务本地持久化 (reminders.json)
3. 毫秒级后台巡检线程 (到点主动向飞书推送富文本提醒)
4. 联动思源笔记 (创建时登记待办，触发时打勾标记)
"""

import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timedelta
import requests

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody
)

logger = logging.getLogger("FeishuCopilot.Reminder")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REMINDERS_FILE = os.path.join(BASE_DIR, "reminders.json")

def clean_task_name(task: str) -> str:
    """清洗事项文本中的提醒/待办关键词前缀与后缀，保留纯净的任务正文"""
    if not task:
        return ""
    # 剥离句首指令词 (如 "待办：", "【闹钟】", "提醒我 ")
    clean = re.sub(
        r'^(?:[【\[\(（]?(?:闹钟|待办|待办事项|提醒|提醒我|定闹钟|设闹钟|定个闹钟|设个闹钟|设个提醒|帮我提醒|记一下|备忘|TODO|todo)[】\]\)）]?[:：\s\-\/]*)+',
        '', task, flags=re.IGNORECASE
    )
    # 剥离句尾指令词 (如 "... 待办", "... 提醒", "... 叫我")
    clean = re.sub(
        r'[-——\s\(\[（【]*(?:闹钟|待办|提醒|提醒我|叫我|记一下|备忘|定闹钟|设闹钟)[】）\]\)]*$',
        '', clean
    )
    clean = re.sub(r'^(?:帮我|请|设置|安排|一个|记得|要|发我|对我说)\s*', '', clean)
    clean = re.sub(r'^[，,、:：\s]+|[，,、:：\s]+$', '', clean).strip()
    return clean or task

class ReminderScheduler:
    def __init__(self, lark_client: lark.Client, siyuan_client=None, llm_caller=None):
        self.client = lark_client
        self.siyuan = siyuan_client
        self.llm_caller = llm_caller
        self.lock = threading.Lock()
        self.reminders = self._load_reminders()
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="ReminderWorker")
        self.thread.start()
        logger.info(f"⏰ 闹钟日程调度引擎已就绪，当前待触发提醒: {len(self.get_active_reminders())} 条")

    def _load_reminders(self) -> list:
        if not os.path.exists(REMINDERS_FILE):
            return []
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载 reminders.json 失败: {e}")
            return []

    def _save_reminders(self):
        try:
            with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.reminders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 reminders.json 失败: {e}")

    @classmethod
    def has_reminder_signal(cls, text: str) -> bool:
        """
        判断用户是否明确具有设定闹钟/待办的意图 (严格遵循前后关键词与显式祈使使役动词逻辑)
        - 1. 负向过滤：纯粹的过去耗时叙述与身体状态陈述（如'睡了一个小时'、'看了2小时'、'花了半小时'）绝对拦截。
        - 2. 纠错规则：包含修改/设置错误且有上下文。
        - 3. 句首关键词：以 闹钟/待办/提醒/提醒我/定个闹钟/设个闹钟/记一下/备忘/TODO 开头。
        - 4. 句尾关键词：以 闹钟/待办/提醒/叫我/记一下/备忘 结尾。
        - 5. 显式祈使使役动词短语：包含 提醒我/叫我/通知我/定个闹钟/设个闹钟/设个提醒。
        """
        text_clean = text.strip()
        if not text_clean:
            return False

        # 1. 负向过滤：典型的过去时耗时叙述与日常状态陈述（如：睡了/看了/花了/用了/等了/跑了 X小时/分钟）
        past_duration = re.search(r'(?:睡了|看了|花了|用了|等了|玩了|做了|走了|跑了|学了|搞了|忙了|歇了|躺了)\s*(?:[一二两三四五六七八九十\d]+|半)\s*(?:个)?\s*(?:小时|分钟|分|秒|钟头)', text_clean)
        if past_duration and not re.search(r'(?:提醒我|叫我|通知我)', text_clean):
            prefix_match = re.search(r'^(?:[【\[\(（]?(?:闹钟|待办|提醒)[】\]\)）]?[:：\s])', text_clean)
            suffix_match = re.search(r'(?:闹钟|待办|提醒)[】）\]\)]*$', text_clean)
            if not prefix_match and not suffix_match:
                return False

        # 2. 纠错指令信号（如："时间弄错了，改成下午3点"）
        corr_pattern = r'(设置错误|闹钟错误|时间错了|弄错了|改到|应该是|改下时间|修改闹钟|修改提醒)'
        if re.search(corr_pattern, text_clean):
            return True

        # 3. 句首关键词 (Prefix) - 无论有无空格冒号均可匹配
        prefix_pattern = r'^(?:[【\[\(（]?(?:闹钟|待办|待办事项|提醒|提醒我|定闹钟|设闹钟|定个闹钟|设个闹钟|设个提醒|帮我提醒|记一下|备忘|TODO|todo)[】\]\)）]?[:：\s\-\/]*\s*)'
        if re.search(prefix_pattern, text_clean, re.IGNORECASE):
            # 若以待办/提醒开头，且包含明确的时间词或点钟词，确认为提醒日程信号
            if re.search(r'(?:今天|明天|后天|周[一二三四五六日天]|星期|点|分|小时|半小时|\d+:\d+)', text_clean):
                return True
            if re.search(r'^(?:[【\[\(（](?:闹钟|待办|提醒)[】\]\)）]|(?:闹钟|提醒)[:：\s])', text_clean):
                return True

        # 4. 句尾关键词 (Suffix)
        suffix_pattern = r'[-——\s\(\[（【]+(?:闹钟|待办|提醒|提醒我|叫我|记一下|备忘|定闹钟|设闹钟)[】）\]\)]*$'
        if re.search(suffix_pattern, text_clean):
            return True

        # 5. 句中明确的祈使/使役命令短语（用户直接向助手发出"提醒我/叫我/叫谁起床"指令）
        imperative_pattern = r'(?:提醒我|叫我|通知我|叫[\w\u4e00-\u9fa5]+(?:起床|起来)?|记得提醒我|帮我定个闹钟|帮我设个闹钟|定个闹钟|设个闹钟|设个提醒|记得通知我)'
        if re.search(imperative_pattern, text_clean):
            return True

        return False

    def parse_intent(self, text: str, now: datetime = None):
        """解析文本是否包含提醒意图与具体时间 (支持上下文纠错与地点/事件高精度抽取)"""
        if now is None:
            now = datetime.now()

        text_clean = text.strip()

        # 0. 快速初筛门禁：如果连前后关键词、使役动词、纠错词都没有，绝不是闹钟，直接放行 (提速且防误判)
        if not self.has_reminder_signal(text_clean):
            return None, None, False

        # 检查是否包含最近的待办/日程上下文 (用于纠错和上下文承接)
        recent_context = ""
        with self.lock:
            active = [r for r in self.reminders if r.get("status") in ["pending", "triggered"]]
            if active:
                last_r = active[-1]
                recent_context = f"上一条事项: 【{last_r['task']}】 (预定时间: {last_r['remind_at']})"

        # 1. 优先使用大模型进行深层语义理解 (能精准识别倒装句、复杂地点、人物、以及纠错修改)
        if self.llm_caller:
            dt_llm, task_llm, is_correction = self._parse_with_llm(text_clean, now, recent_context)
            if dt_llm and task_llm:
                return dt_llm, clean_task_name(task_llm), is_correction

        # 2. 本地高精度规则兜底 (0 Token 离线算法)
        dt_reg, task_reg = self._parse_regex_enhanced(text_clean, now)
        if dt_reg and task_reg:
            is_corr = any(k in text_clean for k in ["设置错误", "错了", "改到", "应该是", "改下", "修改"])
            return dt_reg, clean_task_name(task_reg), is_corr

        return None, None, False

    CN_NUM = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10
    }

    def _parse_val(self, s: str) -> int:
        s = s.strip()
        if s.isdigit():
            return int(s)
        if s == "十":
            return 10
        if s.startswith("十") and len(s) == 2:
            return 10 + self.CN_NUM.get(s[1], 0)
        if s.endswith("十") and len(s) == 2:
            return self.CN_NUM.get(s[0], 1) * 10
        return self.CN_NUM.get(s, 1)

    def _parse_regex_enhanced(self, text: str, now: datetime):
        """本地高精度时间与事项正则解析 (增强版，支持倒装句、地点与事件提取)"""
        if not self.has_reminder_signal(text):
            return None, None

        # A. 半小时/半个钟头单独处理
        m_half = re.search(r"半(?:个)?(?:小时|钟头)之?后(?:\s*(?:发(?:条)?(?:信息|消息)?(?:给|对)?我|提醒我|叫我|通知我|记得|要|发我|对我说))?(.*)", text)
        if m_half:
            task = m_half.group(1).strip()
            task = re.sub(r"^(?:说|道|：|:)\s*", "", task)
            task = clean_task_name(task) or "半小时预定事项"
            return now + timedelta(minutes=30), task

        # B. 相对时间: (一|两|三|10) 分钟/小时之后
        m_rel = re.search(r"([一二两三四五六七八九十\d]+)\s*(?:个)?\s*(秒钟|秒|分钟|分|小时|天)之?后(?:\s*(?:发(?:条)?(?:信息|消息)?(?:给|对)?我|提醒我|叫我|通知我|记得|要|发我|对我说))?(.*)", text)
        if m_rel:
            num_str = m_rel.group(1).replace(" ", "")
            unit = m_rel.group(2)
            task = m_rel.group(3).strip()
            task = re.sub(r"^(?:说|道|：|:)\s*", "", task)
            task = task or "预定提醒事项"

            val = self._parse_val(num_str)
            if "秒" in unit:
                return now + timedelta(seconds=val), task
            elif "分" in unit:
                return now + timedelta(minutes=val), task
            elif "小时" in unit:
                return now + timedelta(hours=val), task
            elif "天" in unit:
                return now + timedelta(days=val), task

        # C. 绝对时间 (支持中文修饰与倒装句提取)
        day_offset = 0
        if "后天" in text:
            day_offset = 2
        elif "明天" in text:
            day_offset = 1

        is_pm = any(k in text for k in ["下午", "晚上", "今晚", "明晚", "傍晚", "夜里"])
        m_time = re.search(r"([一二两三四五六七八九十\d]{1,2})\s*[点:：]\s*(半|[一二两三四五六七八九十\d]{1,2})?(?:分)?", text)
        if m_time:
            h_str, m_str = m_time.groups()
            h = self._parse_val(h_str)
            if is_pm and h < 12:
                h += 12
            elif any(k in text for k in ["中午"]) and h < 11:
                h += 12

            m = 30 if m_str == "半" else (self._parse_val(m_str) if m_str else 0)
            target_dt = (now + timedelta(days=day_offset)).replace(hour=h, minute=m, second=0, microsecond=0)
            if day_offset == 0 and target_dt < now and not is_pm and h < 12:
                # 尝试当作下午
                target_dt += timedelta(hours=12)
            if day_offset == 0 and target_dt < now:
                target_dt += timedelta(days=1)

            # 清洗提取具体事项和地点
            clean = re.sub(r"(帮我|请|设置|安排|一个|的闹钟|闹钟|待办事项|待办|备忘|记一下|todo|TODO|提醒我|叫我|通知我|记得|要|发我|对我说)", "", text)
            clean = re.sub(r"(今天|明天|后天|早上|上午|中午|下午|晚上|今晚|明晚|傍晚)", "", clean)
            clean = re.sub(r"([一二两三四五六七八九十\d]{1,2})\s*[点:：]\s*(半|[一二两三四五六七八九十\d]{1,2})?(?:分)?", "", clean)
            clean = re.sub(r"^[，,、\s]+|[，,、\s]+$", "", clean)
            clean = re.sub(r"[，,、]+", " ", clean).strip()
            task = clean_task_name(clean) or "预定日程"
            return target_dt, task

        return None, None

    def _parse_with_llm(self, text: str, now: datetime, recent_context: str = ""):
        """调用大模型进行深层意图理解 (支持地点、人物、事件提取以及上下文纠错)"""
        now_str = now.strftime("%Y-%m-%d %H:%M:%S (%A)")
        context_part = f"【最近一条闹钟/待办上下文】：{recent_context}\n" if recent_context else ""

        prompt = f"""当前系统时间是：{now_str}。
{context_part}用户最新发来一条消息："{text}"

请严格按照以下规则分析：
1. 意图判断（极其严谨，必须遵循前后关键词与强动词逻辑）：
   - 用户是否在【明确要求定未来的闹钟、设置定时提醒、或者修改日程】？
   - 必须具有明确的指令标记：句首/句尾含有【闹钟、待办、提醒、记一下、备忘】或句中含有【提醒我、叫我、通知我】等明确的使役指令！
   - 如果用户只是在描述自己做过的事、耗时叙述（例如："睡了一个小时"、"写了两个小时代码"、"看了半小时书"）、当前的身体或心理状态（"好一点了"、"没那么晕了"、"晚上睡不好"），绝不是闹钟！必须输出 {{"is_reminder": false}}。
   - 严禁脑补与凭空捏造时间！必须是用户在文本中明确指定了未来的触发时间点（如"明早8点"、"半小时后"、"周日处理"）。
   - "八部金刚功"、"八段锦"等专有名词中的数字绝对不是时间！
2. 时间计算（关键）：
   - 必须换算为 24 小时制（例如："晚上6点" -> 18:00:00，绝不可算为 06:00:00；"下午3点" -> 15:00:00；"中午12点" -> 12:00:00）。
   - "明天"、"后天"必须以当前系统时间为准准确推算公历日期。
3. 事项提取与关键词剥离（核心）：
   - 提取出的 task 必须完整包含用户提到的【具体事件、地点、人物】（例如"待办：明天晚上吃饭，在利和广场，6点" -> 事项提取为"在利和广场吃饭"）。
   - 必须剥离掉"待办："、"闹钟："、"提醒我"、"记一下"等指令标记词，只保留纯净的具体事项！
   - 如果用户是在纠正上一条日程（如"上面这个日程设置错误，应该是明天晚上6点"），请继承并保持上一条上下文的具体事项（如"在利和广场吃饭"），并将时间纠正为新时间，设置 is_correction 为 true。

请输出纯 JSON 格式：
{{"is_reminder": true, "is_correction": false, "remind_at": "YYYY-MM-DD HH:MM:SS", "task": "具体事项与地点"}}
若不是明确的定时日程或闹钟，输出：
{{"is_reminder": false}}
只输出纯 JSON，不要有任何多余文字或 markdown 标记。"""

        try:
            res = self.llm_caller(prompt, system_prompt="你是一个高情商、极其严谨的个人日程与意图理解引擎。")
            if res:
                json_str = re.sub(r"```(?:json)?", "", res).strip()
                data = json.loads(json_str)
                if data.get("is_reminder") and data.get("remind_at") and data.get("task"):
                    target_dt = datetime.strptime(data["remind_at"], "%Y-%m-%d %H:%M:%S")
                    is_correction = bool(data.get("is_correction", False))
                    task_str = clean_task_name(data["task"].strip())
                    logger.info(f"🧠 大模型提取日程成功: [{data['remind_at']}] {task_str} (纠错={is_correction})")
                    return target_dt, task_str, is_correction
        except Exception as e:
            logger.warning(f"大模型解析日程异常: {e}")
        return None, None, False

    def add_reminder(self, chat_id: str, remind_dt: datetime, task: str, is_correction: bool = False, raw_text: str = "") -> dict:
        """添加一条新闹钟提醒，如果为纠错则自动注销上一条冲突闹钟"""
        with self.lock:
            if is_correction:
                # 寻找上一条 pending 的记录并注销，避免重复提醒
                for r in reversed(self.reminders):
                    if r.get("status") == "pending":
                        r["status"] = "cancelled"
                        logger.info(f"🔄 纠错模式：已自动废弃旧冲突闹钟: {r['id']} ({r['task']} @ {r['remind_at']})")
                        break

            rem_id = f"rem_{int(time.time() * 1000)}"
            now = datetime.now()
            item = {
                "id": rem_id,
                "chat_id": chat_id,
                "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "remind_at": remind_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "task": task,
                "status": "pending"
            }
            self.reminders.append(item)
            self._save_reminders()

            # 联动思源笔记：完整保留用户原始消息与结构化提醒
            if self.siyuan and self.siyuan.is_active():
                target_str = remind_dt.strftime("%H:%M") if remind_dt.date() == now.date() else remind_dt.strftime("%m-%d %H:%M")
                tag = "修正闹钟" if is_correction else "待办闹钟"
                memo_msg = f"⏰ 【已定闹钟】计划在 [{target_str}] 提醒：{task}"
                if raw_text and raw_text != task:
                    memo_msg += f"\n*(原始消息: {raw_text})*"
                self.siyuan.append_memo(memo_msg, summary_tag=tag)

            logger.info(f"✅ 成功设定提醒: [{item['remind_at']}] {task} (Chat: {chat_id}, is_correction={is_correction})")
            return item

    def get_active_reminders(self) -> list:
        with self.lock:
            return [r for r in self.reminders if r["status"] == "pending"]

    def cancel_reminder(self, index: int) -> bool:
        with self.lock:
            active = [r for r in self.reminders if r["status"] == "pending"]
            if 0 <= index < len(active):
                target = active[index]
                target["status"] = "cancelled"
                target["cancelled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._save_reminders()
                return True
        return False

    def get_latest_actionable_reminder(self, chat_id: str = ""):
        """获取最近一条可操作的提醒（优先已触发未闭环的，其次临近的待触发）"""
        with self.lock:
            now = datetime.now()
            # 1. 优先查找最近 24 小时内触发但尚未标记完成的提醒
            triggered_candidates = [
                r for r in self.reminders
                if r.get("status") == "triggered"
            ]
            if chat_id:
                chat_triggered = [r for r in triggered_candidates if r.get("chat_id") == chat_id]
                if chat_triggered:
                    triggered_candidates = chat_triggered

            if triggered_candidates:
                triggered_candidates.sort(key=lambda x: x.get("triggered_at", x.get("remind_at", "")), reverse=True)
                latest = triggered_candidates[0]
                t_str = latest.get("triggered_at", latest.get("remind_at", ""))
                try:
                    t_dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
                    if (now - t_dt).total_seconds() <= 86400:
                        return latest, "triggered"
                except Exception:
                    return latest, "triggered"

            # 2. 查找待触发 pending 的提醒（按提醒时间正序，取最近要触发的）
            pending_candidates = [
                r for r in self.reminders
                if r.get("status") == "pending"
            ]
            if chat_id:
                chat_pending = [r for r in pending_candidates if r.get("chat_id") == chat_id]
                if chat_pending:
                    pending_candidates = chat_pending

            if pending_candidates:
                pending_candidates.sort(key=lambda x: x.get("remind_at", ""))
                return pending_candidates[0], "pending"

            return None, ""

    def complete_reminder(self, rem_id: str):
        """将指定提醒标记为完成/已闭环"""
        with self.lock:
            for r in self.reminders:
                if r.get("id") == rem_id:
                    r["status"] = "completed"
                    r["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._save_reminders()
                    logger.info(f"✅ 闹钟已标记闭环: {r['id']} ({r.get('task')})")
                    return r
        return None

    def snooze_reminder(self, rem_id: str, minutes: int = 10):
        """将提醒推迟延期指定分钟"""
        with self.lock:
            for r in self.reminders:
                if r.get("id") == rem_id:
                    new_dt = datetime.now() + timedelta(minutes=minutes)
                    new_time_str = new_dt.strftime("%Y-%m-%d %H:%M:%S")
                    r["status"] = "pending"
                    r["remind_at"] = new_time_str
                    r["snooze_count"] = r.get("snooze_count", 0) + 1
                    r["last_snoozed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._save_reminders()
                    logger.info(f"⏰ 闹钟已推迟 {minutes} 分钟至 {new_time_str}: {r['id']} ({r.get('task')})")
                    return r, new_dt
        return None

    def cancel_reminder_by_id(self, rem_id: str):
        """按 ID 取消提醒"""
        with self.lock:
            for r in self.reminders:
                if r.get("id") == rem_id:
                    r["status"] = "cancelled"
                    r["cancelled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._save_reminders()
                    logger.info(f"🗑️ 闹钟已取消: {r['id']} ({r.get('task')})")
                    return r
        return None

    def update_reminder_feishu_guid(self, rem_id: str, guid: str):
        """关联飞书待办 Task GUID"""
        with self.lock:
            for r in self.reminders:
                if r.get("id") == rem_id:
                    r["feishu_task_guid"] = guid
                    self._save_reminders()
                    break

    def update_reminder_calendar_event(self, rem_id: str, event_id: str):
        """关联飞书官方日历日程 Event ID"""
        with self.lock:
            for r in self.reminders:
                if r.get("id") == rem_id:
                    r["calendar_event_id"] = event_id
                    self._save_reminders()
                    break

    def _trigger_alert(self, item: dict):
        """时间到达，主动向飞书推送提醒卡片"""
        chat_id = item["chat_id"]
        task = item["task"]
        remind_time = item["remind_at"]

        # 极简 Deja Vu 风格通知卡片：无长分割线、无多余emoji，提供明确闭环与延期指引
        alert_msg = (
            "⏰ 备忘闹钟\n"
            f"事项：{task}\n"
            f"时间：{remind_time}\n\n"
            "回复「确认/完成」闭环，或「延期 10分钟」推迟"
        )

        try:
            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(CreateMessageRequestBody.builder()
                              .receive_id(chat_id)
                              .msg_type("text")
                              .content(json.dumps({"text": alert_msg}))
                              .build()) \
                .build()
            
            resp = self.client.im.v1.message.create(req)
            if resp.success():
                logger.info(f"🎉 成功向飞书推送闹钟提醒: {task}")
            else:
                logger.error(f"主动推送闹钟失败: {resp.code} - {resp.msg}")
        except Exception as e:
            logger.error(f"发送到期闹钟网络异常: {e}")

        # 联动思源笔记：打勾完成标记
        if self.siyuan and self.siyuan.is_active():
            self.siyuan.append_memo(f"⏰ 【闹钟已触发】{task} (预定于 {remind_time})", summary_tag="#闹钟提醒")

    def _run_loop(self):
        """后台高频巡检线程"""
        while self.running:
            try:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with self.lock:
                    for item in self.reminders:
                        if item["status"] == "pending" and item["remind_at"] <= now_str:
                            item["status"] = "triggered"
                            item["triggered_at"] = now_str
                            self._save_reminders()
                            # 异步触发避免阻塞扫描锁
                            threading.Thread(target=self._trigger_alert, args=(item,), daemon=True).start()
            except Exception as e:
                logger.error(f"巡检闹钟发生异常: {e}")
            time.sleep(2)
