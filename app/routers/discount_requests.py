import csv
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.database import get_db
from app.models import DiscountRequest
from app.services.discount_price_service import calculate_approved_price
from app.services.logger import log_action
from app.services.ozon_discount_requests_api import OzonDiscountClient
from app.services.ozon_discount_requests_service import (
    get_all_discount_requests,
    get_discount_request_by_id,
    get_discount_request_filter_options,
    get_discount_requests_summary,
    process_discount_request_by_mode,
)


router = APIRouter(prefix="/discount-requests", tags=["discount_requests"])
templates = Jinja2Templates(directory="app/templates")


def _parse_float(raw_value: str | None) -> float | None:
    """Аккуратно разобрать число из CSV."""
    cleaned = (raw_value or "").strip().replace(",", ".")
    if not cleaned:
        return None
    return float(cleaned)


def _parse_datetime(raw_value: str | None) -> datetime | None:
    """Поддержка нескольких форматов даты для CSV."""
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return None

    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"Некорректная дата: {cleaned}")


def _normalize_discount_request_payload(item: dict) -> dict:
    """Привести запись из Ozon API к полям нашей модели."""
    external_id = (
        item.get("external_id")
        or item.get("id")
        or item.get("request_id")
        or item.get("discount_id")
        or ""
    )
    sku = item.get("sku") or item.get("offer_id") or item.get("product_sku") or ""
    product_name = item.get("product_name") or item.get("name") or item.get("title")
    current_price = item.get("current_price") or item.get("price")
    requested_discount_percent = item.get("requested_discount_percent") or item.get("discount_percent")
    requested_price = item.get("requested_price") or item.get("discount_price")
    approved_discount_percent = item.get("approved_discount_percent")
    approved_price = item.get("approved_price")
    buyer_comment = item.get("buyer_comment") or item.get("comment")

    return {
        "external_id": str(external_id).strip(),
        "sku": str(sku).strip(),
        "product_name": str(product_name).strip() if product_name else None,
        "current_price": float(current_price) if current_price not in (None, "") else None,
        "requested_discount_percent": (
            float(requested_discount_percent) if requested_discount_percent not in (None, "") else None
        ),
        "requested_price": float(requested_price) if requested_price not in (None, "") else None,
        "approved_discount_percent": (
            float(approved_discount_percent) if approved_discount_percent not in (None, "") else None
        ),
        "approved_price": float(approved_price) if approved_price not in (None, "") else None,
        "buyer_comment": str(buyer_comment).strip() if buyer_comment else None,
        "status": "новая",
    }


@router.get("/", response_class=HTMLResponse)
def discount_requests_page(
    request: Request,
    sku: str = Query(default=""),
    status: str = Query(default=""),
    send_status: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """Страница со списком заявок на скидку Ozon и простыми фильтрами."""
    requests_list = get_all_discount_requests(
        db=db,
        sku=sku.strip(),
        status=status.strip(),
        send_status=send_status.strip(),
    )
    summary = get_discount_requests_summary(db)
    filter_options = get_discount_request_filter_options(db)

    return templates.TemplateResponse(
        "discount_requests/list.html",
        {
            "request": request,
            "discount_requests": requests_list,
            "summary": summary,
            "filters": {
                "sku": sku,
                "status": status,
                "send_status": send_status,
            },
            "status_options": filter_options["status_options"],
            "send_status_options": filter_options["send_status_options"],
        },
    )


@router.post("/test-api")
def test_discount_requests_api(db: Session = Depends(get_db)):
    """Проверить, что ключи Ozon API для заявок на скидку загружены."""
    client = OzonDiscountClient()
    result = client.test_connection()

    log_action(
        db,
        "discount_requests_api_checked",
        (
            "Проверка Ozon API для заявок на скидку: "
            f"client_id_present={result['client_id_present']}, "
            f"api_key_present={result['api_key_present']}, "
            f"message={result['message']}"
        ),
    )

    return RedirectResponse(
        url=(
            "/discount-requests/?message="
            f"Проверка Ozon API: client_id_present={result['client_id_present']}, "
            f"api_key_present={result['api_key_present']}, message={result['message']}"
        ),
        status_code=303,
    )


@router.get("/load")
def load_discount_requests_from_ozon(db: Session = Depends(get_db)):
    """Загрузить заявки на скидку из Ozon и сохранить их в локальную базу."""
    client = OzonDiscountClient()
    result = client.fetch_discount_requests()

    if not result.get("ok"):
        log_action(
            db,
            "discount_requests_load_error",
            f"Ошибка загрузки заявок из Ozon: {result.get('message', 'Неизвестная ошибка')}",
        )
        return RedirectResponse(
            url=f"/discount-requests/?message=Не удалось загрузить заявки: {result.get('message', 'Неизвестная ошибка')}",
            status_code=303,
        )

    items = result.get("items") or []
    loaded = len(items)
    created = 0
    updated = 0
    errors = 0

    for item in items:
        try:
            payload = _normalize_discount_request_payload(item)
            external_id = payload.pop("external_id")

            if not external_id:
                errors += 1
                log_action(db, "discount_requests_load_row_error", "Запись из Ozon пропущена: пустой external_id.")
                continue

            discount_request = db.query(DiscountRequest).filter(DiscountRequest.external_id == external_id).first()

            if discount_request is None:
                discount_request = DiscountRequest(external_id=external_id, **payload)
                db.add(discount_request)
                db.flush()
                process_discount_request_by_mode(db, discount_request)
                created += 1
            else:
                discount_request.sku = payload["sku"]
                discount_request.product_name = payload["product_name"]
                discount_request.current_price = payload["current_price"]
                discount_request.requested_discount_percent = payload["requested_discount_percent"]
                discount_request.requested_price = payload["requested_price"]
                discount_request.approved_discount_percent = payload["approved_discount_percent"]
                discount_request.approved_price = payload["approved_price"]
                discount_request.buyer_comment = payload["buyer_comment"]
                discount_request.updated_at = datetime.utcnow()
                db.add(discount_request)
                db.flush()
                process_discount_request_by_mode(db, discount_request)
                updated += 1
        except Exception as exc:
            errors += 1
            log_action(db, "discount_requests_load_row_error", f"Ошибка обработки заявки из Ozon: {exc}")

    db.commit()
    log_action(
        db,
        "discount_requests_loaded",
        (
            f"Загрузка заявок из Ozon завершена. Загружено {loaded} заявок. "
            f"Создано {created}. Обновлено {updated}. Ошибок {errors}."
        ),
    )

    return RedirectResponse(
        url=(
            "/discount-requests/?message="
            f"Загружено {loaded} заявок. Создано {created}. Обновлено {updated}. Ошибок {errors}."
        ),
        status_code=303,
    )


@router.post("/import")
def import_discount_requests_csv(db: Session = Depends(get_db)):
    """Импортировать discount_requests.csv из корня проекта с upsert по external_id."""
    csv_path = Path(BASE_DIR) / "discount_requests.csv"
    if not csv_path.exists():
        return RedirectResponse(
            url="/discount-requests/?message=Файл discount_requests.csv не найден в корне проекта",
            status_code=303,
        )

    processed = 0
    created = 0
    updated = 0
    skipped = 0
    errors = 0

    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)

        for row_number, row in enumerate(reader, start=2):
            processed += 1

            try:
                external_id = (row.get("external_id") or "").strip()
                if not external_id:
                    skipped += 1
                    log_action(db, "discount_request_csv_row_skipped", f"Строка {row_number} пропущена: пустой external_id.")
                    continue

                discount_request = db.query(DiscountRequest).filter(DiscountRequest.external_id == external_id).first()

                payload = {
                    "sku": (row.get("sku") or "").strip(),
                    "product_name": (row.get("product_name") or "").strip() or None,
                    "current_price": _parse_float(row.get("current_price")),
                    "requested_discount_percent": _parse_float(row.get("requested_discount_percent")),
                    "requested_price": _parse_float(row.get("requested_price")),
                    "approved_discount_percent": _parse_float(row.get("approved_discount_percent")),
                    "approved_price": _parse_float(row.get("approved_price")),
                    "buyer_comment": (row.get("buyer_comment") or "").strip() or None,
                    "status": (row.get("status") or "новая").strip() or "новая",
                    "send_status": (row.get("send_status") or "ожидает").strip() or "ожидает",
                    "ozon_response": (row.get("ozon_response") or "").strip() or None,
                    "processed_at": _parse_datetime(row.get("processed_at")),
                }

                if discount_request is None:
                    discount_request = DiscountRequest(external_id=external_id, **payload)
                    db.add(discount_request)
                    db.flush()
                    process_discount_request_by_mode(db, discount_request)
                    created += 1
                else:
                    discount_request.sku = payload["sku"]
                    discount_request.product_name = payload["product_name"]
                    discount_request.current_price = payload["current_price"]
                    discount_request.requested_discount_percent = payload["requested_discount_percent"]
                    discount_request.requested_price = payload["requested_price"]
                    discount_request.approved_discount_percent = payload["approved_discount_percent"]
                    discount_request.approved_price = payload["approved_price"]
                    discount_request.buyer_comment = payload["buyer_comment"]
                    discount_request.status = payload["status"]
                    discount_request.send_status = payload["send_status"]
                    discount_request.ozon_response = payload["ozon_response"]
                    discount_request.processed_at = payload["processed_at"]
                    discount_request.updated_at = datetime.utcnow()
                    db.add(discount_request)
                    db.flush()
                    process_discount_request_by_mode(db, discount_request)
                    updated += 1
            except ValueError as exc:
                skipped += 1
                log_action(db, "discount_requests_csv_row_skipped", f"Строка {row_number} пропущена: {exc}")
            except Exception as exc:
                errors += 1
                log_action(db, "discount_requests_csv_row_error", f"Ошибка импорта строки {row_number}: {exc}")

    db.commit()
    log_action(
        db,
        "discount_requests_csv_imported",
        (
            "Импорт discount_requests.csv завершён. "
            f"Обработано: {processed}, создано: {created}, обновлено: {updated}, "
            f"пропущено: {skipped}, ошибок: {errors}."
        ),
    )

    return RedirectResponse(
        url=(
            "/discount-requests/?message="
            f"Импорт завершён. Обработано: {processed}, создано: {created}, "
            f"обновлено: {updated}, пропущено: {skipped}, ошибок: {errors}"
        ),
        status_code=303,
    )


@router.post("/{discount_request_id}/calculate")
def calculate_discount_request(discount_request_id: int, db: Session = Depends(get_db)):
    """Рассчитать одобренную скидку 3% для одной заявки."""
    discount_request = get_discount_request_by_id(db, discount_request_id)
    if discount_request is None:
        raise HTTPException(status_code=404, detail="Discount request not found")

    if discount_request.current_price is None:
        log_action(
            db,
            "discount_request_calculation_error",
            f"Не удалось рассчитать 3% для заявки #{discount_request.id}: текущая цена не заполнена.",
        )
        return RedirectResponse(
            url=f"/discount-requests/{discount_request.id}?message=Не удалось рассчитать: текущая цена не заполнена",
            status_code=303,
        )

    approved_discount_percent, approved_price = calculate_approved_price(discount_request.current_price)
    discount_request.approved_discount_percent = approved_discount_percent
    discount_request.approved_price = approved_price
    discount_request.status = "рассчитана"
    discount_request.updated_at = datetime.utcnow()
    db.add(discount_request)
    db.commit()

    log_action(
        db,
        "discount_request_calculated",
        (
            f"Для заявки #{discount_request.id} выполнен расчёт 3%. "
            f"approved_discount_percent={approved_discount_percent}, approved_price={approved_price}, "
            "status=рассчитана."
        ),
    )
    return RedirectResponse(
        url=f"/discount-requests/{discount_request.id}?message=Скидка 3% успешно рассчитана",
        status_code=303,
    )


@router.post("/{discount_request_id}/approve")
def approve_discount_request_manually(discount_request_id: int, db: Session = Depends(get_db)):
    """Подтвердить рассчитанную заявку вручную."""
    discount_request = get_discount_request_by_id(db, discount_request_id)
    if discount_request is None:
        raise HTTPException(status_code=404, detail="Discount request not found")

    discount_request.status = "одобрена"
    discount_request.processed_at = datetime.utcnow()
    discount_request.updated_at = datetime.utcnow()
    db.add(discount_request)
    db.commit()

    log_action(
        db,
        "discount_request_approved",
        f"Заявка #{discount_request.id} одобрена вручную. status=одобрена.",
    )
    return RedirectResponse(
        url=f"/discount-requests/{discount_request.id}?message=Заявка одобрена",
        status_code=303,
    )


@router.post("/{discount_request_id}/send")
def send_discount_request_to_ozon(discount_request_id: int, db: Session = Depends(get_db)):
    """Отправить одну заявку в Ozon из карточки."""
    discount_request = get_discount_request_by_id(db, discount_request_id)
    if discount_request is None:
        raise HTTPException(status_code=404, detail="Discount request not found")

    if discount_request.approved_price is None:
        log_action(
            db,
            "discount_request_send_error",
            f"Не удалось отправить заявку #{discount_request.id}: approved_price не заполнена.",
        )
        return RedirectResponse(
            url=f"/discount-requests/{discount_request.id}?message=Не удалось отправить: сначала рассчитайте одобренную цену",
            status_code=303,
        )

    client = OzonDiscountClient()
    result = client.approve_discount_request(
        external_id=discount_request.external_id,
        approved_discount_percent=3,
        approved_price=discount_request.approved_price,
    )

    discount_request.processed_at = datetime.utcnow()
    discount_request.updated_at = datetime.utcnow()
    discount_request.ozon_response = str(result.get("raw") or result.get("message") or "")

    if result.get("ok"):
        discount_request.send_status = "успешно"
        discount_request.status = "отправлена"
        db.add(discount_request)
        db.commit()

        log_action(
            db,
            "discount_request_sent",
            (
                f"Заявка #{discount_request.id} успешно отправлена в Ozon. "
                f"status=отправлена, send_status=успешно, status_code={result.get('status_code')}."
            ),
        )
        return RedirectResponse(
            url=f"/discount-requests/{discount_request.id}?message=Заявка отправлена в Ozon",
            status_code=303,
        )

    discount_request.send_status = "ошибка"
    discount_request.status = "ошибка"
    db.add(discount_request)
    db.commit()

    log_action(
        db,
        "discount_request_send_error",
        (
            f"Ошибка отправки заявки #{discount_request.id} в Ozon. "
            f"status=ошибка, send_status=ошибка, status_code={result.get('status_code')}, "
            f"message={result.get('message')}"
        ),
    )
    return RedirectResponse(
        url=f"/discount-requests/{discount_request.id}?message={result.get('message', 'Ошибка отправки заявки')}",
        status_code=303,
    )


@router.post("/{discount_request_id}/archive")
def archive_discount_request(discount_request_id: int, db: Session = Depends(get_db)):
    """Перевести заявку в архивный статус."""
    discount_request = get_discount_request_by_id(db, discount_request_id)
    if discount_request is None:
        raise HTTPException(status_code=404, detail="Discount request not found")

    discount_request.status = "архив"
    discount_request.updated_at = datetime.utcnow()
    db.add(discount_request)
    db.commit()

    log_action(
        db,
        "discount_request_archived",
        f"Заявка #{discount_request.id} переведена в архив. status=архив.",
    )
    return RedirectResponse(
        url=f"/discount-requests/{discount_request.id}?message=Заявка отправлена в архив",
        status_code=303,
    )


@router.get("/{discount_request_id}", response_class=HTMLResponse)
def discount_request_detail_page(
    discount_request_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Детальная страница одной заявки на скидку."""
    discount_request = get_discount_request_by_id(db, discount_request_id)
    if discount_request is None:
        raise HTTPException(status_code=404, detail="Discount request not found")

    return templates.TemplateResponse(
        "discount_requests/detail.html",
        {
            "request": request,
            "discount_request": discount_request,
            "message": request.query_params.get("message", ""),
        },
    )
