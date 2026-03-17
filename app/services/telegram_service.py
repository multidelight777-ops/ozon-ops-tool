import asyncio
import logging

from telegram import Bot

from app.config import settings


logger = logging.getLogger("app.telegram")


def send_telegram_message(message: str, chat_id: str | None = None) -> bool:
    """
    Send a Telegram message if credentials are configured.
    Returns False instead of raising so the dashboard remains usable without Telegram.
    """
    token = settings.TELEGRAM_BOT_TOKEN
    target_chat_id = chat_id or settings.TELEGRAM_CHAT_ID or settings.TELEGRAM_DEFAULT_CHAT_ID

    if not token or not target_chat_id:
        return False

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=target_chat_id, text=message)

    try:
        asyncio.run(_send())
        return True
    except Exception:
        return False


def log_telegram_message(message: str, chat_id: str | None = None) -> bool:
    """
    Temporary Telegram stub for scheduled status updates.
    We log the outgoing message instead of sending it to the API.
    """
    target_chat_id = chat_id or settings.TELEGRAM_CHAT_ID or settings.TELEGRAM_DEFAULT_CHAT_ID or "not_set"
    logger.info("Telegram stub message to %s: %s", target_chat_id, message)
    return True
