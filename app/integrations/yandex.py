"""
Интеграция с API Яндекс Доставки.

Документация: https://yandex.com/support/delivery-profile/ru/api/other-day/
"""

import requests
from flask import current_app


class YandexDeliveryError(Exception):
    """Ошибка API Яндекс Доставки."""
    def __init__(self, message, code=None, errors=None):
        super().__init__(message)
        self.code = code
        self.errors = errors or []


class YandexDeliveryClient:
    """
    Клиент для работы с API Яндекс Доставки.
    
    Требуемые учетные данные:
    - client_id: Идентификатор клиента
    - client_secret: Секретный ключ
    """
    
    # API endpoints
    BASE_URL = "https://b2b.taxi.yandex.net/api/v1"
    
    def __init__(self, client_id=None, client_secret=None, token=None):
        """
        Инициализация клиента.
        
        Args:
            client_id: Идентификатор клиента (из ЛК)
            client_secret: Секретный ключ (из ЛК)
            token: OAuth токен (если получен заранее)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = token
        self._session = requests.Session()
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    def authenticate(self):
        """
        Получение OAuth токена.
        
        Returns:
            str: Токен доступа
        """
        url = f"{self.BASE_URL}/oauth/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials"
        }
        
        response = self._session.post(url, json=data)
        response.raise_for_status()
        
        token_data = response.json()
        self.token = token_data.get("access_token")
        self._session.headers.update({"Authorization": f"Bearer {self.token}"})
        return self.token

    def get_offers(self, from_location, to_location, weight, dimensions):
        """
        Получение доступных офферов доставки.
        
        Args:
            from_location: dict -.Location отправления {'latitude': float, 'longitude': float}
            to_location: dict - Location получения
            weight: float - Вес в кг
            dimensions: dict - {'length':, 'width':, 'height':} в см
        
        Returns:
            list: Список доступных офферов
        """
        url = f"{self.BASE_URL}/b2b/platform/offersinfo"
        
        payload = {
            "from_location": from_location,
            "to_location": to_location,
            "packages": [{
                "weight": weight * 1000,  # в граммах
                "dimensions": dimensions
            }]
        }
        
        response = self._session.post(url, json=payload)
        response.raise_for_status()
        
        return response.json().get("offers", [])

    def get_pvz_list(self, region=None, latitude=None, longitude=None, limit=50):
        """
        Получение списка ПВЗ.
        
        Args:
            region: str - Регион (город)
            latitude: float - Широта
            longitude: float - Долгота
            limit: int - Количество результатов
        
        Returns:
            list: Список ПВЗ
        """
        url = f"{self.BASE_URL}/b2b/platform/pickup-points/list"
        
        payload = {"limit": limit}
        if region:
            payload["region"] = region
        if latitude and longitude:
            payload["location"] = {"latitude": latitude, "longitude": longitude}
        
        response = self._session.post(url, json=payload)
        response.raise_for_status()
        
        return response.json().get("points", [])

    def create_request(self, offer_id, from_location, to_location, sender, recipient, packages):
        """
        Создание заявки на доставку.
        
        Args:
            offer_id: str - ID выбранного оффера
            from_location: dict - Локация отправления
            to_location: dict - Локация получения
            sender: dict - Данные отправителя
            recipient: dict - Данные получателя
            packages: list - Список мест с товарами
        
        Returns:
            dict: Данные созданной заявки
        """
        url = f"{self.BASE_URL}/b2b/platform/requests/create"
        
        payload = {
            "offer_id": offer_id,
            "from_location": from_location,
            "to_location": to_location,
            "sender": sender,
            "recipient": recipient,
            "places": packages
        }
        
        response = self._session.post(url, json=payload)
        response.raise_for_status()
        
        return response.json()

    def get_request_info(self, request_id):
        """
        Получение информации о заявке.
        
        Args:
            request_id: str - ID заявки
        
        Returns:
            dict: Информация о заявке
        """
        url = f"{self.BASE_URL}/b2b/platform/request/{request_id}/info"
        
        response = self._session.get(url)
        response.raise_for_status()
        
        return response.json()

    def confirm_request(self, request_id):
        """
        Подтверждение заявки.
        
        Args:
            request_id: str - ID заявки
        
        Returns:
            dict: Результат подтверждения
        """
        url = f"{self.BASE_URL}/b2b/platform/requests/confirm"
        
        payload = {"request_id": request_id}
        
        response = self._session.post(url, json=payload)
        response.raise_for_status()
        
        return response.json()

    def cancel_request(self, request_id):
        """
        Отмена заявки.
        
        Args:
            request_id: str - ID заявки
        
        Returns:
            dict: Результат отмены
        """
        url = f"{self.BASE_URL}/b2b/platform/requests/{request_id}/cancel"
        
        response = self._session.post(url)
        response.raise_for_status()
        
        return response.json()

    def generate_labels(self, request_id):
        """
        Генерация печатных форм (этикеток).
        
        Args:
            request_id: str - ID заявки
        
        Returns:
            dict: Ссылки на этикетки
        """
        url = f"{self.BASE_URL}/b2b/platform/requests/{request_id}/generate-labels"
        
        response = self._session.post(url)
        response.raise_for_status()
        
        return response.json()

    def get_warehouses(self):
        """
        Получение списка складов.
        
        Returns:
            list: Список складов
        """
        url = f"{self.BASE_URL}/b2b/platform/warehouses/list"
        
        response = self._session.post(url, json={})
        response.raise_for_status()
        
        return response.json().get("warehouses", [])


def get_client(delivery_service=None):
    """
    Фабрика для получения клиента Яндекс Доставки.
    
    Args:
        delivery_service: Модель DeliveryService
    
    Returns:
        YandexDeliveryClient или None
    """
    if not delivery_service or delivery_service.code != 'yandex':
        return None
    
    api_settings = delivery_service.api_settings or {}
    client_id = api_settings.get('client_id')
    client_secret = api_settings.get('client_secret')
    token = api_settings.get('token')
    
    if not client_id or not client_secret:
        return None
    
    return YandexDeliveryClient(
        client_id=client_id,
        client_secret=client_secret,
        token=token
    )