# KOL Outreach Agent v2.1

基于**阿里企业邮箱 (IMAP/SMTP)** + **大语言模型** 的电商达人合作谈判智能体。

自动处理与海外 KOL 的多阶段邮件往来，从破冰邀请到返款确认，全程 AI 驱动，内置防 Spam 机制。

---

## 核心特性

- **防 Spam 回复**：正确设置 `In-Reply-To` + `References` 头部，回复始终串联在同一会话中，Gmail 不会将其识别为陌生邮件
- **真实发件人头部**：`From` 显示真实人名（如 `Sarah from KeepGlad`），附带 `Reply-To`、`Date` 等完整商务邮件头
- **4 阶段状态机**：LLM 自动识别当前合作进度，生成对应阶段的回复
- **语言镜像**：无论对方用英/日/德/西等语言，LLM 自动用相同语言回复
- **高情商语气**：极度谦卑热情，绝不出现"刷单/买好评"等违规词汇
- **LLM 灵活切换**：修改 `.env` 即可切换通义千问 / OpenAI / DeepSeek / Ollama 等任意服务商
- **SQLite 防重复**：通过 `Message-ID` 持久化，防止对同一邮件重复回复

---

## 合作阶段说明

| 阶段 | 名称 | 触发时机 | 核心动作 |
|------|------|---------|---------|
| 1 | 破冰邀请 | KOL 首次来信 | 赞美内容，邀请免费体验产品 |
| 2 | 规则确认 | KOL 表现出兴趣 | 委婉介绍"购买→评价→报销"流程 |
| 3 | 跟进评价 | 确认合作，货已发出 | 关心体验感受，引导留下真实评价 |
| 4 | 返款确认 | KOL 提供评价截图/链接 | 确认打款，表达感谢，期待长期合作 |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

编辑项目根目录的 `.env` 文件（必须为纯 ASCII 编码，不含中文注释）：

```env
# 阿里企业邮箱
EMAIL_ADDRESS=support@yourdomain.com
EMAIL_PASSWORD=your_authorization_code    # 填授权码，不是登录密码
SENDER_DISPLAY_NAME=Sarah from YourBrand  # 收件人看到的发件人名字

IMAP_HOST=imap.qiye.aliyun.com
IMAP_PORT=993
SMTP_HOST=smtp.qiye.aliyun.com
SMTP_PORT=465

# LLM 配置
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

# 品牌信息
BRAND_NAME=YourBrand Store
BRAND_SIGNATURE=Sarah | YourBrand Partnership Team
```

> **授权码获取方式**：登录阿里企业邮箱网页版 → 设置 → 客户端设置 → 生成授权码

### 3. 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动会自动创建 `kol_agent.db` 数据库。

### 4. 启动自动轮询

```powershell
Invoke-WebRequest -Uri "http://localhost:8000/start-auto" -Method POST -UseBasicParsing
```

或打开监控面板点按钮：[http://localhost:8000/dashboard](http://localhost:8000/dashboard)

---

## 防 Spam 配置（重要）

仅靠代码层面的头部优化还不够，**必须在域名 DNS 控制台添加以下 3 条记录**，才能让 Gmail 信任你的发件域名：

### SPF — 授权阿里邮箱代发

```
类型: TXT    主机名: @
值: v=spf1 include:spf.qiye.aliyun.com ~all
```

### DMARC — 声明域名邮件策略

```
类型: TXT    主机名: _dmarc
值: v=DMARC1; p=none; rua=mailto:support@yourdomain.com
```

### DKIM — 数字签名（需先在阿里后台生成）

1. 登录阿里企业邮箱管理后台 → **域名管理 → DKIM 管理 → 启用**
2. 复制生成的 TXT 记录，添加到你的域名 DNS

DNS 生效后（约 10-30 分钟），用 [mail-tester.com](https://www.mail-tester.com) 测试邮件评分，目标 9 分以上。

---

## API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 健康检查 |
| `/start-auto` | POST | 启动后台自动轮询（默认每 2 分钟） |
| `/stop-auto` | POST | 停止后台轮询 |
| `/check` | POST | 立即执行一次邮件检查 |
| `/status` | GET | 查看服务状态 |
| `/emails` | GET | 查看当前未读邮件列表 |
| `/kols` | GET | 查看所有 KOL 会话进度 |
| `/logs` | GET | 获取最近运行日志 |
| `/dashboard` | GET | **可视化监控面板（推荐）** |

---

## LLM 服务商切换

只需修改 `.env`，无需改代码：

```env
# 通义千问（当前默认）
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

# OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o

# DeepSeek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# 本地 Ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3
```

---

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── config.py          # 配置管理（读取 .env）
│   ├── database.py        # SQLite 状态持久化
│   ├── mail_service.py    # 阿里邮箱 IMAP 收件 + SMTP 防 Spam 发件
│   ├── llm_service.py     # LLM 调用 + 阶段判断 + 回复生成
│   ├── agent.py           # KOL 状态机核心逻辑
│   └── main.py            # FastAPI 应用 + 后台轮询
├── kol_agent.db           # SQLite 数据库（自动创建）
├── .env                   # 环境变量配置（纯 ASCII）
├── requirements.txt       # Python 依赖
└── README.md
```

---

## 注意事项

- `.env` 文件**必须使用纯 ASCII 编码**（不含中文字符），否则 `dotenv_values` 解析会静默失败
- `EMAIL_PASSWORD` 填写邮箱**授权码**（非登录密码），在阿里企业邮箱后台生成
- `BRAND_SIGNATURE` 会出现在每封回复邮件的署名处，请填写真实信息
- `POLL_INTERVAL` 默认 120 秒（2 分钟），不建议设置过小以免触发 IMAP 频率限制
- 已处理过的邮件 `Message-ID` 记录在 `kol_agent.db` 中，不会重复回复
