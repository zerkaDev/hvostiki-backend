# tracker/services/ucaller_service.py
from urllib.parse import urljoin

import requests
import logging
from django.conf import settings
from typing import Optional, Dict, Any
from datetime import datetime

from tracker.utils import normalize_phone

logger = logging.getLogger(__name__)


class UCallerService:
    """
    Сервис для работы с uCaller API для подтверждения номеров телефонов.
    Документация: https://ucaller.ru/doc
    """

    def __init__(self):
        self.api_key = settings.UCALLER_API_KEY
        self.service_id = settings.UCALLER_SERVICE_ID
        self.api_url = settings.UCALLER_API_URL

        if not self.api_key or not self.service_id:
            raise Exception('Не указаны credentials для ucaller')

    def get_auth_header(self) -> str:
        """Формирует заголовок авторизации для uCaller API"""
        return f"{self.api_key}.{self.service_id}"

    def send_call_code(
        self,
        phone: str,
        code: str,
    ) -> dict[str, Any]:
        """
        Отправляет звонок с кодом подтверждения.

        Args:
            phone: Номер телефона (в любом формате)
            code: 4-6 значный код подтверждения
            client: Имя клиента (опционально)
            unique: Уникальный идентификатор запроса (опционально)
            voice: Использовать голосовой звонок или SMS

        Returns:
            Dict с ответом от API или мок-ответом
        """
        # Нормализуем телефон
        normalized_phone = int(normalize_phone(phone))

        # Подготавливаем данные
        data = {
            "phone": normalized_phone,
            "code": code,
        }

        url = urljoin(self.api_url, 'initCall')

        response = requests.post(
            url=url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.get_auth_header()}"
            },
            json=data,
            timeout=10  # 10 секунд таймаут
        )

        response.raise_for_status()

        result = response.json()

        logger.info(
            f"Call sent to {phone}. "
            f"result: {result}"
        )

        return result
