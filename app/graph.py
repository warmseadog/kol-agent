"""
graph.py — 基于 LangGraph 的 KOL 邮件工作流
"""

import logging
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.database import (
    create_crisis_work_order,
    create_shipping_work_order,
    create_standard_work_order,
    get_checkpointer,
    upsert_thread_state,
)
from app.llm_service import (
    analyze_sentiment,
    build_product_recommendation_strategy,
    build_sop_crisis_strategy,
    build_sop_perfect_strategy,
    extract_order_info,
    extract_refund_info,
    extract_value_info,
    generate_order_collection_reply,
    generate_refund_collection_reply,
    generate_sop_salvage_reply,
    generate_value_collection_reply,
    recognize_intent,
)

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    thread_id: str
    messages: Annotated[list[BaseMessage], add_messages]
    current_stage: str
    extracted_info: dict[str, Any]
    sentiment: Literal["positive", "mild_negative", "severe_negative"] | None
    latest_email: dict[str, Any]
    candidate_products: list[dict[str, Any]]
    reply_body: str
    db_stage: int
    db_status: str
    route_intent: str
    negotiation_outcome: Literal["accepted_and_satisfied", "still_unhappy", "not_applicable"]


def _merge_extracted(state: AgentState, updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(state.get("extracted_info", {}))
    merged.update(updates)
    return merged


def _upsert_from_state(
    state: AgentState,
    *,
    stage: int,
    status: str,
    notes: str,
    extracted_info: dict[str, Any],
) -> None:
    latest_email = state["latest_email"]
    upsert_thread_state(
        thread_id=state["thread_id"],
        kol_email=latest_email["from_email"],
        kol_name=latest_email.get("from_name", ""),
        stage=stage,
        last_message_id=latest_email.get("message_id") or latest_email["uid"],
        notes=notes,
        status=status,
        extracted_info=extracted_info,
    )


def intent_recognition_node(state: AgentState) -> dict[str, Any]:
    result = recognize_intent(
        messages=state["messages"],
        latest_email=state["latest_email"],
        db_status=state.get("db_status", "线索识别"),
        db_stage=state.get("db_stage", 1),
    )
    extracted = _merge_extracted(state, {
        "intent_reasoning": result["reasoning"],
        "identity_verified": result["identity_verified"],
    })
    return {
        "current_stage": result["current_stage"],
        "route_intent": result["intent"],
        "extracted_info": extracted,
    }


def product_recommendation_node(state: AgentState) -> dict[str, Any]:
    strategy = build_product_recommendation_strategy(
        messages=state["messages"],
        latest_email=state["latest_email"],
        candidate_products=state.get("candidate_products") or [],
    )
    products = [
        {
            "name": product.get("name", ""),
            "store_name": product.get("store_name", ""),
            "asin": product.get("asin", ""),
        }
        for product in (state.get("candidate_products") or [])[:3]
    ]
    extracted = _merge_extracted(state, {
        "recommended_products": products,
        "recommendation_strategy": strategy,
    })
    _upsert_from_state(
        state,
        stage=1,
        status="推荐产品",
        notes="已进入产品推荐与合作引导阶段",
        extracted_info=extracted,
    )
    return {
        "current_stage": "product_recommendation",
        "extracted_info": extracted,
    }


def order_info_extraction_node(state: AgentState) -> dict[str, Any]:
    order_info = extract_order_info(
        messages=state["messages"],
        latest_email=state["latest_email"],
        candidate_products=state.get("candidate_products") or [],
    )
    recommendation_strategy = state.get("extracted_info", {}).get("recommendation_strategy", "")
    reply_body = generate_order_collection_reply(
        messages=state["messages"],
        latest_email=state["latest_email"],
        recommendation_strategy=recommendation_strategy,
        order_info=order_info,
    )
    extracted = _merge_extracted(state, {"order_info": order_info})

    if order_info["is_complete"]:
        work_order_path = create_shipping_work_order(
            thread_id=state["thread_id"],
            kol_email=state["latest_email"]["from_email"],
            kol_name=state["latest_email"].get("from_name", ""),
            order_info=order_info,
        )
        extracted["shipping_work_order"] = work_order_path
        notes = f"已生成发货工单，状态标记为待收货：{work_order_path}"
        _upsert_from_state(
            state,
            stage=2,
            status="待收货",
            notes=notes,
            extracted_info=extracted,
        )
        stage = "waiting_delivery"
    else:
        missing = ", ".join(order_info.get("missing_fields", [])) or "收件信息"
        notes = f"待补充订单信息：{missing}"
        _upsert_from_state(
            state,
            stage=1,
            status="待补充订单信息",
            notes=notes,
            extracted_info=extracted,
        )
        stage = "order_collection"

    return {
        "current_stage": stage,
        "reply_body": reply_body,
        "extracted_info": extracted,
        "messages": [AIMessage(content=reply_body)],
    }


def sentiment_analysis_node(state: AgentState) -> dict[str, Any]:
    result = analyze_sentiment(
        messages=state["messages"],
        latest_email=state["latest_email"],
        db_status=state.get("db_status", "待反馈"),
    )
    extracted = _merge_extracted(state, {
        "sentiment": result["sentiment"],
        "sentiment_reasoning": result["reasoning"],
    })
    return {
        "current_stage": "sentiment_routing",
        "sentiment": result["sentiment"],
        "negotiation_outcome": result["negotiation_outcome"],
        "extracted_info": extracted,
    }


def sop_perfect_node(state: AgentState) -> dict[str, Any]:
    strategy = build_sop_perfect_strategy(
        messages=state["messages"],
        latest_email=state["latest_email"],
    )
    extracted = _merge_extracted(state, {"sop_perfect_strategy": strategy})
    _upsert_from_state(
        state,
        stage=4,
        status="满意待收款信息",
        notes="客户反馈正向，进入价值信息收集",
        extracted_info=extracted,
    )
    return {
        "current_stage": "value_extraction",
        "extracted_info": extracted,
    }


def sop_salvage_node(state: AgentState) -> dict[str, Any]:
    reply_body = generate_sop_salvage_reply(
        messages=state["messages"],
        latest_email=state["latest_email"],
    )
    extracted = _merge_extracted(state, {"salvage_offer_sent": True})
    _upsert_from_state(
        state,
        stage=3,
        status="返款安抚中",
        notes="已发送直接返款安抚方案，等待对方确认是否接受",
        extracted_info=extracted,
    )
    return {
        "current_stage": "sop_salvage",
        "reply_body": reply_body,
        "extracted_info": extracted,
        "messages": [AIMessage(content=reply_body)],
    }


def sop_crisis_node(state: AgentState) -> dict[str, Any]:
    strategy = build_sop_crisis_strategy(
        messages=state["messages"],
        latest_email=state["latest_email"],
    )
    extracted = _merge_extracted(state, {"sop_crisis_strategy": strategy})
    _upsert_from_state(
        state,
        stage=4,
        status="危机退款处理中",
        notes="进入危机安抚与退款信息收集阶段",
        extracted_info=extracted,
    )
    return {
        "current_stage": "refund_extraction",
        "extracted_info": extracted,
    }


def value_extraction_node(state: AgentState) -> dict[str, Any]:
    value_info = extract_value_info(
        messages=state["messages"],
        latest_email=state["latest_email"],
    )
    reply_body = generate_value_collection_reply(
        messages=state["messages"],
        latest_email=state["latest_email"],
        sop_strategy=state.get("extracted_info", {}).get("sop_perfect_strategy", ""),
        value_info=value_info,
    )
    extracted = _merge_extracted(state, {"value_info": value_info})

    if value_info["is_complete"]:
        work_order_path = create_standard_work_order(
            thread_id=state["thread_id"],
            kol_email=state["latest_email"]["from_email"],
            kol_name=state["latest_email"].get("from_name", ""),
            value_info=value_info,
        )
        extracted["standard_work_order"] = work_order_path
        notes = f"已生成标准工单：{work_order_path}"
        status = "已生成标准工单"
    else:
        missing = ", ".join(value_info.get("missing_fields", [])) or "收款信息"
        notes = f"待补充价值信息：{missing}"
        status = "满意待收款信息"

    _upsert_from_state(
        state,
        stage=4,
        status=status,
        notes=notes,
        extracted_info=extracted,
    )
    return {
        "current_stage": "value_extraction",
        "reply_body": reply_body,
        "extracted_info": extracted,
        "messages": [AIMessage(content=reply_body)],
    }


def refund_info_extraction_node(state: AgentState) -> dict[str, Any]:
    refund_info = extract_refund_info(
        messages=state["messages"],
        latest_email=state["latest_email"],
    )
    reply_body = generate_refund_collection_reply(
        messages=state["messages"],
        latest_email=state["latest_email"],
        crisis_strategy=state.get("extracted_info", {}).get("sop_crisis_strategy", ""),
        refund_info=refund_info,
    )
    extracted = _merge_extracted(state, {"refund_info": refund_info})

    if refund_info["is_complete"]:
        work_order_path = create_crisis_work_order(
            thread_id=state["thread_id"],
            kol_email=state["latest_email"]["from_email"],
            kol_name=state["latest_email"].get("from_name", ""),
            refund_info=refund_info,
        )
        extracted["crisis_work_order"] = work_order_path
        notes = f"已生成危机摘要工单：{work_order_path}"
        status = "已生成危机工单"
    else:
        missing = ", ".join(refund_info.get("missing_fields", [])) or "退款信息"
        notes = f"待补充退款信息：{missing}"
        status = "危机退款处理中"

    _upsert_from_state(
        state,
        stage=4,
        status=status,
        notes=notes,
        extracted_info=extracted,
    )
    return {
        "current_stage": "refund_extraction",
        "reply_body": reply_body,
        "extracted_info": extracted,
        "messages": [AIMessage(content=reply_body)],
    }


def _intent_router(state: AgentState) -> str:
    db_status = state.get("db_status", "")
    route_intent = state.get("route_intent", "unknown")

    if db_status in {"满意待收款信息", "已生成标准工单"} or route_intent == "value_submission":
        return "value_extraction_node"
    if db_status in {"危机退款处理中", "已生成危机工单"} or route_intent == "refund_submission":
        return "refund_info_extraction_node"
    if route_intent == "order_info_submission" or db_status in {"推荐产品", "待补充订单信息"}:
        return "order_info_extraction_node"
    if route_intent == "pre_sales_collaboration":
        return "product_recommendation_node"
    return "sentiment_analysis_node"


def _sentiment_router(state: AgentState) -> str:
    if state.get("db_status") == "返款安抚中":
        outcome = state.get("negotiation_outcome", "not_applicable")
        return "sop_perfect_node" if outcome == "accepted_and_satisfied" else "sop_crisis_node"

    sentiment = state.get("sentiment", "mild_negative")
    if sentiment == "positive":
        return "sop_perfect_node"
    if sentiment == "mild_negative":
        return "sop_salvage_node"
    return "sop_crisis_node"


_GRAPH = None


def get_agent_graph():
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH

    workflow = StateGraph(AgentState)

    workflow.add_node("intent_recognition_node", intent_recognition_node)
    workflow.add_node("product_recommendation_node", product_recommendation_node)
    workflow.add_node("order_info_extraction_node", order_info_extraction_node)
    workflow.add_node("sentiment_analysis_node", sentiment_analysis_node)
    workflow.add_node("sop_perfect_node", sop_perfect_node)
    workflow.add_node("sop_salvage_node", sop_salvage_node)
    workflow.add_node("sop_crisis_node", sop_crisis_node)
    workflow.add_node("value_extraction_node", value_extraction_node)
    workflow.add_node("refund_info_extraction_node", refund_info_extraction_node)

    workflow.add_edge(START, "intent_recognition_node")
    workflow.add_conditional_edges(
        "intent_recognition_node",
        _intent_router,
        {
            "product_recommendation_node": "product_recommendation_node",
            "order_info_extraction_node": "order_info_extraction_node",
            "sentiment_analysis_node": "sentiment_analysis_node",
            "value_extraction_node": "value_extraction_node",
            "refund_info_extraction_node": "refund_info_extraction_node",
        },
    )
    workflow.add_edge("product_recommendation_node", "order_info_extraction_node")
    workflow.add_conditional_edges(
        "sentiment_analysis_node",
        _sentiment_router,
        {
            "sop_perfect_node": "sop_perfect_node",
            "sop_salvage_node": "sop_salvage_node",
            "sop_crisis_node": "sop_crisis_node",
        },
    )
    workflow.add_edge("sop_perfect_node", "value_extraction_node")
    workflow.add_edge("sop_crisis_node", "refund_info_extraction_node")
    workflow.add_edge("order_info_extraction_node", END)
    workflow.add_edge("sop_salvage_node", END)
    workflow.add_edge("value_extraction_node", END)
    workflow.add_edge("refund_info_extraction_node", END)

    _GRAPH = workflow.compile(checkpointer=get_checkpointer())
    return _GRAPH
