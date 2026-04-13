import csv
from datetime import datetime
import logging
from pathlib import Path
import traceback
from urllib.parse import quote

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, env_presence_map
from app.database import get_db
from app.models import Review
from app.services.logger import log_action
from app.services.ozon_reviews_api import OzonClient
from app.services.ozon_reviews_service import load_reviews_from_api, reanalyze_all_reviews
from app.services.review_classifier import apply_review_classification
from app.services.reply_templates import apply_reply_template
from app.services.telegram_bot import send_high_risk_review_message


router = APIRouter(prefix="/reviews", tags=["reviews"])
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger("app.reviews_router")


def _parse_review_datetime(raw_value: str) -> datetime | None:
    """Parse common CSV datetime formats for review publication date."""
    cleaned_value = (raw_value or "").strip()
    if not cleaned_value:
        return None

    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
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

    raise ValueError(f"unsupported published_at format: '{cleaned_value}'")


def _parse_review_rating(raw_value: str) -> int | None:
    """Parse rating safely from CSV. Empty value stays nullable."""
    cleaned_value = (raw_value or "").strip()
    if not cleaned_value:
        return None
    return int(cleaned_value)


def _send_high_risk_alert_if_needed(db: Session, review: Review) -> None:
    """Send Telegram notification for high-risk reviews and save the result to AuditLog."""
    if review.risk_level != "РІС‹СЃРѕРєРёР№":
        return

    sent = send_high_risk_review_message(review)
    log_action(
        db,
        "review_telegram_alert",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} РѕС‚РїСЂР°РІР»РµРЅРѕ Telegram-СѓРІРµРґРѕРјР»РµРЅРёРµ Рѕ РІС‹СЃРѕРєРѕРј СЂРёСЃРєРµ: {sent}.",
    )


def _processing_mode_from_automation_mode(automation_mode: str) -> str:
    """Map analytical automation_mode to the user-facing processing mode."""
    normalized_value = (automation_mode or "").strip()
    if normalized_value == "auto":
        return "Р°РІС‚Рѕ_РѕС‚РІРµС‚"
    if normalized_value == "manual_only":
        return "С‚РѕР»СЊРєРѕ_С‡РµСЂРЅРѕРІРёРєРё"
    return "С‚СЂРµР±СѓРµС‚СЃСЏ_РїСЂРѕРІРµСЂРєР°"


def _resolve_controlled_processing_mode(review: Review) -> str:
    """Resolve processing mode by explicit controlled automation rules."""
    if review.category == "РІРѕРїСЂРѕСЃ" and review.risk_level == "РЅРёР·РєРёР№":
        return "Р°РІС‚Рѕ_РѕС‚РІРµС‚"

    if review.rating is not None and review.rating >= 5:
        return "Р°РІС‚Рѕ_РѕС‚РІРµС‚"

    if review.rating == 4:
        return "С‚РѕР»СЊРєРѕ_С‡РµСЂРЅРѕРІРёРєРё"

    if review.rating is not None and review.rating <= 3:
        return "С‚СЂРµР±СѓРµС‚СЃСЏ_РїСЂРѕРІРµСЂРєР°"

    return _processing_mode_from_automation_mode(review.automation_mode)


def _try_auto_send_review(db: Session, review: Review) -> bool:
    """
    Auto-send a reply only when the processing mode and classifier result allow it.
    This keeps the rules in one place for import and manual generation.
    """
    if review.processing_mode != "Р°РІС‚Рѕ_РѕС‚РІРµС‚":
        return False

    if review.automation_mode != "auto" or review.risk_level != "РЅРёР·РєРёР№":
        log_action(
            db,
            "review_action",
            (
                f"РђРІС‚РѕРѕС‚РІРµС‚ РґР»СЏ РѕС‚Р·С‹РІР° #{review.id} РЅРµ РІС‹РїРѕР»РЅРµРЅ. "
                f"automation_mode={review.automation_mode}, risk_level={review.risk_level}."
            ),
        )
        return False

    if not review.draft_reply:
        log_action(
            db,
            "review_action",
            f"РђРІС‚РѕРѕС‚РІРµС‚ РґР»СЏ РѕС‚Р·С‹РІР° #{review.id} РЅРµ РІС‹РїРѕР»РЅРµРЅ: РѕС‚СЃСѓС‚СЃС‚РІСѓРµС‚ draft_reply.",
        )
        return False

    client = OzonClient()
    review.final_reply = review.draft_reply
    review.status = "РѕРґРѕР±СЂРµРЅ"
    review.updated_at = datetime.utcnow()

    if review.source_type == "question":
        sent = client.send_question_reply(review.external_id, review.final_reply)
    else:
        sent = client.send_review_reply(review.external_id, review.final_reply)

    if sent:
        review.send_status = "СѓСЃРїРµС€РЅРѕ"
        review.sent_at = datetime.utcnow()
        review.status = "РѕС‚РїСЂР°РІР»РµРЅ"
        review.updated_at = datetime.utcnow()
        db.add(review)
        log_action(
            db,
            "review_action",
            f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РѕС‚РїСЂР°РІР»РµРЅ РѕС‚РІРµС‚.",
        )
        return True

    review.send_status = "РѕС€РёР±РєР°"
    review.status = "РѕС€РёР±РєР°"
    review.updated_at = datetime.utcnow()
    db.add(review)
    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} РїРѕРїС‹С‚РєР° Р°РІС‚РѕРѕС‚РІРµС‚Р° Р·Р°РІРµСЂС€РёР»Р°СЃСЊ РѕС€РёР±РєРѕР№.",
    )
    return False


@router.get("/", response_class=HTMLResponse)
def reviews_page(
    request: Request,
    sku: str = Query(""),
    source_type: str = Query(""),
    category: str = Query(""),
    rating: str = Query(""),
    status: str = Query(""),
    risk_level: str = Query(""),
    automation_mode: str = Query(""),
    confidence_score: str = Query(""),
    q: str = Query(""),
    db: Session = Depends(get_db),
):
    """Reviews page with simple server-side filters and text search."""
    query = db.query(Review)

    if sku.strip():
        query = query.filter(Review.sku.ilike(f"%{sku.strip()}%"))

    if source_type.strip():
        query = query.filter(Review.source_type == source_type.strip())

    if category.strip():
        query = query.filter(Review.category == category.strip())

    if rating.strip():
        try:
            query = query.filter(Review.rating == int(rating.strip()))
        except ValueError:
            pass

    if status.strip():
        query = query.filter(Review.status == status.strip())

    if risk_level.strip():
        query = query.filter(Review.risk_level == risk_level.strip())

    if automation_mode.strip():
        query = query.filter(Review.automation_mode == automation_mode.strip())

    if confidence_score.strip():
        try:
            query = query.filter(Review.confidence_score >= float(confidence_score.strip()))
        except ValueError:
            pass

    if q.strip():
        search_value = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Review.text.ilike(search_value),
                Review.product_name.ilike(search_value),
                Review.author_name.ilike(search_value),
                Review.external_id.ilike(search_value),
            )
        )

    reviews = query.order_by(Review.published_at.desc().nullslast(), Review.created_at.desc()).all()
    total_reviews = db.query(func.count(Review.id)).scalar() or 0

    return templates.TemplateResponse(
        "reviews/list.html",
        {
            "request": request,
            "reviews": reviews,
            "total_reviews": total_reviews,
            "filtered_reviews_count": len(reviews),
            "filters": {
                "sku": sku,
                "source_type": source_type,
                "category": category,
                "rating": rating,
                "status": status,
                "risk_level": risk_level,
                "automation_mode": automation_mode,
                "confidence_score": confidence_score,
                "q": q,
            },
            "source_type_options": ["review", "question"],
            "category_options": ["РїРѕР·РёС‚РёРІ", "РЅРµР№С‚СЂР°Р»СЊРЅС‹Р№", "РЅРµРіР°С‚РёРІ", "РІРѕРїСЂРѕСЃ"],
            "status_options": ["new", "drafted", "РѕРґРѕР±СЂРµРЅ", "РѕС‚РїСЂР°РІР»РµРЅ", "РѕС€РёР±РєР°", "archived"],
            "risk_level_options": ["low", "medium", "high"],
            "automation_mode_options": ["auto", "review_required", "manual_only"],
            "rating_options": ["1", "2", "3", "4", "5"],
            "processing_mode_options": ["С‚РѕР»СЊРєРѕ_С‡РµСЂРЅРѕРІРёРєРё", "С‚СЂРµР±СѓРµС‚СЃСЏ_РїСЂРѕРІРµСЂРєР°", "Р°РІС‚Рѕ_РѕС‚РІРµС‚"],
        },
    )


@router.post("/import")
def import_reviews_csv(db: Session = Depends(get_db)):
    """Import reviews and questions from the root-level reviews.csv file."""
    csv_path = Path(BASE_DIR) / "reviews.csv"
    if not csv_path.exists():
        return RedirectResponse(url="/reviews/?message=Р¤Р°Р№Р» reviews.csv РЅРµ РЅР°Р№РґРµРЅ РІ РєРѕСЂРЅРµ РїСЂРѕРµРєС‚Р°", status_code=303)

    processed_count = 0
    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)

        for row_number, row in enumerate(reader, start=2):
            processed_count += 1

            try:
                external_id = (row.get("external_id") or "").strip()
                if not external_id:
                    skipped_count += 1
                    log_action(db, "review_csv_row_skipped", f"Row {row_number} skipped: external_id is empty.")
                    continue

                published_at = _parse_review_datetime(row.get("published_at") or "")
                rating = _parse_review_rating(row.get("rating") or "")

                review = db.query(Review).filter(Review.external_id == external_id).first()

                if review is None:
                    review = Review(
                        external_id=external_id,
                        sku=(row.get("sku") or "").strip(),
                        product_name=(row.get("product_name") or "").strip() or None,
                        source_type=(row.get("source_type") or "review").strip() or "review",
                        rating=rating,
                        author_name=(row.get("author_name") or "").strip() or None,
                        text=(row.get("text") or "").strip(),
                        published_at=published_at,
                        status=(row.get("status") or "new").strip() or "new",
                        draft_reply=(row.get("draft_reply") or "").strip() or None,
                        final_reply=(row.get("final_reply") or "").strip() or None,
                        automation_mode=(row.get("automation_mode") or "review_required").strip() or "review_required",
                        processing_mode=(row.get("processing_mode") or "").strip() or "С‚СЂРµР±СѓРµС‚СЃСЏ_РїСЂРѕРІРµСЂРєР°",
                        risk_level=(row.get("risk_level") or "medium").strip() or "medium",
                        send_status=(row.get("send_status") or "pending").strip() or "pending",
                    )
                    apply_review_classification(review)
                    if not (row.get("processing_mode") or "").strip():
                        review.processing_mode = _resolve_controlled_processing_mode(review)
                    apply_reply_template(review)
                    db.add(review)
                    db.flush()
                    _send_high_risk_alert_if_needed(db, review)
                    _try_auto_send_review(db, review)
                    created_count += 1
                    continue

                review.text = (row.get("text") or "").strip()
                review.rating = rating
                review.published_at = published_at
                apply_review_classification(review)
                if not (row.get("processing_mode") or "").strip():
                    review.processing_mode = _resolve_controlled_processing_mode(review)
                else:
                    review.processing_mode = (row.get("processing_mode") or "").strip()
                apply_reply_template(review)
                db.add(review)
                db.flush()
                _send_high_risk_alert_if_needed(db, review)
                _try_auto_send_review(db, review)
                updated_count += 1
            except ValueError as exc:
                skipped_count += 1
                log_action(db, "review_csv_row_skipped", f"Row {row_number} skipped: {exc}")
            except Exception as exc:
                error_count += 1
                log_action(db, "review_csv_row_error", f"Row {row_number} import failed: {exc}")

    db.commit()
    log_action(
        db,
        "reviews_csv_imported",
        (
            "РРјРїРѕСЂС‚РёСЂРѕРІР°РЅ reviews.csv РёР· РєРѕСЂРЅСЏ РїСЂРѕРµРєС‚Р°. "
            f"РћР±СЂР°Р±РѕС‚Р°РЅРѕ: {processed_count}, СЃРѕР·РґР°РЅРѕ: {created_count}, РѕР±РЅРѕРІР»РµРЅРѕ: {updated_count}, "
            f"РїСЂРѕРїСѓС‰РµРЅРѕ: {skipped_count}, РѕС€РёР±РѕРє: {error_count}."
        ),
    )
    return RedirectResponse(
        url=(
            "/reviews/?message="
            f"РРјРїРѕСЂС‚ reviews.csv Р·Р°РІРµСЂС€С‘РЅ. РћР±СЂР°Р±РѕС‚Р°РЅРѕ: {processed_count}, СЃРѕР·РґР°РЅРѕ: {created_count}, "
            f"РѕР±РЅРѕРІР»РµРЅРѕ: {updated_count}, РїСЂРѕРїСѓС‰РµРЅРѕ: {skipped_count}, РѕС€РёР±РѕРє: {error_count}"
        ),
        status_code=303,
    )


@router.get("/load")
def load_reviews_from_ozon_api(db: Session = Depends(get_db)):
    """Load reviews from Ozon API and save them to the local database."""
    endpoint = "/reviews/load"
    env_diag = {
        key: value
        for key, value in env_presence_map().items()
        if key in {"DATABASE_URL", "OZON_CLIENT_ID", "OZON_API_KEY", "OZON_SELLER_BASE_URL", "OZON_REVIEWS_LIST_PATH", "OZON_REVIEWS_TIMEOUT_SECONDS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    }
    logger.info("Вход в маршрут %s", endpoint)
    logger.info("Диагностика env для %s: %s", endpoint, env_diag)
    logger.info("В %s будет вызван сервис load_reviews_from_api()", endpoint)

    try:
        stats = load_reviews_from_api(db)
        if stats.get("api_ok") is False:
            logger.warning(
                "Ozon reviews API вернул неуспешный ответ в %s. status=%s body=%s",
                endpoint,
                stats.get("api_status"),
                stats.get("api_body"),
            )
            try:
                log_action(
                    db,
                    "reviews_load_error",
                    f"Ozon reviews API error: status={stats.get('api_status')} body={stats.get('api_body')}",
                )
            except Exception:
                logger.exception("Не удалось записать Ozon API error в AuditLog для %s", endpoint)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Ozon API вернул ошибку",
                    "details": {
                        "status": stats.get("api_status"),
                        "body": stats.get("api_body"),
                    },
                    "endpoint": endpoint,
                    "env": env_diag,
                },
            )
        logger.info(
            "Загрузка отзывов через %s завершена. processed=%s created=%s updated=%s loaded=%s errors=%s",
            endpoint,
            stats["processed"],
            stats["created"],
            stats["updated"],
            stats["loaded"],
            stats["errors"],
        )
        log_action(
            db,
            "reviews_loaded_from_api",
            (
                "Загрузка отзывов из Ozon API завершена. "
                f"Processed: {stats['processed']}, created: {stats['created']}, "
                f"updated: {stats['updated']}, loaded: {stats['loaded']}, errors: {stats['errors']}."
            ),
        )
        return RedirectResponse(
            url=(
                "/reviews/?message="
                f"Загружено отзывов: {stats['loaded']}. Создано: {stats['created']}, "
                f"обновлено: {stats['updated']}, ошибок: {stats['errors']}"
            ),
            status_code=303,
        )
    except Exception as exc:
        trace = traceback.format_exc()
        logger.exception("Ошибка в маршруте %s: %s", endpoint, exc)
        try:
            log_action(
                db,
                "reviews_load_error",
                f"Ошибка загрузки отзывов из Ozon API: {exc}",
            )
        except Exception:
            logger.exception("Не удалось записать ошибку маршрута %s в AuditLog", endpoint)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Не удалось загрузить отзывы из Ozon API",
                "details": str(exc),
                "endpoint": endpoint,
                "traceback": trace,
                "env": env_diag,
            },
        )


@router.post("/reanalyze")
def reanalyze_reviews(db: Session = Depends(get_db)):
    """Re-run classification for all stored reviews."""
    logger.info("Р—Р°РїСѓС‰РµРЅ СЂСѓС‡РЅРѕР№ РїРµСЂРµР°РЅР°Р»РёР· РѕС‚Р·С‹РІРѕРІ С‡РµСЂРµР· РјР°СЂС€СЂСѓС‚ /reviews/reanalyze.")
    stats = reanalyze_all_reviews(db)
    log_action(
        db,
        "reviews_reanalyzed",
        (
            "РџРµСЂРµР°РЅР°Р»РёР· РѕС‚Р·С‹РІРѕРІ Р·Р°РІРµСЂС€РµРЅ. "
            f"Processed: {stats['processed']}, updated: {stats['updated']}, errors: {stats['errors']}."
        ),
    )
    logger.info(
        "РџРµСЂРµР°РЅР°Р»РёР· РѕС‚Р·С‹РІРѕРІ Р·Р°РІРµСЂС€РµРЅ. processed=%s updated=%s errors=%s",
        stats["processed"],
        stats["updated"],
        stats["errors"],
    )
    return RedirectResponse(
        url=(
            "/reviews/?message="
            f"РџРµСЂРµР°РЅР°Р»РёР· Р·Р°РІРµСЂС€РµРЅ. РћР±СЂР°Р±РѕС‚Р°РЅРѕ: {stats['processed']}, РѕР±РЅРѕРІР»РµРЅРѕ: {stats['updated']}, РѕС€РёР±РѕРє: {stats['errors']}"
        ),
        status_code=303,
    )


@router.post("/generate-new")
def generate_replies_for_new_reviews(db: Session = Depends(get_db)):
    """Generate draft replies for reviews that do not have one yet."""
    reviews = db.query(Review).filter(Review.draft_reply.is_(None)).all()

    processed = 0
    for review in reviews:
        apply_review_classification(review)
        if not review.processing_mode:
            review.processing_mode = _resolve_controlled_processing_mode(review)
        apply_reply_template(review)
        review.updated_at = datetime.utcnow()
        db.add(review)
        processed += 1
        log_action(
            db,
            "review_action",
            f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё СЃРіРµРЅРµСЂРёСЂРѕРІР°РЅ С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р°.",
        )

    db.commit()
    log_action(
        db,
        "reviews_bulk_reply_generation",
        f"РњР°СЃСЃРѕРІР°СЏ РіРµРЅРµСЂР°С†РёСЏ С‡РµСЂРЅРѕРІРёРєРѕРІ Р·Р°РІРµСЂС€РµРЅР°. РЎРіРµРЅРµСЂРёСЂРѕРІР°РЅРѕ РѕС‚РІРµС‚РѕРІ: {processed}.",
    )
    return RedirectResponse(
        url=f"/reviews/?message=РЎРіРµРЅРµСЂРёСЂРѕРІР°РЅРѕ С‡РµСЂРЅРѕРІРёРєРѕРІ РґР»СЏ РЅРѕРІС‹С… РѕС‚Р·С‹РІРѕРІ: {processed}",
        status_code=303,
    )


@router.post("/{review_id}/generate-reply")
def generate_review_reply(review_id: int, db: Session = Depends(get_db)):
    """Generate draft_reply from the current review category."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    apply_review_classification(review)
    apply_reply_template(review)
    if not review.processing_mode:
        review.processing_mode = _resolve_controlled_processing_mode(review)
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} СЃРѕР·РґР°РЅ С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р°.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=Р§РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р° СѓСЃРїРµС€РЅРѕ СЃРѕР·РґР°РЅ",
        status_code=303,
    )

    reply_text = (review.final_reply or "").strip() or (review.draft_reply or "").strip()
    if not reply_text:
        review.status = "РѕС€РёР±РєР°"
        review.send_status = "РѕС€РёР±РєР°"
        review.updated_at = datetime.utcnow()
        db.add(review)
        db.commit()

        log_action(
            db,
            "review_action",
            f"РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ РѕС‚РІРµС‚ РґР»СЏ РѕС‚Р·С‹РІР° #{review.id}: С‚РµРєСЃС‚ РѕС‚РІРµС‚Р° РїСѓСЃС‚РѕР№.",
        )
        return RedirectResponse(
            url=f"/reviews/{review.id}?message={quote('РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ: С‚РµРєСЃС‚ РѕС‚РІРµС‚Р° РїСѓСЃС‚РѕР№')}",
            status_code=303,
        )

    review.final_reply = reply_text
    review.status = "РѕРґРѕР±СЂРµРЅ"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    client = OzonClient()
    if review.source_type == "question":
        result = client.send_question_reply(review.id, review.external_id, review.final_reply)
    else:
        result = client.send_review_reply(review.id, review.external_id, review.final_reply)

    success = bool(result.get("ok"))
    status_code = result.get("status_code")
    response_summary = result.get("response_summary") or "-"

    review.send_status = "СѓСЃРїРµС€РЅРѕ" if success else "РѕС€РёР±РєР°"
    review.sent_at = datetime.utcnow() if success else None
    review.status = "РѕС‚РїСЂР°РІР»РµРЅ" if success else "РѕС€РёР±РєР°"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        (
            f"Р СѓС‡РЅР°СЏ РѕС‚РїСЂР°РІРєР° РѕС‚РІРµС‚Р° РґР»СЏ РѕС‚Р·С‹РІР° #{review.id} Р·Р°РІРµСЂС€РµРЅР°. "
            f"external_id={review.external_id}. "
            f"source_type={review.source_type}. "
            f"РЈСЃРїРµС…: {success}. "
            f"РЎС‚Р°С‚СѓСЃ РєРѕРґ: {status_code}. "
            f"РћС‚РІРµС‚ Ozon: {response_summary}"
        ),
    )

    ui_message = "РћС‚РІРµС‚ РѕС‚РїСЂР°РІР»РµРЅ" if success else f"РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ: {result.get('message') or 'РѕС€РёР±РєР° Ozon'}"
    return RedirectResponse(
        url=f"/reviews/{review.id}?message={quote(ui_message)}",
        status_code=303,
    )

    apply_review_classification(review)
    apply_reply_template(review)
    if not review.processing_mode:
        review.processing_mode = _resolve_controlled_processing_mode(review)
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} СЃРѕР·РґР°РЅ С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р°.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=Р§РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р° СѓСЃРїРµС€РЅРѕ СЃРѕР·РґР°РЅ",
        status_code=303,
    )

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} СЃРіРµРЅРµСЂРёСЂРѕРІР°РЅ С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р°.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=Р§РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р° СѓСЃРїРµС€РЅРѕ СЃРіРµРЅРµСЂРёСЂРѕРІР°РЅ",
        status_code=303,
    )


@router.post("/{review_id}/approve")
def approve_review_reply(review_id: int, db: Session = Depends(get_db)):
    """Move draft_reply to final_reply and mark the review as approved."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    if not review.draft_reply:
        return RedirectResponse(
            url=f"/reviews/{review.id}?message=РЎРЅР°С‡Р°Р»Р° РЅСѓР¶РЅРѕ СЃРіРµРЅРµСЂРёСЂРѕРІР°С‚СЊ С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р°",
            status_code=303,
        )

    review.final_reply = review.draft_reply
    review.status = "РѕРґРѕР±СЂРµРЅ"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р° РѕРґРѕР±СЂРµРЅ Рё РїРµСЂРµРЅРµСЃРµРЅ РІ final_reply.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=РћС‚РІРµС‚ РѕРґРѕР±СЂРµРЅ",
        status_code=303,
    )

    if not review.draft_reply:
        return RedirectResponse(
            url=f"/reviews/{review.id}?message=РЎРЅР°С‡Р°Р»Р° РЅСѓР¶РЅРѕ СЃРіРµРЅРµСЂРёСЂРѕРІР°С‚СЊ С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р°",
            status_code=303,
        )

    review.final_reply = review.draft_reply
    review.status = "РѕРґРѕР±СЂРµРЅ"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р° РѕРґРѕР±СЂРµРЅ Рё РїРµСЂРµРЅРµСЃРµРЅ РІ final_reply.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=РћС‚РІРµС‚ РѕРґРѕР±СЂРµРЅ",
        status_code=303,
    )


@router.post("/{review_id}/edit")
def edit_review_reply(
    review_id: int,
    final_reply: str = Form(...),
    db: Session = Depends(get_db),
):
    """Save manual edits for final_reply."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    review.final_reply = final_reply.strip()
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} СЃРѕС…СЂР°РЅРµРЅ РѕС‚СЂРµРґР°РєС‚РёСЂРѕРІР°РЅРЅС‹Р№ С„РёРЅР°Р»СЊРЅС‹Р№ РѕС‚РІРµС‚.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=Р¤РёРЅР°Р»СЊРЅС‹Р№ РѕС‚РІРµС‚ СЃРѕС…СЂР°РЅРµРЅ",
        status_code=303,
    )

    review.final_reply = final_reply.strip()
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} РѕС‚СЂРµРґР°РєС‚РёСЂРѕРІР°РЅ С„РёРЅР°Р»СЊРЅС‹Р№ РѕС‚РІРµС‚.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=Р¤РёРЅР°Р»СЊРЅС‹Р№ РѕС‚РІРµС‚ СЃРѕС…СЂР°РЅРµРЅ",
        status_code=303,
    )


@router.post("/{review_id}/send")
def send_review_reply(review_id: int, db: Session = Depends(get_db)):
    """Send the final reply using the Ozon client stub and store the sending state."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    reply_text = (review.final_reply or "").strip() or (review.draft_reply or "").strip()
    if not reply_text:
        review.status = "РѕС€РёР±РєР°"
        review.send_status = "РѕС€РёР±РєР°"
        review.updated_at = datetime.utcnow()
        db.add(review)
        db.commit()

        log_action(
            db,
            "review_action",
            f"РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ РѕС‚РІРµС‚ РґР»СЏ РѕС‚Р·С‹РІР° #{review.id}: С‚РµРєСЃС‚ РѕС‚РІРµС‚Р° РїСѓСЃС‚РѕР№.",
        )
        return RedirectResponse(
            url=f"/reviews/{review.id}?message={quote('РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ: С‚РµРєСЃС‚ РѕС‚РІРµС‚Р° РїСѓСЃС‚РѕР№')}",
            status_code=303,
        )

    review.final_reply = reply_text
    review.status = "РѕРґРѕР±СЂРµРЅ"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    client = OzonClient()
    if review.source_type == "question":
        result = client.send_question_reply(review.id, review.external_id, review.final_reply)
    else:
        result = client.send_review_reply(review.id, review.external_id, review.final_reply)

    success = bool(result.get("ok"))
    status_code = result.get("status_code")
    response_summary = result.get("response_summary") or "-"

    review.send_status = "СѓСЃРїРµС€РЅРѕ" if success else "РѕС€РёР±РєР°"
    review.sent_at = datetime.utcnow() if success else None
    review.status = "РѕС‚РїСЂР°РІР»РµРЅ" if success else "РѕС€РёР±РєР°"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        (
            f"Р СѓС‡РЅР°СЏ РѕС‚РїСЂР°РІРєР° РѕС‚РІРµС‚Р° РґР»СЏ РѕС‚Р·С‹РІР° #{review.id} Р·Р°РІРµСЂС€РµРЅР°. "
            f"external_id={review.external_id}. "
            f"source_type={review.source_type}. "
            f"РЈСЃРїРµС…: {success}. "
            f"РЎС‚Р°С‚СѓСЃ РєРѕРґ: {status_code}. "
            f"РћС‚РІРµС‚ Ozon: {response_summary}"
        ),
    )

    ui_message = "РћС‚РІРµС‚ РѕС‚РїСЂР°РІР»РµРЅ" if success else f"РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ: {result.get('message') or 'РѕС€РёР±РєР° Ozon'}"
    return RedirectResponse(
        url=f"/reviews/{review.id}?message={quote(ui_message)}",
        status_code=303,
    )

    if not review.final_reply and not review.draft_reply:
        message = "РќРµР»СЊР·СЏ РѕС‚РїСЂР°РІРёС‚СЊ РїСѓСЃС‚РѕР№ РѕС‚РІРµС‚"
        return RedirectResponse(url=f"/reviews/{review.id}?message={quote(message)}", status_code=303)

    review.final_reply = review.final_reply or review.draft_reply
    review.status = "РѕРґРѕР±СЂРµРЅ"

    client = OzonClient()
    if review.source_type == "question":
        result = client.send_question_reply(review.external_id, review.final_reply)
    else:
        result = client.send_review_reply(review.external_id, review.final_reply)

    success = bool(result.get("ok"))
    status_code = result.get("status_code")
    response_summary = result.get("response_summary") or "-"
    review.send_status = "СѓСЃРїРµС€РЅРѕ" if success else "РѕС€РёР±РєР°"
    review.sent_at = datetime.utcnow() if success else None
    review.status = "РѕС‚РїСЂР°РІР»РµРЅ" if success else "РѕС€РёР±РєР°"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        (
            f"РўРµСЃС‚РѕРІР°СЏ РѕС‚РїСЂР°РІРєР° РѕС‚РІРµС‚Р° РґР»СЏ РѕС‚Р·С‹РІР° #{review.id} Р·Р°РІРµСЂС€РµРЅР°. "
            f"РЈСЃРїРµС…: {success}. "
            f"РЎС‚Р°С‚СѓСЃ РєРѕРґ: {status_code}. "
            f"РћС‚РІРµС‚ Ozon: {response_summary}"
        ),
    )

    flash_message = (
        f"{'РЈСЃРїРµС…' if success else 'РћС€РёР±РєР°'}; "
        f"status_code={status_code if status_code is not None else '-'}; "
        f"ozon={response_summary}"
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message={quote(flash_message)}",
        status_code=303,
    )

    if not review.final_reply and not review.draft_reply:
        return RedirectResponse(
            url=f"/reviews/{review.id}?message=РќРµР»СЊР·СЏ РѕС‚РїСЂР°РІРёС‚СЊ РїСѓСЃС‚РѕР№ РѕС‚РІРµС‚",
            status_code=303,
        )

    review.final_reply = review.final_reply or review.draft_reply
    review.status = "РѕРґРѕР±СЂРµРЅ"

    client = OzonClient()

    try:
        if review.source_type == "question":
            sent = client.send_question_reply(review.external_id, review.final_reply)
        else:
            sent = client.send_review_reply(review.external_id, review.final_reply)
    except Exception as exc:
        review.status = "РѕС€РёР±РєР°"
        review.send_status = "РѕС€РёР±РєР°"
        review.sent_at = None
        review.updated_at = datetime.utcnow()
        db.add(review)
        db.commit()

        log_action(
            db,
            "review_action",
            f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} РїСЂРѕРёР·РѕС€Р»Р° РѕС€РёР±РєР° РѕС‚РїСЂР°РІРєРё РѕС‚РІРµС‚Р°: {exc}",
        )
        return RedirectResponse(
            url=f"/reviews/{review.id}?message=РћС€РёР±РєР° РѕС‚РїСЂР°РІРєРё РѕС‚РІРµС‚Р°",
            status_code=303,
        )

    review.send_status = "СѓСЃРїРµС€РЅРѕ" if sent else "РѕС€РёР±РєР°"
    review.sent_at = datetime.utcnow() if sent else None
    review.status = "РѕС‚РїСЂР°РІР»РµРЅ" if sent else "РѕС€РёР±РєР°"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        (
            f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} РІС‹РїРѕР»РЅРµРЅР° РѕС‚РїСЂР°РІРєР° РѕС‚РІРµС‚Р°. "
            f"РС‚РѕРіРѕРІС‹Р№ СЃС‚Р°С‚СѓСЃ: {'РѕС‚РїСЂР°РІР»РµРЅ' if sent else 'РѕС€РёР±РєР°'}."
        ),
    )
    return RedirectResponse(
        url=(
            f"/reviews/{review.id}?message="
            + ("РћС‚РІРµС‚ РѕС‚РїСЂР°РІР»РµРЅ" if sent else "РћС€РёР±РєР° РѕС‚РїСЂР°РІРєРё РѕС‚РІРµС‚Р°")
        ),
        status_code=303,
    )

    if not review.final_reply and not review.draft_reply:
        return RedirectResponse(
            url=f"/reviews/{review.id}?message=РќРµР»СЊР·СЏ РѕС‚РїСЂР°РІРёС‚СЊ РїСѓСЃС‚РѕР№ С„РёРЅР°Р»СЊРЅС‹Р№ РѕС‚РІРµС‚",
            status_code=303,
        )

    review.final_reply = review.final_reply or review.draft_reply
    review.status = "РѕРґРѕР±СЂРµРЅ"

    client = OzonClient()
    if review.source_type == "question":
        sent = client.send_question_reply(review.external_id, review.final_reply)
    else:
        sent = client.send_review_reply(review.external_id, review.final_reply)

    review.send_status = "СѓСЃРїРµС€РЅРѕ" if sent else "РѕС€РёР±РєР°"
    review.sent_at = datetime.utcnow() if sent else None
    review.status = "РѕС‚РїСЂР°РІР»РµРЅ" if sent else "РѕС€РёР±РєР°"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"Р”Р»СЏ РѕС‚Р·С‹РІР° #{review.id} РІС‹РїРѕР»РЅРµРЅР° РѕС‚РїСЂР°РІРєР° РѕС‚РІРµС‚Р°. РЈСЃРїРµС…: {sent}.",
    )
    return RedirectResponse(
        url=(
            f"/reviews/{review.id}?message="
            + ("РћС‚РІРµС‚ РѕС‚РїСЂР°РІР»РµРЅ" if sent else "РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РїСЂР°РІРёС‚СЊ РѕС‚РІРµС‚")
        ),
        status_code=303,
    )


@router.post("/{review_id}/archive")
def archive_review(review_id: int, db: Session = Depends(get_db)):
    """Move the review to archive status."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    review.status = "Р°СЂС…РёРІ"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"РћС‚Р·С‹РІ #{review.id} РїРµСЂРµРІРµРґРµРЅ РІ Р°СЂС…РёРІ.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=РћС‚Р·С‹РІ РѕС‚РїСЂР°РІР»РµРЅ РІ Р°СЂС…РёРІ",
        status_code=303,
    )

    review.status = "archived"
    review.updated_at = datetime.utcnow()
    db.add(review)
    db.commit()

    log_action(
        db,
        "review_action",
        f"РћС‚Р·С‹РІ #{review.id} РїРµСЂРµРІРµРґРµРЅ РІ Р°СЂС…РёРІ.",
    )
    return RedirectResponse(
        url=f"/reviews/{review.id}?message=РћС‚Р·С‹РІ РѕС‚РїСЂР°РІР»РµРЅ РІ Р°СЂС…РёРІ",
        status_code=303,
    )

@router.get("/{review_id}", response_class=HTMLResponse)
def review_detail_page(review_id: int, request: Request, db: Session = Depends(get_db)):
    """Detail page for one review or question."""
    review = db.query(Review).filter(Review.id == review_id).first()
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    return templates.TemplateResponse(
        "reviews/detail.html",
        {
            "request": request,
            "review": review,
            "edit_mode": request.query_params.get("edit") == "1",
            "message": request.query_params.get("message", ""),
        },
    )

