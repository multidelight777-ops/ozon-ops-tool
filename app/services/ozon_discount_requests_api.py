import logging
from typing import Any

import requests

from app.config import settings


logger = logging.getLogger(__name__)


OZON_API_BASE_URL = "https://api-seller.ozon.ru"
FETCH_DISCOUNT_REQUESTS_ENDPOINT = "/v1/actions/discounts"
APPROVE_DISCOUNT_REQUEST_ENDPOINT = "/v1/actions/discounts/approve"

HEADER_CLIENT_ID = "Client-Id"
HEADER_API_KEY = "Api-Key"
HEADER_CONTENT_TYPE = "Content-Type"
CONTENT_TYPE_JSON = "application/json"

DEFAULT_FETCH_PAYLOAD = {
    "filter": {},
    "limit": 100,
    "offset": 0,
}

DEFAULT_TIMEOUT_SECONDS = 30


class OzonDiscountClient:
    """Простой клиент для работы с заявками на скидку Ozon."""

    def __init__(self) -> None:
        self.client_id = settings.OZON_CLIENT_ID
        self.api_key = settings.OZON_API_KEY
        self.base_url = OZON_API_BASE_URL
        self.session = requests.Session()

        logger.info(
            "Инициализирован OzonDiscountClient: client_id_present=%s, api_key_present=%s",
            bool(self.client_id),
            bool(self.api_key),
        )

    def headers(self) -> dict[str, str]:
        """Собрать стандартные заголовки для запросов в Ozon API."""
        return {
            HEADER_CLIENT_ID: self.client_id,
            HEADER_API_KEY: self.api_key,
            HEADER_CONTENT_TYPE: CONTENT_TYPE_JSON,
        }

    def test_connection(self) -> dict[str, Any]:
        """Проверить, что ключи Ozon API загружены из .env."""
        client_id_present = bool((self.client_id or "").strip())
        api_key_present = bool((self.api_key or "").strip())
        ok = client_id_present and api_key_present

        if ok:
            message = "Ключи Ozon API загружены."
        else:
            message = "Не все ключи Ozon API заполнены в .env."

        logger.info(
            "Проверка настроек Ozon API для скидок: ok=%s, client_id_present=%s, api_key_present=%s",
            ok,
            client_id_present,
            api_key_present,
        )

        return {
            "ok": ok,
            "client_id_present": client_id_present,
            "api_key_present": api_key_present,
            "message": message,
        }

    def fetch_discount_requests(self) -> dict[str, Any]:
        """Получить заявки на скидку из Ozon Seller API."""
        url = f"{self.base_url}{FETCH_DISCOUNT_REQUESTS_ENDPOINT}"
        payload = dict(DEFAULT_FETCH_PAYLOAD)

        logger.info("Запрос заявок на скидку Ozon: url=%s", url)

        if not self.client_id or not self.api_key:
            logger.warning("Загрузка заявок из Ozon не выполнена: ключи API не заполнены.")
            return {
                "ok": False,
                "items": [],
                "status_code": None,
                "message": "Не заполнены OZON_CLIENT_ID и/или OZON_API_KEY.",
            }

        try:
            response = self.session.post(
                url,
                headers=self.headers(),
                json=payload,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()

            items: list[dict[str, Any]] = []
            if isinstance(data, dict):
                result = data.get("result")
                if isinstance(result, list):
                    items = result
                elif isinstance(result, dict):
                    items = result.get("items") or result.get("discounts") or []

            logger.info(
                "Заявки на скидку из Ozon получены успешно: status_code=%s, items=%s",
                response.status_code,
                len(items),
            )
            return {
                "ok": True,
                "items": items,
                "status_code": response.status_code,
                "message": f"Получено заявок: {len(items)}",
                "raw": data,
            }
        except requests.RequestException as exc:
            status_code = getattr(exc.response, "status_code", None)
            response_text = getattr(exc.response, "text", "")
            logger.exception(
                "Ошибка загрузки заявок на скидку из Ozon: status_code=%s, response=%s",
                status_code,
                response_text,
            )
            return {
                "ok": False,
                "items": [],
                "status_code": status_code,
                "message": f"Ошибка запроса к Ozon API: {exc}",
                "raw": response_text,
            }

    def approve_discount_request(
        self,
        external_id: str,
        approved_discount_percent: float | None = None,
        approved_price: float | None = None,
    ) -> dict[str, Any]:
        """Отправить одобрение одной заявки на скидку в Ozon."""
        url = f"{self.base_url}{APPROVE_DISCOUNT_REQUEST_ENDPOINT}"
        payload = {
            "external_id": external_id,
            "approved_discount_percent": approved_discount_percent,
            "approved_price": approved_price,
        }

        logger.info(
            "Подготовка запроса на одобрение заявки на скидку: external_id=%s, approved_discount_percent=%s, approved_price=%s",
            external_id,
            approved_discount_percent,
            approved_price,
        )
        logger.info("Endpoint одобрения заявки Ozon: url=%s", url)

        if not external_id:
            return {
                "ok": False,
                "status_code": None,
                "payload": payload,
                "message": "Не указан внешний ID заявки.",
            }

        if approved_price in (None, ""):
            return {
                "ok": False,
                "status_code": None,
                "payload": payload,
                "message": "Не заполнена одобренная цена.",
            }

        if not self.client_id or not self.api_key:
            return {
                "ok": False,
                "status_code": None,
                "payload": payload,
                "message": "Не заполнены OZON_CLIENT_ID и/или OZON_API_KEY.",
            }

        try:
            response = self.session.post(
                url,
                headers=self.headers(),
                json=payload,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response_text = response.text

            logger.info(
                "Ответ Ozon по одобрению скидки: status_code=%s, body=%s",
                response.status_code,
                response_text,
            )

            try:
                data = response.json()
            except ValueError:
                data = {"raw": response_text}

            if response.ok:
                return {
                    "ok": True,
                    "status_code": response.status_code,
                    "payload": payload,
                    "message": "Заявка успешно отправлена в Ozon.",
                    "raw": data,
                }

            return {
                "ok": False,
                "status_code": response.status_code,
                "payload": payload,
                "message": f"Ozon вернул ошибку: {response_text}",
                "raw": data,
            }
        except requests.RequestException as exc:
            status_code = getattr(exc.response, "status_code", None)
            response_text = getattr(exc.response, "text", "")
            logger.exception(
                "Ошибка отправки заявки на скидку в Ozon: status_code=%s, response=%s",
                status_code,
                response_text,
            )
            return {
                "ok": False,
                "status_code": status_code,
                "payload": payload,
                "message": f"Ошибка запроса к Ozon API: {exc}",
                "raw": response_text,
            }
