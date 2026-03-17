import asyncio
import logging
import threading
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app.config import settings
from app.database import SessionLocal
from app.models import Task
from app.services.logger import log_action


logger = logging.getLogger("app.telegram_bot")
_bot_thread: threading.Thread | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_application: Application | None = None


def _build_review_message(task: Task) -> str:
    """Build a compact Telegram message with all fields needed for review."""
    planned_date = task.planned_date.strftime("%Y-%m-%d %H:%M") if task.planned_date else "-"
    return (
        "Task is ready for review\n\n"
        f"SKU: {task.sku or '-'}\n"
        f"Planned date: {planned_date}\n"
        f"Delivery type: {task.delivery_type or '-'}\n"
        f"Quantity: {task.quantity}\n"
        f"Planned price: {task.planned_price or '-'}\n"
        f"Comment: {task.comment or '-'}"
    )


def _build_review_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Inline actions for reviewing a task directly from Telegram."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Подтвердить", callback_data=f"task_action:{task_id}:approved"),
                InlineKeyboardButton("Отложить", callback_data=f"task_action:{task_id}:postponed"),
                InlineKeyboardButton("Отменить", callback_data=f"task_action:{task_id}:cancelled"),
            ]
        ]
    )


async def _handle_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update task status from Telegram inline buttons."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    await query.answer()

    try:
        _, task_id_raw, new_status = query.data.split(":")
        task_id = int(task_id_raw)
    except ValueError:
        logger.warning("Unsupported callback payload: %s", query.data)
        await query.edit_message_text("Unsupported action payload.")
        return

    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            await query.edit_message_text("Task not found.")
            return

        previous_status = task.status
        task.status = new_status
        task.updated_at = datetime.utcnow()
        db.add(task)
        db.commit()

        log_action(
            db,
            "task_status_updated_from_telegram",
            f"Telegram changed task #{task.id} status from '{previous_status}' to '{new_status}'.",
        )

        await query.edit_message_text(
            text=_build_review_message(task) + f"\n\nNew status: {task.status}",
        )
    except Exception as exc:
        logger.exception("Telegram callback failed for payload %s: %s", query.data, exc)
        await query.edit_message_text("Failed to update task status.")
    finally:
        db.close()


def send_ready_for_review_message(task: Task) -> bool:
    """
    Send a Telegram message when a task becomes ready_for_review.
    Returns False when Telegram is not configured or sending fails.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram bot token is not configured. Skip review notification for task #%s.", task.id)
        return False

    target_chat_id = task.telegram_chat_id or settings.TELEGRAM_CHAT_ID or settings.TELEGRAM_DEFAULT_CHAT_ID
    if not target_chat_id:
        logger.warning("Telegram chat id is not configured. Skip review notification for task #%s.", task.id)
        return False

    application = _bot_application
    if application is None or _bot_loop is None:
        logger.warning("Telegram bot application is not running. Skip review notification for task #%s.", task.id)
        return False

    future = asyncio.run_coroutine_threadsafe(
        application.bot.send_message(
            chat_id=target_chat_id,
            text=_build_review_message(task),
            reply_markup=_build_review_keyboard(task.id),
        ),
        _bot_loop,
    )

    try:
        future.result(timeout=15)
        logger.info("Telegram review message sent for task #%s", task.id)
        return True
    except Exception as exc:
        logger.exception("Failed to send Telegram review message for task #%s: %s", task.id, exc)
        return False


async def _telegram_runner() -> None:
    """Run Telegram polling inside a dedicated event loop thread."""
    global _bot_application

    application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CallbackQueryHandler(_handle_task_action, pattern=r"^task_action:"))
    _bot_application = application

    await application.initialize()
    await application.start()
    if application.updater is not None:
        await application.updater.start_polling()


async def _telegram_shutdown() -> None:
    """Stop Telegram polling and clean up the application."""
    global _bot_application

    if _bot_application is None:
        return

    if _bot_application.updater is not None:
        await _bot_application.updater.stop()
    await _bot_application.stop()
    await _bot_application.shutdown()
    _bot_application = None


def _run_bot_thread() -> None:
    """Thread entry point for Telegram polling."""
    global _bot_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _bot_loop = loop

    try:
        loop.run_until_complete(_telegram_runner())
        loop.run_forever()
    finally:
        loop.run_until_complete(_telegram_shutdown())
        loop.close()
        _bot_loop = None


def start_telegram_bot() -> None:
    """Start the Telegram bot polling thread if a token is configured."""
    global _bot_thread

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.info("Telegram bot token is empty. Bot polling is disabled.")
        return

    if _bot_thread and _bot_thread.is_alive():
        return

    _bot_thread = threading.Thread(target=_run_bot_thread, name="telegram-bot-thread", daemon=True)
    _bot_thread.start()
    logger.info("Telegram bot polling thread started.")


def stop_telegram_bot() -> None:
    """Stop the Telegram bot thread gracefully."""
    global _bot_thread

    if _bot_loop is not None:
        shutdown_future = asyncio.run_coroutine_threadsafe(_telegram_shutdown(), _bot_loop)
        try:
            shutdown_future.result(timeout=15)
        except Exception as exc:
            logger.exception("Telegram bot shutdown failed: %s", exc)
        _bot_loop.call_soon_threadsafe(_bot_loop.stop)

    if _bot_thread and _bot_thread.is_alive():
        _bot_thread.join(timeout=15)

    _bot_thread = None
