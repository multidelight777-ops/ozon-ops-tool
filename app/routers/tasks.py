import csv
import io
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.database import get_db
from app.models import Task
from app.services.logger import log_action
from app.services.telegram_service import send_telegram_message


router = APIRouter(prefix="/tasks", tags=["tasks"])
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger("app.csv_importer")


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


def _parse_planned_date(raw_value: str) -> datetime:
    """Parse planned_date strictly and raise a clear error for unsupported formats."""
    cleaned_value = (raw_value or "").strip()
    if not cleaned_value:
        raise ValueError("planned_date is empty")

    for fmt in (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            parsed = datetime.strptime(cleaned_value, fmt)
            if fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                return parsed.replace(hour=9, minute=0)
            return parsed
        except ValueError:
            continue

    raise ValueError(f"unsupported planned_date format: '{cleaned_value}'")


def _parse_int(raw_value: str) -> int:
    """Convert quantity safely so broken CSV values do not crash the import."""
    try:
        return int((raw_value or "").strip())
    except ValueError:
        logger.warning("Quantity value '%s' is invalid. Falling back to 0.", raw_value)
        return 0


@router.get("/", response_class=HTMLResponse)
def list_tasks(request: Request, db: Session = Depends(get_db)):
    """Task list page with forms for create and CSV import."""
    tasks = (
        db.query(Task)
        .order_by(func.coalesce(Task.planned_date, Task.due_date).asc().nullslast(), Task.created_at.desc())
        .all()
    )
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
        comment=description.strip(),
        assignee=assignee.strip(),
        due_date=_parse_due_date(due_date.strip()),
        planned_date=_parse_due_date(due_date.strip()),
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
            comment=(row.get("description") or "").strip(),
            assignee=(row.get("assignee") or "").strip(),
            due_date=_parse_due_date((row.get("due_date") or "").strip()),
            planned_date=_parse_due_date((row.get("due_date") or "").strip()),
            telegram_chat_id=(row.get("telegram_chat_id") or "").strip(),
        )
        db.add(task)
        imported_count += 1

    db.commit()
    log_action(db, "tasks_imported", f"{imported_count} tasks imported from CSV '{file.filename}'.")
    return RedirectResponse(url="/tasks/", status_code=303)


@router.post("/import-plans")
def import_plans_csv(db: Session = Depends(get_db)):
    """
    Import plans from the root-level plans.csv file.
    Existing rows are matched by sku + planned_date.
    """
    csv_path = Path(BASE_DIR) / "plans.csv"
    if not csv_path.exists():
        return RedirectResponse(url="/?message=Файл plans.csv не найден в корне проекта", status_code=303)

    processed_count = 0
    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)

        for row_number, row in enumerate(reader, start=2):
            processed_count += 1
            logger.info("plans.csv row %s received: %s", row_number, row)

            try:
                sku = (row.get("sku") or "").strip()
                if not sku:
                    skipped_count += 1
                    logger.warning("plans.csv row %s skipped: sku is empty. Row: %s", row_number, row)
                    continue

                planned_date_raw = row.get("planned_date") or ""
                planned_date = _parse_planned_date(planned_date_raw)
                logger.info(
                    "plans.csv row %s planned_date parsed successfully: %s -> %s",
                    row_number,
                    planned_date_raw,
                    planned_date.isoformat(),
                )

                task = (
                    db.query(Task)
                    .filter(Task.sku == sku)
                    .filter(Task.planned_date == planned_date)
                    .first()
                )

                if task is None:
                    task = Task(
                        title=(row.get("title") or f"Plan for SKU {sku}").strip(),
                        sku=sku,
                        quantity=_parse_int(row.get("quantity") or ""),
                        delivery_type=(row.get("delivery_type") or "").strip(),
                        planned_price=(row.get("planned_price") or "").strip(),
                        comment=(row.get("comment") or "").strip(),
                        description=(row.get("comment") or "").strip(),
                        planned_date=planned_date,
                        due_date=planned_date,
                        status="new",
                    )
                    db.add(task)
                    created_count += 1
                    logger.info(
                        "plans.csv row %s created task for sku=%s planned_date=%s",
                        row_number,
                        sku,
                        planned_date.isoformat(),
                    )
                    continue

                # We refresh planning fields only; task status remains untouched.
                task.quantity = _parse_int(row.get("quantity") or "")
                task.delivery_type = (row.get("delivery_type") or "").strip()
                task.planned_price = (row.get("planned_price") or "").strip()
                task.comment = (row.get("comment") or "").strip()
                task.description = task.comment
                task.updated_at = datetime.utcnow()
                db.add(task)
                updated_count += 1
                logger.info(
                    "plans.csv row %s updated task #%s for sku=%s planned_date=%s",
                    row_number,
                    task.id,
                    sku,
                    planned_date.isoformat(),
                )
            except ValueError as exc:
                skipped_count += 1
                logger.warning("plans.csv row %s skipped: %s. Row: %s", row_number, exc, row)
            except Exception as exc:
                error_count += 1
                logger.exception("plans.csv row %s failed with unexpected error: %s. Row: %s", row_number, exc, row)

    db.commit()
    log_action(
        db,
        "plans_csv_imported",
        "Импортирован plans.csv из корня проекта. "
        f"Обработано: {processed_count}, создано: {created_count}, обновлено: {updated_count}, "
        f"пропущено: {skipped_count}, ошибок: {error_count}.",
    )
    return RedirectResponse(
        url=(
            "/?message="
            f"Импорт plans.csv завершён. Обработано: {processed_count}, создано: {created_count}, "
            f"обновлено: {updated_count}, пропущено: {skipped_count}, ошибок: {error_count}"
        ),
        status_code=303,
    )
