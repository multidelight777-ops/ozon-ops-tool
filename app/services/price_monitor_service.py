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
    """РћР±РЅРѕРІРёС‚СЊ С†РµРЅС‹ РІРёС‚СЂРёРЅС‹ РґР»СЏ РІСЃРµС… С‚РѕРІР°СЂРѕРІ РёР· РјРѕРЅРёС‚РѕСЂРёРЅРіР°."""
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

        logger.info("РЎС‚Р°СЂС‚ РјР°СЃСЃРѕРІРѕРіРѕ РѕР±РЅРѕРІР»РµРЅРёСЏ С†РµРЅ РІРёС‚СЂРёРЅС‹. РўРѕРІР°СЂРѕРІ РІ РјРѕРЅРёС‚РѕСЂРёРЅРіРµ: %s", len(products))

        for product in products:
            stats["processed"] += 1
            try:
                print(f"[AUTO] parsing {product.sku} {product.url}")
                result = await monitor.get_price(product.url)
                logger.info(
                    "%s: %s / %s",
                    product.sku,
                    result.get("price_with_spp"),
                    result.get("price_without_spp"),
                )
                if result.get("price_with_spp") is None and result.get("price_without_spp") is None:
                    stats["errors"] += 1
                    log_action(
                        db,
                        "price_monitor_auto_refresh_error",
                        f"Ошибка автообновления цен для sku={product.sku}: парсер не нашёл цену.",
                    )
                    continue
                price_row = PriceMonitor(
                    sku=product.sku,
                    url=product.url,
                    price_with_spp=result.get("price_with_spp"),
                    price_without_spp=result.get("price_without_spp"),
                    checked_at=datetime.utcnow(),
                )
                # In the current schema there is no separate last_checked field,
                # so we update the product timestamp alongside the history row.
                product.updated_at = datetime.utcnow()
                db.add(product)
                db.add(price_row)
                db.commit()
                stats["updated"] += 1

                log_action(
                    db,
                    "price_monitor_auto_refreshed",
                    (
                        f"РђРІС‚РѕРѕР±РЅРѕРІР»РµРЅРёРµ С†РµРЅ РґР»СЏ sku={product.sku}. "
                        f"price_with_spp={result.get('price_with_spp')}, "
                        f"price_without_spp={result.get('price_without_spp')}."
                    ),
                )
            except Exception as exc:
                stats["errors"] += 1
                print(f"[AUTO] ERROR {product.sku}: {exc}")
                log_action(
                    db,
                    "price_monitor_auto_refresh_error",
                    f"РћС€РёР±РєР° Р°РІС‚РѕРѕР±РЅРѕРІР»РµРЅРёСЏ С†РµРЅ РґР»СЏ sku={product.sku}: {exc}",
                )

        stats["duration_seconds"] = round(time.perf_counter() - started_at, 2)
        logger.info(
            "РњР°СЃСЃРѕРІРѕРµ РѕР±РЅРѕРІР»РµРЅРёРµ С†РµРЅ РІРёС‚СЂРёРЅС‹ Р·Р°РІРµСЂС€РµРЅРѕ. updated=%s, errors=%s, duration_seconds=%s",
            stats["updated"],
            stats["errors"],
            stats["duration_seconds"],
        )
        return stats
    finally:
        db.close()

