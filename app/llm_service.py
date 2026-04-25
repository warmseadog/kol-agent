"""
llm_service.py — LangChain / LangGraph 友好的 LLM 服务层

设计原则：
  1. 判断 / 提取类任务：统一使用 with_structured_output，返回稳定结构
  2. 邮件生成类任务：使用 ChatOpenAI 生成自然回复文本
  3. 兼容 OpenAI Chat Completions 风格服务商（OpenAI / 通义 / DeepSeek / Ollama 等）
"""

import logging
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app.config import config

logger = logging.getLogger(__name__)


def _build_llm(temperature: float = 0.2, max_tokens: int = 900) -> ChatOpenAI:
    return ChatOpenAI(
        model=config.LLM_MODEL,
        api_key=config.LLM_API_KEY,
        base_url=config.LLM_BASE_URL.rstrip("/"),
        temperature=temperature,
        timeout=config.LLM_TIMEOUT,
        max_tokens=max_tokens,
    )


def _message_to_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def _history_to_text(messages: list[BaseMessage], limit: int = 12) -> str:
    lines: list[str] = []
    for message in messages[-limit:]:
        if isinstance(message, HumanMessage):
            role = "KOL"
        elif isinstance(message, AIMessage):
            role = "Brand"
        else:
            role = message.__class__.__name__
        text = _message_to_text(message).strip().replace("\n", " ")
        lines.append(f"[{role}] {text[:500]}")
    return "\n---\n".join(lines) or "（暂无历史）"


def _product_block(products: list[dict[str, Any]] | None) -> str:
    if not products:
        return "当前未匹配到明确产品，可基于创作者需求提出请其选择产品方向。"
    lines = []
    for product in products[:5]:
        lines.append(
            f"- 名称: {product.get('name', '')} | 店铺: {product.get('store_name', '')} | "
            f"ASIN: {product.get('asin', '')} | 描述: {product.get('description', '')}"
        )
    return "\n".join(lines)


def _render_latest_email(latest_email: dict[str, Any]) -> str:
    return (
        f"主题: {latest_email.get('subject', '')}\n"
        f"发件人: {latest_email.get('from_name', '')} <{latest_email.get('from_email', '')}>\n"
        f"正文:\n{latest_email.get('body', '')[:1200]}"
    )


def _invoke_text(system_prompt: str, user_prompt: str, temperature: float = 0.65, max_tokens: int = 900) -> str:
    llm = _build_llm(temperature=temperature, max_tokens=max_tokens)
    response = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    return _message_to_text(response).strip()


class IntentRecognitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "pre_sales_collaboration",
        "order_info_submission",
        "post_sales_feedback",
        "value_submission",
        "refund_submission",
        "unknown",
    ] = Field(description="最新邮件的主意图")
    identity_verified: bool = Field(description="是否可视为同一 KOL 线程的合法继续沟通")
    current_stage: str = Field(description="建议写入 Graph State 的宏观阶段标识")
    reasoning: str = Field(description="简短中文判断原因")


class OrderInfoExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_name: str = Field(default="", description="收件人姓名")
    address: str = Field(default="", description="完整地址")
    phone: str = Field(default="", description="联系电话")
    product_name: str = Field(default="", description="产品型号或产品名称")
    asin: str = Field(default="", description="亚马逊 ASIN")
    store_name: str = Field(default="", description="店铺名称")
    notes: str = Field(default="", description="补充备注")
    missing_fields: list[str] = Field(default_factory=list, description="仍缺失的关键字段")
    is_complete: bool = Field(description="是否足以生成发货工单")


class SentimentAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentiment: Literal["positive", "mild_negative", "severe_negative"] = Field(description="反馈情绪级别")
    negotiation_outcome: Literal["accepted_and_satisfied", "still_unhappy", "not_applicable"] = Field(
        description="仅当线程处于补偿协商后续时使用"
    )
    reasoning: str = Field(description="简短中文解释")


class ValueExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_account: str = Field(default="", description="收款账户")
    payment_method: str = Field(default="", description="收款方式，如 PayPal / bank")
    review_screenshot_verified: bool = Field(default=False, description="是否已在来信中明确提到或附上评价截图")
    review_link: str = Field(default="", description="评价链接，如有")
    notes: str = Field(default="", description="备注")
    missing_fields: list[str] = Field(default_factory=list, description="缺失字段")
    is_complete: bool = Field(description="是否可生成标准工单")


class RefundInfoExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refund_account: str = Field(default="", description="退款账户")
    refund_method: str = Field(default="", description="退款方式")
    order_number: str = Field(default="", description="订单号")
    issue_summary: str = Field(default="", description="问题摘要")
    notes: str = Field(default="", description="补充备注")
    missing_fields: list[str] = Field(default_factory=list, description="缺失字段")
    is_complete: bool = Field(description="是否可生成危机摘要工单")


def recognize_intent(
    messages: list[BaseMessage],
    latest_email: dict[str, Any],
    db_status: str,
    db_stage: int,
) -> dict[str, Any]:
    llm = _build_llm(temperature=0.1, max_tokens=500).with_structured_output(
        IntentRecognitionResult,
        method="json_schema",
    )
    system_prompt = """你是 KOL 邮件工作流的意图识别与身份核验节点。

请结合：
1. 当前数据库阶段与状态
2. 历史邮件摘要
3. 最新一封来信

输出最新来信属于哪一类：
- pre_sales_collaboration：洽谈合作、询问产品、表达兴趣
- order_info_submission：提交或补充收件人/地址/电话/型号等发货信息
- post_sales_feedback：使用反馈、满意/抱怨/破损/物流问题
- value_submission：提供收款账号、评价截图、评价链接等
- refund_submission：提供退款账户、订单号、确认退款
- unknown：无法可靠判断

current_stage 请使用宏观阶段标识，例如：
- pre_sales_conversion
- order_collection
- waiting_delivery
- post_sales_support
- value_extraction
- refund_extraction

只根据邮件语义判断，不要臆造。"""
    user_prompt = (
        f"数据库阶段: {db_stage}\n"
        f"数据库状态: {db_status}\n\n"
        f"历史摘要:\n{_history_to_text(messages)}\n\n"
        f"最新邮件:\n{_render_latest_email(latest_email)}"
    )
    result = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    return result.model_dump()


def build_product_recommendation_strategy(
    messages: list[BaseMessage],
    latest_email: dict[str, Any],
    candidate_products: list[dict[str, Any]] | None,
) -> str:
    system_prompt = f"""你是 {config.BRAND_NAME} 的 KOL 合作顾问，负责推荐产品并引导对方确认合作。

目标：
1. 结合创作者需求，从候选产品中挑选 1-2 个最契合的产品
2. 说明推荐理由要自然，不像硬推销
3. 明确下一步是：如对方愿意合作，请回复收件人、地址、联系电话和想体验的产品

约束：
- 保持普通商务邮件语气，不夸张营销
- 不出现 fake review / paid review / 刷评 / 买好评
- 可以提到 ASIN 和 store_name，但要自然
- 只输出一小段“沟通策略摘要”，不是最终邮件正文
"""
    user_prompt = (
        f"历史摘要:\n{_history_to_text(messages)}\n\n"
        f"最新邮件:\n{_render_latest_email(latest_email)}\n\n"
        f"候选产品:\n{_product_block(candidate_products)}"
    )
    return _invoke_text(system_prompt, user_prompt, temperature=0.35, max_tokens=260)


def extract_order_info(
    messages: list[BaseMessage],
    latest_email: dict[str, Any],
    candidate_products: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    llm = _build_llm(temperature=0.1, max_tokens=500).with_structured_output(
        OrderInfoExtractionResult,
        method="json_schema",
    )
    system_prompt = """你是发货信息提取节点。请从最新邮件中提取以下字段：
- recipient_name
- address
- phone
- product_name
- asin
- store_name
- notes

规则：
- 仅提取邮件里明确提到的信息
- 若产品名称模糊但能和候选产品明显对上，可补全对应 asin/store_name
- is_complete 只有在 recipient_name/address/phone/product_name 四项都具备时才为 true
- missing_fields 只列缺失的关键字段，字段名使用英文：recipient_name, address, phone, product_name
"""
    user_prompt = (
        f"历史摘要:\n{_history_to_text(messages)}\n\n"
        f"最新邮件:\n{_render_latest_email(latest_email)}\n\n"
        f"候选产品:\n{_product_block(candidate_products)}"
    )
    result = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    return result.model_dump()


def analyze_sentiment(
    messages: list[BaseMessage],
    latest_email: dict[str, Any],
    db_status: str,
) -> dict[str, Any]:
    llm = _build_llm(temperature=0.1, max_tokens=400).with_structured_output(
        SentimentAnalysisResult,
        method="json_schema",
    )
    system_prompt = """你是 KOL 售后邮件的情绪诊断节点。

sentiment 只允许：
- positive：明显满意、认可、愿意继续配合
- mild_negative：轻微不满、产品小瑕疵、体验一般，但仍有机会通过安抚与直接返款挽回
- severe_negative：强烈不满、物流丢件、严重质量问题、退款/投诉倾向

negotiation_outcome 只在数据库状态表明“返款安抚中”时有意义：
- accepted_and_satisfied：接受返款安抚，态度明显转好，愿意继续
- still_unhappy：仍不满意，继续施压、拒绝方案、倾向退款
- not_applicable：非补偿协商后续
"""
    user_prompt = (
        f"数据库状态: {db_status}\n\n"
        f"历史摘要:\n{_history_to_text(messages)}\n\n"
        f"最新邮件:\n{_render_latest_email(latest_email)}"
    )
    result = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    return result.model_dump()


def build_sop_perfect_strategy(messages: list[BaseMessage], latest_email: dict[str, Any]) -> str:
    system_prompt = """你负责“满分体验 SOP”策略。

目标：
- 先真诚感谢积极反馈
- 再自然过渡到：如方便，请对方补充评价截图/链接与收款账户，便于安排后续报销或合作记录
- 只输出简短的沟通策略摘要，不要写成完整邮件
"""
    user_prompt = f"历史摘要:\n{_history_to_text(messages)}\n\n最新邮件:\n{_render_latest_email(latest_email)}"
    return _invoke_text(system_prompt, user_prompt, temperature=0.35, max_tokens=220)


def generate_sop_salvage_reply(messages: list[BaseMessage], latest_email: dict[str, Any]) -> str:
    system_prompt = f"""你是 {config.BRAND_NAME} 的售后挽回专员。

请生成一封回复邮件，目标是：
- 真诚致歉
- 先承认问题感受，再直接明确表示我们愿意返还本次购买费用
- 用安抚性的表达降低对方情绪，并自然引导对方在问题解决后继续给出真实、正面的使用反馈
- 下一步请对方回复收款账户，便于我们尽快安排返款

约束：
- 不推责
- 不空泛承诺
- 邮件像真实商务往来
- 语言跟随对方来信
- 不要提供多个补偿选项，不要写成“请选择方案”
- 结尾署名固定为：{config.BRAND_SIGNATURE}
"""
    user_prompt = f"历史摘要:\n{_history_to_text(messages)}\n\n最新邮件:\n{_render_latest_email(latest_email)}"
    return _invoke_text(system_prompt, user_prompt, temperature=0.68, max_tokens=420)


def build_sop_crisis_strategy(messages: list[BaseMessage], latest_email: dict[str, Any]) -> str:
    system_prompt = """你负责“危机 SOP”策略。

目标：
- 最高优先级安抚情绪
- 明确表达愿意优先退款，并额外赠送产品以示诚意
- 下一步重点是收集退款账户与订单号
- 只输出简短策略摘要，不要写成完整邮件
"""
    user_prompt = f"历史摘要:\n{_history_to_text(messages)}\n\n最新邮件:\n{_render_latest_email(latest_email)}"
    return _invoke_text(system_prompt, user_prompt, temperature=0.35, max_tokens=220)


def extract_value_info(messages: list[BaseMessage], latest_email: dict[str, Any]) -> dict[str, Any]:
    llm = _build_llm(temperature=0.1, max_tokens=400).with_structured_output(
        ValueExtractionResult,
        method="json_schema",
    )
    system_prompt = """你是价值信息提取节点。请从最新邮件中提取：
- payment_account
- payment_method
- review_screenshot_verified
- review_link
- notes

规则：
- 如果来信明确表示“已附截图”“see attached screenshot”“screenshot attached”等，可将 review_screenshot_verified 设为 true
- is_complete 仅当 payment_account 不为空，且 review_screenshot_verified 为 true 或 review_link 不为空时为 true
- missing_fields 只列 payment_account / review_screenshot
"""
    user_prompt = f"历史摘要:\n{_history_to_text(messages)}\n\n最新邮件:\n{_render_latest_email(latest_email)}"
    result = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    return result.model_dump()


def extract_refund_info(messages: list[BaseMessage], latest_email: dict[str, Any]) -> dict[str, Any]:
    llm = _build_llm(temperature=0.1, max_tokens=400).with_structured_output(
        RefundInfoExtractionResult,
        method="json_schema",
    )
    system_prompt = """你是退款信息提取节点。请从最新邮件中提取：
- refund_account
- refund_method
- order_number
- issue_summary
- notes

规则：
- is_complete 仅当 refund_account 与 order_number 都不为空时为 true
- missing_fields 只列 refund_account / order_number
- issue_summary 用一句中文概括问题
"""
    user_prompt = f"历史摘要:\n{_history_to_text(messages)}\n\n最新邮件:\n{_render_latest_email(latest_email)}"
    result = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])
    return result.model_dump()


def generate_order_collection_reply(
    messages: list[BaseMessage],
    latest_email: dict[str, Any],
    recommendation_strategy: str,
    order_info: dict[str, Any],
) -> str:
    system_prompt = f"""你是 {config.BRAND_NAME} 的合作运营专员，请写一封真实自然的邮件回复。

场景：
- 当前处于产品推荐 / 收件信息收集环节
- 如果 order_info.is_complete 为 false，请礼貌索取缺失字段
- 如果 order_info.is_complete 为 true，请确认已经记录发货信息，并告知会安排发货、状态进入待收货

约束：
- 像正常商务邮件
- 语言跟随对方来信
- 不出现虚假评价相关敏感词
- 结尾署名固定为：{config.BRAND_SIGNATURE}
"""
    user_prompt = (
        f"历史摘要:\n{_history_to_text(messages)}\n\n"
        f"最新邮件:\n{_render_latest_email(latest_email)}\n\n"
        f"推荐策略摘要:\n{recommendation_strategy}\n\n"
        f"结构化订单信息:\n{order_info}"
    )
    return _invoke_text(system_prompt, user_prompt, temperature=0.65, max_tokens=420)


def generate_value_collection_reply(
    messages: list[BaseMessage],
    latest_email: dict[str, Any],
    sop_strategy: str,
    value_info: dict[str, Any],
) -> str:
    system_prompt = f"""你是 {config.BRAND_NAME} 的合作结算专员。

场景：
- 当前是正向反馈后的价值收集阶段
- 需要在感谢 positive feedback 的同时，收集或确认收款账户与评价截图/链接
- 如果 value_info.is_complete 为 true，则说明已足够生成标准工单，回复应以确认收到、即将安排处理为主
- 如果 value_info.is_complete 为 false，则礼貌说明还缺什么

约束：
- 语气温暖、自然
- 不营销化
- 结尾署名固定为：{config.BRAND_SIGNATURE}
"""
    user_prompt = (
        f"历史摘要:\n{_history_to_text(messages)}\n\n"
        f"最新邮件:\n{_render_latest_email(latest_email)}\n\n"
        f"SOP 策略摘要:\n{sop_strategy}\n\n"
        f"结构化价值信息:\n{value_info}"
    )
    return _invoke_text(system_prompt, user_prompt, temperature=0.66, max_tokens=420)


def generate_refund_collection_reply(
    messages: list[BaseMessage],
    latest_email: dict[str, Any],
    crisis_strategy: str,
    refund_info: dict[str, Any],
) -> str:
    system_prompt = f"""你是 {config.BRAND_NAME} 的危机售后负责人。

场景：
- 当前属于强烈不满 / 退款安抚阶段
- 如果 refund_info.is_complete 为 false，需要以最高优先级、低刺激语气收集退款账户与订单号
- 如果 refund_info.is_complete 为 true，则确认已记录信息、将生成优先工单处理退款，并附带赠送产品承诺

约束：
- 第一优先级是安抚和解决问题
- 避免辩解
- 结尾署名固定为：{config.BRAND_SIGNATURE}
"""
    user_prompt = (
        f"历史摘要:\n{_history_to_text(messages)}\n\n"
        f"最新邮件:\n{_render_latest_email(latest_email)}\n\n"
        f"危机策略摘要:\n{crisis_strategy}\n\n"
        f"结构化退款信息:\n{refund_info}"
    )
    return _invoke_text(system_prompt, user_prompt, temperature=0.62, max_tokens=420)
