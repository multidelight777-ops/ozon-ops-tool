from sqlalchemy import func
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db
from app.models import ActionLog, Task


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Main dashboard page with quick stats and recent actions."""
    total_tasks = db.query(func.count(Task.id)).scalar() or 0
    open_tasks = db.query(func.count(Task.id)).filter(Task.status != "done").scalar() or 0
    done_tasks = db.query(func.count(Task.id)).filter(Task.status == "done").scalar() or 0
    recent_tasks = db.query(Task).order_by(Task.created_at.desc()).limit(10).all()
    recent_logs = db.query(ActionLog).order_by(ActionLog.created_at.desc()).limit(15).all()

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
        },
    )
