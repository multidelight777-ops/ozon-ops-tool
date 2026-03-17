import csv
import io
from datetime import datetime

from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db
from app.models import Task
from app.services.logger import log_action
from app.services.telegram_service import send_telegram_message


router = APIRouter(prefix="/tasks", tags=["tasks"])
templates = Jinja2Templates(directory="app/templates")


def _parse_due_date(raw_value: str) -> datetime | None:
    """Accept a couple of easy date formats for manual forms and CSV."""
    if not raw_value:
        return None

    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw_value, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.replace(hour=9, minute=0)
            return parsed
        except ValueError:
            continue
    return None


@router.get("/", response_class=HTMLResponse)
def list_tasks(request: Request, db: Session = Depends(get_db)):
    """Task list page with forms for create and CSV import."""
    tasks = db.query(Task).order_by(Task.due_date.asc().nullslast(), Task.created_at.desc()).all()
    return templates.TemplateResponse("tasks/list.html", {"request": request, "tasks": tasks})


@router.post("/create")
def create_task(
    title: str = Form(...),
    description: str = Form(""),
    assignee: str = Form(""),
    due_date: str = Form(""),
    telegram_chat_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a task from the HTML form."""
    task = Task(
        title=title.strip(),
        description=description.strip(),
        assignee=assignee.strip(),
        due_date=_parse_due_date(due_date.strip()),
        telegram_chat_id=telegram_chat_id.strip(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    log_action(db, "task_created", f"Task #{task.id} created manually.")
    send_telegram_message(f"New task created: {task.title}", task.telegram_chat_id or None)

    return RedirectResponse(url="/tasks/", status_code=303)


@router.post("/{task_id}/status")
def update_task_status(task_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    """Update task status from the task list table."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.status = status
        task.updated_at = datetime.utcnow()
        db.add(task)
        db.commit()
        log_action(db, "task_status_updated", f"Task #{task.id} status changed to '{status}'.")
    return RedirectResponse(url="/tasks/", status_code=303)


@router.post("/import")
async def import_tasks(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Import tasks from a CSV file with simple column names."""
    content = await file.read()
    csv_text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(csv_text))

    imported_count = 0
    for row in reader:
        title = (row.get("title") or "").strip()
        if not title:
            continue

        task = Task(
            title=title,
            description=(row.get("description") or "").strip(),
            assignee=(row.get("assignee") or "").strip(),
            due_date=_parse_due_date((row.get("due_date") or "").strip()),
            telegram_chat_id=(row.get("telegram_chat_id") or "").strip(),
        )
        db.add(task)
        imported_count += 1

    db.commit()
    log_action(db, "tasks_imported", f"{imported_count} tasks imported from CSV '{file.filename}'.")
    return RedirectResponse(url="/tasks/", status_code=303)
