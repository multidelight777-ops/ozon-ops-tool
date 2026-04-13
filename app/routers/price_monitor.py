import asyncio
import logging
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MonitoredProduct, PriceMonitor
from app.services.logger import log_action
from app.services.ozon_price_monitor import OzonPriceMonitor


moscow = ZoneInfo("Europe/Moscow")
logger = logging.getLogger("app.price_monitor")

router = APIRouter(prefix="/price-monitor", tags=["price_monitor"])
templates = Jinja2Templates(directory="app/templates")


def _to_moscow_string(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str | None:
    """Преобразовать datetime в строку московского времени."""
    if value is None:
        return None

    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(moscow).strftime(fmt)


def _build_rows(db: Session) -> list[dict]:
    """Собрать строки таблицы с последней ценой по каждому товару."""
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
        difference = None
        if price_with_spp is not None and price_without_spp is not None:
            difference = round(price_without_spp - price_with_spp, 2)

        rows.append(
            {
                "product": product,
                "price_with_spp": price_with_spp,
                "price_without_spp": price_without_spp,
                "difference": difference,
                "checked_at": _to_moscow_string(latest_check.checked_at) if latest_check else None,
            }
        )

    return rows


@router.get("/", response_class=HTMLResponse)
def price_monitor_page(request: Request, db: Session = Depends(get_db)):
    """Показать страницу мониторинга витринных цен."""
    return templates.TemplateResponse(
        "price_monitor.html",
        {
            "request": request,
            "rows": _build_rows(db),
            "message": request.query_params.get("message", ""),
        },
    )


@router.get("/{product_id}/chart", response_class=HTMLResponse)
def price_monitor_chart_page(product_id: int, request: Request, db: Session = Depends(get_db)):
    """Показать график истории цен по одному товару."""
    product = db.query(MonitoredProduct).filter(MonitoredProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

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
def add_monitored_product(request: Request, db: Session = Depends(get_db)):
    """Добавить товар в мониторинг витринных цен."""
    form = asyncio.run(request.form())
    sku = str(form.get("sku") or "").strip()
    product_name = str(form.get("product_name") or "").strip()
    url = str(form.get("url") or "").strip()

    if not sku or not product_name or not url:
        return RedirectResponse(
            url="/price-monitor/?message=Заполните SKU, название товара и URL",
            status_code=303,
        )

    product = MonitoredProduct(sku=sku, product_name=product_name, url=url)
    db.add(product)
    db.commit()

    log_action(db, "price_monitor_product_added", f"В мониторинг добавлен товар: sku={sku}, product_name={product_name}.")
    return RedirectResponse(
        url="/price-monitor/?message=Товар добавлен в мониторинг",
        status_code=303,
    )


@router.post("/{product_id}/refresh")
def refresh_product_price(product_id: int, db: Session = Depends(get_db)):
    """Обновить витринные цены для одного товара и вернуть диагностику при ошибке."""
    logger.info("Вход в маршрут обновления цены витрины: product_id=%s", product_id)

    product = db.query(MonitoredProduct).filter(MonitoredProduct.id == product_id).first()
    if not product:
        logger.warning("Товар для обновления цены не найден: product_id=%s", product_id)
        raise HTTPException(status_code=404, detail="Товар не найден")

    logger.info(
        "Старт обновления цены витрины: product_id=%s, sku=%s, url=%s",
        product_id,
        product.sku,
        product.url,
    )

    monitor = OzonPriceMonitor()
    logger.info(
        "Playwright browser engine=%s, timeout_ms=%s, launch_args=%s",
        "chromium",
        monitor.timeout_ms,
        monitor.browser_args,
    )

    try:
        result = asyncio.run(monitor.get_price(product.url))
        logger.info(
            "Парсинг цены завершён: product_id=%s, sku=%s, result=%s",
            product_id,
            product.sku,
            result,
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
                f"Обновлены цены товара sku={product.sku}. "
                f"Цена с СПП={result.get('price_with_spp')}, "
                f"цена без СПП={result.get('price_without_spp')}."
            ),
        )
        return RedirectResponse(
            url="/price-monitor/?message=Цены успешно обновлены",
            status_code=303,
        )
    except Exception as exc:
        trace = traceback.format_exc()
        logger.exception(
            "Ошибка в refresh flow мониторинга цен: product_id=%s, sku=%s, url=%s, error=%s",
            product_id,
            product.sku,
            product.url,
            exc,
        )

        try:
            log_action(
                db,
                "price_monitor_refresh_error",
                f"Ошибка обновления цены товара sku={product.sku}, url={product.url}: {exc}",
            )
        except Exception:
            logger.exception("Не удалось записать ошибку обновления цены в AuditLog: product_id=%s", product_id)

        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "message": "Не удалось обновить цену витрины",
                "product_id": product_id,
                "sku": product.sku,
                "url": product.url,
                "error": str(exc),
                "traceback": trace,
            },
        )


@router.post("/{product_id}/delete")
def delete_monitored_product(product_id: int, db: Session = Depends(get_db)):
    """Удалить товар из мониторинга."""
    product = db.query(MonitoredProduct).filter(MonitoredProduct.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    db.query(PriceMonitor).filter(
        PriceMonitor.sku == product.sku,
        PriceMonitor.url == product.url,
    ).delete()

    db.delete(product)
    db.commit()

    log_action(db, "price_monitor_deleted", f"Удалён товар из мониторинга: sku={product.sku}.")
    return RedirectResponse(
        url="/price-monitor/?message=Товар удалён",
        status_code=303,
    )
