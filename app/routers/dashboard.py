from sqlalchemy import func
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db
from app.models import AuditLog, Task
from app.services.logger import log_action
from app.services.telegram_service import send_telegram_message


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Main dashboard page with quick stats and recent actions."""
    total_tasks = db.query(func.count(Task.id)).scalar() or 0
    open_tasks = db.query(func.count(Task.id)).filter(Task.status != "done").scalar() or 0
    done_tasks = db.query(func.count(Task.id)).filter(Task.status == "done").scalar() or 0
    recent_tasks = db.query(Task).order_by(Task.created_at.desc()).limit(10).all()
    recent_logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(15).all()
    message = request.query_params.get("message", "")

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": {
                "total_tasks": total_tasks,
                "open_tasks": open_tasks,
                "done_tasks": done_tasks,
            },
            "recent_tasks": recent_tasks,
            "recent_logs": recent_logs,
            "message": message,
        },
    )


@router.post("/telegram/test")
def send_test_telegram_message(db: Session = Depends(get_db)):
    """Send a simple Telegram test message from the dashboard."""
    sent = send_telegram_message("TEST OK")
    log_action(db, "telegram_test_message", f"Dashboard test Telegram message sent: {sent}")

    if sent:
        return RedirectResponse(url="/?message=Test Telegram message sent successfully", status_code=303)

    return RedirectResponse(
        url="/?message=Failed to send test Telegram message. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
        status_code=303,
    )
