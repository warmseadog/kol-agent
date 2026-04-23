from pathlib import Path
from dotenv import dotenv_values

# 直接用 dotenv_values 解析 .env，不依赖 os.environ，彻底规避系统环境变量缓存问题
_env_path = Path(__file__).parent.parent / ".env"
_env = dotenv_values(_env_path)


def _get(key: str, default: str = "") -> str:
    return _env.get(key) or default


class Config:
    # Alibaba Enterprise Mail
    EMAIL_ADDRESS: str = _get("EMAIL_ADDRESS")
    EMAIL_PASSWORD: str = _get("EMAIL_PASSWORD")
    IMAP_HOST: str = _get("IMAP_HOST", "imap.qiye.aliyun.com")
    IMAP_PORT: int = int(_get("IMAP_PORT", "993"))
    SMTP_HOST: str = _get("SMTP_HOST", "smtp.qiye.aliyun.com")
    SMTP_PORT: int = int(_get("SMTP_PORT", "465"))

    # LLM
    LLM_API_KEY:  str = _get("LLM_API_KEY")
    LLM_BASE_URL: str = _get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    LLM_MODEL:    str = _get("LLM_MODEL", "qwen-plus")
    LLM_TIMEOUT:  int = int(_get("LLM_TIMEOUT", "60"))

    # Brand
    BRAND_NAME:        str = _get("BRAND_NAME", "Our Brand")
    BRAND_SIGNATURE:   str = _get("BRAND_SIGNATURE", "The Partnership Team")
    # 发件人显示名（会出现在收件人的 From 字段，真实人名比纯邮箱地址可信度更高）
    SENDER_DISPLAY_NAME: str = _get("SENDER_DISPLAY_NAME", "Support Team")

    # Database
    DB_FILE: str = _get("DB_FILE", "kol_agent.db")

    # Thread memory
    MAX_THREAD_MESSAGES:  int = int(_get("MAX_THREAD_MESSAGES", "10"))
    BODY_EXCERPT_LENGTH:  int = int(_get("BODY_EXCERPT_LENGTH", "600"))

    # Products
    PRODUCTS_PATH: str = _get("PRODUCTS_PATH", "data/products.json")

    # Server
    HOST:                 str = _get("HOST", "0.0.0.0")
    PORT:                 int = int(_get("PORT", "8000"))
    POLL_INTERVAL:        int = int(_get("POLL_INTERVAL", "120"))
    MAX_EMAILS_PER_CYCLE: int = int(_get("MAX_EMAILS_PER_CYCLE", "20"))


config = Config()
