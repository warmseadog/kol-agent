"""
agent.py — KOL Outreach Agent 编排入口

职责：
  1. 读取邮件
  2. 维护 thread 级别的业务与审计数据
  3. 调用 LangGraph 工作流生成回复与结构化状态
  4. 发送邮件并写回历史
"""

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.config import config
from app.database import (
    get_thread_messages,
    get_thread_state,
    has_graph_checkpoint,
    is_message_processed,
    mark_message_processed,
    save_thread_message,
)
from app.graph import get_agent_graph
from app.mail_service import fetch_unread_emails, send_reply

logger = logging.getLogger(__name__)


def _normalize_product(raw: dict[str, Any]) -> dict[str, Any] | None:
    required_fields = ("name", "description", "keywords", "store_name", "asin")
    if not all(raw.get(field) for field in required_fields):
        return None
    keywords = raw.get("keywords", [])
    if not isinstance(keywords, list):
        return None
    return {
        "name": str(raw["name"]).strip(),
        "description": str(raw["description"]).strip(),
        "keywords": [str(keyword).strip() for keyword in keywords if str(keyword).strip()],
        "store_name": str(raw["store_name"]).strip(),
        "asin": str(raw["asin"]).strip(),
    }


def _load_products() -> list[dict[str, Any]]:
    """加载新产品结构，遇到坏数据时跳过单条记录。"""
    path = Path(config.PRODUCTS_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    if not path.exists():
        logger.warning(f"⚠️ 产品库文件不存在: {path}，将不注入产品信息")
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        products = []
        for item in payload:
            normalized = _normalize_product(item)
            if normalized:
                products.append(normalized)
        logger.info(f"✅ 已加载产品 {len(products)} 条（新结构）")
        return products
    except Exception as e:
        logger.warning(f"⚠️ 产品库加载失败: {e}，将不注入产品信息")
        return []


_PRODUCTS: list[dict[str, Any]] = _load_products()


def _filter_products(text: str, products: list[dict[str, Any]], top_n: int = 5) -> list[dict[str, Any]]:
    """按关键词命中数筛选候选产品，若全无命中则返回前 top_n 条。"""
    if not products:
        return []
    needle = (text or "").lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for product in products:
        hits = sum(1 for kw in product.get("keywords", []) if kw.lower() in needle)
        scored.append((hits, product))
    scored.sort(key=lambda item: item[0], reverse=True)
    matched = [product for hits, product in scored if hits > 0]
    return (matched if matched else products)[:top_n]


def _thread_key(msg: dict[str, Any]) -> str:
    refs = msg.get("references", "").strip()
    if refs:
        return refs.split()[0]
    return msg.get("message_id", msg["uid"])


def _build_langchain_history(thread_id: str) -> list[HumanMessage | AIMessage]:
    rows = get_thread_messages(thread_id, limit=config.MAX_THREAD_MESSAGES)
    history: list[HumanMessage | AIMessage] = []
    for row in rows:
        content = f"Subject: {row.get('subject', '')}\n\n{row.get('body', '')}"
        if row["role"] == "our":
            history.append(AIMessage(content=content))
        else:
            history.append(HumanMessage(content=content))
    return history


def _build_graph_input(thread_id: str, msg: dict[str, Any], db_state: dict[str, Any] | None) -> dict[str, Any]:
    has_checkpoint = has_graph_checkpoint(thread_id)
    if has_checkpoint:
        messages = [HumanMessage(content=f"Subject: {msg['subject']}\n\n{msg['body']}")]
    else:
        messages = _build_langchain_history(thread_id)

    return {
        "thread_id": thread_id,
        "messages": messages,
        "current_stage": (db_state or {}).get("status", "线索识别"),
        "extracted_info": (db_state or {}).get("extracted_info", {}),
        "sentiment": None,
        "latest_email": msg,
        "candidate_products": _filter_products(msg.get("body", ""), _PRODUCTS),
        "reply_body": "",
        "db_stage": (db_state or {}).get("current_stage", 1),
        "db_status": (db_state or {}).get("status", "线索识别"),
        "route_intent": "unknown",
        "negotiation_outcome": "not_applicable",
    }


def _handle_one_email(msg: dict[str, Any]) -> bool:
    thread_key = _thread_key(msg)
    kol_email = msg["from_email"]
    kol_name = msg["from_name"]
    message_id = msg["message_id"] or msg["uid"]

    logger.info("─" * 50)
    logger.info(f"📩 来自: {kol_name} <{kol_email}>")
    logger.info(f"   主题: {msg['subject']}")
    logger.info(f"   Thread key: {thread_key[:60]}")

    db_state = get_thread_state(thread_key)

    save_thread_message(
        thread_id=thread_key,
        message_id=message_id,
        role="kol",
        subject=msg["subject"],
        body=msg["body"][:config.BODY_EXCERPT_LENGTH],
    )

    graph_input = _build_graph_input(thread_key, msg, db_state)
    candidate_products = graph_input.get("candidate_products", [])
    if candidate_products:
        logger.info("🛍️  候选产品: %s", ", ".join(product["name"] for product in candidate_products))

    logger.info("🧠 调用 LangGraph 工作流...")
    try:
        graph = get_agent_graph()
        result = graph.invoke(
            graph_input,
            config={"configurable": {"thread_id": thread_key}},
        )
        reply_body = (result.get("reply_body") or "").strip()
        current_stage = result.get("current_stage", "")
        logger.info(f"🎯 Graph 阶段: {current_stage}")
    except Exception as e:
        logger.error(f"❌ LangGraph 处理失败: {e}", exc_info=True)
        return False

    if not reply_body:
        logger.warning("⚠️ Graph 未生成可发送回复，跳过发信")
        return False

    logger.info(f"📝 回复预览: {reply_body[:100].replace(chr(10), ' ')}...")
    success = send_reply(original=msg, reply_body=reply_body)
    if not success:
        return False

    save_thread_message(
        thread_id=thread_key,
        message_id=f"our-reply-to-{message_id}",
        role="our",
        subject=f"Re: {msg['subject']}",
        body=reply_body[:config.BODY_EXCERPT_LENGTH],
    )
    logger.info("✅ 已完成 LangGraph 编排与邮件回复")
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
