from sqlalchemy import func
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db
from app.models import AuditLog, Review, Task
from app.services.logger import log_action
from app.services.ozon_reviews_api import OzonClient
from app.services.ozon_reviews_service import get_recent_reviews
from app.services.telegram_service import send_telegram_message


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Main dashboard page with quick stats and recent actions."""
    total_tasks = db.query(func.count(Task.id)).scalar() or 0
    open_tasks = db.query(func.count(Task.id)).filter(Task.status != "done").scalar() or 0
    done_tasks = db.query(func.count(Task.id)).filter(Task.status == "done").scalar() or 0
    total_reviews = db.query(func.count(Review.id)).scalar() or 0
    recent_tasks = db.query(Task).order_by(Task.created_at.desc()).limit(10).all()
    recent_reviews = get_recent_reviews(db, limit=5)
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
                "total_reviews": total_reviews,
            },
            "recent_tasks": recent_tasks,
            "recent_reviews": recent_reviews,
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
        return RedirectResponse(url="/?message=Тестовое сообщение в Telegram успешно отправлено", status_code=303)

    return RedirectResponse(
        url="/?message=Не удалось отправить тестовое сообщение в Telegram. Проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID",
        status_code=303,
    )


@router.post("/ozon/test")
def test_ozon_api_settings(db: Session = Depends(get_db)):
    """Check whether Ozon API credentials are loaded from .env."""
    client = OzonClient()
    result = client.test_connection()

    log_action(
        db,
        "ozon_api_test",
        (
            "Проверка настроек Ozon API. "
            f"ok={result['ok']}, client_id_present={result['client_id_present']}, "
            f"api_key_present={result['api_key_present']}, message={result['message']}"
        ),
    )

    return RedirectResponse(url=f"/?message={result['message']}", status_code=303)
