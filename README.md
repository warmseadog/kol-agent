# KOL Outreach Agent — 阿里企业邮箱自动回复系统

> **电商达人合作谈判智能体**：通过 IMAP 自动收信、LLM 判断合作阶段、生成高情商合规回复，经 SMTP 发出，配套 Web 仪表盘全程可视化监控。

---

## 功能概览

| 功能 | 说明 |
|---|---|
| **多轮会话记忆** | 按邮件线程（Thread）隔离，完整保存 KOL 来信 + 我方回复历史 |
| **阶段状态机** | 自动识别破冰→规则确认→跟进评价→返款确认四阶段 |
| **产品库注入** | 本地 JSON 产品库，关键词匹配后由 LLM 自主决定是否提及 |
| **防 Spam 设计** | In-Reply-To / References / multipart 头部，避免进垃圾箱 |
| **Web 仪表盘** | 会话进度、处理流水、对话详情弹窗、实时日志 |

---

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入真实邮箱密码、LLM API Key 等

# 3. 启动服务（会自动初始化数据库）
python -m app.main
# 或：
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4. 打开仪表盘
# http://localhost:8000/dashboard
```

---

## 项目结构

```
.
├── app/
│   ├── agent.py          # 核心状态机：收信→判断→回复→写DB
│   ├── config.py         # 环境变量统一读取
│   ├── database.py       # SQLite 持久化层（三张表）
│   ├── llm_service.py    # LLM 调用：阶段判断 + 回复生成 + 产品注入
│   ├── mail_service.py   # IMAP 收件 + SMTP 发件
│   └── main.py           # FastAPI 入口 + 仪表盘
├── data/
│   └── products.json     # 本地产品库（可替换为真实产品）
├── .env                  # 实际配置（不提交 Git）
├── .env.example          # 配置模板
├── kol_agent.db          # SQLite 数据库（自动创建）
└── requirements.txt
```

---

## 数据库表结构

系统使用 SQLite，包含三张表，职责严格分离：

### `processed_messages` — 去重防重复

| 字段 | 类型 | 说明 |
|---|---|---|
| message_id | TEXT PK | RFC Message-ID，防止同一封邮件重复触发 |
| thread_id | TEXT | 所属 Thread Key |
| processed_at | TEXT | 处理时间（ISO 格式） |

### `kol_threads` — KOL 会话状态

| 字段 | 类型 | 说明 |
|---|---|---|
| thread_id | TEXT PK | Thread Key（与 `_thread_key()` 一致） |
| kol_email | TEXT | KOL 邮箱 |
| kol_name | TEXT | KOL 显示名 |
| current_stage | INTEGER | 当前阶段（1-4） |
| last_message_id | TEXT | 最后处理的邮件 ID |
| notes | TEXT | 阶段备注 |
| created_at / updated_at | TEXT | 时间戳 |

### `thread_messages` — 多轮对话历史（核心）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER | 自增主键 |
| thread_id | TEXT | 所属 Thread Key |
| message_id | TEXT UNIQUE | 邮件唯一 ID，防重复写入 |
| role | TEXT | `kol`（KOL 来信）或 `our`（我方回复） |
| subject | TEXT | 邮件主题 |
| body | TEXT | 正文（截断至 `BODY_EXCERPT_LENGTH` 字符） |
| created_at | TEXT | 时间戳 |

---

## 多轮会话记忆机制

### Thread 隔离原则

- **Thread Key**（`_thread_key()`）：优先取 `References` 头中**第一个** Message-ID（会话根），无 References 则退回当前邮件的 Message-ID。
- **严格隔离**：不同 thread_id 的历史绝不合并，即使同一 KOL 发起多个独立线程，也各自独立处理。
- **与 kol_email 解耦**：历史由 thread_id 索引，不按 kol_email 聚合，避免串线。

### 写入时机

```
KOL 发来邮件
  ↓
写入 thread_messages（role=kol）← 在 LLM 调用之前
  ↓
从 DB 读取最近 MAX_THREAD_MESSAGES 条 → 构造 thread_history
  ↓
LLM 判断阶段 + 生成回复
  ↓
SMTP 发送
  ↓（仅在发送成功后）
写入 thread_messages（role=our）← 以发信成功为唯一可信来源
```

> 不依赖 IMAP「已发送」文件夹是否同步，我方历史以 **DB 写入** 为准。

### 长度控制

- **`MAX_THREAD_MESSAGES`**（默认 10）：每个线程最多送给 LLM 的历史条数。
- **`BODY_EXCERPT_LENGTH`**（默认 600）：每条邮件正文入库及送给 LLM 前的截断字符数。
- 两个参数均可在 `.env` 中覆盖。

---

## 产品库

### 文件格式

`data/products.json` — JSON 数组，每条产品包含：

```json
{
  "id": "P001",
  "name": "竹纤维抗菌床品套装",
  "tagline": "天然竹纤维材质，抑菌透气，告别过敏困扰",
  "scene": "卧室睡眠改善，适合过敏体质家庭",
  "keywords": ["床品", "bedding", "sleep", "bamboo"],
  "intro": "采用100%有机竹纤维，天然抑菌率高达99.8%..."
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| id | 是 | 唯一标识 |
| name | 是 | 产品名称 |
| tagline | 是 | 简短卖点（注入 LLM） |
| scene | 是 | 适用场景（注入 LLM） |
| keywords | 是 | 中英关键词，用于匹配来信内容 |
| intro | 否 | 对外介绍一句（可选） |

### 替换为真实产品

1. 编辑 `data/products.json`，替换为真实产品信息
2. 调整 `keywords` 使其贴合目标 KOL 可能提及的词汇
3. 重启服务即可生效（无需改代码）
4. 可通过 `PRODUCTS_PATH` 指向其他路径

### 产品注入策略

- **关键词匹配**：对 KOL 来信做关键词命中计数，命中多的优先
- **最多 5 个候选**注入 LLM prompt
- **由 LLM 自主决定**是否提及，提及时最多 2 个，自然融入邮件
- 遵守合规约束（不使用 fake review 等词汇）

---

## 仪表盘说明

访问 `http://localhost:8000/dashboard`

| 标签页 | 数据来源 | 用途 |
|---|---|---|
| **KOL 会话进度** | `kol_threads` | 查看所有 KOL 当前阶段、更新时间、备注；点击任意行可展开完整对话历史 |
| **处理流水** | `processed_messages` + 关联表 | 查看每封已处理邮件的时间、发件人、Thread ID、主题摘要；用于排查重复处理 |
| **实时日志** | 内存 log buffer | 展示最近 80 条运行日志 |

### API 端点

| 端点 | 说明 |
|---|---|
| `GET /dashboard` | Web 仪表盘 |
| `POST /check` | 立即触发一轮邮件检查 |
| `POST /start-auto` | 启动后台定时轮询 |
| `POST /stop-auto` | 停止后台轮询 |
| `GET /kols` | 所有 KOL 会话 JSON |
| `GET /processed` | 最近处理记录 JSON |
| `GET /thread/{thread_id}` | 某线程完整对话历史 |
| `GET /status` | 服务状态 |
| `GET /logs` | 最近日志 |

---

## 本地验证步骤

```bash
# 1. 初始化数据库（启动时自动执行，也可单独验证）
python -c "from app.database import init_db; init_db()"

# 2. 验证产品库加载
python -c "
import json
from pathlib import Path
products = json.loads(Path('data/products.json').read_text(encoding='utf-8'))
print(f'产品数量: {len(products)}')
for p in products: print(f'  {p[\"id\"]} {p[\"name\"]}')
"

# 3. 启动服务
python -m app.main

# 4. 触发一轮处理（需要真实邮箱有未读邮件）
curl -X POST http://localhost:8000/check

# 5. 验证 thread_messages 写入
python -c "
import sqlite3
conn = sqlite3.connect('kol_agent.db')
rows = conn.execute('SELECT thread_id, role, subject, created_at FROM thread_messages ORDER BY created_at').fetchall()
for r in rows: print(r)
conn.close()
"

# 6. 打开仪表盘查看会话列表与处理流水
# http://localhost:8000/dashboard
```

---

## 配置项速查

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMAIL_ADDRESS` | — | 阿里企业邮箱地址 |
| `EMAIL_PASSWORD` | — | 邮箱密码 |
| `LLM_API_KEY` | — | LLM 服务 API Key |
| `LLM_BASE_URL` | 通义千问 | LLM 接入地址（OpenAI 格式） |
| `LLM_MODEL` | qwen-plus | 模型名称 |
| `BRAND_NAME` | Our Brand | 品牌名（注入 LLM prompt） |
| `BRAND_SIGNATURE` | The Partnership Team | 邮件署名 |
| `DB_FILE` | kol_agent.db | SQLite 数据库文件路径 |
| `MAX_THREAD_MESSAGES` | 10 | 每线程送 LLM 的最大历史条数 |
| `BODY_EXCERPT_LENGTH` | 600 | 邮件正文截断字符数 |
| `PRODUCTS_PATH` | data/products.json | 产品库 JSON 路径 |
| `POLL_INTERVAL` | 120 | 轮询间隔（秒） |
| `MAX_EMAILS_PER_CYCLE` | 20 | 每轮最多处理邮件数 |

---

## 注意事项

- **`processed_emails.txt` 已废弃**：所有处理记录统一以 SQLite 为准，该文件不再使用。
- **多轮历史与线程绑定**：即使同一 KOL 有多个独立邀约线程，历史绝不跨线程混用。
- **我方回复可信来源**：仅在 SMTP 发送成功后才写入 `thread_messages(role=our)`，IMAP 已发送文件夹不作为依据。
- **产品注入可选**：若产品库文件不存在或加载失败，系统自动降级为不注入产品，正常生成回复。
