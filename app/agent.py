"""
agent.py — KOL Outreach Agent 核心状态机

将 mail_service、llm_service、database 三层整合，
实现"收邮件 → 判断阶段 → 生成回复 → 发送 → 更新状态"完整闭环。

多轮会话记忆：
  - 每个 Thread（由 _thread_key 唯一标识）维护独立的对话历史
  - KOL 来信在调用 LLM 前写入 thread_messages（role=kol）
  - 我方回复仅在 send_reply 成功后写入 thread_messages（role=our）
  - 不同 thread_id 的历史严格隔离，绝不混拼
"""

import json
import logging
from pathlib import Path

from app.config import config
from app.database import (
    is_message_processed,
    mark_message_processed,
    get_thread_state,
    upsert_thread_state,
    save_thread_message,
    get_thread_messages,
)
from app.mail_service import fetch_unread_emails, send_reply
from app.llm_service import detect_stage, generate_kol_reply

logger = logging.getLogger(__name__)


# ─── 产品库加载 ────────────────────────────────────────────────────────────────

def _load_products() -> list[dict]:
    """加载本地产品库 JSON，加载失败时返回空列表（降级处理）"""
    path = Path(config.PRODUCTS_PATH)
    if not path.exists():
        logger.warning(f"⚠️ 产品库文件不存在: {path}，将不注入产品信息")
        return []
    try:
        with open(path, encoding="utf-8") as f:
            products = json.load(f)
        logger.debug(f"✅ 已加载 {len(products)} 条产品")
        return products
    except Exception as e:
        logger.warning(f"⚠️ 产品库加载失败: {e}，将不注入产品信息")
        return []


_PRODUCTS: list[dict] = _load_products()


def _filter_products(text: str, products: list[dict], top_n: int = 5) -> list[dict]:
    """
    简单关键词匹配，从产品库中筛选与来信相关的候选产品。
    若无匹配则返回全量（最多 top_n 条），确保模型始终有产品可参考。
    """
    if not products:
        return []
    needle = text.lower()
    scored: list[tuple[int, dict]] = []
    for p in products:
        kws = p.get("keywords", [])
        hits = sum(1 for kw in kws if kw.lower() in needle)
        scored.append((hits, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    matched = [p for hits, p in scored if hits > 0]
    return (matched if matched else products)[:top_n]


# ─── Thread Key ────────────────────────────────────────────────────────────────

def _thread_key(msg: dict) -> str:
    """
    用于唯一标识一个邮件会话（Thread）的 key。
    优先使用 References 链的第一个 Message-ID（最早的那封），
    其次使用邮件自身的 Message-ID，保证同一会话始终用同一个 key。
    """
    refs = msg.get("references", "").strip()
    if refs:
        # References 形如 "<id1> <id2> ..."，取第一个作为会话根 ID
        return refs.split()[0]
    return msg.get("message_id", msg["uid"])


# ─── Thread 历史构建 ───────────────────────────────────────────────────────────

def _build_thread_history(thread_id: str) -> list[dict]:
    """
    从 thread_messages 表按时间从旧到新读取该线程的完整对话历史，
    转换为 llm_service 兼容的格式（含 subject、body、is_mine）。

    调用前须确保当前 KOL 来信已写入 DB（role=kol），
    这样此函数的输出天然包含最新来信，不会重复。
    """
    rows = get_thread_messages(thread_id, limit=config.MAX_THREAD_MESSAGES)
    history = []
    for row in rows:
        history.append({
            "subject":  row["subject"] or "",
            "body":     row["body"] or "",
            "is_mine":  row["role"] == "our",
        })
    return history


# ─── 单封邮件处理 ──────────────────────────────────────────────────────────────

def _handle_one_email(msg: dict) -> bool:
    """
    处理单封 KOL 来信，完整执行：
      1. 确定 Thread key 并查询数据库当前阶段
      2. 将 KOL 来信写入 thread_messages（role=kol，去重安全）
      3. 从 DB 读取完整多轮历史，构造 thread_history
      4. LLM 判断实际阶段（结合多轮历史）
      5. 关键词筛选候选产品
      6. 生成对应阶段的高情商回复（含产品上下文）
      7. SMTP 发送（含防 Spam 头部）
      8. 发送成功后将我方回复写入 thread_messages（role=our）
      9. 更新数据库阶段状态

    Returns:
        bool: 是否成功完成回复
    """
    thread_key = _thread_key(msg)
    kol_email  = msg["from_email"]
    kol_name   = msg["from_name"]
    message_id = msg["message_id"] or msg["uid"]

    logger.info("─" * 50)
    logger.info(f"📩 来自: {kol_name} <{kol_email}>")
    logger.info(f"   主题: {msg['subject']}")
    logger.info(f"   Thread key: {thread_key[:60]}")

    # ── 1. 读取数据库阶段 ─────────────────────────────────────────────────
    db_state = get_thread_state(thread_key)
    db_stage  = db_state["current_stage"] if db_state else 1

    # ── 2. 将 KOL 来信写入 thread_messages（在调用 LLM 之前） ─────────────
    body_excerpt = msg["body"][:config.BODY_EXCERPT_LENGTH]
    save_thread_message(
        thread_id  = thread_key,
        message_id = message_id,
        role       = "kol",
        subject    = msg["subject"],
        body       = body_excerpt,
    )

    # ── 3. 从 DB 读取完整多轮历史（已包含刚写入的当前来信） ────────────────
    thread_history = _build_thread_history(thread_key)
    logger.info(f"📜 多轮历史: {len(thread_history)} 条（含本封）")

    # ── 4. LLM 判断实际阶段 ───────────────────────────────────────────────
    logger.info(f"🧠 分析合作阶段（数据库基准: 阶段 {db_stage}）...")
    stage_result  = detect_stage(thread_history, db_stage)
    current_stage = stage_result["stage"]
    logger.info(f"🎯 判断结果: 阶段 {current_stage} — {stage_result['reasoning']}")

    # ── 5. 关键词筛选候选产品 ─────────────────────────────────────────────
    candidate_products = _filter_products(msg["body"], _PRODUCTS)
    if candidate_products:
        names = [p["name"] for p in candidate_products]
        logger.info(f"🛍️  候选产品({len(candidate_products)}): {', '.join(names)}")

    # ── 6. 生成回复 ───────────────────────────────────────────────────────
    logger.info(f"✍️  生成阶段 {current_stage} 回复...")
    try:
        reply_body = generate_kol_reply(
            thread_history     = thread_history,
            kol_name           = kol_name,
            kol_email          = kol_email,
            stage              = current_stage,
            latest_message     = msg["body"],
            candidate_products = candidate_products or None,
        )
        logger.info(f"📝 回复预览: {reply_body[:80].replace(chr(10), ' ')}...")
    except Exception as e:
        logger.error(f"❌ 回复生成失败: {e}")
        return False

    # ── 7. 发送（含防 Spam 头部） ─────────────────────────────────────────
    success = send_reply(original=msg, reply_body=reply_body)
    if not success:
        return False

    # ── 8. 发送成功后写入我方回复历史（唯一可信来源）────────────────────────
    our_pseudo_id = f"our-reply-to-{message_id}"
    save_thread_message(
        thread_id  = thread_key,
        message_id = our_pseudo_id,
        role       = "our",
        subject    = f"Re: {msg['subject']}",
        body       = reply_body[:config.BODY_EXCERPT_LENGTH],
    )

    # ── 9. 更新数据库，阶段自动推进（最高到 4） ───────────────────────────
    next_stage = min(current_stage + 1, 4)
    upsert_thread_state(
        thread_id       = thread_key,
        kol_email       = kol_email,
        kol_name        = kol_name,
        stage           = next_stage,
        last_message_id = message_id,
        notes           = f"阶段{current_stage}已回复 | {stage_result['reasoning']}",
    )
    logger.info(f"✅ 完成！阶段 {current_stage} → {next_stage}")
    return True


# ─── 主轮询 ────────────────────────────────────────────────────────────────────

def run_check_cycle() -> dict:
    """
    执行一次完整的邮件检查轮询。

    Returns:
        dict: {"total": int, "processed": int, "success": int}
    """
    logger.info("=" * 50)
    logger.info("🔄 开始新一轮邮件检查")

    emails = fetch_unread_emails(limit=config.MAX_EMAILS_PER_CYCLE)

    if not emails:
        logger.info("😴 暂无未读邮件")
        return {"total": 0, "processed": 0, "success": 0}

    processed = success = 0

    for msg in emails:
        uid = msg["uid"]

        # 用 RFC Message-ID 作去重 key（更稳定），没有则退回 uid
        dedup_key = msg["message_id"] or uid

        if is_message_processed(dedup_key):
            logger.debug(f"⏭️  跳过已处理: uid={uid}")
            continue

        # 跳过自己发出的邮件，防止自回复死循环
        if msg["from_email"].lower() == config.EMAIL_ADDRESS.lower():
            mark_message_processed(dedup_key, _thread_key(msg))
            continue

        processed += 1
        ok = _handle_one_email(msg)
        if ok:
            success += 1

        # 无论成功与否，标记已处理，防止下次重复
        mark_message_processed(dedup_key, _thread_key(msg))

    if processed:
        logger.info(f"🎉 本轮完成: 处理 {processed} 封，成功回复 {success} 封")
    else:
        logger.info("😴 无新的 KOL 来信")

    logger.info("=" * 50)
    return {"total": len(emails), "processed": processed, "success": success}
