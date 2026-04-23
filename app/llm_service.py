"""
llm_service.py — 大语言模型调用层

核心能力：
  1. call_llm()           通用 LLM HTTP 调用（兼容所有 OpenAI Chat Completions 格式服务商）
  2. detect_stage()       根据 Thread 历史判断当前处于哪个合作阶段（1-4）
  3. generate_kol_reply() 根据阶段 + 上下文生成合规、高情商的 KOL 回复邮件
"""

import json
import logging
import requests
from typing import Any

from app.config import config

logger = logging.getLogger(__name__)


# ─── 底层 LLM 调用（可替换任意服务商） ────────────────────────────────────────

def call_llm(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 1500
) -> str:
    """
    调用大语言模型 API（标准 OpenAI Chat Completions 格式）。

    更换服务商只需修改 .env 中的 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL，代码无需改动：
      - OpenAI GPT-4o:    LLM_BASE_URL=https://api.openai.com/v1
      - 通义千问 Plus:    LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
      - DeepSeek:         LLM_BASE_URL=https://api.deepseek.com/v1
      - 本地 Ollama:      LLM_BASE_URL=http://localhost:11434/v1

    Args:
        messages:    OpenAI 格式的对话消息列表 [{"role": ..., "content": ...}]
        temperature: 生成温度，判断类任务用低值（0.1-0.3），创作类用较高值（0.6-0.8）
        max_tokens:  最大生成 token 数

    Returns:
        str: 模型返回的文本内容

    Raises:
        Exception: 网络错误或 API 错误时抛出
    """
    url = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ─── 阶段判断 ──────────────────────────────────────────────────────────────────

def detect_stage(thread_history: list[dict], db_stage: int) -> dict:
    """
    让 LLM 分析 Thread 历史邮件，自动判断当前所处的合作阶段。

    Args:
        thread_history: 由 agent.py 格式化的消息列表，每条含 is_mine/subject/body 字段
        db_stage:       数据库中上次记录的阶段（作为 fallback）

    Returns:
        dict: {"stage": int(1-4), "reasoning": str}
    """
    # 构建供 LLM 阅读的对话摘要（每条消息截取前 400 字符，避免超长上下文）
    summary_lines = []
    for msg in thread_history:
        role = "【我方】" if msg.get("is_mine") else "【KOL】"
        body_snippet = msg["body"][:400].replace("\n", " ")
        summary_lines.append(f"{role} 主题: {msg['subject']}\n内容: {body_snippet}")

    history_text = "\n\n---\n\n".join(summary_lines) or "（无历史记录）"

    system_prompt = """你是一个 KOL 合作谈判进度分析专家。请根据以下邮件对话记录，判断当前合作所处阶段。

阶段定义：
- 阶段1 [破冰邀请]：我方刚发出初次邀请，或 KOL 尚未回复
- 阶段2 [规则确认]：KOL 表达了兴趣，正在了解/确认合作细节（购买体验→评价→报销）
- 阶段3 [跟进评价]：KOL 已确认合作意向且可能已收货，需跟进引导留下真实评价
- 阶段4 [返款确认]：KOL 已提供评价截图或链接，准备确认打款并致谢

请只返回 JSON，格式：{"stage": <1|2|3|4>, "reasoning": "<简短中文判断理由>"}"""

    user_msg = f"数据库记录阶段：{db_stage}\n\n邮件历史（从旧到新）：\n\n{history_text}"

    try:
        raw = call_llm(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_msg}],
            temperature=0.1,
            max_tokens=200
        )
        # 清洗可能的 Markdown 代码块包裹
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(clean)
        stage = max(1, min(4, int(result.get("stage", db_stage))))
        return {"stage": stage, "reasoning": result.get("reasoning", "")}
    except Exception as e:
        logger.warning(f"⚠️ 阶段判断失败，沿用数据库阶段 {db_stage}: {e}")
        return {"stage": db_stage, "reasoning": "自动判断失败，使用上次记录"}


# ─── KOL 回复生成 ──────────────────────────────────────────────────────────────

# 每个阶段的具体任务说明，注入到 System Prompt 的任务区块
_STAGE_TASKS = {
    1: """
【当前任务：破冰邀请】
目标：以简洁真诚的商务语气表达合作意向，让对方愿意了解详情。

要点：
- 简短自我介绍品牌，一句话说明为何联系对方（如内容风格与品牌调性契合）
- 提出希望寄送产品供对方真实体验，不急于解释完整商业条件
- 全程像一封普通商务邮件，而非营销广告；不堆叠形容词和感叹
- 结尾用开放式问句邀请回复即可
""",
    2: """
【当前任务：规则确认】
目标：向已表现出兴趣的 KOL 委婉、清晰地介绍合作流程，让对方感受到这是一个轻松、双赢的体验机会。

合作流程描述方式（请用以下优雅措辞，绝不用粗暴直白的表达）：
  步骤1: 请对方以真实用户身份在平台下单购买（强调：这样体验才最真实、最有参考价值）
  步骤2: 收到产品后，按照真实感受在平台写下诚实的体验分享
  步骤3: 我们会将购买费用作为"体验报销"（Reimbursement）全额退还

绝对禁止的词汇：刷单、刷评、买好评、fake review、paid review
推荐使用的词汇：产品测评(Product Evaluation)、体验报销(Reimbursement)、真诚分享(Honest Sharing)、真实体验(Genuine Experience)

- 强调我们最珍视真实的用户声音，对方可以完全按真实感受评价
- 询问对方是否方便，并请对方确认感兴趣的产品
""",
    3: """
【当前任务：跟进评价】
目标：极度关心 KOL 的产品体验，用最温柔、不强迫的方式引导其在方便时留下真实评价。

要点：
- 首先亲切询问产品是否顺利到达、使用体验如何
- 表达"我们非常期待您的真实感受，无论如何评价我们都真心接受"
- 轻描淡写地提一句，"如果体验满意，许多粉丝很期待看到您的分享"
- 如有任何不满意，承诺立刻处理（更换/退款）——体现极致服务
- 语气像极其贴心的朋友，而非商业催单
""",
    4: """
【当前任务：返款确认】
目标：确认收到评价，安排报销，表达最诚挚的感谢，并展望长期合作关系。

要点：
- 热情表达已看到对方精彩的评价，大力称赞其质量（真实、专业、对消费者有参考价值）
- 告知报销流程：请对方提供收款信息（PayPal 账户/银行信息等），X 个工作日内完成转账
- 表达"这次合作愉快极了，希望我们成为长期合作伙伴"
- 告知后续有新品会第一时间想到对方
- 用极度感激、温暖的语气收尾，让 KOL 感受到被真心珍视
""",
}


def generate_kol_reply(
    thread_history: list[dict],
    kol_name: str,
    kol_email: str,
    stage: int,
    latest_message: str,
    candidate_products: list[dict] | None = None,
) -> str:
    """
    根据当前合作阶段和 Thread 历史，生成面向 KOL 的高情商回复邮件正文。

    核心特性：
      - 语言镜像：自动检测 KOL 最近来信的语言，用完全相同的语言回复
      - 极致服务语气：谦卑、热情、以对方为中心
      - 内容合规：不使用可能触发 Spam 或法律风险的词汇
      - 产品感知：若提供候选产品，由模型自主决定是否融入，最多提及 2 个

    Args:
        thread_history:     格式化后的历史消息列表（含多轮 KOL + 我方）
        kol_name:           KOL 姓名
        kol_email:          KOL 邮箱
        stage:              当前合作阶段 (1-4)
        latest_message:     KOL 最新一封来信的正文
        candidate_products: 可选候选产品列表，每条含 name/tagline/scene/intro 字段

    Returns:
        str: 可直接发送的回复邮件正文（纯文本）
    """
    # 格式化历史记录供 LLM 阅读（控制每条消息长度）
    history_lines = []
    for msg in thread_history:
        role = "Our Team" if msg.get("is_mine") else f"Creator ({kol_name or 'KOL'})"
        body_snippet = msg["body"][:config.BODY_EXCERPT_LENGTH].replace("\n", " ")
        history_lines.append(f"[{role}]: {body_snippet}")
    history_text = "\n\n---\n\n".join(history_lines) or "（首次联系，无历史记录）"

    stage_task = _STAGE_TASKS.get(stage, _STAGE_TASKS[2])

    # 构建产品参考区块（仅在有候选产品时注入）
    product_section = ""
    if candidate_products:
        product_lines = []
        for p in candidate_products[:5]:
            line = f"- 【{p.get('name', '')}】{p.get('tagline', '')}（适用：{p.get('scene', '')}）"
            product_lines.append(line)
        product_section = f"""
# 可参考的品牌产品（供你自主决定是否提及）
以下是当前可供合作体验的产品，**你来判断**是否在这封邮件中自然提及——若提及，最多 2 个，融入邮件内容，切勿生硬推销：

{chr(10).join(product_lines)}

若不适合提及（如当前阶段不需要介绍产品），可完全忽略以上列表。
"""

    system_prompt = f"""你是 {config.BRAND_NAME} 品牌的高级 KOL 合作专员，也是一位极度专业、谦卑、热情的品牌大使。

# 人设与沟通风格
你对每一位内容创作者都怀有发自内心的尊重与欣赏。你的沟通风格：
- 像极其贴心的高级私人管家：谦卑、细腻、完全以对方为中心
- 把 KOL 当作独一无二的尊贵合作伙伴，无限放大对方的价值
- 用词温暖、真诚，绝不生硬或功利
- 称呼方式参考：Dear [名字]、尊敬的创作者朋友、了不起的 [名字] 等

# 核心约束（必须严格遵守）

1. 语言镜像（Language Mirroring）——最高优先级：
   检测 KOL 最近来信所用的语言，你的整封回复邮件必须使用完全相同的语言。
   对方英语 → 你用英语；日语 → 日语；西班牙语 → 西班牙语；以此类推。
   如果无法确定，默认使用英语。

2. 禁止词汇（绝对不出现）：
   fake review / paid review / buy reviews / 刷单 / 刷评 / 买好评
   以及任何可能被解读为"花钱购买虚假评价"的表述。

3. 推荐替代词汇：
   - Product Evaluation / 产品测评
   - cover your cost / refund your purchase / 报销购买费用（避免单独大写 REIMBURSEMENT）
   - Honest Sharing / Genuine Experience / 真诚分享 / 真实体验
   - Collaboration / Partnership / 合作 / 长期伙伴

4. 反垃圾邮件写作规范（Gmail 内容过滤器敏感点）：
   - 开头不超过 1 句问候，禁止连续 2 句以上的夸赞或溢美（会触发营销邮件检测）
   - 全文语气像普通商务往来邮件，而非广告文案
   - 避免全大写词汇、感叹号堆叠、以及"amazing / incredible / deeply inspired"此类营销腔
   - "reimburse / reimbursement" 每封最多出现 1 次，且用小写嵌入句子中
   - 避免开头连续 3 个词都是形容词或副词修饰语

5. 格式要求：
   - 直接输出邮件正文，不要加任何解释性文字或标注
   - 结尾署名使用：{config.BRAND_SIGNATURE}（不要用占位符）
   - 长度控制在 150-250 词，简洁自然，不要填充无实质内容的客套话
{product_section}
# 当前阶段任务
{stage_task}"""

    user_content = f"""KOL 信息：
- 姓名：{kol_name or '朋友'}
- 邮箱：{kol_email}

--- 邮件历史（从旧到新）---
{history_text}

--- KOL 最新来信 ---
{latest_message[:800]}

请根据以上背景，生成【阶段 {stage}】的回复邮件正文。"""

    return call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": user_content}],
        temperature=0.72,
        max_tokens=900
    )
