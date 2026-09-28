#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
思源笔记离线增量向量化调度同步引擎 (SiYuan Note Incremental Vector Syncer)
====================================================================
功能契约与架构规范:
1. 增量跟踪 (Watermark State): 基于 ~/.local_vectors/siyuan_sync_state.json 记录上次同步水位
2. 降噪与过滤:
   - 块类型筛选: type IN ('p', 'h', 'b') (段落、标题、引述)，自动跳过列表容器 ('l') 避免重复
   - 过滤长度 < 15 字的碎语与纯符号
   - 过滤飞书自动化运维系统模板与复盘回显
3. 智能排重 (Two-tier Deduplication):
   - 基于块 ID (sy_blk_{id}) 防止重复入库
   - 基于内容指纹 MD5 排重，防止与飞书即时写入的 sy_memo_{timestamp} 产生冗余向量
4. 定时调度 (Nightly Scheduler):
   - 每日凌晨 03:30 自动唤醒执行增量扫描入库
   - 守护线程周期性巡检，每日仅触发一次，耗时毫秒级
5. 命令行与热调用支持:
   - 支持 python3 siyuan_sync.py --run-now 立即增量运行
   - 支持 python3 siyuan_sync.py --status 查看当前水位与知识库概况
   - 支持 python3 siyuan_sync.py --days 3 回溯近 N 天数据
"""

import os
import sys
import json
import time
import re
import hashlib
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import requests

# 确保能加载项目同级模块
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from vector_engine import VectorEngine, get_default_paths

logger = logging.getLogger("siyuan_sync")

DEFAULT_STATE_FILE = os.path.join(
    os.getenv("VECTOR_LOCAL_DIR", os.path.expanduser("~/.local_vectors")),
    "siyuan_sync_state.json"
)

# 需过滤的系统与模板噪音关键词
SYSTEM_NOISE_PATTERNS = [
    "由飞书智能助理自动同步与整理",
    "已提醒 🔔",
    "⏰ 【已定闹钟】",
    "⏰ 事项已延期",
    "🗑️ 取消提醒",
    "✅ 事项已完成闭环",
    "⚙️ 切换大模型",
    "⚙️ 用户确认反馈",
    "⚠️ 系统管理指令请求",
    "🔍 查询待触发提醒列表",
    "📊 呼唤今日综合复盘",
    "【飞书×思源 点滴心流与心智认知监控】",
    "【系统状态】: 正常同步中",
    "🧠 【今日智能复盘与认知归纳】",
    "💡 Deja Vu",
    "今日智能复盘",
    "归纳：",
    "洞见：",
    "思源已同步 ·"
]


def resolve_siyuan_config() -> Tuple[str, str]:
    """解析思源 API URL 和 Token (优先读取 Keychain/环境变量，回退 config.json)"""
    config_path = os.path.join(CURRENT_DIR, "config.json")
    api_url = "http://127.0.0.1:6806"
    token = ""

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                sy_cfg = data.get("siyuan", {})
                api_url = sy_cfg.get("api_url", api_url).rstrip("/")
                token = sy_cfg.get("token", "")
        except Exception as e:
            logger.debug(f"读取 config.json 失败: {e}")

    # 尝试从 Keychain 或系统环境变量获取最新凭据
    try:
        from bot_service import _resolve_secret
        token = _resolve_secret("siyuan", "api_token", "SIYUAN_TOKEN", default=token)
    except Exception:
        token = os.getenv("SIYUAN_TOKEN", token)

    return api_url, token


def format_siyuan_timestamp(sy_time_str: str) -> str:
    """将思源 14 位时间字符串 20260928153003 转为标准日期 2026-09-28 15:30:03"""
    if len(sy_time_str) == 14 and sy_time_str.isdigit():
        return f"{sy_time_str[:4]}-{sy_time_str[4:6]}-{sy_time_str[6:8]} {sy_time_str[8:10]}:{sy_time_str[10:12]}:{sy_time_str[12:14]}"
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_block_content(raw_content: str) -> str:
    """清洗思源块正文：剥离前导时间戳、前导标签及零宽隐藏字符"""
    if not raw_content:
        return ""
    text = raw_content.strip()
    # 剥离前导 [15:30:03] 及随后的 #标签
    text = re.sub(r"^\[\d{2}:\d{2}:\d{2}\]\s*(?:#\S+\s*)*", "", text).strip()
    # 剥离不可见字符与零宽空格
    text = re.sub(r"[\u200b-\u200f\ufeff]+", "", text).strip()
    return text


def extract_content_tags(raw_content: str, doc_title: str = "") -> str:
    """提取标签元数据"""
    found = re.findall(r"#([\w\u4e00-\u9fa5]+)", raw_content)
    tag_list = [f"#{t}" for t in found if t not in ["随想", "闲笔", "每日随想"]]
    if doc_title and doc_title != "每日点滴与灵感":
        tag_list.insert(0, f"#{doc_title[:10]}")
    if not tag_list:
        tag_list.append("#思源笔记")
    return " ".join(dict.fromkeys(tag_list))  # 去重保持顺序


class SiYuanIncrementalSync:
    """思源笔记增量向量同步器"""

    def __init__(self,
                 vector_engine: Optional[VectorEngine] = None,
                 siyuan_client=None,
                 api_url: Optional[str] = None,
                 token: Optional[str] = None,
                 state_file: str = DEFAULT_STATE_FILE):
        self.state_file = state_file
        self.vector_engine = vector_engine
        self.siyuan_client = siyuan_client

        if api_url and token:
            self.api_url = api_url.rstrip("/")
            self.token = token
        else:
            resolved_url, resolved_token = resolve_siyuan_config()
            self.api_url = api_url or resolved_url
            self.token = token or resolved_token

        self.title_cache: Dict[str, str] = {}
        self.scheduler: Optional['SiYuanNightlyScheduler'] = None

    def _get_engine(self) -> VectorEngine:
        if self.vector_engine is None:
            self.vector_engine = VectorEngine()
        return self.vector_engine

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Token {self.token}"
        return h

    def query_sql(self, sql: str) -> list:
        """执行思源内核 SQL 查询"""
        try:
            url = f"{self.api_url}/api/query/sql"
            resp = requests.post(url, headers=self._headers(), json={"stmt": sql}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    return data.get("data", [])
                else:
                    logger.warning(f"思源 SQL 执行警告: {data.get('msg')}")
            else:
                logger.warning(f"思源 SQL 请求 HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"思源 SQL 查询异常: {e}")
        return []

    def load_watermark(self) -> dict:
        """加载上次同步水位，如不存在则根据数据库或当前时间自动初始化"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取水位文件异常: {e}，将重新初始化")

        # 尝试从向量库中查找最新的思源记录时间
        latest_ts = None
        try:
            eng = self._get_engine()
            with sqlite3.connect(eng.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT MAX(created_at) FROM knowledge_vectors WHERE source = 'siyuan'")
                row = cursor.fetchone()
                if row and row[0]:
                    dt_str = row[0]  # e.g. 2026-09-28 15:30:10
                    clean_digits = re.sub(r"\D", "", dt_str)
                    if len(clean_digits) >= 14:
                        latest_ts = clean_digits[:14]
        except Exception as e:
            logger.debug(f"从数据库探测最新时间戳异常: {e}")

        # 如果没有历史记录，默认从 24 小时前开始
        if not latest_ts:
            yesterday = datetime.now() - timedelta(days=1)
            latest_ts = yesterday.strftime("%Y%m%d%H%M%S")

        initial_state = {
            "last_sync_time": latest_ts,
            "last_sync_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_synced_blocks": 0,
            "last_batch_count": 0,
            "last_status": "initialized",
            "last_message": "水位初次初始化"
        }
        self.save_watermark(initial_state)
        return initial_state

    def save_watermark(self, state: dict):
        """持久化水位状态"""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            temp_file = f"{self.state_file}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, self.state_file)
        except Exception as e:
            logger.error(f"保存水位状态异常: {e}")

    def get_existing_signatures(self) -> Tuple[set, set]:
        """获取本地向量库中已存在的思源块 ID 集合与内容 MD5 哈希集合，防止二次录入"""
        id_set = set()
        md5_set = set()
        try:
            eng = self._get_engine()
            with sqlite3.connect(eng.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, content FROM knowledge_vectors WHERE source = 'siyuan'")
                for rid, cnt in cursor.fetchall():
                    if rid:
                        id_set.add(rid)
                    if cnt:
                        cleaned = clean_block_content(cnt)
                        if cleaned:
                            h = hashlib.md5(cleaned.encode("utf-8")).hexdigest()
                            md5_set.add(h)
        except Exception as e:
            logger.error(f"读取向量库排重签名异常: {e}")
        return id_set, md5_set

    def resolve_doc_titles(self, root_ids: List[str]) -> Dict[str, str]:
        """批量解析文档 ID 对应的文档标题"""
        unresolved = [rid for rid in set(root_ids) if rid and rid not in self.title_cache]
        if not unresolved:
            return self.title_cache

        # 分批查询，每批最多 60 个
        batch_size = 60
        for i in range(0, len(unresolved), batch_size):
            chunk = unresolved[i:i + batch_size]
            id_in_clause = ", ".join([f"'{rid}'" for rid in chunk])
            sql = f"SELECT id, content FROM blocks WHERE id IN ({id_in_clause})"
            rows = self.query_sql(sql)
            for r in rows:
                self.title_cache[r["id"]] = r.get("content", "").strip() or "思源笔记"

        # 对未查到的补充默认值
        for rid in unresolved:
            if rid not in self.title_cache:
                self.title_cache[rid] = "思源笔记"

        return self.title_cache

    def is_noise_block(self, content: str) -> bool:
        """判定是否为系统模板、飞书自动回复或机器人复盘等无需向量化的噪音"""
        for pattern in SYSTEM_NOISE_PATTERNS:
            if pattern in content:
                return True
        return False

    def sync_once(self, override_since: Optional[str] = None) -> dict:
        """
        执行一次增量同步流水线:
        1. 读取水位或 override_since
        2. 查询 SiYuan SQL blocks
        3. 过滤长度与噪音
        4. 双重排重 (ID 与 MD5)
        5. 批量生成向量并写入知识库
        6. 更新并落盘水位
        """
        t0 = time.time()
        state = self.load_watermark()
        since_time = override_since or state.get("last_sync_time", "")

        logger.info(f"🔄 开始执行思源笔记增量同步 | 起始水位: {since_time}")

        # 1. 增量拉取更新的块 (每次最多 1000 块)
        sql = (
            f"SELECT id, root_id, box, type, content, updated "
            f"FROM blocks "
            f"WHERE type IN ('p', 'h', 'b') "
            f"  AND length(content) >= 15 "
            f"  AND updated > '{since_time}' "
            f"ORDER BY updated ASC "
            f"LIMIT 1000"
        )
        blocks = self.query_sql(sql)

        if not blocks:
            msg = f"未检测到新增或修改的笔记块 (水位: {since_time})"
            logger.info(f"✨ {msg}")
            state["last_sync_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["last_batch_count"] = 0
            state["last_status"] = "success"
            state["last_message"] = msg
            self.save_watermark(state)
            return {
                "status": "success",
                "scanned": 0,
                "new_indexed": 0,
                "skipped": 0,
                "elapsed_s": round(time.time() - t0, 2),
                "watermark": since_time,
                "message": msg
            }

        # 2. 获取已有签名防重
        existing_ids, existing_md5s = self.get_existing_signatures()

        # 3. 收集未入库的有效块
        candidate_blocks = []
        root_ids = []
        skipped_count = 0
        max_updated = since_time

        for b in blocks:
            b_id = b.get("id")
            b_updated = b.get("updated", "")
            raw_content = b.get("content", "")

            if b_updated > max_updated:
                max_updated = b_updated

            # 过滤噪音模板
            if self.is_noise_block(raw_content):
                skipped_count += 1
                continue

            cleaned = clean_block_content(raw_content)
            if len(cleaned) < 15:
                skipped_count += 1
                continue

            record_id = f"sy_blk_{b_id}"
            if record_id in existing_ids:
                skipped_count += 1
                continue

            c_md5 = hashlib.md5(cleaned.encode("utf-8")).hexdigest()
            if c_md5 in existing_md5s:
                skipped_count += 1
                continue

            # 标记以防本批次内重复
            existing_ids.add(record_id)
            existing_md5s.add(c_md5)

            candidate_blocks.append({
                "id": record_id,
                "root_id": b.get("root_id", ""),
                "content": cleaned,
                "raw_content": raw_content,
                "updated": b_updated
            })
            root_ids.append(b.get("root_id", ""))

        if not candidate_blocks:
            msg = f"扫描 {len(blocks)} 个候选块，全部为已入库或系统噪音"
            logger.info(f"✨ {msg} (推进水位至 {max_updated})")
            state["last_sync_time"] = max_updated
            state["last_sync_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["last_batch_count"] = 0
            state["last_status"] = "success"
            state["last_message"] = msg
            self.save_watermark(state)
            return {
                "status": "success",
                "scanned": len(blocks),
                "new_indexed": 0,
                "skipped": skipped_count,
                "elapsed_s": round(time.time() - t0, 2),
                "watermark": max_updated,
                "message": msg
            }

        # 4. 批量解析文档标题
        doc_titles = self.resolve_doc_titles(root_ids)

        # 5. 组装向量记录
        records_to_embed = []
        for item in candidate_blocks:
            title = doc_titles.get(item["root_id"], "思源笔记")
            tags = extract_content_tags(item["raw_content"], title)
            created_at = format_siyuan_timestamp(item["updated"])
            records_to_embed.append({
                "id": item["id"],
                "source": "siyuan",
                "ref_id": item["root_id"],
                "title": title,
                "content": item["content"],
                "tags": tags,
                "created_at": created_at
            })

        # 6. 批量调用向量引擎录入
        eng = self._get_engine()
        added_count = eng.add_records_batch(records_to_embed)

        # 7. 更新水位
        state["last_sync_time"] = max_updated
        state["last_sync_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["total_synced_blocks"] = state.get("total_synced_blocks", 0) + added_count
        state["last_batch_count"] = added_count
        state["last_status"] = "success"
        state["last_message"] = f"增量录入 {added_count} 条笔记记录"
        self.save_watermark(state)

        elapsed = round(time.time() - t0, 2)
        summary_msg = f"思源笔记增量同步完成: 扫描 {len(blocks)} 块, 新入库 {added_count} 条, 跳过 {skipped_count} 条, 耗时 {elapsed}s"
        logger.info(f"✅ {summary_msg}")

        return {
            "status": "success",
            "scanned": len(blocks),
            "new_indexed": added_count,
            "skipped": skipped_count,
            "elapsed_s": elapsed,
            "watermark": max_updated,
            "message": summary_msg
        }

    def start_nightly_scheduler(self, target_hour: int = 3, target_minute: int = 30) -> 'SiYuanNightlyScheduler':
        """启动每日凌晨后台调度器"""
        if self.scheduler and self.scheduler.is_alive():
            logger.info("夜间增量调度器已在运行中")
            return self.scheduler

        self.scheduler = SiYuanNightlyScheduler(self, target_hour=target_hour, target_minute=target_minute)
        self.scheduler.start()
        logger.info(f"🌙 思源夜间增量向量调度已启动 (计划触发时间: 每日 {target_hour:02d}:{target_minute:02d})")
        return self.scheduler


class SiYuanNightlyScheduler(threading.Thread):
    """每日凌晨自动触发增量同步的轻量守护线程"""

    def __init__(self, syncer: SiYuanIncrementalSync, target_hour: int = 3, target_minute: int = 30):
        super().__init__(daemon=True, name="SiYuanNightlyScheduler")
        self.syncer = syncer
        self.target_hour = target_hour
        self.target_minute = target_minute
        self.running = True
        self.last_run_date = ""

    def run(self):
        logger.info(f"🕒 夜间增量同步巡检线程已就绪 (目标时刻: {self.target_hour:02d}:{self.target_minute:02d})")
        while self.running:
            try:
                now = datetime.now()
                today_str = now.strftime("%Y-%m-%d")

                if (today_str != self.last_run_date and
                        now.hour == self.target_hour and
                        now.minute == self.target_minute):
                    logger.info(f"⏰ [夜间定时器触发] 执行思源笔记凌晨定时增量向量化 (时刻: {now.strftime('%H:%M:%S')})")
                    res = self.syncer.sync_once()
                    self.last_run_date = today_str
                    logger.info(f"🌙 定时增量结果: {res.get('message')}")
                    time.sleep(65)  # 跨过当前这一分钟，避免同一分钟内重复触发
            except Exception as e:
                logger.error(f"夜间增量调度线程异常: {e}")

            time.sleep(30)

    def stop(self):
        self.running = False


# 全局单例，便于其他模块引用
_global_syncer: Optional[SiYuanIncrementalSync] = None

def get_syncer() -> SiYuanIncrementalSync:
    global _global_syncer
    if _global_syncer is None:
        _global_syncer = SiYuanIncrementalSync()
    return _global_syncer


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="思源笔记离线增量向量化工具")
    parser.add_argument("--run-now", action="store_true", help="立即执行一次增量同步")
    parser.add_argument("--status", action="store_true", help="查看当前同步水位与状态")
    parser.add_argument("--days", type=int, help="从指定天数前重新增量同步 (例如 --days 3)")
    parser.add_argument("--daemon", action="store_true", help="作为独立守护进程持续运行夜间调度")
    parser.add_argument("--hour", type=int, default=3, help="每日定时触发小时 (默认 3)")
    parser.add_argument("--minute", type=int, default=30, help="每日定时触发分钟 (默认 30)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    syncer = get_syncer()

    if args.status:
        st = syncer.load_watermark()
        eng = syncer._get_engine()
        print("\n" + "=" * 50)
        print("📊 思源笔记增量向量同步状态看板")
        print("=" * 50)
        print(f"当前水位时间戳: {st.get('last_sync_time')}")
        print(f"最后同步时间:   {st.get('last_sync_at')}")
        print(f"累计同步块数:   {st.get('total_synced_blocks')} 条")
        print(f"上次同步批次:   {st.get('last_batch_count')} 条")
        print(f"上次运行状态:   {st.get('last_status')} ({st.get('last_message')})")
        print(f"向量库路径:     {eng.db_path}")
        print(f"当前向量总记录: {eng.count_records()} 条")
        print("=" * 50 + "\n")

    elif args.days:
        since_time = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d%H%M%S")
        print(f"🚀 指定回溯 {args.days} 天，起始时间戳: {since_time}")
        res = syncer.sync_once(override_since=since_time)
        print(f"执行结果: {json.dumps(res, ensure_ascii=False, indent=2)}")

    elif args.run_now:
        print("🚀 立即执行思源笔记增量向量同步...")
        res = syncer.sync_once()
        print(f"\n执行结果: {json.dumps(res, ensure_ascii=False, indent=2)}")

    elif args.daemon:
        print(f"🌙 启动独立夜间增量守护进程 (每日 {args.hour:02d}:{args.minute:02d} 触发)... 按 Ctrl+C 退出")
        syncer.start_nightly_scheduler(target_hour=args.hour, target_minute=args.minute)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 调度守护进程已停止")
    else:
        parser.print_help()
