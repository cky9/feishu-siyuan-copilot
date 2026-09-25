# -*- coding: utf-8 -*-
"""
本地知识库统一向量引擎 (Local Vector Engine)
============================================
基于 Apple Silicon MPS 芯片深度优化的本地私有化向量数据库。
- 本地存储: ~/.local_vectors/knowledge.db
- 本地模型: BAAI/bge-base-zh-v1.5 (768维, 100% 离线本地运行)
- 兼容体系: 思源笔记 (SiYuan) + Obsidian Vault + 未来任何本地文本/对话/复盘
"""

import os
import sqlite3
import time
import logging
from datetime import datetime
from typing import List, Dict, Optional
import re
import numpy as np

logger = logging.getLogger("vector_engine")

EXTERNAL_DIR = os.getenv("VECTOR_EXTERNAL_DIR", None)
LOCAL_DIR = os.getenv("VECTOR_LOCAL_DIR", os.path.expanduser("~/.local_vectors"))


def _is_db_accessible(db_path: str) -> bool:
    """测试指定的 SQLite 数据库路径是否在当前权限环境中可用且可读写"""
    try:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with sqlite3.connect(db_path, timeout=1.0) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA schema_version;")
            return True
    except Exception:
        return False


def get_default_paths():
    """智能决断数据库和模型路径，优先外置存储，权限受限或脱盘时自动降级到 ~/.local_vectors"""
    adata_db = os.path.join(EXTERNAL_DIR, "knowledge.db") if EXTERNAL_DIR else None
    local_db = os.path.join(LOCAL_DIR, "knowledge.db")
    
    adata_model = os.path.join(EXTERNAL_DIR, "models", "bge-base-zh-v1.5") if EXTERNAL_DIR else None
    local_model = os.path.join(LOCAL_DIR, "models", "bge-base-zh-v1.5")

    if adata_db and _is_db_accessible(adata_db):
        primary_db = adata_db
        mirror_db = local_db if os.path.exists(LOCAL_DIR) else None
    else:
        primary_db = local_db
        mirror_db = adata_db if (adata_db and os.path.exists(EXTERNAL_DIR)) else None

    # 模型加载路径检测
    try:
        if adata_model and os.path.exists(adata_model) and os.access(adata_model, os.R_OK):
            # 试读一个文件确认不是 TCC 假阳性
            with open(os.path.join(adata_model, "config.json"), "r") as f:
                pass
            model_path = adata_model
        else:
            model_path = local_model
    except Exception:
        model_path = local_model

    return primary_db, mirror_db, model_path


class VectorEngine:
    def __init__(self, db_path: Optional[str] = None, model_path: Optional[str] = None):
        auto_primary_db, auto_mirror_db, auto_model = get_default_paths()
        self.db_path = db_path or auto_primary_db
        self.mirror_db_path = auto_mirror_db if not db_path else None
        self.model_path = model_path or auto_model
        self._model = None
        self._init_db(self.db_path)
        if self.mirror_db_path and _is_db_accessible(self.mirror_db_path):
            self._init_db(self.mirror_db_path)

    def _init_db(self, target_path: str):
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with sqlite3.connect(target_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_vectors (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        ref_id TEXT NOT NULL,
                        title TEXT,
                        content TEXT NOT NULL,
                        tags TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        vector BLOB NOT NULL
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON knowledge_vectors(source)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON knowledge_vectors(created_at)")
                conn.commit()
        except Exception as e:
            logger.debug(f"初始化数据库 {target_path} 异常: {e}")

    @property
    def model(self):
        """懒加载模型，避免服务冷启动时阻塞"""
        if self._model is None:
            t0 = time.time()
            logger.info(f"⏳ 正在加载本地 BGE 向量模型: {self.model_path} ...")
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_path)
            logger.info(f"✅ BGE 本地向量模型就绪，耗时: {time.time() - t0:.2f}s")
        return self._model

    def encode(self, text: str) -> np.ndarray:
        """计算文本向量并做 L2 归一化 (使得余弦相似度等价于点积)"""
        emb = self.model.encode(text, normalize_embeddings=True)
        return np.asarray(emb, dtype=np.float32)

    def _write_single_db(self, path: str, params: tuple) -> bool:
        try:
            with sqlite3.connect(path, timeout=3.0) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO knowledge_vectors 
                    (id, source, ref_id, title, content, tags, created_at, updated_at, vector)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, params)
                conn.commit()
            return True
        except Exception as e:
            logger.debug(f"写入 {path} 异常: {e}")
            return False

    def add_record(self, record_id: str, source: str, ref_id: str, content: str,
                   title: str = "", tags: str = "", created_at: str = None) -> bool:
        """增量写入一条笔记/随想的向量记录 (自动执行双写同步)"""
        content_clean = content.strip()
        if len(content_clean) < 5:
            return False  # 忽略极短或无意义内容

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not created_at:
            created_at = now_str

        try:
            vec = self.encode(content_clean)
            vec_blob = vec.tobytes()
            params = (record_id, source, ref_id, title, content_clean, tags, created_at, now_str, vec_blob)

            success = self._write_single_db(self.db_path, params)
            if self.mirror_db_path:
                self._write_single_db(self.mirror_db_path, params)

            if success:
                logger.info(f"💾 [向量已入库] ID: {record_id} ({source}) | 长度: {len(content_clean)}字")
                return True
            return False
        except Exception as e:
            logger.error(f"写入向量数据库异常: {e}")
            return False

    def search_similar(self, query_text: str, top_k: int = 3, min_similarity: float = 0.60,
                       filter_source: str = None, exclude_id: str = None) -> List[Dict]:
        """对知识库进行语义相似度检索"""
        query_clean = query_text.strip()
        if len(query_clean) < 4:
            return []

        try:
            q_vec = self.encode(query_clean)

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if filter_source:
                    cursor.execute("""
                        SELECT id, source, ref_id, title, content, tags, created_at, vector 
                        FROM knowledge_vectors WHERE source = ?
                    """, (filter_source,))
                else:
                    cursor.execute("""
                        SELECT id, source, ref_id, title, content, tags, created_at, vector 
                        FROM knowledge_vectors
                    """)
                rows = cursor.fetchall()

            if not rows:
                return []

            results = []
            for r in rows:
                rec_id = r[0]
                if exclude_id and rec_id == exclude_id:
                    continue

                v_bytes = r[7]
                doc_vec = np.frombuffer(v_bytes, dtype=np.float32)
                # L2 归一化后的点积即为余弦相似度
                score = float(np.dot(q_vec, doc_vec))

                if score >= min_similarity:
                    results.append({
                        "id": rec_id,
                        "source": r[1],
                        "ref_id": r[2],
                        "title": r[3] or "",
                        "content": r[4],
                        "tags": r[5] or "",
                        "created_at": r[6],
                        "score": score
                    })

            # 按相似度降序排列
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:top_k]
        except Exception as e:
            logger.error(f"向量相似度检索异常: {e}")
            return []

    search = search_similar

    def format_reflection_hint(self, current_text: str, exclude_id: str = None, threshold: float = 0.50) -> str:
        """若检索到历史高相似度记录或典籍，格式化为精炼美观的「跨时空智囊与灵感回响」"""
        # 通道 1: 典籍智囊通道 (书籍专线，阈值 0.50，古今概念跨越)
        book_sims = self.search_similar(current_text, top_k=1, min_similarity=0.50, filter_source="book")
        book_match = book_sims[0] if book_sims else None

        # 通道 2: 往日笔记通道 (思源/Obsidian，阈值 0.58，防近期回环冷冻 30 分钟)
        from datetime import datetime, timedelta
        cooldown_time = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

        all_sims = self.search_similar(current_text, top_k=6, min_similarity=0.58, exclude_id=exclude_id)
        note_match = None
        for s in all_sims:
            if s["source"] in ["siyuan", "obsidian"]:
                # 排除 30 分钟以内刚发的新点滴，避免刚才说的话被当成历史笔记回弹
                if s["source"] == "obsidian" or s["created_at"] < cooldown_time:
                    note_match = s
                    break

        if not book_match and not note_match:
            return ""

        sections = []

        # 1. 典籍智囊回响
        if book_match:
            b_pct = int(book_match["score"] * 100)
            b_raw = book_match["content"]
            b_title = book_match["title"] or "《谋略集成》"

            m_yuan = re.search(r"原文(.*?)(?:译文|白话|释义|评点|$)", b_raw, re.DOTALL)
            m_yi = re.search(r"(?:译文|白话|释义)(.*?)(?:评点|$)", b_raw, re.DOTALL)

            b_lines = [f"典籍：{b_title} ({b_pct}%)"]
            if m_yuan:
                yuan = m_yuan.group(1).strip().replace("\n", " ")
                if len(yuan) > 95:
                    yuan = yuan[:95] + "..."
                b_lines.append(f"原文：「{yuan}」")
            if m_yi:
                yi = m_yi.group(1).strip().replace("\n", " ")
                if len(yi) > 115:
                    yi = yi[:115] + "..."
                b_lines.append(f"释义：{yi}")
            if not m_yuan and not m_yi:
                snippet = b_raw.replace("【出处】", "").strip().replace("\n", " ")
                snippet = re.sub(r"^《.*?》·\s*\S+\s*", "", snippet)
                if len(snippet) > 120:
                    snippet = snippet[:120] + "..."
                b_lines.append(f"「{snippet}」")
            sections.append("\n".join(b_lines))

        # 2. 个人过往笔墨印证
        if note_match:
            n_pct = int(note_match["score"] * 100)
            n_date = note_match["created_at"][:10]
            n_source = "思源笔记" if note_match["source"] == "siyuan" else "Obsidian"
            n_title = f" ({note_match['title']})" if note_match.get("title") and note_match['title'] != "每日随想" else ""

            n_snippet = note_match["content"].replace("\n", " ").strip()
            n_snippet = re.sub(r"[\u200b-\u200f\ufeff]+", "", n_snippet)
            if len(n_snippet) > 110:
                n_snippet = n_snippet[:110] + "..."

            n_lines = [
                f"印证：{n_source}{n_title} · {n_date} ({n_pct}%)",
                f"「{n_snippet}」"
            ]
            sections.append("\n".join(n_lines))

        header = "💡 Deja Vu"
        body = "\n\n".join(sections)
        return f"{header}\n{body}"

    def count_records(self) -> int:
        """获取当前向量库包含的有效记录总数"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM knowledge_vectors")
                return cursor.fetchone()[0]
        except Exception:
            return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing VectorEngine...")
    engine = VectorEngine()
    print(f"DB Path in use: {engine.db_path}")
    print(f"Mirror DB Path: {engine.mirror_db_path}")
    print(f"Model Path in use: {engine.model_path}")
    print(f"Total records in active DB: {engine.count_records()}")
