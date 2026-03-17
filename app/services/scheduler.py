from datetime import datetime, timedelta
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Task
from app.services.logger import log_action
from app.services.telegram_bot import send_ready_for_review_message
from app.services.telegram_service import send_telegram_message


scheduler = BackgroundScheduler()
logger = logging.getLogger("app.scheduler")


def _send_due_task_notifications() -> None:
    """Scan tasks and notify when the due time has arrived."""
    db: Session = SessionLocal()
    try:
        now = datetime.utcnow()
        window_end = now + timedelta(seconds=settings.SCHEDULER_POLL_SECONDS)

        tasks = (
            db.query(Task)
            .filter(Task.planned_date.isnot(None) | Task.due_date.isnot(None))
            .filter(Task.status != "done")
            .filter(Task.reminder_sent.is_(False))
            .all()
        )

        for task in tasks:
            target_date = task.planned_date or task.due_date
            if not target_date or target_date > window_end:
                continue

            due_text = target_date.strftime("%Y-%m-%d %H:%M")
            sent = send_telegram_message(
                message=f"Reminder: task '{task.title}' is due at {due_text}.",
                chat_id=task.telegram_chat_id or None,
            )
            task.reminder_sent = sent
            db.add(task)
            db.commit()
            log_action(
                db,
                action="scheduler_checked_task",
                details=f"Task #{task.id} processed by scheduler. Telegram sent: {sent}",
            )
    finally:
        db.close()


def _move_planned_tasks_to_review() -> None:
    """
    Every 5 minutes move today's planned tasks to ready_for_review.
    Only tasks with status == planned are affected.
    """
    db: Session = SessionLocal()
    try:
        today = datetime.now().date()
        planned_tasks = db.query(Task).filter(Task.status == "planned").filter(Task.planned_date.isnot(None)).all()

        logger.info("Scheduler review scan started for date %s. Planned tasks found: %s", today, len(planned_tasks))

        for task in planned_tasks:
            if task.planned_date.date() != today:
                logger.info(
                    "Task #%s skipped by review scheduler: planned_date %s is not today %s",
                    task.id,
                    task.planned_date.date(),
                    today,
                )
                continue

            previous_status = task.status
            task.status = "ready_for_review"
            task.updated_at = datetime.utcnow()
            db.add(task)
            db.commit()

            log_action(
                db,
                action="task_status_auto_updated",
                details=(
                    f"Scheduler changed task #{task.id} status from '{previous_status}' "
                    f"to 'ready_for_review' for planned_date {task.planned_date.strftime('%Y-%m-%d %H:%M')}."
                ),
            )
            telegram_sent = send_ready_for_review_message(task)
            log_action(
                db,
                action="telegram_review_notification",
                details=(
                    f"Review notification for task #{task.id} after status change to "
                    f"'ready_for_review'. Sent: {telegram_sent}."
                ),
            )
            logger.info(
                "Review notification for task #%s was requested. Sent=%s",
                task.id,
                telegram_sent,
            )
            logger.info(
                "Telegram payload preview for task #%s: sku=%s planned_date=%s delivery_type=%s quantity=%s planned_price=%s comment=%s",
                task.id,
                task.sku,
                task.planned_date,
                task.delivery_type,
                task.quantity,
                task.planned_price,
                task.comment,
            )
            logger.info("Task #%s moved to ready_for_review by scheduler", task.id)
    finally:
        db.close()


def start_scheduler() -> None:
    """Start the background scheduler once on application startup."""
    if scheduler.running:
        return

    scheduler.add_job(
        _send_due_task_notifications,
        "interval",
        seconds=settings.SCHEDULER_POLL_SECONDS,
        id="due_task_notifications",
        replace_existing=True,
    )
    scheduler.add_job(
        _move_planned_tasks_to_review,
        "interval",
        minutes=5,
        id="planned_to_ready_for_review",
        replace_existing=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    """Stop the scheduler cleanly on shutdown."""
    if scheduler.running:
        scheduler.shutdown()
