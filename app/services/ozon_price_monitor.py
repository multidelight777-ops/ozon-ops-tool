import asyncio
import logging
import os
import random
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.config import BASE_DIR, settings


logger = logging.getLogger("app.ozon_price_monitor")


DEFAULT_TIMEOUT_MS = 30000
PLAYWRIGHT_BROWSER_ENGINE = "chromium"
DEBUG_HTML_DIR = Path(BASE_DIR) / "app" / "data" / "debug"
NEGATIVE_PAGE_MARKERS = [
    "captcha",
    "доступ ограничен",
    "403",
    "temporarily unavailable",
    "cloudflare",
]


class OzonPriceMonitor:
    """Асинхронный монитор витринных цен Ozon через Playwright."""

    def __init__(self, timeout_ms: int | None = None) -> None:
        self.timeout_ms = timeout_ms or settings.PRICE_MONITOR_TIMEOUT_MS or DEFAULT_TIMEOUT_MS
        self.browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]

    async def get_price(self, url: str) -> dict[str, float | None]:
        """Открыть карточку товара и вернуть точные цены из известных CSS-селекторов Ozon."""
        logger.info("Запуск мониторинга цены Ozon: url=%s", url)
        print("START PARSING:", url)
        os.makedirs("app/data/debug", exist_ok=True)

        browser: Browser | None = None
        html = ""
        page_title = ""
        final_url = url

        try:
            playwright_version = self._get_playwright_version()
            headless_env_value = settings.PRICE_MONITOR_HEADLESS or "false"
            proxy = {
                "server": "http://135.106.99.221:62656",
                "username": "p3CrpmrE",
                "password": "WaWYAaFF",
            }
            launch_options = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
                "proxy": proxy,
            }

            logger.info("USING PROXY: %s", proxy["server"])
            logger.info("PRICE_MONITOR_HEADLESS=%s", headless_env_value)
            logger.info("Playwright headless=%s", launch_options["headless"])
            logger.info("Playwright package version=%s", playwright_version)
            logger.info("Browser engine=%s", PLAYWRIGHT_BROWSER_ENGINE)
            logger.info("Параметры запуска Playwright=%s", launch_options)

            async with async_playwright() as playwright:
                logger.info("Chromium executable path=%s", playwright.chromium.executable_path)
                browser = None
                for launch_attempt in range(2):
                    try:
                        browser = await playwright.chromium.launch(**launch_options)
                        browser.on("disconnected", lambda: logger.error("BROWSER CRASHED"))
                        break
                    except Exception as exc:
                        logger.warning("Ошибка запуска Chromium, попытка=%s error=%s", launch_attempt + 1, exc)
                        if launch_attempt == 1:
                            raise exc
                        await asyncio.sleep(2)

                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                    locale="ru-RU",
                )
                await context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                    """
                )

                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)

                for attempt in range(3):
                    logger.info("Попытка парсинга Ozon: attempt=%s url=%s", attempt + 1, url)
                    await self._open_product_page(page, url)
                    await page.mouse.move(200, 300)
                    await page.mouse.move(400, 500)
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(random.randint(3000, 7000))

                    page_title = await page.title()
                    final_url = page.url
                    await page.screenshot(path="app/data/debug/debug.png")

                    html = await page.content()
                    with open("app/data/debug/last_page.html", "w", encoding="utf-8") as file:
                        file.write(html)
                    print("HTML SAVED")

                    logger.info(
                        "Диагностика страницы Ozon: requested_url=%s, final_url=%s, title=%s, html_length=%s",
                        url,
                        final_url,
                        page_title,
                        len(html),
                    )
                    await self._dump_debug_html(html, "latest_price_monitor_page")

                    if self._looks_like_block_page(page_title, html):
                        logger.warning(
                            "Страница Ozon выглядит как блокировка: url=%s, final_url=%s, title=%s, html_snippet=%s",
                            url,
                            final_url,
                            page_title,
                            html[:1000],
                        )
                        with open("app/data/debug/last_failed_page.html", "w", encoding="utf-8") as file:
                            file.write(html)
                        await page.wait_for_timeout(random.randint(4000, 8000))
                        continue

                    price_with_spp_text = None
                    price_without_spp_text = None

                    try:
                        el = page.locator("span.tsHeadline600Large").first
                        if await el.count() > 0:
                            price_with_spp_text = await el.inner_text()
                    except Exception:
                        pass

                    try:
                        el2 = page.locator("span.pdp_i4b.tsHeadline500Medium").first
                        if await el2.count() > 0:
                            price_without_spp_text = await el2.inner_text()
                    except Exception:
                        pass

                    logger.info("OZON RAW PRICES: spp=%s, no_spp=%s", price_with_spp_text, price_without_spp_text)

                    price_with_spp = self._clean_price(price_with_spp_text)
                    price_without_spp = self._clean_price(price_without_spp_text)

                    logger.info(
                        "Результат парсинга цен Ozon: url=%s, final_url=%s, title=%s, raw_with_spp=%s, raw_without_spp=%s, price_with_spp=%s, price_without_spp=%s",
                        url,
                        final_url,
                        page_title,
                        price_with_spp_text,
                        price_without_spp_text,
                        price_with_spp,
                        price_without_spp,
                    )

                    if price_with_spp is not None or price_without_spp is not None:
                        return {
                            "price_with_spp": price_with_spp,
                            "price_without_spp": price_without_spp,
                        }

                    print("PRICE NOT FOUND")
                    with open("app/data/debug/last_failed_page.html", "w", encoding="utf-8") as file:
                        file.write(html)
                    logger.warning(
                        "Цена не найдена по точным селекторам: attempt=%s url=%s final_url=%s title=%s",
                        attempt + 1,
                        url,
                        final_url,
                        page_title,
                    )
                    await page.wait_for_timeout(random.randint(4000, 8000))

                return {
                    "price_with_spp": None,
                    "price_without_spp": None,
                }
        except PlaywrightTimeoutError:
            logger.exception("Таймаут при мониторинге цены Ozon: url=%s", url)
            if html:
                with open("app/data/debug/last_failed_page.html", "w", encoding="utf-8") as file:
                    file.write(html)
            return {
                "price_with_spp": None,
                "price_without_spp": None,
            }
        except Exception:
            logger.exception("Ошибка мониторинга цены Ozon: url=%s", url)
            if html:
                with open("app/data/debug/last_failed_page.html", "w", encoding="utf-8") as file:
                    file.write(html)
            return {
                "price_with_spp": None,
                "price_without_spp": None,
            }
        finally:
            if browser is not None:
                await browser.close()

    async def _open_product_page(self, page: Page, url: str) -> None:
        """Открыть страницу товара и дождаться базовой загрузки."""
        await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await page.wait_for_selector("body", timeout=60000)
        await page.wait_for_timeout(3000)
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(2000)

    async def _dump_debug_html(self, html: str, label: str) -> None:
        """Сохранить HTML страницы в debug-файл для диагностики."""
        try:
            DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
            file_path = DEBUG_HTML_DIR / f"{label}.html"
            file_path.write_text(html, encoding="utf-8")
            logger.info("HTML страницы сохранён в debug-файл: path=%s", file_path)
        except Exception as exc:
            logger.warning("Не удалось сохранить debug HTML: error=%s", exc)

    def _clean_price(self, raw_text: str | None) -> float | None:
        """Очистить текст цены и вернуть число."""
        if not raw_text:
            return None

        candidate = (
            raw_text.replace("₽", " ")
            .replace("&thinsp;", " ")
            .replace("\u2009", " ")
            .replace("\u202f", " ")
            .replace("\xa0", " ")
        )
        match = re.search(r"(\d[\d\s.,]*)", candidate)
        if not match:
            return None

        normalized = match.group(1).replace(" ", "").replace(",", ".").strip()
        try:
            return float(normalized)
        except ValueError:
            logger.warning("Не удалось преобразовать цену в float: raw_text=%s normalized=%s", raw_text, normalized)
            return None

    def _looks_like_block_page(self, title: str, html: str) -> bool:
        """Проверить, не вернул ли Ozon капчу, 403 или заглушку вместо карточки товара."""
        haystack = f"{title}\n{html[:4000]}".lower()
        return any(marker in haystack for marker in NEGATIVE_PAGE_MARKERS)

    def _get_playwright_version(self) -> str:
        """Вернуть версию playwright, если пакет установлен."""
        try:
            return version("playwright")
        except PackageNotFoundError:
            return "not_installed"
