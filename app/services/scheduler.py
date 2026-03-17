from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Task
from app.services.logger import log_action
from app.services.telegram_service import send_telegram_message


scheduler = BackgroundScheduler()


def _send_due_task_notifications() -> None:
    """Scan tasks and notify when the due time has arrived."""
    db: Session = SessionLocal()
    try:
        now = datetime.utcnow()
        window_end = now + timedelta(seconds=settings.SCHEDULER_POLL_SECONDS)

        tasks = (
            db.query(Task)
            .filter(Task.due_date.isnot(None))
            .filter(Task.status != "done")
            .filter(Task.reminder_sent.is_(False))
            .filter(Task.due_date <= window_end)
            .all()
        )

        for task in tasks:
            due_text = task.due_date.strftime("%Y-%m-%d %H:%M") if task.due_date else "without date"
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
    scheduler.start()


def stop_scheduler() -> None:
    """Stop the scheduler cleanly on shutdown."""
    if scheduler.running:
        scheduler.shutdown()
