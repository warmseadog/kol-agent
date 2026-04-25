"""
database.py — SQLite 持久化层

职责：
  1. 记录已处理邮件，防止重复回复
  2. 维护业务线程状态，供 Dashboard 与业务查询使用
  3. 保存完整邮件历史，供审计与冷启动迁移使用
  4. 复用同一个 SQLite 文件作为 LangGraph SqliteSaver 的 checkpoint 存储
  5. 模拟生成本地工单 CSV，供人工发货/售后协作
"""

import csv
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import config

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORK_ORDER_DIR = _PROJECT_ROOT / "data" / "work_orders"
_CHECKPOINTER_LOCK = threading.Lock()
_CHECKPOINTER_CONN: sqlite3.Connection | None = None
_CHECKPOINTER: SqliteSaver | None = None


def _db_path() -> Path:
    return (_PROJECT_ROOT / config.DB_FILE).resolve() if not Path(config.DB_FILE).is_absolute() else Path(config.DB_FILE)


def _get_conn() -> sqlite3.Connection:
    """创建并返回数据库连接，启用 Row 工厂便于字典访问"""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    """初始化数据库，建表并做轻量 schema migration（幂等）"""
    conn = _get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id   TEXT PRIMARY KEY,
            thread_id    TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kol_threads (
            thread_id       TEXT PRIMARY KEY,
            kol_email       TEXT NOT NULL,
            kol_name        TEXT,
            current_stage   INTEGER NOT NULL DEFAULT 1,
            status          TEXT NOT NULL DEFAULT '线索识别',
            last_message_id TEXT,
            notes           TEXT,
            extracted_info  TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
    """)

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

    _ensure_column(conn, "kol_threads", "status", "status TEXT NOT NULL DEFAULT '线索识别'")
    _ensure_column(conn, "kol_threads", "extracted_info", "extracted_info TEXT NOT NULL DEFAULT '{}'")

    conn.commit()
    conn.close()

    # 预热 LangGraph checkpoint 表，确保与业务表共用同一个 DB 文件。
    get_checkpointer()
    _WORK_ORDER_DIR.mkdir(parents=True, exist_ok=True)
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
    if not row:
        return None

    data = dict(row)
    raw = data.get("extracted_info") or "{}"
    try:
        data["extracted_info"] = json.loads(raw)
    except json.JSONDecodeError:
        data["extracted_info"] = {}
    return data


def upsert_thread_state(
    thread_id: str,
    kol_email: str,
    kol_name: str,
    stage: int,
    last_message_id: str,
    notes: str = "",
    status: str = "线索识别",
    extracted_info: dict[str, Any] | None = None,
) -> None:
    """
    创建或更新 KOL 会话状态（UPSERT）。
    首次插入时记录 created_at；更新时只修改可变字段。
    """
    conn = _get_conn()
    now = datetime.now().isoformat()
    serialized_info = json.dumps(extracted_info or {}, ensure_ascii=False)
    conn.execute("""
        INSERT INTO kol_threads
            (thread_id, kol_email, kol_name, current_stage, status, last_message_id, notes, extracted_info, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            kol_name        = excluded.kol_name,
            current_stage   = excluded.current_stage,
            status          = excluded.status,
            last_message_id = excluded.last_message_id,
            notes           = excluded.notes,
            extracted_info  = excluded.extracted_info,
            updated_at      = excluded.updated_at
    """, (
        thread_id,
        kol_email,
        kol_name,
        stage,
        status,
        last_message_id,
        notes,
        serialized_info,
        now,
        now,
    ))
    conn.commit()
    conn.close()


def list_all_threads() -> list[dict]:
    """列出所有 KOL 会话（用于 Dashboard 展示）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM kol_threads ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        data = dict(row)
        raw = data.get("extracted_info") or "{}"
        try:
            data["extracted_info"] = json.loads(raw)
        except json.JSONDecodeError:
            data["extracted_info"] = {}
        results.append(data)
    return results


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
    deleted += conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,)).rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_all_data() -> dict:
    """清空全部数据（三张表），用于测试重置。返回各表删除行数。"""
    conn = _get_conn()
    tm  = conn.execute("DELETE FROM thread_messages").rowcount
    pm  = conn.execute("DELETE FROM processed_messages").rowcount
    kt  = conn.execute("DELETE FROM kol_threads").rowcount
    cp  = conn.execute("DELETE FROM checkpoints").rowcount
    wr  = conn.execute("DELETE FROM writes").rowcount
    conn.commit()
    conn.close()
    return {
        "thread_messages": tm,
        "processed_messages": pm,
        "kol_threads": kt,
        "checkpoints": cp,
        "checkpoint_writes": wr,
    }


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


def get_checkpointer() -> SqliteSaver:
    """返回与业务数据库共用同一 SQLite 文件的 LangGraph checkpointer。"""
    global _CHECKPOINTER_CONN, _CHECKPOINTER

    with _CHECKPOINTER_LOCK:
        if _CHECKPOINTER is None:
            db_path = _db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _CHECKPOINTER_CONN = sqlite3.connect(db_path, check_same_thread=False)
            _CHECKPOINTER_CONN.row_factory = sqlite3.Row
            _CHECKPOINTER = SqliteSaver(_CHECKPOINTER_CONN)
            _CHECKPOINTER.setup()
        return _CHECKPOINTER


def has_graph_checkpoint(thread_id: str) -> bool:
    """判断指定 thread 是否已有 LangGraph checkpoint。"""
    conn = _get_conn()
    exists = conn.execute(
        "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
        (thread_id,),
    ).fetchone()
    conn.close()
    return exists is not None


def _append_csv_row(file_path: Path, headers: list[str], row: dict[str, Any]) -> str:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    need_header = not file_path.exists()
    with file_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if need_header:
            writer.writeheader()
        writer.writerow(row)
    return str(file_path)


def create_shipping_work_order(
    thread_id: str,
    kol_email: str,
    kol_name: str,
    order_info: dict[str, Any],
) -> str:
    file_path = _WORK_ORDER_DIR / "shipping_orders.csv"
    headers = [
        "created_at",
        "thread_id",
        "kol_email",
        "kol_name",
        "recipient_name",
        "phone",
        "address",
        "product_name",
        "product_asin",
        "store_name",
        "notes",
    ]
    row = {
        "created_at": datetime.now().isoformat(),
        "thread_id": thread_id,
        "kol_email": kol_email,
        "kol_name": kol_name,
        "recipient_name": order_info.get("recipient_name", ""),
        "phone": order_info.get("phone", ""),
        "address": order_info.get("address", ""),
        "product_name": order_info.get("product_name", ""),
        "product_asin": order_info.get("asin", ""),
        "store_name": order_info.get("store_name", ""),
        "notes": order_info.get("notes", ""),
    }
    return _append_csv_row(file_path, headers, row)


def create_standard_work_order(
    thread_id: str,
    kol_email: str,
    kol_name: str,
    value_info: dict[str, Any],
) -> str:
    file_path = _WORK_ORDER_DIR / "standard_orders.csv"
    headers = [
        "created_at",
        "thread_id",
        "kol_email",
        "kol_name",
        "payment_account",
        "payment_method",
        "review_screenshot_verified",
        "review_link",
        "notes",
    ]
    row = {
        "created_at": datetime.now().isoformat(),
        "thread_id": thread_id,
        "kol_email": kol_email,
        "kol_name": kol_name,
        "payment_account": value_info.get("payment_account", ""),
        "payment_method": value_info.get("payment_method", ""),
        "review_screenshot_verified": str(value_info.get("review_screenshot_verified", False)),
        "review_link": value_info.get("review_link", ""),
        "notes": value_info.get("notes", ""),
    }
    return _append_csv_row(file_path, headers, row)


def create_crisis_work_order(
    thread_id: str,
    kol_email: str,
    kol_name: str,
    refund_info: dict[str, Any],
) -> str:
    file_path = _WORK_ORDER_DIR / "crisis_orders.csv"
    headers = [
        "created_at",
        "thread_id",
        "kol_email",
        "kol_name",
        "order_number",
        "refund_account",
        "refund_method",
        "issue_summary",
        "notes",
    ]
    row = {
        "created_at": datetime.now().isoformat(),
        "thread_id": thread_id,
        "kol_email": kol_email,
        "kol_name": kol_name,
        "order_number": refund_info.get("order_number", ""),
        "refund_account": refund_info.get("refund_account", ""),
        "refund_method": refund_info.get("refund_method", ""),
        "issue_summary": refund_info.get("issue_summary", ""),
        "notes": refund_info.get("notes", ""),
    }
    return _append_csv_row(file_path, headers, row)
