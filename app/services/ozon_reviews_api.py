import json
import logging
from typing import Any

import requests

from app.config import settings


logger = logging.getLogger("app.ozon_reviews_api")


class OzonClient:
    """Simple Ozon Seller API client for reviews and questions."""

    def __init__(self) -> None:
        self.client_id = settings.OZON_CLIENT_ID
        self.api_key = settings.OZON_API_KEY
        self.base_url = "https://api-seller.ozon.ru"
        self.session = requests.Session()

        logger.info(
            "Инициализирован OzonClient. client_id_present=%s api_key_present=%s",
            bool(self.client_id),
            bool(self.api_key),
        )

    def build_headers(self) -> dict[str, str]:
        """Build standard Ozon API headers in one place."""
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def _summarize_response_body(self, response: requests.Response) -> str:
        """Return a short readable summary of the Ozon response body."""
        try:
            payload = response.json()
            compact = json.dumps(payload, ensure_ascii=False)
            return compact[:500] if compact else "-"
        except ValueError:
            text = (response.text or "").strip()
            return text[:500] if text else "-"

    def _build_result(
        self,
        ok: bool,
        status_code: int | None,
        message: str,
        response_summary: str,
        response_json: Any = None,
    ) -> dict[str, Any]:
        """Return one normalized result object for UI and logs."""
        return {
            "ok": ok,
            "status_code": status_code,
            "message": message,
            "response_summary": response_summary,
            "response_json": response_json,
        }

    def _post_json(self, path: str, payload: dict) -> dict:
        """Execute a POST request to Ozon Seller API and return parsed JSON."""
        url = f"{self.base_url}{path}"
        logger.info("Отправляем запрос в Ozon Seller API: %s", url)
        response = self.session.post(
            url,
            json=payload,
            headers=self.build_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def test_connection(self) -> dict[str, bool | str]:
        """
        Check only that credentials are loaded from .env.
        No real API request is executed here yet.
        """
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
        """
        Load reviews from Ozon Seller API.

        Note:
        - this uses a common Seller API path for reviews
        - if your Ozon account uses a different path/version, replace it here
        """
        logger.info("Запущена загрузка отзывов из Ozon Seller API.")
        data = self._post_json(
            "/v1/review/list",
            {
                "limit": 100,
                "last_id": "",
                "sort_dir": "DESC",
            },
        )
        items = data.get("reviews") or data.get("result", {}).get("reviews") or data.get("result") or []
        logger.info("Из Ozon Seller API получено отзывов: %s", len(items) if isinstance(items, list) else 0)
        return items if isinstance(items, list) else []

    def fetch_questions(self) -> list[dict]:
        """
        Placeholder for loading questions from Ozon.

        TODO:
        - add real request to Ozon Seller API for questions
        - map response to internal question structure
        """
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
        """Send one reply to Ozon and return a normalized result dict."""
        cleaned_text = (reply_text or "").strip()
        cleaned_external_id = (external_id or "").strip()

        if not self.client_id or not self.api_key:
            message = "Не настроены OZON_CLIENT_ID или OZON_API_KEY."
            logger.warning(message)
            return self._build_result(False, None, message, "-")

        if not cleaned_external_id:
            message = "Не удалось отправить: отсутствует внешний ID отзыва в Ozon."
            logger.warning(
                "%s review_id=%s external_id=%s source_type=%s",
                message,
                review_id,
                external_id,
                source_type,
            )
            return self._build_result(False, None, message, "-")

        if not cleaned_text:
            message = "Не удалось отправить: текст ответа пустой."
            logger.warning(
                "%s review_id=%s external_id=%s source_type=%s",
                message,
                review_id,
                cleaned_external_id,
                source_type,
            )
            return self._build_result(False, None, message, "-")

        payload = {
            payload_id_key: cleaned_external_id,
            text_key: cleaned_text,
        }

        if not payload or not payload.get(text_key):
            message = "Не удалось отправить: payload пустой или не содержит текст ответа."
            logger.warning(
                "%s review_id=%s external_id=%s source_type=%s payload=%s",
                message,
                review_id,
                cleaned_external_id,
                source_type,
                payload,
            )
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
                timeout=30,
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
                return self._build_result(
                    True,
                    response.status_code,
                    "Ответ отправлен",
                    response_summary,
                    response_json,
                )

            return self._build_result(
                False,
                response.status_code,
                "Не удалось отправить ответ в Ozon.",
                response_summary,
                response_json,
            )
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
        """
        Send one review reply to Ozon.

        Note:
        - path may need adjustment for your seller account version
        """
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
        """
        Send one question reply to Ozon.

        Note:
        - path may need adjustment for your seller account version
        """
        return self._send_reply(
            review_id=review_id,
            external_id=external_id,
            source_type="question",
            reply_text=reply_text,
            path="/v1/question/answer/create",
            payload_id_key="question_id",
            text_key="text",
        )
