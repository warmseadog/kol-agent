"""
agent.py — KOL Outreach Agent 核心状态机

将 mail_service、llm_service、database 三层整合，
实现"收邮件 → 判断阶段 → 生成回复 → 发送 → 更新状态"完整闭环。
"""

import logging
from app.config import config
from app.database import (
    is_message_processed,
    mark_message_processed,
    get_thread_state,
    upsert_thread_state,
)
from app.mail_service import fetch_unread_emails, send_reply
from app.llm_service import detect_stage, generate_kol_reply

logger = logging.getLogger(__name__)


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


def _build_thread_history_from_single(msg: dict) -> list[dict]:
    """
    当只有单封邮件时，构造供 LLM 阅读的历史列表（仅含这一封）。
    完整历史由 LLM 通过上下文推断（后续可升级为拉取完整 Thread）。
    """
    return [{
        "subject":  msg["subject"],
        "from":     msg["from_raw"],
        "body":     msg["body"],
        "is_mine":  False,
    }]


def _handle_one_email(msg: dict) -> bool:
    """
    处理单封 KOL 来信，完整执行：
      1. 确定 Thread key 并查询数据库当前阶段
      2. LLM 判断实际阶段（结合历史）
      3. 生成对应阶段的高情商回复
      4. SMTP 发送（含防 Spam 头部）
      5. 更新数据库阶段状态

    Returns:
        bool: 是否成功完成回复
    """
    thread_key  = _thread_key(msg)
    kol_email   = msg["from_email"]
    kol_name    = msg["from_name"]

    logger.info("─" * 50)
    logger.info(f"📩 来自: {kol_name} <{kol_email}>")
    logger.info(f"   主题: {msg['subject']}")
    logger.info(f"   Thread key: {thread_key[:60]}")

    # ── 1. 读取数据库阶段 ─────────────────────────────────────────────────
    db_state = get_thread_state(thread_key)
    db_stage  = db_state["current_stage"] if db_state else 1

    # ── 2. LLM 判断实际阶段 ───────────────────────────────────────────────
    thread_history = _build_thread_history_from_single(msg)
    logger.info(f"🧠 分析合作阶段（数据库基准: 阶段 {db_stage}）...")
    stage_result   = detect_stage(thread_history, db_stage)
    current_stage  = stage_result["stage"]
    logger.info(f"🎯 判断结果: 阶段 {current_stage} — {stage_result['reasoning']}")

    # ── 3. 生成回复 ───────────────────────────────────────────────────────
    logger.info(f"✍️  生成阶段 {current_stage} 回复...")
    try:
        reply_body = generate_kol_reply(
            thread_history=thread_history,
            kol_name=kol_name,
            kol_email=kol_email,
            stage=current_stage,
            latest_message=msg["body"],
        )
        logger.info(f"📝 回复预览: {reply_body[:80].replace(chr(10), ' ')}...")
    except Exception as e:
        logger.error(f"❌ 回复生成失败: {e}")
        return False

    # ── 4. 发送（含防 Spam 头部） ─────────────────────────────────────────
    success = send_reply(original=msg, reply_body=reply_body)
    if not success:
        return False

    # ── 5. 更新数据库，阶段自动推进（最高到 4） ───────────────────────────
    next_stage = min(current_stage + 1, 4)
    upsert_thread_state(
        thread_id=thread_key,
        kol_email=kol_email,
        kol_name=kol_name,
        stage=next_stage,
        last_message_id=msg["message_id"],
        notes=f"阶段{current_stage}已回复 | {stage_result['reasoning']}",
    )
    logger.info(f"✅ 完成！阶段 {current_stage} → {next_stage}")
    return True


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
