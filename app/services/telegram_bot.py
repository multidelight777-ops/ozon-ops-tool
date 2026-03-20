import asyncio
import logging
import threading
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app.config import settings
from app.database import SessionLocal
from app.models import Review, Task
from app.services.logger import log_action


logger = logging.getLogger("app.telegram_bot")
_bot_thread: threading.Thread | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_application: Application | None = None


def _is_updater_running() -> bool:
    """Return True only when the Telegram updater exists and is really running."""
    if _bot_application is None or _bot_application.updater is None:
        return False
    return bool(getattr(_bot_application.updater, "running", False))


def _build_task_message(task: Task) -> str:
    """Build a compact Telegram message with all task fields needed for review."""
    planned_date = task.planned_date.strftime("%Y-%m-%d %H:%M") if task.planned_date else "-"
    return (
        "Задача готова к проверке\n\n"
        f"SKU: {task.sku or '-'}\n"
        f"Плановая дата: {planned_date}\n"
        f"Тип поставки: {task.delivery_type or '-'}\n"
        f"Количество: {task.quantity}\n"
        f"Плановая цена: {task.planned_price or '-'}\n"
        f"Комментарий: {task.comment or '-'}"
    )


def _build_task_keyboard(task_id: int) -> InlineKeyboardMarkup:
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


def _build_high_risk_review_message(review: Review) -> str:
    """Build the exact Telegram message for a new high-risk review or question."""
    rating_text = str(review.rating) if review.rating is not None else "-"
    source_type = "вопрос" if review.source_type == "question" else "отзыв"
    return (
        "⚠️ Новый отзыв высокого риска\n\n"
        f"SKU: {review.sku or '-'}\n"
        f"Оценка: {rating_text}\n"
        f"Автор: {review.author_name or '-'}\n"
        f"Тип: {source_type}\n"
        f"Текст:\n{review.text or '-'}\n\n"
        f"Черновик ответа:\n{review.draft_reply or '-'}"
    )


def _build_high_risk_review_keyboard(review_id: int) -> InlineKeyboardMarkup:
    """Inline buttons for a new high-risk review notification."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Открыть в панели",
                    url=f"{settings.APP_BASE_URL}/reviews/{review_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "Одобрить ответ",
                    callback_data=f"review_action:{review_id}:approve_reply",
                ),
                InlineKeyboardButton(
                    "Пометить как ручная обработка",
                    callback_data=f"review_action:{review_id}:manual_only",
                ),
            ],
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
        logger.warning("Некорректный callback payload задачи: %s", query.data)
        await query.edit_message_text("Некорректное действие для задачи.")
        return

    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            await query.edit_message_text("Задача не найдена.")
            return

        previous_status = task.status
        task.status = new_status
        task.updated_at = datetime.utcnow()
        db.add(task)
        db.commit()

        log_action(
            db,
            "task_status_updated_from_telegram",
            f"Статус задачи #{task.id} изменён из Telegram: {previous_status} -> {new_status}.",
        )

        await query.edit_message_text(
            text=_build_task_message(task) + f"\n\nНовый статус: {task.status}",
        )
    except Exception as exc:
        logger.exception("Ошибка обработки Telegram callback для задачи %s: %s", query.data, exc)
        await query.edit_message_text("Не удалось обновить статус задачи.")
    finally:
        db.close()


async def _handle_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process inline actions for review moderation from Telegram."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    await query.answer()

    try:
        _, review_id_raw, action_name = query.data.split(":")
        review_id = int(review_id_raw)
    except ValueError:
        logger.warning("Некорректный callback payload отзыва: %s", query.data)
        await query.edit_message_text("Некорректное действие для отзыва.")
        return

    db = SessionLocal()
    try:
        review = db.query(Review).filter(Review.id == review_id).first()
        if review is None:
            await query.edit_message_text("Отзыв не найден.")
            return

        if action_name == "approve_reply":
            if not review.draft_reply:
                await query.edit_message_text("У отзыва нет черновика ответа для одобрения.")
                return

            review.final_reply = review.draft_reply
            review.status = "одобрен"
            review.updated_at = datetime.utcnow()
            db.add(review)
            db.commit()

            log_action(
                db,
                "review_telegram_action",
                f"Для отзыва #{review.id} ответ одобрен из Telegram.",
            )
            await query.edit_message_text(
                _build_high_risk_review_message(review) + "\n\nДействие: ответ одобрен"
            )
            return

        if action_name == "manual_only":
            review.automation_mode = "manual_only"
            review.status = "ручная_обработка"
            review.updated_at = datetime.utcnow()
            db.add(review)
            db.commit()

            log_action(
                db,
                "review_telegram_action",
                f"Отзыв #{review.id} помечен в Telegram как требующий ручной обработки.",
            )
            await query.edit_message_text(
                _build_high_risk_review_message(review) + "\n\nДействие: помечен как ручная обработка"
            )
            return

        await query.edit_message_text("Неизвестное действие для отзыва.")
    except Exception as exc:
        logger.exception("Ошибка обработки Telegram callback для отзыва %s: %s", query.data, exc)
        await query.edit_message_text("Не удалось обработать действие для отзыва.")
    finally:
        db.close()


def send_ready_for_review_message(task: Task) -> bool:
    """
    Send a Telegram message when a task becomes ready_for_review.
    Returns False when Telegram is not configured or sending fails.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Токен Telegram-бота не настроен. Уведомление по задаче #%s пропущено.", task.id)
        return False

    target_chat_id = task.telegram_chat_id or settings.TELEGRAM_CHAT_ID or settings.TELEGRAM_DEFAULT_CHAT_ID
    if not target_chat_id:
        logger.warning("TELEGRAM_CHAT_ID не настроен. Уведомление по задаче #%s пропущено.", task.id)
        return False

    application = _bot_application
    if application is None or _bot_loop is None:
        logger.warning("Telegram-бот не запущен. Уведомление по задаче #%s пропущено.", task.id)
        return False

    future = asyncio.run_coroutine_threadsafe(
        application.bot.send_message(
            chat_id=target_chat_id,
            text=_build_task_message(task),
            reply_markup=_build_task_keyboard(task.id),
        ),
        _bot_loop,
    )

    try:
        future.result(timeout=15)
        logger.info("Уведомление по задаче #%s успешно отправлено в Telegram.", task.id)
        return True
    except Exception as exc:
        logger.exception("Не удалось отправить Telegram-уведомление по задаче #%s: %s", task.id, exc)
        return False


def send_high_risk_review_message(review: Review) -> bool:
    """
    Send a Telegram message for a high-risk review or question.
    Returns False when Telegram is not configured or sending fails.
    """
    if review.risk_level != "высокий":
        logger.info("Отзыв #%s не требует high-risk Telegram-уведомления.", review.id)
        return False

    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Токен Telegram-бота не настроен. Уведомление по отзыву #%s пропущено.", review.id)
        return False

    target_chat_id = settings.TELEGRAM_CHAT_ID or settings.TELEGRAM_DEFAULT_CHAT_ID
    if not target_chat_id:
        logger.warning("TELEGRAM_CHAT_ID не настроен. Уведомление по отзыву #%s пропущено.", review.id)
        return False

    application = _bot_application
    if application is None or _bot_loop is None:
        logger.warning("Telegram-бот не запущен. Уведомление по отзыву #%s пропущено.", review.id)
        return False

    future = asyncio.run_coroutine_threadsafe(
        application.bot.send_message(
            chat_id=target_chat_id,
            text=_build_high_risk_review_message(review),
            reply_markup=_build_high_risk_review_keyboard(review.id),
        ),
        _bot_loop,
    )

    try:
        future.result(timeout=15)
        logger.info("High-risk уведомление по отзыву #%s успешно отправлено в Telegram.", review.id)
        return True
    except Exception as exc:
        logger.exception("Не удалось отправить high-risk уведомление по отзыву #%s: %s", review.id, exc)
        return False


async def _telegram_runner() -> None:
    """Run Telegram polling inside a dedicated event loop thread."""
    global _bot_application

    application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CallbackQueryHandler(_handle_task_action, pattern=r"^task_action:"))
    application.add_handler(CallbackQueryHandler(_handle_review_action, pattern=r"^review_action:"))
    _bot_application = application

    await application.initialize()
    await application.start()
    if application.updater is not None:
        await application.updater.start_polling()


async def _telegram_shutdown() -> None:
    """Stop Telegram polling and clean up the application."""
    global _bot_application

    if _bot_application is None:
        logger.info("Остановка Telegram-бота пропущена: приложение бота не было запущено.")
        return

    try:
        if _bot_application.updater is not None and _is_updater_running():
            await _bot_application.updater.stop()
            logger.info("Telegram Updater успешно остановлен.")
        else:
            logger.info("Остановка Telegram Updater не требуется: он не был запущен.")
    except Exception as exc:
        logger.warning("Не удалось корректно остановить Telegram Updater: %s", exc)

    try:
        if getattr(_bot_application, "running", False):
            await _bot_application.stop()
            logger.info("Telegram Application успешно остановлено.")
        else:
            logger.info("Остановка Telegram Application не требуется: оно не было запущено.")
    except Exception as exc:
        logger.warning("Не удалось корректно остановить Telegram Application: %s", exc)

    try:
        await _bot_application.shutdown()
        logger.info("Telegram Application успешно завершило shutdown.")
    except Exception as exc:
        logger.warning("Во время shutdown Telegram Application возникла ошибка: %s", exc)
    finally:
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
        logger.info("Токен Telegram-бота пустой. Polling не запускается.")
        return

    if _bot_thread and _bot_thread.is_alive():
        return

    _bot_thread = threading.Thread(target=_run_bot_thread, name="telegram-bot-thread", daemon=True)
    _bot_thread.start()
    logger.info("Поток Telegram-бота запущен.")


def stop_telegram_bot() -> None:
    """Stop the Telegram bot thread gracefully."""
    global _bot_thread

    if _bot_loop is None:
        logger.info("Остановка Telegram-бота пропущена: event loop не был запущен.")
    else:
        try:
            shutdown_future = asyncio.run_coroutine_threadsafe(_telegram_shutdown(), _bot_loop)
            shutdown_future.result(timeout=15)
        except Exception as exc:
            logger.warning("Безопасная остановка Telegram-бота завершилась с предупреждением: %s", exc)
        finally:
            try:
                if _bot_loop.is_running():
                    _bot_loop.call_soon_threadsafe(_bot_loop.stop)
                    logger.info("Telegram event loop остановлен.")
            except Exception as exc:
                logger.warning("Не удалось корректно остановить Telegram event loop: %s", exc)

    try:
        if _bot_thread and _bot_thread.is_alive():
            _bot_thread.join(timeout=15)
            logger.info("Поток Telegram-бота завершён.")
        else:
            logger.info("Поток Telegram-бота уже был остановлен или не запускался.")
    except Exception as exc:
        logger.warning("Не удалось корректно дождаться завершения потока Telegram-бота: %s", exc)

    _bot_thread = None
