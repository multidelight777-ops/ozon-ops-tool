from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models import DiscountRequest
from app.services.discount_price_service import calculate_approved_price
from app.services.logger import log_action


SUPPORTED_DISCOUNT_REQUESTS_MODES = {
    "только_черновики",
    "требуется_проверка",
    "авто_одобрение_3процента",
}


def get_discount_requests_mode() -> str:
    """Вернуть активный режим обработки заявок из .env."""
    mode = (settings.DISCOUNT_REQUESTS_MODE or "").strip()
    if mode in SUPPORTED_DISCOUNT_REQUESTS_MODES:
        return mode
    return "требуется_проверка"


def build_discount_requests_query(
    db: Session,
    sku: str = "",
    status: str = "",
    send_status: str = "",
):
    """Собрать базовый query с простыми фильтрами для страницы заявок."""
    query = db.query(DiscountRequest)

    if sku:
        query = query.filter(DiscountRequest.sku.ilike(f"%{sku}%"))

    if status:
        query = query.filter(DiscountRequest.status == status)

    if send_status:
        query = query.filter(DiscountRequest.send_status == send_status)

    return query


def get_all_discount_requests(
    db: Session,
    sku: str = "",
    status: str = "",
    send_status: str = "",
) -> list[DiscountRequest]:
    """Вернуть список заявок на скидку с учётом фильтров."""
    return (
        build_discount_requests_query(
            db=db,
            sku=sku,
            status=status,
            send_status=send_status,
        )
        .order_by(DiscountRequest.created_at.desc())
        .all()
    )


def get_discount_request_by_id(db: Session, discount_request_id: int) -> DiscountRequest | None:
    """Вернуть одну заявку по внутреннему ID."""
    return db.query(DiscountRequest).filter(DiscountRequest.id == discount_request_id).first()


def get_discount_requests_summary(db: Session) -> dict[str, int]:
    """Подготовить сводку по ключевым статусам заявок."""
    total = db.query(DiscountRequest).count()
    pending = db.query(DiscountRequest).filter(DiscountRequest.status == "новая").count()
    processed = (
        db.query(DiscountRequest)
        .filter(DiscountRequest.status.in_(["рассчитана", "одобрена", "отправлена"]))
        .count()
    )
    errors = db.query(DiscountRequest).filter(DiscountRequest.send_status == "ошибка").count()

    return {
        "total": total,
        "pending": pending,
        "processed": processed,
        "errors": errors,
    }


def get_discount_request_filter_options(db: Session) -> dict[str, list[str]]:
    """Собрать значения фильтров для select-полей."""
    status_options = [
        row[0]
        for row in db.query(DiscountRequest.status)
        .distinct()
        .order_by(DiscountRequest.status)
        .all()
        if row[0]
    ]
    send_status_options = [
        row[0]
        for row in db.query(DiscountRequest.send_status)
        .distinct()
        .order_by(DiscountRequest.send_status)
        .all()
        if row[0]
    ]

    return {
        "status_options": status_options,
        "send_status_options": send_status_options,
    }


def apply_discount_calculation(discount_request: DiscountRequest) -> bool:
    """Рассчитать и сохранить фиксированную скидку 3%, если есть текущая цена."""
    if discount_request.current_price is None:
        return False

    approved_discount_percent, approved_price = calculate_approved_price(discount_request.current_price)
    discount_request.approved_discount_percent = approved_discount_percent
    discount_request.approved_price = approved_price
    discount_request.status = "рассчитана"
    discount_request.updated_at = datetime.utcnow()
    return True


def mark_discount_request_approved(discount_request: DiscountRequest) -> None:
    """Перевести заявку в статус одобрения."""
    discount_request.status = "одобрена"
    discount_request.processed_at = datetime.utcnow()
    discount_request.updated_at = datetime.utcnow()


def send_discount_request_stub(discount_request: DiscountRequest) -> bool:
    """Временная отправка-заглушка до подключения реального Ozon API."""
    try:
        discount_request.send_status = "успешно"
        discount_request.status = "отправлена"
        discount_request.processed_at = datetime.utcnow()
        discount_request.ozon_response = "Тестовая отправка заявки выполнена без реального запроса в Ozon API."
        discount_request.updated_at = datetime.utcnow()
        return True
    except Exception as exc:
        discount_request.status = "ошибка"
        discount_request.send_status = "ошибка"
        discount_request.ozon_response = f"Ошибка отправки: {exc}"
        discount_request.updated_at = datetime.utcnow()
        return False


def process_discount_request_by_mode(db: Session, discount_request: DiscountRequest) -> None:
    """Применить режим обработки заявок после загрузки из CSV или будущего API."""
    mode = get_discount_requests_mode()

    if mode == "только_черновики":
        calculated = apply_discount_calculation(discount_request)
        if calculated:
            log_action(
                db,
                "discount_request_mode_applied",
                f"Для заявки #{discount_request.id} применён режим только_черновики: рассчитана скидка 3%.",
            )
        else:
            log_action(
                db,
                "discount_request_mode_applied",
                (
                    f"Для заявки #{discount_request.id} применён режим только_черновики, "
                    "но расчёт не выполнен: текущая цена не заполнена."
                ),
            )
        db.add(discount_request)
        return

    if mode == "авто_одобрение_3процента":
        calculated = apply_discount_calculation(discount_request)
        if not calculated:
            discount_request.status = "ошибка"
            discount_request.send_status = "ошибка"
            discount_request.ozon_response = "Автообработка не выполнена: текущая цена не заполнена."
            discount_request.updated_at = datetime.utcnow()
            db.add(discount_request)
            log_action(
                db,
                "discount_request_mode_applied",
                (
                    f"Для заявки #{discount_request.id} автообработка завершилась ошибкой: "
                    "текущая цена не заполнена."
                ),
            )
            return

        mark_discount_request_approved(discount_request)
        sent = send_discount_request_stub(discount_request)
        db.add(discount_request)
        log_action(
            db,
            "discount_request_mode_applied",
            (
                f"Для заявки #{discount_request.id} применён режим авто_одобрение_3процента. "
                f"Отправка успешна: {sent}."
            ),
        )
        return

    # Режим по умолчанию: ждём ручного одобрения и отправки.
    db.add(discount_request)
    log_action(
        db,
        "discount_request_mode_applied",
        (
            f"Для заявки #{discount_request.id} применён режим требуется_проверка: "
            "ожидается ручное одобрение и отправка."
        ),
    )


def log_discount_request_action(db: Session, action: str, details: str) -> None:
    """Писать события по заявкам на скидку в общий AuditLog."""
    log_action(db, action, details)
