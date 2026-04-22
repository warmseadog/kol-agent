"""
database.py — SQLite 持久化层

负责：
  1. 记录已处理的 Gmail Message ID，防止重复回复
  2. 存储每个 KOL 会话（Thread）的合作阶段和元数据
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path

from app.config import config

logger = logging.getLogger(__name__)


def _get_conn() -> sqlite3.Connection:
    """创建并返回数据库连接，启用 Row 工厂便于字典访问"""
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化数据库，建表（幂等操作，可重复调用）"""
    conn = _get_conn()
    cursor = conn.cursor()

    # 已处理消息表：防止对同一封邮件重复触发回复
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id  TEXT PRIMARY KEY,
            thread_id   TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
    """)

    # KOL 会话状态表：记录每个 Thread 的合作进展
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kol_threads (
            thread_id       TEXT PRIMARY KEY,
            kol_email       TEXT NOT NULL,
            kol_name        TEXT,
            current_stage   INTEGER NOT NULL DEFAULT 1,
            last_message_id TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("✅ 数据库初始化完成")


# ─── processed_messages 操作 ──────────────────────────────────────────────────

def is_message_processed(message_id: str) -> bool:
    """检查某条 Gmail 消息是否已被处理过"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    conn.close()
    return row is not None


def mark_message_processed(message_id: str, thread_id: str) -> None:
    """将消息标记为已处理"""
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id, thread_id, processed_at) VALUES (?, ?, ?)",
        (message_id, thread_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ─── kol_threads 操作 ─────────────────────────────────────────────────────────

def get_thread_state(thread_id: str) -> dict | None:
    """获取指定 Thread 的 KOL 会话状态，不存在则返回 None"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM kol_threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_thread_state(
    thread_id: str,
    kol_email: str,
    kol_name: str,
    stage: int,
    last_message_id: str,
    notes: str = ""
) -> None:
    """
    创建或更新 KOL 会话状态（UPSERT）。
    首次插入时记录 created_at；更新时只修改可变字段。
    """
    conn = _get_conn()
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO kol_threads
            (thread_id, kol_email, kol_name, current_stage, last_message_id, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            kol_name        = excluded.kol_name,
            current_stage   = excluded.current_stage,
            last_message_id = excluded.last_message_id,
            notes           = excluded.notes,
            updated_at      = excluded.updated_at
    """, (thread_id, kol_email, kol_name, stage, last_message_id, notes, now, now))
    conn.commit()
    conn.close()


def list_all_threads() -> list[dict]:
    """列出所有 KOL 会话（用于 Dashboard 展示）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM kol_threads ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
