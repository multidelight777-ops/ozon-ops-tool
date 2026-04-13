import logging
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


logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_MS = 30000
DEFAULT_WAIT_AFTER_LOAD_MS = 5000
DEFAULT_WAIT_AFTER_SCROLL_MS = 2000
PLAYWRIGHT_BROWSER_ENGINE = "chromium"
DEFAULT_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
DEBUG_HTML_DIR = Path(BASE_DIR) / "app" / "data" / "debug"

PRICE_WITH_SPP_SELECTORS = [
    "span.tsHeadline600Large",
    "span[data-widget='webPrice']",
    "div[data-widget='webPrice'] span",
]
PRICE_WITHOUT_SPP_SELECTORS = [
    "span.pdp_i4b.tsHeadline500Medium",
    "span:has-text('без Ozon Банка')",
    "div:has-text('без Ozon Банка') span",
]
NEGATIVE_PAGE_MARKERS = ["captcha", "доступ ограничен", "403", "temporarily unavailable"]


class OzonPriceMonitor:
    """Асинхронный монитор витринных цен Ozon через Playwright."""

    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.timeout_ms = timeout_ms
        self.browser_args = list(DEFAULT_BROWSER_ARGS)

    async def get_price(self, url: str) -> dict[str, float | None]:
        """Открыть карточку товара и вернуть цены с Ozon Банком и без него."""
        logger.info("Запуск мониторинга цены Ozon: url=%s", url)

        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            playwright_version = self._get_playwright_version()
            headless_env_value = settings.PRICE_MONITOR_HEADLESS
            headless = self._normalize_headless(headless_env_value)
            launch_options = {"headless": headless, "args": self.browser_args}

            logger.info("PRICE_MONITOR_HEADLESS=%s", headless_env_value)
            logger.info("Playwright headless=%s", headless)
            logger.info("Playwright package version=%s", playwright_version)
            logger.info("Browser engine=%s", PLAYWRIGHT_BROWSER_ENGINE)
            logger.info("Параметры запуска Playwright=%s", launch_options)

            async with async_playwright() as playwright:
                logger.info(
                    "Chromium executable path=%s",
                    playwright.chromium.executable_path,
                )

                browser = await playwright.chromium.launch(**launch_options)
                logger.info(
                    "Playwright запущен в %s режиме",
                    "headless" if headless else "видимом",
                )

                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                    locale="ru-RU",
                )
                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)
                logger.info("Stealth режим отключён")

                await self._open_product_page(page, url)
                await self._log_page_diagnostics(page, url)

                price_with_spp_text = await self._find_price_by_selectors(
                    page=page,
                    url=url,
                    label="price_with_spp",
                    selectors=PRICE_WITH_SPP_SELECTORS,
                )
                price_without_spp_text = await self._find_price_by_selectors(
                    page=page,
                    url=url,
                    label="price_without_spp",
                    selectors=PRICE_WITHOUT_SPP_SELECTORS,
                )

                if price_with_spp_text is None or price_without_spp_text is None:
                    fallback_with_spp, fallback_without_spp = await self._extract_fallback_prices(page, url)
                    price_with_spp_text = price_with_spp_text or fallback_with_spp
                    price_without_spp_text = price_without_spp_text or fallback_without_spp

                page_title = await page.title()
                current_url = page.url
                html = await page.content()
                html_snippet = html[:1000]

                if self._looks_like_block_page(page_title, html):
                    logger.warning(
                        "Похоже, Ozon вернул страницу блокировки или капчу: url=%s, final_url=%s, title=%s, html_snippet=%s",
                        url,
                        current_url,
                        page_title,
                        html_snippet,
                    )

                price_with_spp = self._clean_price(price_with_spp_text)
                price_without_spp = self._clean_price(price_without_spp_text)

                logger.info(
                    "Результат парсинга цен Ozon: url=%s, final_url=%s, title=%s, raw_with_spp=%s, raw_without_spp=%s, price_with_spp=%s, price_without_spp=%s",
                    url,
                    current_url,
                    page_title,
                    price_with_spp_text,
                    price_without_spp_text,
                    price_with_spp,
                    price_without_spp,
                )

                return {
                    "price_with_spp": price_with_spp,
                    "price_without_spp": price_without_spp,
                }
        except PlaywrightTimeoutError as exc:
            logger.exception("Таймаут при мониторинге цены Ozon: url=%s, error=%s", url, exc)
            raise RuntimeError(f"Не удалось дождаться загрузки страницы или цен для {url}") from exc
        except Exception as exc:
            logger.exception("Ошибка мониторинга цены Ozon: url=%s, error=%s", url, exc)
            raise RuntimeError(f"Не удалось получить цены с витрины Ozon для {url}: {exc}") from exc
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()

    async def _open_product_page(self, page: Page, url: str) -> None:
        """Открыть страницу товара и дождаться основной загрузки."""
        await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        await page.wait_for_timeout(DEFAULT_WAIT_AFTER_LOAD_MS)
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(DEFAULT_WAIT_AFTER_SCROLL_MS)

    async def _find_price_by_selectors(
        self,
        page: Page,
        url: str,
        label: str,
        selectors: list[str],
    ) -> str | None:
        """Найти цену по нескольким селекторам, не падая при смене вёрстки."""
        for selector in selectors:
            text = await self._get_price_text(page, selector, url, label)
            if text:
                return text
        return None

    async def _get_price_text(self, page: Page, selector: str, url: str, label: str) -> str | None:
        """Получить сырой текст цены по селектору без ожидания visible."""
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count > 0:
                text = await locator.first.inner_text()
                logger.info(
                    "Цена найдена по селектору: url=%s, label=%s, selector=%s, text=%s",
                    url,
                    label,
                    selector,
                    text,
                )
                return text

            html = await page.content()
            logger.warning(
                "Селектор цены не найден: url=%s, label=%s, selector=%s, html_length=%s, html_snippet=%s",
                url,
                label,
                selector,
                len(html),
                html[:1000],
            )
            await self._dump_debug_html(html, f"{label}_{self._slugify_selector(selector)}")
            return None
        except Exception as exc:
            html = await page.content()
            logger.warning(
                "Ошибка чтения цены по селектору: url=%s, label=%s, selector=%s, error=%s, html_length=%s, html_snippet=%s",
                url,
                label,
                selector,
                exc,
                len(html),
                html[:1000],
            )
            await self._dump_debug_html(html, f"{label}_{self._slugify_selector(selector)}")
            return None

    async def _extract_fallback_prices(self, page: Page, url: str) -> tuple[str | None, str | None]:
        """Fallback: найти любые цены на странице и взять первую и вторую подходящие."""
        try:
            all_spans = await page.locator("span").all_inner_texts()
            html = await page.content()
            candidates = [text.strip() for text in all_spans if "₽" in text or "в‚Ѕ" in text]
            price_with_spp = candidates[0] if len(candidates) > 0 else None
            price_without_spp = candidates[1] if len(candidates) > 1 else None

            logger.warning(
                "Сработал fallback-парсинг цен Ozon: url=%s, fallback_with_spp=%s, fallback_without_spp=%s, candidates=%s, html_length=%s",
                url,
                price_with_spp,
                price_without_spp,
                candidates[:10],
                len(html),
            )
            return price_with_spp, price_without_spp
        except Exception as exc:
            logger.warning("Fallback-парсинг цен не удался: url=%s, error=%s", url, exc)
            return None, None

    async def _log_page_diagnostics(self, page: Page, url: str) -> None:
        """Залогировать диагностические данные страницы после загрузки."""
        try:
            title = await page.title()
            html = await page.content()
            logger.info(
                "Диагностика страницы Ozon: requested_url=%s, final_url=%s, title=%s, html_length=%s",
                url,
                page.url,
                title,
                len(html),
            )
        except Exception as exc:
            logger.warning("Не удалось собрать диагностику страницы: url=%s, error=%s", url, exc)

    async def _dump_debug_html(self, html: str, label: str) -> None:
        """Сохранить HTML страницы в debug-файл для разбора проблемного кейса."""
        try:
            DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
            file_path = DEBUG_HTML_DIR / f"{label}_page_dump.html"
            file_path.write_text(html, encoding="utf-8")
            logger.warning("HTML страницы сохранён в debug-файл: path=%s", file_path)
        except Exception as exc:
            logger.warning("Не удалось сохранить HTML страницы в debug-файл: error=%s", exc)

    def _clean_price(self, raw_text: str | None) -> float | None:
        """Очистить текст цены и вернуть float."""
        if not raw_text:
            return None

        normalized = (
            raw_text.replace("₽", "")
            .replace("в‚Ѕ", "")
            .replace("&thinsp;", "")
            .replace("\u2009", "")
            .replace("\u202f", "")
            .replace("\xa0", " ")
            .replace(" ", "")
            .replace(",", ".")
            .strip()
        )

        if not normalized:
            return None

        try:
            return float(normalized)
        except ValueError:
            logger.warning("Не удалось преобразовать цену в float: raw_text=%s, normalized=%s", raw_text, normalized)
            return None

    def _normalize_headless(self, value: object) -> bool:
        """Надёжно нормализовать строковое значение headless из .env."""
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        return True

    def _looks_like_block_page(self, title: str, html: str) -> bool:
        """Определить, что вместо товара вернулась заглушка, блокировка или капча."""
        haystack = f"{title}\n{html[:4000]}".lower()
        return any(marker in haystack for marker in NEGATIVE_PAGE_MARKERS)

    def _slugify_selector(self, selector: str) -> str:
        """Сделать селектор безопасным для имени debug-файла."""
        return (
            selector.replace(" ", "_")
            .replace(".", "_")
            .replace("[", "_")
            .replace("]", "_")
            .replace(":", "_")
            .replace("'", "")
            .replace('"', "")
            .replace("/", "_")
        )

    def _get_playwright_version(self) -> str:
        """Вернуть версию пакета playwright, если он установлен."""
        try:
            return version("playwright")
        except PackageNotFoundError:
            return "not_installed"
