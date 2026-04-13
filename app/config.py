from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE_PATH = BASE_DIR / ".env"
load_dotenv(ENV_FILE_PATH)


class Settings:
    """Настройки проекта в одном простом объекте."""

    APP_NAME = os.getenv("APP_NAME", "Marketplace Ops Tool")
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app' / 'data' / 'ops.db'}")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
    OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
    OZON_API_KEY = os.getenv("OZON_API_KEY", "")
    OZON_SELLER_BASE_URL = os.getenv("OZON_SELLER_BASE_URL", "https://api-seller.ozon.ru")
    OZON_REVIEWS_LIST_PATH = os.getenv("OZON_REVIEWS_LIST_PATH", "/v1/review/list")
    OZON_REVIEWS_TIMEOUT_SECONDS = int(os.getenv("OZON_REVIEWS_TIMEOUT_SECONDS", "30"))
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_DEFAULT_CHAT_ID = os.getenv("TELEGRAM_DEFAULT_CHAT_ID", "")
    DISCOUNT_REQUESTS_MODE = os.getenv("DISCOUNT_REQUESTS_MODE", "требуется_проверка")
    PRICE_MONITOR_HEADLESS = os.getenv("PRICE_MONITOR_HEADLESS", "true")
    PRICE_MONITOR_TIMEOUT_MS = int(os.getenv("PRICE_MONITOR_TIMEOUT_MS", "30000"))
    HTTP_PROXY = os.getenv("HTTP_PROXY", "")
    HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")
    NO_PROXY = os.getenv("NO_PROXY", "")
    SCHEDULER_POLL_SECONDS = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))


settings = Settings()


def env_presence_map() -> dict[str, dict[str, bool]]:
    """Безопасная диагностика наличия критичных env-переменных без раскрытия секретов."""
    names = [
        "DATABASE_URL",
        "OZON_CLIENT_ID",
        "OZON_API_KEY",
        "OZON_SELLER_BASE_URL",
        "OZON_REVIEWS_LIST_PATH",
        "OZON_REVIEWS_TIMEOUT_SECONDS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "PRICE_MONITOR_HEADLESS",
        "PRICE_MONITOR_TIMEOUT_MS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    ]
    result: dict[str, dict[str, bool]] = {}
    for name in names:
        value = os.getenv(name, "")
        result[name] = {
            "found": name in os.environ,
            "non_empty": bool(str(value).strip()),
        }
    return result
