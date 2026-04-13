from datetime import datetime
import time
import logging

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import MonitoredProduct, PriceMonitor
from app.services.logger import log_action
from app.services.ozon_price_monitor import OzonPriceMonitor


logger = logging.getLogger("app.price_monitor_service")


async def refresh_all_prices() -> dict[str, int]:
    """Обновить цены витрины для всех товаров из мониторинга."""
    db: Session = SessionLocal()
    started_at = time.perf_counter()
    stats = {
        "processed": 0,
        "updated": 0,
        "errors": 0,
        "duration_seconds": 0.0,
    }

    try:
        products = db.query(MonitoredProduct).order_by(MonitoredProduct.created_at.desc()).all()
        monitor = OzonPriceMonitor()

        logger.info("Старт массового обновления цен витрины. Товаров в мониторинге: %s", len(products))

        for product in products:
            stats["processed"] += 1
            try:
                result = await monitor.get_price(product.url)
                price_row = PriceMonitor(
                    sku=product.sku,
                    url=product.url,
                    price_with_spp=result.get("price_with_spp"),
                    price_without_spp=result.get("price_without_spp"),
                    checked_at=datetime.utcnow(),
                )
                db.add(price_row)
                db.commit()
                stats["updated"] += 1

                log_action(
                    db,
                    "price_monitor_auto_refreshed",
                    (
                        f"Автообновление цен для sku={product.sku}. "
                        f"price_with_spp={result.get('price_with_spp')}, "
                        f"price_without_spp={result.get('price_without_spp')}."
                    ),
                )
            except Exception as exc:
                stats["errors"] += 1
                log_action(
                    db,
                    "price_monitor_auto_refresh_error",
                    f"Ошибка автообновления цен для sku={product.sku}: {exc}",
                )

        stats["duration_seconds"] = round(time.perf_counter() - started_at, 2)
        logger.info(
            "Массовое обновление цен витрины завершено. updated=%s, errors=%s, duration_seconds=%s",
            stats["updated"],
            stats["errors"],
            stats["duration_seconds"],
        )
        return stats
    finally:
        db.close()
