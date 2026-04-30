from datetime import datetime
import logging
import time

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import MonitoredProduct, PriceMonitor
from app.services.logger import log_action
from app.services.ozon_price_monitor import OzonPriceMonitor


logger = logging.getLogger("app.price_monitor_service")


def _calculate_percents(product: MonitoredProduct) -> None:
    """Persist percentages on the product snapshot when base_price is available."""
    product.percent_with_spp = None
    product.percent_without_spp = None

    if product.base_price and product.base_price > 0:
        if product.price_with_spp:
            product.percent_with_spp = round((product.price_with_spp / product.base_price) * 100, 2)
        if product.price_without_spp:
            product.percent_without_spp = round((product.price_without_spp / product.base_price) * 100, 2)


async def refresh_all_prices() -> dict[str, int | float]:
    """Refresh storefront prices for all monitored products and save snapshot + history."""
    db: Session = SessionLocal()
    started_at = time.perf_counter()
    stats: dict[str, int | float] = {
        "processed": 0,
        "updated": 0,
        "errors": 0,
        "duration_seconds": 0.0,
    }

    try:
        products = db.query(MonitoredProduct).order_by(MonitoredProduct.created_at.desc()).all()
        monitor = OzonPriceMonitor()

        logger.info("Старт массового обновления цен. Товаров: %s", len(products))

        for product in products:
            stats["processed"] += 1
            try:
                print(f"[AUTO] parsing {product.sku} {product.url}")
                result = await monitor.get_price(product.url)

                price_with_spp = result.get("price_with_spp")
                price_without_spp = result.get("price_without_spp")
                if price_with_spp is None and price_without_spp is None:
                    stats["errors"] += 1
                    log_action(
                        db,
                        "price_monitor_auto_refresh_error",
                        f"Ошибка автообновления цен для sku={product.sku}: парсер не нашёл цену.",
                    )
                    continue

                now_utc = datetime.utcnow()
                product.price_with_spp = price_with_spp
                product.price_without_spp = price_without_spp
                product.last_checked = now_utc
                product.updated_at = now_utc
                _calculate_percents(product)

                price_row = PriceMonitor(
                    sku=product.sku,
                    url=product.url,
                    price_with_spp=price_with_spp,
                    price_without_spp=price_without_spp,
                    checked_at=now_utc,
                )

                db.add(product)
                db.add(price_row)
                db.commit()
                db.refresh(product)
                stats["updated"] += 1

                log_action(
                    db,
                    "price_monitor_auto_refreshed",
                    (
                        f"Автообновление цен для sku={product.sku}. "
                        f"price_with_spp={price_with_spp}, price_without_spp={price_without_spp}, "
                        f"percent_with_spp={product.percent_with_spp}, percent_without_spp={product.percent_without_spp}."
                    ),
                )
            except Exception as exc:
                stats["errors"] += 1
                db.rollback()
                print(f"[AUTO] ERROR {product.sku}: {exc}")
                log_action(
                    db,
                    "price_monitor_auto_refresh_error",
                    f"Ошибка автообновления цен для sku={product.sku}: {exc}",
                )

        stats["duration_seconds"] = round(time.perf_counter() - started_at, 2)
        logger.info(
            "Массовое обновление цен завершено. updated=%s, errors=%s, duration_seconds=%s",
            stats["updated"],
            stats["errors"],
            stats["duration_seconds"],
        )
        return stats
    finally:
        db.close()
