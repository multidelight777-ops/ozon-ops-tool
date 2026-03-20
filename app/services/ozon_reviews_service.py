from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Review
from app.services.logger import log_action
from app.services.ozon_reviews_api import OzonClient
from app.services.reply_templates import apply_reply_template
from app.services.review_classifier import apply_review_classification
from app.services.telegram_bot import send_high_risk_review_message


def get_recent_reviews(db: Session, limit: int = 10) -> list[Review]:
    """Return the newest reviews for dashboard and the /reviews page."""
    return db.query(Review).order_by(Review.created_at.desc()).limit(limit).all()


def get_all_reviews(db: Session) -> list[Review]:
    """Return all reviews ordered by newest first."""
    return db.query(Review).order_by(Review.created_at.desc()).all()


def _parse_api_datetime(raw_value: str | None) -> datetime | None:
    """Parse a few friendly datetime formats from future API payloads."""
    cleaned_value = (raw_value or "").strip()
    if not cleaned_value:
        return None

    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(cleaned_value, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.replace(hour=9, minute=0)
            return parsed
        except ValueError:
            continue
    return None


def resolve_controlled_processing_mode(review: Review) -> str:
    """Resolve processing mode by explicit controlled automation rules."""
    if review.category == "вопрос" and review.risk_level == "низкий":
        return "авто_ответ"

    if review.rating is not None and review.rating >= 5:
        return "авто_ответ"

    if review.rating == 4:
        return "только_черновики"

    if review.rating is not None and review.rating <= 3:
        return "требуется_проверка"

    if review.automation_mode == "auto":
        return "авто_ответ"
    if review.automation_mode == "manual_only":
        return "только_черновики"
    return "требуется_проверка"


def upsert_review_from_payload(db: Session, payload: dict, source_type: str) -> tuple[Review, bool]:
    """
    Create or update a review/question from API payload data.
    Returns (review, created).
    """
    external_id = (payload.get("external_id") or "").strip()
    if not external_id:
        raise ValueError("external_id is required")

    review = db.query(Review).filter(Review.external_id == external_id).first()
    created = review is None

    if created:
        review = Review(external_id=external_id)

    review.sku = (payload.get("sku") or "").strip()
    review.product_name = (payload.get("product_name") or "").strip() or None
    review.source_type = (source_type or payload.get("source_type") or "review").strip() or "review"
    review.rating = payload.get("rating")
    review.author_name = (payload.get("author_name") or "").strip() or None
    review.text = (payload.get("text") or "").strip()
    review.published_at = _parse_api_datetime(payload.get("published_at"))
    review.updated_at = datetime.utcnow()

    if created:
        review.status = (payload.get("status") or "new").strip() or "new"
        review.send_status = (payload.get("send_status") or "pending").strip() or "pending"

    apply_review_classification(review)
    review.processing_mode = resolve_controlled_processing_mode(review)
    apply_reply_template(review)
    log_action(
        db,
        "review_classified",
        (
            f"Отзыв {external_id} классифицирован: "
            f"category={review.category}, risk_level={review.risk_level}, "
            f"automation_mode={review.automation_mode}, confidence_score={review.confidence_score}."
        ),
    )

    db.add(review)
    db.flush()
    return review, created


def send_high_risk_alert_if_needed(db: Session, review: Review) -> bool:
    """Send a Telegram alert once for a high-risk review."""
    if review.risk_level != "высокий":
        return False

    if review.high_risk_notified_at is not None:
        return False

    sent = send_high_risk_review_message(review)
    if sent:
        review.high_risk_notified_at = datetime.utcnow()
        review.updated_at = datetime.utcnow()
        db.add(review)

    log_action(
        db,
        "review_telegram_alert",
        f"Для отзыва #{review.id} отправлено Telegram-уведомление о высоком риске: {sent}.",
    )
    return sent


def try_auto_send_review(db: Session, review: Review) -> bool:
    """
    Auto-send a reply only when controlled automation allows it.
    Protection from repeats:
    - do not send again if send_status is already successful
    - do not process again if automation marker is already set
    """
    log_action(
        db,
        "review_action",
        f"Автоотправка для отзыва #{review.id} пропущена: на этом этапе разрешена только ручная тестовая отправка одной записи.",
    )
    return False

    if review.processing_mode != "авто_ответ":
        return False

    if review.automation_mode != "auto" or review.risk_level != "низкий":
        return False

    if review.send_status == "успешно" or review.last_automation_processed_at is not None:
        return False

    if not review.draft_reply:
        log_action(db, "review_action", f"Автоответ для отзыва #{review.id} не выполнен: отсутствует draft_reply.")
        return False

    client = OzonClient()
    review.final_reply = review.draft_reply
    review.status = "одобрен"
    review.updated_at = datetime.utcnow()

    if review.source_type == "question":
        sent = client.send_question_reply(review.external_id, review.final_reply)
    else:
        sent = client.send_review_reply(review.external_id, review.final_reply)

    review.last_automation_processed_at = datetime.utcnow()
    if sent:
        review.status = "отправлен"
        review.send_status = "успешно"
        review.sent_at = datetime.utcnow()
        log_action(db, "review_action", f"Для отзыва #{review.id} автоматически отправлен ответ.")
    else:
        review.status = "ошибка"
        review.send_status = "ошибка"
        log_action(db, "review_action", f"Для отзыва #{review.id} попытка автоответа завершилась ошибкой.")

    review.updated_at = datetime.utcnow()
    db.add(review)
    return sent


def sync_ozon_reviews_placeholder(db: Session) -> dict[str, int]:
    """
    Fetch reviews and questions from the Ozon client stub and process them.
    Even though the API client is still a placeholder, the workflow is ready.
    """
    client = OzonClient()
    processed = 0
    created = 0
    updated = 0
    auto_sent = 0
    alerts_sent = 0
    skipped = 0
    errors = 0

    payloads: list[tuple[dict, str]] = []
    payloads.extend((item, "review") for item in client.fetch_reviews())
    payloads.extend((item, "question") for item in client.fetch_questions())

    for payload, source_type in payloads:
        processed += 1
        try:
            review, is_created = upsert_review_from_payload(db, payload, source_type)

            # Skip duplicate scheduler work for records already processed before.
            if review.last_automation_processed_at is not None or review.high_risk_notified_at is not None:
                skipped += 1
                continue

            if is_created:
                created += 1
            else:
                updated += 1

            if try_auto_send_review(db, review):
                auto_sent += 1

            if send_high_risk_alert_if_needed(db, review):
                alerts_sent += 1
        except Exception as exc:
            errors += 1
            log_action(db, "review_scheduler_error", f"Ошибка обработки отзыва планировщиком: {exc}")

    db.commit()
    return {
        "processed": processed,
        "created": created,
        "updated": updated,
        "auto_sent": auto_sent,
        "alerts_sent": alerts_sent,
        "skipped": skipped,
        "errors": errors,
    }


def _map_ozon_review_payload(raw_item: dict) -> dict:
    """Map possible Ozon review payload keys to our internal review structure."""
    author = raw_item.get("author") or {}
    product = raw_item.get("product") or {}

    external_id = (
        raw_item.get("id")
        or raw_item.get("review_id")
        or raw_item.get("uuid")
        or raw_item.get("external_id")
        or ""
    )
    return {
        "external_id": str(external_id).strip(),
        "sku": str(raw_item.get("sku") or product.get("sku") or "").strip(),
        "product_name": raw_item.get("product_name") or product.get("name") or "",
        "text": raw_item.get("text") or raw_item.get("content") or raw_item.get("comment") or "",
        "rating": raw_item.get("rating"),
        "author_name": raw_item.get("author_name") or author.get("name") or author.get("user_name") or "",
        "published_at": raw_item.get("published_at") or raw_item.get("created_at") or raw_item.get("date") or "",
        "status": "new",
    }


def load_reviews_from_api(db: Session) -> dict[str, int]:
    """Load reviews from Ozon API and upsert them into the local reviews table."""
    client = OzonClient()
    raw_reviews = client.fetch_reviews()

    processed = 0
    created = 0
    updated = 0
    errors = 0

    for raw_item in raw_reviews:
        processed += 1
        try:
            payload = _map_ozon_review_payload(raw_item)
            review, is_created = upsert_review_from_payload(db, payload, "review")
            review.status = "new"
            db.add(review)

            if is_created:
                created += 1
            else:
                updated += 1
        except Exception as exc:
            errors += 1
            log_action(db, "review_api_load_error", f"Ошибка загрузки отзыва из Ozon API: {exc}")

    db.commit()
    return {
        "processed": processed,
        "created": created,
        "updated": updated,
        "loaded": created + updated,
        "errors": errors,
    }


def reanalyze_all_reviews(db: Session) -> dict[str, int]:
    """Re-run classification for all stored reviews and refresh draft replies."""
    processed = 0
    updated = 0
    errors = 0

    reviews = db.query(Review).all()
    for review in reviews:
        processed += 1
        try:
            apply_review_classification(review)
            review.processing_mode = resolve_controlled_processing_mode(review)
            apply_reply_template(review)
            review.updated_at = datetime.utcnow()
            db.add(review)
            updated += 1
            log_action(
                db,
                "review_reanalyzed",
                (
                    f"Отзыв #{review.id} переанализирован: "
                    f"category={review.category}, risk_level={review.risk_level}, "
                    f"automation_mode={review.automation_mode}, confidence_score={review.confidence_score}."
                ),
            )
        except Exception as exc:
            errors += 1
            log_action(db, "review_reanalyze_error", f"Ошибка переанализа отзыва #{review.id}: {exc}")

    db.commit()
    return {
        "processed": processed,
        "updated": updated,
        "errors": errors,
    }
