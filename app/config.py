from pathlib import Path
import os

from dotenv import load_dotenv


# Load variables from the local .env file if it exists.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    """Project settings in one simple object."""

    APP_NAME = os.getenv("APP_NAME", "Marketplace Ops Tool")
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app' / 'data' / 'ops.db'}")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_DEFAULT_CHAT_ID = os.getenv("TELEGRAM_DEFAULT_CHAT_ID", "")
    SCHEDULER_POLL_SECONDS = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))


settings = Settings()
