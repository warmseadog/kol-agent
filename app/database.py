"""
database.py — SQLite 持久化层

负责：
  1. 记录已处理的邮件 Message-ID，防止重复回复（processed_messages）
  2. 存储每个 KOL 会话（Thread）的合作阶段和元数据（kol_threads）
  3. 存储每个 Thread 的完整多轮对话历史，供 LLM 上下文使用（thread_messages）
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

    # 多轮对话历史表：按 thread_id 存储每封邮件，供 LLM 上下文使用
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS thread_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id  TEXT NOT NULL,
            message_id TEXT NOT NULL,
            role       TEXT NOT NULL CHECK(role IN ('kol', 'our')),
            subject    TEXT,
            body       TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(message_id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_thread_messages_thread_id
        ON thread_messages (thread_id, created_at)
    """)

    conn.commit()
    conn.close()
    logger.info("✅ 数据库初始化完成")


# ─── processed_messages 操作 ──────────────────────────────────────────────────

def is_message_processed(message_id: str) -> bool:
    """检查某条邮件消息是否已被处理过"""
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


def list_processed_messages(limit: int = 100) -> list[dict]:
    """
    列出最近处理的消息记录，关联 kol_threads 展示发件人信息。
    用于仪表盘「处理流水」视图。
    """
    conn = _get_conn()
    rows = conn.execute("""
        SELECT
            pm.message_id,
            pm.thread_id,
            pm.processed_at,
            kt.kol_email,
            kt.kol_name,
            tm.subject,
            SUBSTR(tm.body, 1, 120) AS body_excerpt
        FROM processed_messages pm
        LEFT JOIN kol_threads kt ON pm.thread_id = kt.thread_id
        LEFT JOIN thread_messages tm
            ON pm.message_id = tm.message_id AND tm.role = 'kol'
        ORDER BY pm.processed_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def delete_thread(thread_id: str) -> int:
    """
    删除指定 Thread 的全部数据（三张表联动）。
    返回受影响的总行数。
    """
    conn = _get_conn()
    deleted = 0
    deleted += conn.execute("DELETE FROM thread_messages   WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM processed_messages WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM kol_threads        WHERE thread_id = ?", (thread_id,)).rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_all_data() -> dict:
    """清空全部数据（三张表），用于测试重置。返回各表删除行数。"""
    conn = _get_conn()
    tm  = conn.execute("DELETE FROM thread_messages").rowcount
    pm  = conn.execute("DELETE FROM processed_messages").rowcount
    kt  = conn.execute("DELETE FROM kol_threads").rowcount
    conn.commit()
    conn.close()
    return {"thread_messages": tm, "processed_messages": pm, "kol_threads": kt}


# ─── thread_messages 操作 ─────────────────────────────────────────────────────

def save_thread_message(
    thread_id: str,
    message_id: str,
    role: str,
    subject: str,
    body: str,
    created_at: str | None = None,
) -> None:
    """
    将单封邮件写入多轮对话历史表。

    - role 只能为 'kol'（KOL 来信）或 'our'（我方发出的回复）
    - UNIQUE(message_id) 保证同一封邮件不会重复写入
    - body 入库前应已截断，避免无限增长

    写入时机：
      - KOL 来信：在调用 LLM 之前写入（role=kol）
      - 我方回复：仅在 send_reply 成功后写入（role=our）
    """
    if role not in ("kol", "our"):
        raise ValueError(f"role 必须为 'kol' 或 'our'，实际为: {role!r}")

    ts = created_at or datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO thread_messages
               (thread_id, message_id, role, subject, body, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (thread_id, message_id, role, subject or "", body or "", ts)
    )
    conn.commit()
    conn.close()


def get_thread_messages(thread_id: str, limit: int | None = None) -> list[dict]:
    """
    按时间从旧到新读取指定 Thread 的对话历史，最多返回 limit 条。

    实现「取最近 N 条，然后按时间正序排列」，确保 LLM 看到连贯历史。
    """
    effective_limit = limit or config.MAX_THREAD_MESSAGES
    conn = _get_conn()
    rows = conn.execute("""
        SELECT * FROM (
            SELECT thread_id, message_id, role, subject, body, created_at
            FROM thread_messages
            WHERE thread_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        )
        ORDER BY created_at ASC
    """, (thread_id, effective_limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
