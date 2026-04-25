# KOL Outreach Agent

基于阿里企业邮箱、FastAPI、LangChain 和 LangGraph 的 KOL 邮件合作工作流系统。

当前版本已经从“轻量级阶段判断脚本”升级为真正的 `LangGraph` 工作流，支持：

- 基于邮件线程的多轮会话记忆
- 售前推荐、订单信息采集、售后情绪路由、价值提取、退款处理
- `SqliteSaver` 持久化 Graph Checkpoint
- 本地 CSV 工单输出
- 可视化 Dashboard 查看线程状态、情绪、结构化信息和处理流水

---

## 核心特性

| 模块 | 说明 |
|---|---|
| `LangGraph` 工作流 | 将 KOL 邮件流程拆成多个节点和条件边，替代原来的硬编码阶段流转 |
| 结构化输出 | 使用 `langchain_openai.ChatOpenAI(...).with_structured_output(...)` 做意图识别、订单提取、情绪诊断、退款信息提取 |
| 双层持久化 | 业务审计仍落在 SQLite 业务表中，Graph 状态由 `SqliteSaver` 落到同一数据库 |
| 产品推荐 | 从本地 `products.json` 读取产品数据，通过关键词筛选候选品并注入推荐节点 |
| 工单生成 | 自动输出发货工单、标准工单、危机工单到本地 CSV |
| 仪表盘 | 展示线程状态、DB 阶段、情绪标签、结构化信息摘要、处理流水与实时日志 |

---

## 当前工作流

```text
独立站邮件入口
  -> intent_recognition_node
  -> 前置转化 / 售后分流

前置转化：
  product_recommendation_node
  -> order_info_extraction_node
  -> 待收货

售后反馈：
  sentiment_analysis_node
  -> positive       -> sop_perfect_node -> value_extraction_node
  -> mild_negative  -> sop_salvage_node
                        -> 接受返款安抚并满意 -> sop_perfect_node -> value_extraction_node
                        -> 仍不满意       -> sop_crisis_node  -> refund_info_extraction_node
  -> severe_negative -> sop_crisis_node -> refund_info_extraction_node
```

---

## 旧版对比

为了方便理解这次重构的价值，下面给出旧版轻量状态机与当前 LangGraph 版本的对比。

| 维度 | 旧版实现 | 当前版本 |
|---|---|---|
| 编排方式 | `agent.py` 中串行 if/else 流程 | `app/graph.py` 中显式节点 + 条件边 |
| 状态表达 | 主要依赖 `current_stage` 数字阶段 | `AgentState` + `current_stage` + `extracted_info` + `sentiment` |
| LLM 调用 | 阶段判断与文本生成混在一起 | 判断/提取任务结构化输出，回复任务单独生成 |
| 记忆机制 | `thread_messages` 作为历史上下文 | `thread_messages` 审计 + `SqliteSaver` checkpoint 双层记忆 |
| 路由能力 | 固定阶段推进 | 支持情绪路由、返款安抚分流、退款分流 |
| 数据提取 | 主要依赖自由文本理解 | 订单、收款、退款信息都走结构化提取 |
| 工单输出 | 无明确工单产物 | 自动生成发货、标准、危机三类 CSV 工单 |
| Dashboard | 主要看阶段号与处理流水 | 可看状态、情绪、结构化摘要和关键业务计数 |
| 可扩展性 | 增加新分支需要直接改主流程 | 可继续新增节点、边和子流程 |

### 旧版的特点

- 实现简单，适合快速验证邮件收发、基础阶段判断和自动回复能力
- 入口集中在 `agent.py`，理解成本低，但流程一旦变复杂会快速膨胀
- 适合“阶段线性推进”的合作流程，不适合复杂售后博弈和多分支路由

### 新版的提升

- 把业务流程从“代码顺序”升级为“图结构”，状态流转更清晰
- 把“意图识别、情绪诊断、信息提取、回复生成”拆成职责明确的节点
- 同一个 `thread_id` 能通过 checkpoint 恢复 Graph 状态，不再只依赖历史拼接
- 结构化提取结果可以直接驱动工单和仪表盘，而不是只能藏在邮件正文里
- 后续如果要加入人工审核、更多售后策略、工具调用或外部系统集成，会更容易扩展

### 什么时候旧版仍然够用

如果你的目标只是：

- 自动收信
- 按粗粒度阶段回复
- 不需要复杂分流
- 不需要结构化售后/退款处理

那旧版已经够轻便。

但如果你的目标是：

- 真正把 KOL 合作流程拆成可维护的工作流
- 支持售前转化、售后情绪诊断、返款安抚分流、退款闭环
- 需要稳定的结构化提取和持久化恢复能力

那么当前 LangGraph 版本更适合作为长期演进基础。

---

## Graph State

`app/graph.py` 中定义了 `AgentState`，核心字段包括：

- `thread_id`：当前邮件线程 ID
- `messages`：兼容 LangChain `BaseMessage` 的历史消息
- `current_stage`：当前宏观阶段标识
- `extracted_info`：结构化提取结果总线
- `sentiment`：`positive` / `mild_negative` / `severe_negative`
- `latest_email`：当前邮件上下文
- `candidate_products`：当前候选产品列表
- `reply_body`：待发送邮件正文

---

## 节点说明

### `intent_recognition_node`

识别当前来信属于：

- 售前合作沟通
- 收件信息提交
- 售后反馈
- 收款信息提交
- 退款信息提交

同时给出宏观阶段建议。

### `product_recommendation_node`

根据本地产品库推荐 1 到 2 个合适产品，并引导对方确认合作意向和收件信息。

### `order_info_extraction_node`

结构化提取：

- 收件人
- 地址
- 联系方式
- 产品名称
- `asin`
- `store_name`

若信息完整，自动输出发货工单并把线程状态置为 `待收货`。

### `sentiment_analysis_node`

将售后反馈归类为：

- `positive`
- `mild_negative`
- `severe_negative`

若线程已经在“返款安抚中”，还会进一步判断是“接受返款安抚并满意”还是“仍不满意”。

### `sop_perfect_node`

用于正向反馈场景，进入价值收集分支。

### `sop_salvage_node`

用于轻微不满场景，生成“直接返款 + 安抚 + 引导后续正向反馈”的回复。

### `sop_crisis_node`

用于强烈不满场景，进入最高优先级安抚与退款信息收集分支。

### `value_extraction_node`

提取：

- 收款账户
- 收款方式
- 评价截图核实状态
- 评价链接

信息完整时输出标准工单。

### `refund_info_extraction_node`

提取：

- 退款账户
- 退款方式
- 订单号
- 问题摘要

信息完整时输出危机工单。

---

## 项目结构

```text
.
├── app/
│   ├── agent.py          # 邮件轮询入口，负责收信、调 Graph、发信、写审计历史
│   ├── config.py         # 统一读取 .env 配置
│   ├── database.py       # SQLite 业务表 + LangGraph checkpointer + 工单输出
│   ├── graph.py          # LangGraph 状态定义、节点与条件路由
│   ├── llm_service.py    # LangChain / OpenAI 兼容 LLM 层
│   ├── mail_service.py   # IMAP 收件 + SMTP 回复
│   └── main.py           # FastAPI 入口与 Dashboard
├── data/
│   ├── products.json     # 产品库
│   └── work_orders/      # 自动生成的 CSV 工单
├── .env.example
├── requirements.txt
└── kol_agent.db
```

---

## 持久化设计

### 业务表

系统仍然保留三张业务表，用于审计、Dashboard 和手工排查：

- `processed_messages`
- `kol_threads`
- `thread_messages`

其中 `kol_threads` 额外记录：

- `status`
- `notes`
- `extracted_info`

### LangGraph Checkpoint

Graph 的多轮状态由 `SqliteSaver` 负责，和业务表共用同一个 `SQLite` 文件。

这样带来的好处是：

- 同一个 `thread_id` 能恢复 Graph 状态
- 外层 API 不用改
- 旧 Dashboard 和历史查询接口仍然能复用

---

## 产品库格式

`data/products.json` 采用新的产品结构：

```json
{
  "name": "竹纤维抗菌床品套装",
  "description": "天然竹纤维材质，主打亲肤、透气和低敏睡眠体验",
  "keywords": ["床品", "bedding", "sleep", "bamboo"],
  "store_name": "Aurora Home",
  "asin": "B0CHBED001"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 产品名称 |
| `description` | 是 | 用于推荐和 LLM 注入的产品描述 |
| `keywords` | 是 | 匹配邮件内容的关键词组 |
| `store_name` | 是 | 店铺名称 |
| `asin` | 是 | 亚马逊 ASIN |

匹配策略：

- 对来信正文做关键词命中计数
- 返回命中最高的前 `top_n` 个产品
- 若完全无命中，则回退到产品库前几项，确保推荐节点可继续工作

---

## 工单输出

结构化信息完整时，系统会自动输出 CSV 工单：

| 文件 | 触发节点 | 用途 |
|---|---|---|
| `data/work_orders/shipping_orders.csv` | `order_info_extraction_node` | 发货工单 |
| `data/work_orders/standard_orders.csv` | `value_extraction_node` | 标准合作工单 |
| `data/work_orders/crisis_orders.csv` | `refund_info_extraction_node` | 危机退款工单 |

---

## Dashboard

访问：`http://localhost:8000/dashboard`

新版 Dashboard 提供三类视图：

### `KOL 会话进度`

展示每个线程的：

- DB 阶段
- 当前业务状态
- 情绪标签
- 结构化信息摘要
- 最近更新时间
- 备注

同时顶部提供几个关键统计：

- KOL 总数
- 待收货
- 价值转化中
- 危机处理中
- 正向反馈 / 轻微不满 / 强烈不满

### `处理流水`

查看最近已经处理过的邮件，用于排查重复回复、主题异常或线程串线问题。

### `实时日志`

查看最近运行日志，便于排查 Graph 节点执行和 SMTP / IMAP 异常。

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/status` | 当前服务状态 |
| `POST` | `/check` | 立即执行一轮邮件检查 |
| `POST` | `/start-auto` | 启动后台轮询 |
| `POST` | `/stop-auto` | 停止后台轮询 |
| `GET` | `/emails` | 查看当前未读邮件 |
| `GET` | `/kols` | 查看所有线程状态 |
| `GET` | `/processed` | 查看最近处理记录 |
| `GET` | `/thread/{thread_id}` | 查看线程完整对话历史 |
| `DELETE` | `/thread/{thread_id}` | 删除指定线程的业务表与 checkpoint |
| `DELETE` | `/all-data` | 清空所有业务数据与 checkpoint |
| `GET` | `/logs` | 获取最近运行日志 |
| `GET` | `/dashboard` | 打开监控面板 |

---

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 复制环境配置
cp .env.example .env

# 3. 填写真实邮箱 / LLM 参数
# EMAIL_ADDRESS=
# EMAIL_PASSWORD=
# LLM_API_KEY=
# LLM_BASE_URL=
# LLM_MODEL=

# 4. 启动服务
python -m app.main
```

启动后可访问：

- `http://localhost:8000/status`
- `http://localhost:8000/dashboard`

---

## 本地验证

```bash
# 检查 Python 语法
python -m compileall app

# 初始化数据库
python -c "from app.database import init_db; init_db()"

# 验证 Graph 可构建
python -c "from app.graph import get_agent_graph; print('graph-ok')"

# 启动服务
python -m app.main
```

---

## 配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMAIL_ADDRESS` | - | 阿里企业邮箱地址 |
| `EMAIL_PASSWORD` | - | 邮箱密码 |
| `IMAP_HOST` | `imap.qiye.aliyun.com` | IMAP 主机 |
| `IMAP_PORT` | `993` | IMAP 端口 |
| `SMTP_HOST` | `smtp.qiye.aliyun.com` | SMTP 主机 |
| `SMTP_PORT` | `465` | SMTP 端口 |
| `LLM_API_KEY` | - | 大模型 API Key |
| `LLM_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容接口地址 |
| `LLM_MODEL` | `qwen-plus` | 模型名 |
| `LLM_TIMEOUT` | `60` | LLM 请求超时秒数 |
| `BRAND_NAME` | `Our Brand` | 品牌名 |
| `BRAND_SIGNATURE` | `The Partnership Team` | 邮件署名 |
| `SENDER_DISPLAY_NAME` | `Support Team` | 发件人显示名 |
| `DB_FILE` | `kol_agent.db` | SQLite 文件路径 |
| `MAX_THREAD_MESSAGES` | `10` | 注入 LLM 的最大历史条数 |
| `BODY_EXCERPT_LENGTH` | `600` | 邮件入库摘要长度 |
| `PRODUCTS_PATH` | `data/products.json` | 产品库路径 |
| `POLL_INTERVAL` | `120` | 自动轮询间隔 |
| `MAX_EMAILS_PER_CYCLE` | `20` | 每轮最多处理邮件数 |

---

## 注意事项

- 我方回复只在 SMTP 发送成功后才会写入 `thread_messages`
- `thread_id` 以邮件线程为准，不按邮箱地址聚合，避免串线
- 若产品库加载失败，推荐节点会自动降级
- `SqliteSaver` 适合当前这种轻量级同步单机部署场景
- 清空或删除线程时，会同时删除业务表和 LangGraph checkpoint
