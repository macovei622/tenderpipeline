"""
config.py — Централізована конфігурація застосунку
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

# ── OpenRouter ────────────────────────────────────────────
# Єдиний ключ для всіх моделей (Gemini, Claude, DeepSeek, Qwen, Llama...)
# Отримати: https://openrouter.ai/keys
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")

# ── Prozorro API ──────────────────────────────────────────
PROZORRO_API_BASE = "https://public-api.prozorro.gov.ua/api/2.5"
PROZORRO_REQUEST_TIMEOUT = 30  # секунди

# ── Фільтри для моніторингу (Вінницька область) ──────────
TARGET_REGION   = "Вінницька область"
MIN_AMOUNT      = 2_000_000    # грн
MAX_AMOUNT      = 20_000_000   # грн
TARGET_CPV_PREFIX = "45"       # Будівельні роботи (45000000-7 і підкатегорії)

# ── Бізнес-параметри ─────────────────────────────────────
SUCCESS_FEE_PERCENT = 5.0      # %
FREELANCER_COST_PER_AUDIT = 1500  # грн за перевірку пакету

# ── База даних ────────────────────────────────────────────
DB_TYPE = os.getenv("DB_TYPE", "sqlite")  # sqlite або postgres
DATABASE_PATH = os.getenv("DATABASE_PATH", "tender_service.db")
DATABASE_URL = os.getenv("DATABASE_URL", "")  # наприклад, postgresql+asyncpg://user:pass@host/db

# ── Webhook для продакшну ──────────────────────────────────
WEBHOOK_ENABLED = os.getenv("WEBHOOK_ENABLED", "False").lower() == "true"
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")  # наприклад, https://mybot.domain.com
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_PATH = f"/webhook/bot/{TELEGRAM_BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else ""

# ── Логування ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
