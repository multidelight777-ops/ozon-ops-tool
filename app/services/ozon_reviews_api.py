import json
import logging
from typing import Any

import requests

from app.config import env_presence_map, settings


logger = logging.getLogger("app.ozon_reviews_api")


class OzonClient:
    """Простой клиент Ozon Seller API для отзывов и вопросов."""

    def __init__(self) -> None:
        self.client_id = settings.OZON_CLIENT_ID
        self.api_key = settings.OZON_API_KEY
        self.base_url = settings.OZON_SELLER_BASE_URL.rstrip("/")
        self.reviews_list_path = settings.OZON_REVIEWS_LIST_PATH
        self.timeout_seconds = settings.OZON_REVIEWS_TIMEOUT_SECONDS
        self.session = requests.Session()
        self.last_error: dict[str, Any] | None = None

        if settings.HTTP_PROXY:
            self.session.proxies["http"] = settings.HTTP_PROXY
        if settings.HTTPS_PROXY:
            self.session.proxies["https"] = settings.HTTPS_PROXY

        logger.info(
            "Инициализирован OzonClient. base_url=%s reviews_path=%s timeout=%s env=%s",
            self.base_url,
            self.reviews_list_path,
            self.timeout_seconds,
            {
                key: value
                for key, value in env_presence_map().items()
                if key
                in {
                    "OZON_CLIENT_ID",
                    "OZON_API_KEY",
                    "OZON_SELLER_BASE_URL",
                    "OZON_REVIEWS_LIST_PATH",
                    "OZON_REVIEWS_TIMEOUT_SECONDS",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "NO_PROXY",
                }
            },
        )

    def build_headers(self) -> dict[str, str]:
        """Собрать стандартные заголовки Ozon API."""
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def _masked_api_key(self) -> str:
        """Вернуть замаскированный Api-Key для диагностики."""
        if not self.api_key:
            return "MISSING"
        return f"{self.api_key[:6]}..."

    def _summarize_response_body(self, response: requests.Response) -> str:
        """Вернуть короткую строку с телом ответа."""
        try:
            payload = response.json()
            compact = json.dumps(payload, ensure_ascii=False)
            return compact[:1000] if compact else "-"
        except ValueError:
            text = (response.text or "").strip()
            return text[:1000] if text else "-"

    def _build_result(
        self,
        ok: bool,
        status_code: int | None,
        message: str,
        response_summary: str,
        response_json: Any = None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "status_code": status_code,
            "message": message,
            "response_summary": response_summary,
            "response_json": response_json,
        }

    def _post_json(self, path: str, payload: dict) -> dict:
        """Выполнить POST-запрос к Ozon Seller API с полной диагностикой без raise_for_status."""
        if self.client_id is None or self.api_key is None:
            raise RuntimeError("Не заданы OZON_CLIENT_ID или OZON_API_KEY.")

        url = f"{self.base_url}{path}"
        headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

        print("=== OZON REVIEWS DEBUG ===")
        print("URL:", url)
        print("Client-Id:", self.client_id)
        print("Api-Key:", self._masked_api_key())
        print("Headers:", headers)
        print("Payload:", payload)

        logger.info(
            "Запрос в Ozon Seller API. url=%s headers=%s payload=%s timeout=%s",
            url,
            {
                "Client-Id": self.client_id,
                "Api-Key": self._masked_api_key(),
                "Content-Type": headers.get("Content-Type"),
            },
            payload,
            self.timeout_seconds,
        )

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.exception("Сетевая ошибка при запросе в Ozon Seller API: url=%s payload=%s", url, payload)
            raise RuntimeError(f"Сетевая ошибка при обращении к Ozon API: {exc}") from exc

        print("=== OZON RESPONSE ===")
        print("STATUS:", response.status_code)
        print("BODY:", response.text)

        response_summary = self._summarize_response_body(response)
        logger.info(
            "Ответ Ozon Seller API получен. url=%s status_code=%s body=%s",
            url,
            response.status_code,
            response_summary,
        )

        if response.status_code != 200:
            return {
                "ok": False,
                "status": response.status_code,
                "body": response.text,
            }

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"Ozon API вернул невалидный JSON: {response_summary}") from exc

    def test_connection(self) -> dict[str, bool | str]:
        client_id_present = bool(self.client_id)
        api_key_present = bool(self.api_key)
        ok = client_id_present and api_key_present

        if ok:
            message = "Ключи Ozon загружены из .env. Тестовое подключение готово."
            logger.info(message)
        else:
            message = "Не удалось подтвердить подключение: проверьте OZON_CLIENT_ID и OZON_API_KEY в .env."
            logger.warning(message)

        return {
            "ok": ok,
            "client_id_present": client_id_present,
            "api_key_present": api_key_present,
            "message": message,
        }

    def fetch_reviews(self) -> list[dict]:
        payload = {
            "limit": 50,
            "last_id": "",
        }
        logger.info(
            "Подготовка запроса review/list. client_id_is_none=%s api_key_is_none=%s client_id=%s api_key_masked=%s path=%s payload=%s",
            self.client_id is None,
            self.api_key is None,
            self.client_id,
            self._masked_api_key(),
            self.reviews_list_path,
            payload,
        )
        logger.info("Запущена загрузка отзывов из Ozon Seller API.")
        data = self._post_json(self.reviews_list_path, payload)
        if data.get("ok") is False:
            self.last_error = data
            logger.warning(
                "Ozon reviews API вернул ошибку. status=%s body=%s",
                data.get("status"),
                data.get("body"),
            )
            return []
        self.last_error = None
        items = data.get("reviews") or data.get("result", {}).get("reviews") or data.get("result") or []
        logger.info("Из Ozon Seller API получено отзывов: %s", len(items) if isinstance(items, list) else 0)
        return items if isinstance(items, list) else []

    def fetch_questions(self) -> list[dict]:
        logger.info("Вызван fetch_questions(). Реальный запрос к Ozon API для вопросов пока не реализован.")
        return []

    def _send_reply(
        self,
        *,
        review_id: int | None,
        external_id: str,
        source_type: str,
        reply_text: str,
        path: str,
        payload_id_key: str,
        text_key: str = "text",
    ) -> dict[str, Any]:
        cleaned_text = (reply_text or "").strip()
        cleaned_external_id = (external_id or "").strip()

        if not self.client_id or not self.api_key:
            message = "Не настроены OZON_CLIENT_ID или OZON_API_KEY."
            logger.warning(message)
            return self._build_result(False, None, message, "-")

        if not cleaned_external_id:
            message = "Не удалось отправить: отсутствует внешний ID отзыва в Ozon."
            logger.warning("%s review_id=%s external_id=%s source_type=%s", message, review_id, external_id, source_type)
            return self._build_result(False, None, message, "-")

        if not cleaned_text:
            message = "Не удалось отправить: текст ответа пустой."
            logger.warning("%s review_id=%s external_id=%s source_type=%s", message, review_id, cleaned_external_id, source_type)
            return self._build_result(False, None, message, "-")

        payload = {
            payload_id_key: cleaned_external_id,
            text_key: cleaned_text,
        }

        if not payload or not payload.get(text_key):
            message = "Не удалось отправить: payload пустой или не содержит текст ответа."
            logger.warning("%s review_id=%s external_id=%s source_type=%s payload=%s", message, review_id, cleaned_external_id, source_type, payload)
            return self._build_result(False, None, message, str(payload))

        url = f"{self.base_url}{path}"
        logger.info(
            "Отправка ответа в Ozon: review_id=%s external_id=%s source_type=%s text_length=%s payload=%s",
            review_id,
            cleaned_external_id,
            source_type,
            len(cleaned_text),
            payload,
        )

        try:
            response = self.session.post(
                url,
                json=payload,
                headers=self.build_headers(),
                timeout=self.timeout_seconds,
            )
            response_summary = self._summarize_response_body(response)
            logger.info(
                "Ответ Ozon получен: review_id=%s external_id=%s source_type=%s status_code=%s body=%s",
                review_id,
                cleaned_external_id,
                source_type,
                response.status_code,
                response_summary,
            )

            response_json = None
            try:
                response_json = response.json()
            except ValueError:
                response_json = None

            if response.ok:
                return self._build_result(True, response.status_code, "Ответ отправлен", response_summary, response_json)

            return self._build_result(False, response.status_code, "Не удалось отправить ответ в Ozon.", response_summary, response_json)
        except requests.RequestException as exc:
            logger.exception(
                "Сетевая ошибка при отправке ответа в Ozon: review_id=%s external_id=%s source_type=%s text_length=%s",
                review_id,
                cleaned_external_id,
                source_type,
                len(cleaned_text),
            )
            return self._build_result(False, None, f"Сетевая ошибка: {exc}", "-")

    def send_review_reply(self, review_id: int | None, external_id: str, reply_text: str) -> dict[str, Any]:
        return self._send_reply(
            review_id=review_id,
            external_id=external_id,
            source_type="review",
            reply_text=reply_text,
            path="/v1/review/comment/create",
            payload_id_key="review_id",
            text_key="text",
        )

    def send_question_reply(self, review_id: int | None, external_id: str, reply_text: str) -> dict[str, Any]:
        return self._send_reply(
            review_id=review_id,
            external_id=external_id,
            source_type="question",
            reply_text=reply_text,
            path="/v1/question/answer/create",
            payload_id_key="question_id",
            text_key="text",
        )
