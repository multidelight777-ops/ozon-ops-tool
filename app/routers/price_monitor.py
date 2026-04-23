import logging
import traceback
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import MonitoredProduct, PriceMonitor
from app.services.logger import log_action
from app.services.ozon_price_monitor import OzonPriceMonitor


moscow = ZoneInfo("Europe/Moscow")
logger = logging.getLogger("app.price_monitor")

router = APIRouter(prefix="/price-monitor", tags=["price_monitor"])
product_router = APIRouter(tags=["price_monitor"])
templates = Jinja2Templates(directory="app/templates")


def ensure_price_monitor_schema() -> None:
    """Добавить новые колонки мониторинга цен в существующую SQLite-базу."""
    if not str(engine.url).startswith("sqlite"):
        return

    with engine.begin() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(monitored_products)")).fetchall()}
        if "base_price" not in columns:
            connection.execute(text("ALTER TABLE monitored_products ADD COLUMN base_price FLOAT"))
            logger.info("Добавлена колонка monitored_products.base_price")
        if "updated_at" not in columns:
            connection.execute(text("ALTER TABLE monitored_products ADD COLUMN updated_at DATETIME"))
            connection.execute(text("UPDATE monitored_products SET updated_at = created_at WHERE updated_at IS NULL"))
            logger.info("Добавлена колонка monitored_products.updated_at")


def _to_moscow_string(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str | None:
    """РџСЂРµРѕР±СЂР°Р·РѕРІР°С‚СЊ datetime РІ СЃС‚СЂРѕРєСѓ РјРѕСЃРєРѕРІСЃРєРѕРіРѕ РІСЂРµРјРµРЅРё."""
    if value is None:
        return None
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(moscow).strftime(fmt)


def _validate_product_input(sku: str, product_name: str, url: str) -> str | None:
    """РџСЂРѕРІРµСЂРёС‚СЊ РѕР±СЏР·Р°С‚РµР»СЊРЅС‹Рµ РїРѕР»СЏ С„РѕСЂРјС‹ РґРѕР±Р°РІР»РµРЅРёСЏ С‚РѕРІР°СЂР°."""
    if not sku:
        return "Р—Р°РїРѕР»РЅРёС‚Рµ SKU."
    if not product_name:
        return "Р—Р°РїРѕР»РЅРёС‚Рµ РЅР°Р·РІР°РЅРёРµ С‚РѕРІР°СЂР°."
    if not url:
        return "Р—Р°РїРѕР»РЅРёС‚Рµ URL."

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "РЈРєР°Р¶РёС‚Рµ РєРѕСЂСЂРµРєС‚РЅС‹Р№ URL."
    if "ozon" not in parsed.netloc.lower():
        return "Р”Р»СЏ РјРѕРЅРёС‚РѕСЂРёРЅРіР° РЅСѓР¶РµРЅ URL РєР°СЂС‚РѕС‡РєРё Ozon."
    return None


def _build_rows(db: Session) -> list[dict]:
    """РЎРѕР±СЂР°С‚СЊ СЃС‚СЂРѕРєРё С‚Р°Р±Р»РёС†С‹ СЃ РїРѕСЃР»РµРґРЅРµР№ С†РµРЅРѕР№ РїРѕ РєР°Р¶РґРѕРјСѓ С‚РѕРІР°СЂСѓ."""
    rows: list[dict] = []
    products = db.query(MonitoredProduct).order_by(MonitoredProduct.created_at.desc()).all()

    for product in products:
        latest_check = (
            db.query(PriceMonitor)
            .filter(PriceMonitor.sku == product.sku, PriceMonitor.url == product.url)
            .order_by(PriceMonitor.checked_at.desc(), PriceMonitor.created_at.desc())
            .first()
        )

        price_with_spp = latest_check.price_with_spp if latest_check else None
        price_without_spp = latest_check.price_without_spp if latest_check else None
        base_price = product.base_price
        percent_with_spp = None
        percent_without_spp = None
        if base_price and base_price > 0:
            if price_with_spp:
                percent_with_spp = round((price_with_spp / base_price) * 100, 2)
            if price_without_spp:
                percent_without_spp = round((price_without_spp / base_price) * 100, 2)

        rows.append(
            {
                "product": product,
                "id": product.id,
                "sku": product.sku,
                "name": product.product_name,
                "base_price": base_price,
                "price_with_spp": price_with_spp,
                "price_without_spp": price_without_spp,
                "percent_with_spp": percent_with_spp,
                "percent_without_spp": percent_without_spp,
                "updated_at": _to_moscow_string(product.updated_at),
                "checked_at": _to_moscow_string(latest_check.checked_at) if latest_check else None,
            }
        )

    return rows


@router.get("/", response_class=HTMLResponse)
def price_monitor_page(request: Request, db: Session = Depends(get_db)):
    """РџРѕРєР°Р·Р°С‚СЊ СЃС‚СЂР°РЅРёС†Сѓ РјРѕРЅРёС‚РѕСЂРёРЅРіР° РІРёС‚СЂРёРЅРЅС‹С… С†РµРЅ."""
    ensure_price_monitor_schema()
    rows = _build_rows(db)
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(
            content=[
                {
                    "sku": row["sku"],
                    "name": row["name"],
                    "base_price": row["base_price"],
                    "price_with_spp": row["price_with_spp"],
                    "price_without_spp": row["price_without_spp"],
                    "percent_with_spp": row["percent_with_spp"],
                    "percent_without_spp": row["percent_without_spp"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        )

    return templates.TemplateResponse(
        "price_monitor.html",
        {
            "request": request,
            "rows": rows,
            "message": request.query_params.get("message", ""),
        },
    )


@router.get("/{product_id}/chart", response_class=HTMLResponse)
def price_monitor_chart_page(product_id: int, request: Request, db: Session = Depends(get_db)):
    """РџРѕРєР°Р·Р°С‚СЊ РіСЂР°С„РёРє РёСЃС‚РѕСЂРёРё С†РµРЅ РїРѕ РѕРґРЅРѕРјСѓ С‚РѕРІР°СЂСѓ."""
    product = db.query(MonitoredProduct).filter(MonitoredProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="РўРѕРІР°СЂ РЅРµ РЅР°Р№РґРµРЅ")

    history = (
        db.query(PriceMonitor)
        .filter(PriceMonitor.sku == product.sku, PriceMonitor.url == product.url)
        .order_by(PriceMonitor.checked_at.asc(), PriceMonitor.created_at.asc())
        .all()
    )

    chart_labels: list[str] = []
    chart_price_with_spp: list[float | None] = []
    chart_price_without_spp: list[float | None] = []

    for item in history:
        chart_labels.append(_to_moscow_string(item.checked_at) or "-")
        chart_price_with_spp.append(item.price_with_spp)
        chart_price_without_spp.append(item.price_without_spp)

    return templates.TemplateResponse(
        "price_monitor_chart.html",
        {
            "request": request,
            "product": product,
            "chart_labels": chart_labels,
            "chart_price_with_spp": chart_price_with_spp,
            "chart_price_without_spp": chart_price_without_spp,
        },
    )


@router.post("/add")
async def add_monitored_product(request: Request, db: Session = Depends(get_db)):
    """Р”РѕР±Р°РІРёС‚СЊ С‚РѕРІР°СЂ РІ РјРѕРЅРёС‚РѕСЂРёРЅРі С†РµРЅ СЃ РІР°Р»РёРґР°С†РёРµР№ Рё РїРѕРЅСЏС‚РЅС‹РјРё РѕС€РёР±РєР°РјРё."""
    logger.info("Р’С…РѕРґ РІ РјР°СЂС€СЂСѓС‚ РґРѕР±Р°РІР»РµРЅРёСЏ С‚РѕРІР°СЂР° /price-monitor/add")
    Base.metadata.create_all(bind=engine)
    ensure_price_monitor_schema()

    try:
        form = await request.form()
        sku = str(form.get("sku") or "").strip()
        product_name = str(form.get("product_name") or "").strip()
        url = str(form.get("url") or "").strip()
        base_price_raw = str(form.get("base_price") or "").strip()
        try:
            base_price = float(base_price_raw.replace(",", ".")) if base_price_raw else None
        except ValueError:
            return RedirectResponse(
                url=f"/price-monitor/?message={quote('Цена до скидки должна быть числом')}",
                status_code=303,
            )

        logger.info("РџРѕР»СѓС‡РµРЅС‹ РґР°РЅРЅС‹Рµ С„РѕСЂРјС‹ РґРѕР±Р°РІР»РµРЅРёСЏ С‚РѕРІР°СЂР°: sku=%s product_name=%s url=%s", sku, product_name, url)

        validation_error = _validate_product_input(sku, product_name, url)
        if validation_error:
            logger.warning("РћС€РёР±РєР° РІР°Р»РёРґР°С†РёРё РїСЂРё РґРѕР±Р°РІР»РµРЅРёРё С‚РѕРІР°СЂР°: %s", validation_error)
            return RedirectResponse(
                url=f"/price-monitor/?message={quote(validation_error)}",
                status_code=303,
            )

        existing_product = (
            db.query(MonitoredProduct)
            .filter(MonitoredProduct.sku == sku, MonitoredProduct.url == url)
            .first()
        )
        if existing_product:
            message = f"РўРѕРІР°СЂ СЃ SKU {sku} Рё С‚Р°РєРёРј URL СѓР¶Рµ РґРѕР±Р°РІР»РµРЅ РІ РјРѕРЅРёС‚РѕСЂРёРЅРі."
            logger.warning("Р”СѓР±Р»РёРєР°С‚ С‚РѕРІР°СЂР° РІ РјРѕРЅРёС‚РѕСЂРёРЅРіРµ: sku=%s url=%s", sku, url)
            return RedirectResponse(
                url=f"/price-monitor/?message={quote(message)}",
                status_code=303,
            )

        product = MonitoredProduct(sku=sku, product_name=product_name, url=url, base_price=base_price)
        db.add(product)
        db.commit()
        db.refresh(product)

        logger.info("РўРѕРІР°СЂ СѓСЃРїРµС€РЅРѕ СЃРѕС…СЂР°РЅС‘РЅ РІ Р‘Р”: product_id=%s sku=%s", product.id, sku)
        log_action(
            db,
            "price_monitor_product_added",
            f"Р’ РјРѕРЅРёС‚РѕСЂРёРЅРі РґРѕР±Р°РІР»РµРЅ С‚РѕРІР°СЂ: sku={sku}, product_name={product_name}, url={url}.",
        )
        return RedirectResponse(
            url="/price-monitor/?message=РўРѕРІР°СЂ СѓСЃРїРµС€РЅРѕ РґРѕР±Р°РІР»РµРЅ РІ РјРѕРЅРёС‚РѕСЂРёРЅРі",
            status_code=303,
        )
    except Exception as exc:
        trace = traceback.format_exc()
        logger.exception("РћС€РёР±РєР° РґРѕР±Р°РІР»РµРЅРёСЏ С‚РѕРІР°СЂР° РІ РјРѕРЅРёС‚РѕСЂРёРЅРі: %s", exc)
        try:
            log_action(db, "price_monitor_product_add_error", f"РћС€РёР±РєР° РґРѕР±Р°РІР»РµРЅРёСЏ С‚РѕРІР°СЂР°: {exc}")
        except Exception:
            logger.exception("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РїРёСЃР°С‚СЊ РѕС€РёР±РєСѓ РґРѕР±Р°РІР»РµРЅРёСЏ С‚РѕРІР°СЂР° РІ AuditLog")
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "message": "РќРµ СѓРґР°Р»РѕСЃСЊ РґРѕР±Р°РІРёС‚СЊ С‚РѕРІР°СЂ РІ РјРѕРЅРёС‚РѕСЂРёРЅРі",
                "error": str(exc),
                "traceback": trace,
            },
        )


@router.patch("/product/{product_id}")
@product_router.patch("/product/{product_id}")
async def update_monitored_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Обновить ручные поля товара из Excel-таблицы мониторинга."""
    Base.metadata.create_all(bind=engine)
    ensure_price_monitor_schema()

    product = db.query(MonitoredProduct).filter(MonitoredProduct.id == product_id).first()
    if not product:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "message": "Товар не найден"},
        )

    try:
        payload = await request.json()
    except Exception:
        form = await request.form()
        payload = dict(form)

    raw_base_price = payload.get("base_price")
    try:
        if raw_base_price in (None, ""):
            product.base_price = None
        else:
            product.base_price = float(str(raw_base_price).replace(",", "."))
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": "Цена до скидки должна быть числом"},
        )

    product.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(product)
    log_action(db, "price_monitor_base_price_updated", f"Обновлена цена до скидки для sku={product.sku}: {product.base_price}")

    return {
        "ok": True,
        "product_id": product.id,
        "base_price": product.base_price,
        "updated_at": _to_moscow_string(product.updated_at),
    }


@router.post("/{product_id}/refresh")
async def refresh_product_price(product_id: int, db: Session = Depends(get_db)):
    """РћР±РЅРѕРІРёС‚СЊ РІРёС‚СЂРёРЅРЅС‹Рµ С†РµРЅС‹ РґР»СЏ РѕРґРЅРѕРіРѕ С‚РѕРІР°СЂР° Рё РІРµСЂРЅСѓС‚СЊ РґРёР°РіРЅРѕСЃС‚РёРєСѓ РїСЂРё РѕС€РёР±РєРµ."""
    logger.info("Р’С…РѕРґ РІ РјР°СЂС€СЂСѓС‚ РѕР±РЅРѕРІР»РµРЅРёСЏ С†РµРЅС‹ РІРёС‚СЂРёРЅС‹: product_id=%s", product_id)
    Base.metadata.create_all(bind=engine)
    ensure_price_monitor_schema()

    product = db.query(MonitoredProduct).filter(MonitoredProduct.id == product_id).first()
    if not product:
        logger.warning("РўРѕРІР°СЂ РґР»СЏ РѕР±РЅРѕРІР»РµРЅРёСЏ С†РµРЅС‹ РЅРµ РЅР°Р№РґРµРЅ: product_id=%s", product_id)
        raise HTTPException(status_code=404, detail="РўРѕРІР°СЂ РЅРµ РЅР°Р№РґРµРЅ")

    logger.info("РЎС‚Р°СЂС‚ РѕР±РЅРѕРІР»РµРЅРёСЏ С†РµРЅС‹ РІРёС‚СЂРёРЅС‹: product_id=%s sku=%s url=%s", product_id, product.sku, product.url)

    monitor = OzonPriceMonitor()
    logger.info(
        "Playwright browser engine=%s timeout_ms=%s launch_args=%s",
        "chromium",
        monitor.timeout_ms,
        monitor.browser_args,
    )

    try:
        result = await monitor.get_price(product.url)
        logger.info("РџР°СЂСЃРёРЅРі С†РµРЅС‹ Р·Р°РІРµСЂС€С‘РЅ: product_id=%s sku=%s result=%s", product_id, product.sku, result)

        if result.get("price_with_spp") is None and result.get("price_without_spp") is None:
            details = "Парсер не нашёл цену на странице Ozon."
            logger.warning("Парсер цены не нашёл ни одной цены: product_id=%s details=%s", product_id, details)
            log_action(
                db,
                "price_monitor_refresh_error",
                f"Ошибка обновления цены товара sku={product.sku}, url={product.url}: {details}",
            )
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "message": "РќРµ СѓРґР°Р»РѕСЃСЊ РѕР±РЅРѕРІРёС‚СЊ С†РµРЅСѓ РІРёС‚СЂРёРЅС‹",
                    "product_id": product_id,
                    "sku": product.sku,
                    "url": product.url,
                    "details": details,
                },
            )

        price_row = PriceMonitor(
            sku=product.sku,
            url=product.url,
            price_with_spp=result.get("price_with_spp"),
            price_without_spp=result.get("price_without_spp"),
            checked_at=datetime.utcnow(),
        )
        db.add(price_row)
        db.commit()

        log_action(
            db,
            "price_monitor_refreshed",
            (
                f"РћР±РЅРѕРІР»РµРЅС‹ С†РµРЅС‹ С‚РѕРІР°СЂР° sku={product.sku}. "
                f"Р¦РµРЅР° СЃ РЎРџРџ={result.get('price_with_spp')}, "
                f"С†РµРЅР° Р±РµР· РЎРџРџ={result.get('price_without_spp')}."
            ),
        )
        return RedirectResponse(
            url="/price-monitor/?message=Р¦РµРЅС‹ СѓСЃРїРµС€РЅРѕ РѕР±РЅРѕРІР»РµРЅС‹",
            status_code=303,
        )
    except Exception as exc:
        trace = traceback.format_exc()
        logger.exception("РћС€РёР±РєР° РІ refresh flow РјРѕРЅРёС‚РѕСЂРёРЅРіР° С†РµРЅ: product_id=%s sku=%s url=%s", product_id, product.sku, product.url)
        try:
            log_action(
                db,
                "price_monitor_refresh_error",
                f"РћС€РёР±РєР° РѕР±РЅРѕРІР»РµРЅРёСЏ С†РµРЅС‹ С‚РѕРІР°СЂР° sku={product.sku}, url={product.url}: {exc}",
            )
        except Exception:
            logger.exception("РќРµ СѓРґР°Р»РѕСЃСЊ Р·Р°РїРёСЃР°С‚СЊ РѕС€РёР±РєСѓ РѕР±РЅРѕРІР»РµРЅРёСЏ С†РµРЅС‹ РІ AuditLog: product_id=%s", product_id)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "message": "РќРµ СѓРґР°Р»РѕСЃСЊ РѕР±РЅРѕРІРёС‚СЊ С†РµРЅСѓ РІРёС‚СЂРёРЅС‹",
                "product_id": product_id,
                "sku": product.sku,
                "url": product.url,
                "error": str(exc),
                "traceback": trace,
            },
        )


@router.post("/{product_id}/delete")
def delete_monitored_product(product_id: int, db: Session = Depends(get_db)):
    """РЈРґР°Р»РёС‚СЊ С‚РѕРІР°СЂ РёР· РјРѕРЅРёС‚РѕСЂРёРЅРіР°."""
    product = db.query(MonitoredProduct).filter(MonitoredProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="РўРѕРІР°СЂ РЅРµ РЅР°Р№РґРµРЅ")

    db.query(PriceMonitor).filter(
        PriceMonitor.sku == product.sku,
        PriceMonitor.url == product.url,
    ).delete()

    db.delete(product)
    db.commit()

    log_action(db, "price_monitor_deleted", f"РЈРґР°Р»С‘РЅ С‚РѕРІР°СЂ РёР· РјРѕРЅРёС‚РѕСЂРёРЅРіР°: sku={product.sku}.")
    return RedirectResponse(
        url="/price-monitor/?message=РўРѕРІР°СЂ СѓРґР°Р»С‘РЅ",
        status_code=303,
    )

