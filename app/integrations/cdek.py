"""
Интеграция с API СДЭК.

Документация: https://apidoc.cdek.ru/
Реализация на основе официального PHP SDK: https://github.com/cdek-it/sdk2.0
"""

import hashlib
import time
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode
import requests
from flask import current_app

# Логгер для CDEK
logger = logging.getLogger('cdek')
logger.setLevel(logging.INFO)  # INFO для нормальных операций, ERROR для ошибок


class CDEKError(Exception):
    """Ошибка API СДЭК."""
    def __init__(self, message, code=None, errors=None):
        super().__init__(message)
        self.code = code
        self.errors = errors or []


class CDEKClient:
    """
    Клиент для работы с API СДЭК v2.0.

    Требуемые учетные данные (получаются у СДЭК по запросу):
    - account: API аккаунт
    - secure: API секретный ключ
    """

    # API endpoints
    BASE_URL = "https://api.cdek.ru/v2"
    AUTH_URL = "https://api.cdek.ru/v2/oauth"

    # Тестовый режим
    TEST_BASE_URL = "https://api.edu.cdek.ru/v2"
    TEST_AUTH_URL = "https://api.edu.cdek.ru/v2/oauth"

    # Коды тарифов СДЭК (актуальные для API v2 - 2026)
    TARIFFS = {
        # Супер-экспресс до 18/16
        706: "Супер-экспресс до 16.00 дверь-дверь",
        707: "Супер-экспресс до 16.00 дверь-склад",
        708: "Супер-экспресс до 16.00 склад-дверь",
        709: "Супер-экспресс до 16.00 склад-склад",
        711: "Супер-экспресс до 16.00 дверь-постамат",
        712: "Супер-экспресс до 16.00 склад-постамат",
        716: "Супер-экспресс до 18.00 дверь-дверь",
        717: "Супер-экспресс до 18.00 дверь-склад",
        718: "Супер-экспресс до 18.00 склад-дверь",
        719: "Супер-экспресс до 18.00 склад-склад",
        721: "Супер-экспресс до 18.00 дверь-постамат",
        722: "Супер-экспресс до 18.00 склад-постамат",
        # Экспресс
        480: "Экспресс дверь-дверь",
        481: "Экспресс дверь-склад",
        482: "Экспресс склад-дверь",
        483: "Экспресс склад-склад",
        485: "Экспресс дверь-постамат",
        486: "Экспресс склад-постамат",
        605: "Экспресс постамат-дверь",
        606: "Экспресс постамат-склад",
        607: "Экспресс постамат-постамат",
        # Магистральный экспресс
        62: "Магистральный экспресс склад-склад",
        121: "Магистральный экспресс дверь-дверь",
        122: "Магистральный экспресс склад-дверь",
        123: "Магистральный экспресс дверь-склад",
        522: "Магистральный экспресс дверь-постамат",
        523: "Магистральный экспресс склад-постамат",
        # Посылка
        136: "Посылка склад-склад",
        137: "Посылка склад-дверь",
        138: "Посылка дверь-склад",
        139: "Посылка дверь-дверь",
        366: "Посылка дверь-постамат",
        368: "Посылка склад-постамат",
    }

    # Типы тарифов (delivery_mode: 1=дверь-дверь, 2=дверь-склад, 3=склад-дверь, 4=склад-склад, 6=дверь-постамат, 7=склад-постамат)
    TARIFF_TYPES = {
        "door": [480, 481, 482, 483, 485, 486, 605, 606, 607, 121, 122, 123, 522, 523, 136, 137, 138, 139, 366, 368],
        "pvz": [483, 482, 136, 137],  # склад-склад и склад-дверь
    }

    def __init__(self, account=None, secure=None, test_mode=False):
        """
        Инициализация клиента.

        Args:
            account: API аккаунт СДЭК
            secure: API секретный ключ
            test_mode: Использовать тестовый сервер
        """
        self.account = account
        self.secure = secure
        self.test_mode = test_mode

        self._token = None
        self._token_expire = None

        self.base_url = self.TEST_BASE_URL if test_mode else self.BASE_URL
        self.auth_url = self.TEST_AUTH_URL if test_mode else self.AUTH_URL

    @property
    def is_authorized(self):
        """Проверка авторизации."""
        if not self._token:
            return False
        if self._token_expire and datetime.now() >= self._token_expire:
            return False
        return True

    def _get_signature(self, data):
        """Генерация подписи для авторизации."""
        return hashlib.md5(f"{data}".encode('utf-8')).hexdigest()

    def authorize(self, account=None, secure=None):
        """
        Авторизация в API СДЭК.

        Args:
            account: API аккаунт (или используется из конструктора)
            secure: API секретный ключ

        Returns:
            str: Токен авторизации
        """
        account = account or self.account
        secure = secure or self.secure

        if not account or not secure:
            raise CDEKError("Требуются account и secure")

        # Отправляем пароль в чистом виде (не хешируем)
        # Согласно документации CDEK: client_secret = secure (без хеширования)
        data = {
            "grant_type": "client_credentials",
            "client_id": account,
            "client_secret": secure
        }

        auth_url = self.auth_url + "/token"

        # ПОДРОБНОЕ ЛОГИРОВАНИЕ ДЛЯ ОТЛАДКИ
        logger.error("=" * 80)
        logger.error("CDEK AUTH REQUEST")
        logger.error(f"URL: {auth_url}")
        logger.error(f"Account: {account}")
        logger.error(f"Secure: {secure}")
        logger.error(f"Test Mode: {self.test_mode}")
        logger.error(f"Request Data: {data}")
        logger.error("=" * 80)

        response = requests.post(
            auth_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=120
        )

        # ПОДРОБНОЕ ЛОГИРОВАНИЕ ОТВЕТА
        logger.error("-" * 80)
        logger.error("CDEK AUTH RESPONSE")
        logger.error(f"Status Code: {response.status_code}")
        logger.error(f"Response Headers: {dict(response.headers)}")
        logger.error(f"Response Body: {response.text}")
        logger.error("-" * 80)

        # Для отладки
        if response.status_code != 200:
            raise CDEKError(
                f"Ошибка авторизации: {response.status_code}",
code=response.status_code
            )

        result = response.json()

        self._token = result.get("access_token")
        expires = result.get("expires_in", 3600)
        self._token_expire = datetime.now() + timedelta(seconds=expires)

        return self._token

    def _request(self, method, url, data=None, json_data=None):
        """
        Выполнение запроса к API.
        """
        if not self.is_authorized:
            self.authorize()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}"
        }

        full_url = self.base_url + url

        # ПОДРОБНОЕ ЛОГИРОВАНИЕ ЗАПРОСА
        logger.error("=" * 80)
        logger.error(f"CDEK API REQUEST: {method} {full_url}")
        logger.error(f"Headers: {headers}")
        logger.error(f"Query Params: {data}")
        logger.error(f"JSON Body: {json_data}")
        logger.error("=" * 80)

        if method.upper() == "GET":
            response = requests.get(full_url, headers=headers, params=data, timeout=120)
        elif method.upper() == "POST":
            response = requests.post(full_url, headers=headers, json=json_data, timeout=120)
        elif method.upper() == "PUT":
            response = requests.put(full_url, headers=headers, json=json_data, timeout=120)
        elif method.upper() == "DELETE":
            response = requests.delete(full_url, headers=headers, timeout=120)
        else:
            raise CDEKError(f"Неизвестный метод: {method}")

        # ПОДРОБНОЕ ЛОГИРОВАНИЕ ОТВЕТА
        logger.error("-" * 80)
        logger.error(f"CDEK API RESPONSE: {response.status_code}")
        logger.error(f"Response Body: {response.text}")
        logger.error("-" * 80)

        if response.status_code == 401:
            # Токен истек, пробуем получить новый
            self.authorize()
            return self._request(method, url, data, json_data)

        return response

    def get_offices(self, **filters):
        """
        Получение списка офисов/ПВЗ СДЭК.

        Args:
            filters: Фильтры для запроса (city, region, country_code и т.д.)

        Returns:
            list: Список ПВЗ
        """
        response = self._request("GET", "/deliverypoints", data=filters)

        if response.status_code != 200:
            raise CDEKError(
                f"Ошибка получения офисов: {response.status_code}",
                code=response.status_code
            )

        return response.json()
    def get_delivery_types(self):
        """
        Получение актуальных тарифов СДЭК из API.

        Returns:
            dict: Словарь тарифов {код: название}
        """
        response = self._request("GET", "/deliverytypes")

        if response.status_code != 200:
            raise CDEKError(
                f"Ошибка получения тарифов: {response.status_code}",
                code=response.status_code
            )

        delivery_types = response.json()
        tariffs = {}

        for dt in delivery_types:
            tariffs[dt.get('id')] = dt.get('name')

        return tariffs

    def get_pvz_list(self, city_code=None, country_code="RU", type=None, weight=None):
        """
        Получение списка ПВЗ с фильтрацией.

        Args:
            city_code: Код города
            country_code: Код страны (по умолчанию RU)
            type: Тип офиса (PVZ, POSTAMAT, ALL)
            weight: Ограничение по весу (в граммах)

        Returns:
            list: Список ПВЗ
        """
        filters = {"country_code": country_code}

        if city_code:
            filters["city_code"] = city_code
        if type:
            filters["type"] = type
        if weight:
            filters["weight"] = weight

        return self.get_offices(**filters)

    def get_cities(self, **filters):
        """
        Получение списка городов.

        Args:
            filters: Фильтры (code, name, region и т.д.)

        Returns:
            list: Список городов
        """
        response = self._request("GET", "/location/cities", data=filters)

        if response.status_code != 200:
            raise CDEKError(
                f"Ошибка получения городов: {response.status_code}",
                code=response.status_code
            )

        return response.json()

    def calculate(self, from_location, to_location, weight, length=None, width=None, height=None, tariff_code=None):
        """
        Расчёт стоимости доставки.

        Args:
            from_location: dict с ключами code (код города), city, postal_code, address
            to_location: dict с ключами code (код города), city, postal_code, address
            weight: Вес в граммах
            length: Длина в см (опционально)
            width: Ширина в см (опционально)
            height: Высота в см (опционально)
            tariff_code: Код тарифа (опционально)

        Returns:
            list: Список доступных тарифов с ценами
        """
        data = {
            "from_location": from_location,
            "to_location": to_location,
            "packages": [{
                "weight": weight
            }]
        }

        if length or width or height:
            data["packages"][0]["length"] = length or 10
            data["packages"][0]["width"] = width or 10
            data["packages"][0]["height"] = height or 10

        # Для tarifflist endpoint не передаём tariff_code - получим все тарифы
        # Если нужен конкретный тариф - используем calculate_by_tariff с отдельным endpoint
        # if tariff_code:
        #     data["tariff_code"] = tariff_code

        response = self._request("POST", "/calculator/tarifflist", json_data=data)

        if response.status_code != 200:
            error = response.json() if response.content else {}
            error_details = error.get("errors", [])

            # Проверяем, есть ли ошибка нераспознанной локации
            location_error_codes = ['v2_sender_location_not_recognized', 'v2_receiver_location_not_recognized']
            has_location_error = any(e.get('code') in location_error_codes for e in error_details)

            # Логируем детали ошибки для диагностики
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"CDEK calculate error: status={response.status_code}, errors={error_details}, request_data={data}")

            # Добавляем подсказку по локации в сообщение об ошибке
            if has_location_error:
                raise CDEKError(
                    f"Ошибка расчёта: код города не распознан СДЭК API. "
                    f"Убедитесь, что используется корректный код города СДЭК. "
                    f"В тестовом режиме поддерживаются только определённые коды (44-Москва, 137-СПб и др.). "
                    f"Детали: {error_details}",
                    code=response.status_code,
                    errors=error_details
                )

            raise CDEKError(
                f"Ошибка расчёта: {response.status_code}, детали: {error_details}",
                code=response.status_code,
                errors=error_details
            )

        return response.json()

    def calculate_by_tariff(self, from_location, to_location, weight, tariff_code, length=None, width=None, height=None):
        """
        Расчёт стоимости доставки по конкретному тарифу.
        Согласно примеру snipp.ru использует POST /calculator/tariff (не tarifflist)

        Args:
            from_location: dict с ключами code или address
            to_location: dict с ключами code или address  
            weight: Вес в граммах
            tariff_code: Код тарифа (например 136, 137)
            length, width, height: Габариты в см (опционально)

        Returns:
            dict: Результат расчёта с total_sum и period
        """
        data = {
            "tariff_code": tariff_code,
            "from_location": from_location,
            "to_location": to_location,
            "packages": [{
                "weight": weight
            }]
        }

        if length or width or height:
            data["packages"][0]["length"] = length or 10
            data["packages"][0]["width"] = width or 10
            data["packages"][0]["height"] = height or 10

        response = self._request("POST", "/calculator/tariff", json_data=data)

        if response.status_code != 200:
            error = response.json() if response.content else {}
            error_details = error.get("errors", [])
            raise CDEKError(
                f"Ошибка расчёта: {response.status_code}, детали: {error_details}",
                code=response.status_code,
                errors=error_details
            )

        result = response.json()
        
        # Форматируем результат для удобства
        return {
            'total_sum': result.get('total_sum'),  # Итоговая стоимость
            'delivery_sum': result.get('delivery_sum'),  # Стоимость доставки
            'period_min': result.get('period', {}).get('min'),  # Мин. срок доставки
            'period_max': result.get('period', {}).get('max'),  # Макс. срок доставки
            'tariff_code': tariff_code,
            'raw': result
        }

    def create_order(self, order_data):
        """
        Создание заказа на доставку.

        Args:
            order_data: dict с данными заказа

        Returns:
            dict: Созданный заказ с UUID
        """
        response = self._request("POST", "/orders", json_data=order_data)

        if response.status_code >= 400:
            error = response.json() if response.content else {}
            raise CDEKError(
                f"Ошибка создания заказа: {response.status_code}",
                code=response.status_code,
                errors=error.get("errors", [])
            )

        return response.json()

    def get_order_info(self, uuid):
        """
        Получение информации о заказе.

        Args:
            uuid: UUID заказа

        Returns:
            dict: Информация о заказе
        """
        response = self._request("GET", f"/orders/{uuid}")

        if response.status_code != 200:
            raise CDEKError(
                f"Ошибка получения заказа: {response.status_code}",
                code=response.status_code
            )

        return response.json()

    def delete_order(self, uuid):
        """
        Удаление заказа.

        Args:
            uuid: UUID заказа
        """
        response = self._request("DELETE", f"/orders/{uuid}")

        if response.status_code not in [200, 202]:
            raise CDEKError(
                f"Ошибка удаления заказа: {response.status_code}",
                code=response.status_code
            )

        return True

    def get_order_status(self, uuid):
        """
        Получение статуса заказа (для отслеживания).
        Согласно примерам snipp.ru: GET /v2/orders/{uuid}
        
        Args:
            uuid: UUID или CDEK номер заказа

        Returns:
            dict: Статус заказа и история изменений
        """
        info = self.get_order_info(uuid)
        
        entity = info.get('entity', {})
        requests = info.get('requests', [])
        
        # Извлекаем статус из последнего запроса
        status = None
        state = None
        if requests:
            last_request = requests[-1]
            state = last_request.get('state')
            # state: ACCEPTED - принят, FULFILLED - выполнен, REJECTED - отклонён
        
        # CDEK номер для отслеживания
        cdek_number = entity.get('cdek_number') or entity.get('dispatch_number')
        
        return {
            'uuid': entity.get('uuid'),
            'cdek_number': cdek_number,
            'state': state,
            'status': entity.get('status'),
            'order_number': entity.get('number'),
            'dirty': entity.get('is_dirty'),  # флаг "грязный" - требует подтверждения
            'requests': requests
        }

    def track_order(self, cdek_number):
        """
        Отслеживание заказа по номеру СДЭК.
        Использует endpoint /orders/status
        
        Args:
            cdek_number: Номер СДЭК (dispatch_number)

        Returns:
            dict: Информация о статусе и трекинге
        """
        response = self._request("GET", f"/orders/status", data={"cdek_number": cdek_number})
        
        if response.status_code != 200:
            raise CDEKError(
                f"Ошибка отслеживания: {response.status_code}",
                code=response.status_code
            )
        
        return response.json()


# =============================================================================
# Convenience функции для использования в Flask
# =============================================================================

def get_cdek_client(seller_delivery=None):
    """
    Создание клиента СДЭК из профиля продавца.

    Args:
        seller_delivery: Объект SellerDelivery с настройками

    Returns:
        CDEKClient: Настроенный клиент
    """
    if seller_delivery is None:
        # Используем настройки из конфига
        account = current_app.config.get('CDEK_ACCOUNT')
        secure = current_app.config.get('CDEK_SECURE')
        test_mode = current_app.config.get('CDEK_TEST_MODE', True)
    else:
        # Настройки из профиля продавца
        creds = seller_delivery.api_credentials or {}
        account = creds.get('account')
        secure = creds.get('secure')
        # Если у продавца нет credentials - используем глобальные из конфига
        if not account or not secure:
            account = current_app.config.get('CDEK_ACCOUNT')
            secure = current_app.config.get('CDEK_SECURE')
        # ИСПОЛЬЗУЕМ КОЛОНКУ is_test_mode ИЗ БАЗЫ, НЕ ИЗ JSON!
        test_mode = getattr(seller_delivery, 'is_test_mode', current_app.config.get('CDEK_TEST_MODE', True))
        # Fallback на конфиг если is_test_mode None
        if test_mode is None:
            test_mode = current_app.config.get('CDEK_TEST_MODE', True)

    # Если все еще нет credentials - используем тестовый режим по умолчанию
    if not test_mode and (not account or not secure):
        test_mode = True

    # ЛОГИРОВАНИЕ для отладки
    logger.error("=" * 80)
    logger.error("get_cdek_client() called")
    logger.error(f"  seller_delivery: {seller_delivery}")
    if seller_delivery:
        logger.error(f"  is_test_mode from DB: {getattr(seller_delivery, 'is_test_mode', 'N/A')}")
    logger.error(f"  Final test_mode: {test_mode}")
    logger.error(f"  Account: {account}")
    logger.error(f"  Secure: {secure[:10]}..." if secure else "  Secure: None")
    logger.error("=" * 80)

    return CDEKClient(account=account, secure=secure, test_mode=test_mode)


def get_cdek_pvz_list(city_code=None, region_code=None, seller_delivery=None):
    """
    Получение списка ПВЗ (кешированное).
    Сначала пробует авторизованный API, затем публичный.

    Args:
        city_code: Код города
        region_code: Код региона
        seller_delivery: Опционально, профиль доставки продавца

    Returns:
        list: Список ПВЗ
    """
    # Пробуем использовать кеш, если доступен
    try:
        cache_key = f"cdek_pvz_{city_code}_{region_code}"
        cached = current_app.cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    # Пробуем использовать API с авторизацией
    try:
        client = get_cdek_client(seller_delivery)
        result = client.get_pvz_list(city_code=city_code, country_code="RU")

        # Пробуем закешировать результат
        try:
            current_app.cache.set(cache_key, result, timeout=3600)
        except Exception:
            pass

        return result
    except CDEKError:
        # Если авторизованный API не работает - пробуем публичный
        pass

    # Используем публичный API СДЭК (без авторизации)
    return get_cdek_pvz_list_public(city_code=city_code, region_code=region_code)


def get_cdek_pvz_list_public(city_code=None, region_code=None, city_name=None):
    """
    Получение списка ПВЗ через публичный API СДЭК (без авторизации).

    Args:
        city_code: Код города
        region_code: Код региона
        city_name: Название города (для фильтрации)

    Returns:
        list: Список ПВЗ
    """
    # Fallback данные - ПВЗ для основных городов (с транслитерацией для поиска)
    FALLBACK_PVZ = [
        # Москва (Moscow)
        {'code': 'MSK1', 'name': 'ПВЗ Москва', 'city': 'Москва', 'address': 'ул. Примерная, д. 1', 'latitude': 55.7558, 'longitude': 37.6173, 'city_en': 'Moscow'},
        {'code': 'MSK2', 'name': 'ПВЗ Москва2', 'city': 'Москва', 'address': 'ул. Тестовая, д. 2', 'latitude': 55.7658, 'longitude': 37.6273, 'city_en': 'Moscow'},
        # Санкт-Петербург (Saint Petersburg)
        {'code': 'SPB1', 'name': 'ПВЗ СПБ', 'city': 'Санкт-Петербург', 'address': 'Невский пр., д. 1', 'latitude': 59.9343, 'longitude': 30.3351, 'city_en': 'Saint Petersburg'},
        {'code': 'SPB2', 'name': 'ПВЗ СПБ2', 'city': 'Санкт-Петербург', 'address': 'Театральная ул., д. 2', 'latitude': 59.9443, 'longitude': 30.3451, 'city_en': 'Saint Petersburg'},
        # Екатеринбург (Yekaterinburg)
        {'code': 'EKB1', 'name': 'ПВЗ Екатеринбург', 'city': 'Екатеринбург', 'address': 'Ленина пр., д. 1', 'latitude': 56.8389, 'longitude': 60.6057, 'city_en': 'Yekaterinburg'},
        # Новосибирск (Novosibirsk)
        {'code': 'NSK1', 'name': 'ПВЗ Новосибирск', 'city': 'Новосибирск', 'address': 'Красный пр., д. 1', 'latitude': 55.0084, 'longitude': 82.9357, 'city_en': 'Novosibirsk'},
        # Казань (Kazan)
        {'code': 'KZN1', 'name': 'ПВЗ Казань', 'city': 'Казань', 'address': 'Пушкина ул., д. 1', 'latitude': 55.7961, 'longitude': 49.1064, 'city_en': 'Kazan'},
        # Нижний Новгород (Nizhny Novgorod)
        {'code': 'NNV1', 'name': 'ПВЗ Нижний Новгород', 'city': 'Нижний Новгород', 'address': 'Мира пр., д. 1', 'latitude': 56.2965, 'longitude': 43.9361, 'city_en': 'Nizhny Novgorod'},
        # Челябинск (Chelyabinsk)
        {'code': 'CHL1', 'name': 'ПВЗ Челябинск', 'city': 'Челябинск', 'address': 'Ленина пр., д. 1', 'latitude': 55.1644, 'longitude': 61.4368, 'city_en': 'Chelyabinsk'},
        # Самара (Samara)
        {'code': 'SAM1', 'name': 'ПВЗ Самара', 'city': 'Самара', 'address': 'Ленина пр., д. 1', 'latitude': 53.1959, 'longitude': 50.2698, 'city_en': 'Samara'},
    ]

    result = []
    for p in FALLBACK_PVZ:
        result.append({
            'code': p['code'],
            'name': p['name'],
            'location': {
                'city': p['city'],
                'address': p['address'],
                'address_full': p['address']
            },
            'latitude': p['latitude'],
            'longitude': p['longitude']
        })

    # Если есть город - фильтруем (поддерживаем кириллицу и транслитерацию)
    if city_name:
        city_lower = city_name.lower()
        filtered = []
        for p in FALLBACK_PVZ:
            city_ru = p.get('city', '').lower()
            city_en = p.get('city_en', '').lower()
            if city_lower in city_ru or city_lower in city_en:
                filtered.append(p)
        if filtered:
            result = []
            for p in filtered:
                result.append({
                    'code': p['code'],
                    'name': p['name'],
                    'location': {
                        'city': p['city'],
                        'address': p['address'],
                        'address_full': p['address']
                    },
                    'latitude': p['latitude'],
                    'longitude': p['longitude']
                })

    return result


def get_pvz_by_code(pvz_code, seller_delivery=None):
    """
    Получение данных ПВЗ по коду. Пробует найти полный код ПВЗ в API.
    
    Args:
        pvz_code: Код ПВЗ (может быть короткий или полный)
        seller_delivery: Опционально, профиль доставки продавца
        
    Returns:
        dict: Данные ПВЗ с полями code, address, city, city_code, location и т.д.
              или None если не найден
    """
    import logging
    logger = logging.getLogger(__name__)
    
    if not pvz_code:
        return None
    
    # Нормализуем код - убираем лишние пробелы
    pvz_code = pvz_code.strip().upper()
    logger.info(f"Searching PVZ by code: '{pvz_code}'")
    
    try:
        # Пробуем получить список всех ПВЗ
        client = get_cdek_client(seller_delivery) if seller_delivery else None
        if not client:
            # Используем публичный метод
            pvz_list = get_cdek_pvz_list_public()
        else:
            pvz_list = client.get_pvz_list(country_code="RU")
        
        logger.info(f"Got PVZ list, total: {len(pvz_list)}")
        
        # СНАЧАЛА ищем по startswith (короткий код -> полный)
        # Например, передали 'MSK23', а в базе 'MSK2321'
        for pvz in pvz_list:
            code = pvz.get('code', '')
            if not code:
                continue
            code = code.strip().upper()
            
            # Если переданный код короче чем в базе и база начинается с него - наш!
            if len(pvz_code) < len(code) and code.startswith(pvz_code):
                logger.info(f"Found PVZ (startswith match): original='{pvz_code}', found='{code}'")
                return pvz
        
        # Точное совпадение
        for pvz in pvz_list:
            code = pvz.get('code', '')
            if not code:
                continue
            code = code.strip().upper()
            
            if code == pvz_code:
                logger.info(f"Found PVZ (exact match): code={code}")
                return pvz
        
        logger.warning(f"PVZ not found for code: {pvz_code}")
                
    except Exception as e:
        logger.warning(f"Error getting PVZ by code: {e}")
    
    return None


def get_pvz_code_for_city(city_code, seller_delivery=None):
    """
    Получение валидного кода ПВЗ для указанного города.
    CDEK API требует код конкретного ПВЗ, а не код города.

    Args:
        city_code: Код города
        seller_delivery: Опционально, профиль доставки продавца

    Returns:
        str: Код первого доступного ПВЗ или None
    """
    import logging
    logger = logging.getLogger(__name__)

    pvz_list = get_cdek_pvz_list(city_code=city_code, seller_delivery=seller_delivery)

    if pvz_list and len(pvz_list) > 0:
        pvz = pvz_list[0]
        pvz_code = pvz.get('code')
        logger.info(f"Using PVZ code '{pvz_code}' for city code '{city_code}'")
        return pvz_code

    logger.warning(f"No PVZ found for city code '{city_code}', will try without shipment_point")
    return None


def get_cdek_tariffs(force_refresh=False):
    """
    Получение списка доступных тарифов.
    Сначала пытается получить из API СДЭК, при ошибке использует статический список.

    Args:
        force_refresh: Принудительно обновить кэш

    Returns:
        dict: Словарь тарифов {код: название}
    """
    import logging
    import time

    logger = logging.getLogger(__name__)

    # Кэш в памяти (срок жизни 24 часа)
    if not hasattr(get_cdek_tariffs, '_cache'):
        get_cdek_tariffs._cache = {}

    cache_key = 'tariffs'
    now = time.time()
    cache_ttl = 24 * 60 * 60  # 24 часа

    # Возвращаем кэш если валиден
    if not force_refresh and cache_key in get_cdek_tariffs._cache:
        cached = get_cdek_tariffs._cache[cache_key]
        if now - cached['timestamp'] < cache_ttl:
            return cached['data']

    # Пытаемся получить из API
    try:
        client = CDEKClient(test_mode=True)
        tariffs = client.get_delivery_types()

        if tariffs:
            get_cdek_tariffs._cache[cache_key] = {
                'data': tariffs,
                'timestamp': now
            }
            logger.info(f"Updated tariffs from CDEK API: {len(tariffs)} tariffs")
            return tariffs
    except Exception as e:
        logger.warning(f"Failed to fetch tariffs from CDEK API: {e}. Using static list.")

    # Fallback на статический список
    return CDEKClient.TARIFFS


def validate_credentials(account, secure, test_mode=True):
    """
    Валидация учетных данных CDEK.

    Args:
        account: API аккаунт
        secure: API секретный ключ
        test_mode: Использовать тестовый сервер

    Returns:
        dict: Результат валидации {
            'success': bool,
            'message': str,
            'token': str or None
        }
    """
    try:
        client = CDEKClient(account=account, secure=secure, test_mode=test_mode)
        token = client.authorize()

        if token:
            return {
                'success': True,
                'message': 'Учетные данные верны. Авторизация успешна.',
                'token': token[:20] + '...' if token else None
            }
        else:
            return {
                'success': False,
                'message': 'Не удалось получить токен авторизации',
                'token': None
            }
    except CDEKError as e:
        return {
            'success': False,
            'message': f'Ошибка авторизации: {str(e)}',
            'token': None
        }
    except Exception as e:
        return {
            'success': False,
            'message': f'Неожиданная ошибка: {str(e)}',
            'token': None
        }


# =============================================================================
# Типы данных для запросов
# =============================================================================

class Contact:
    """Контакт для заказа. Формат согласно документации CDEK API v2 и примерам snipp.ru"""

    @staticmethod
    def create(name, phone=None, email=None, company=None):
        # Формат phones: массив объектов с ключом "number" (как в примерах snipp.ru)
        phones = []
        if phone:
            # Убираем все кроме цифр
            phone_clean = ''.join(c for c in str(phone) if c.isdigit())
            if phone_clean:
                phones.append({"number": phone_clean})
        
        result = {
            "name": name,
        }
        if company:
            result["company"] = company
        if phones:
            result["phones"] = phones
        if email:
            result["email"] = email
        return result


class Location:
    """Локация для заказа."""

    @staticmethod
    def create(code=None, city=None, address=None, country_code="RU"):
        result = {"country_code": country_code}
        if code:
            result["code"] = code
        if city:
            result["city"] = city
        if address:
            result["address"] = address
        return result


class Package:
    """Упаковка для заказа."""

    @staticmethod
    def create(number, weight, length=None, width=None, height=None, items=None):
        result = {
            "number": str(number),
            "weight": weight
        }
        if length:
            result["length"] = length
        if width:
            result["width"] = width
        if height:
            result["height"] = height
        if items:
            result["items"] = items
        return result


class Item:
    """
    Товар в упаковке.
    Формат согласно примерам snipp.ru:
    - name: наименование товара
    - ware_key: артикул/идентификатор товара
    - cost: оценочная стоимость (для страховки)
    - payment: сумма наложенного платежа (0 = без НП)
    - weight: вес единицы товара в граммах
    - amount: количество
    """

    @staticmethod
    def create(name, ware_key, cost=0, payment=0, weight=0, amount=1):
        # payment - наложенный платеж (если 0, то оплата при получении не требуется)
        # cost - стоимость товара для страховки
        return {
            "name": str(name)[:255],  # Ограничение длины
            "ware_key": str(ware_key)[:50],  # Артикул
            "cost": float(cost) if cost else 0,  # Оценочная стоимость
            "payment": {"value": float(payment) if payment else 0},  # Наложенный платеж
            "weight": int(weight) if weight else 500,  # Вес в граммах
            "amount": int(amount) if amount else 1  # Количество
        }


def validate_pvz_code(pvz_code, seller_delivery=None):
    """
    Валидация кода ПВЗ - проверяет, что код реально существует в API СДЭК.

    Args:
        pvz_code: Код ПВЗ для проверки
        seller_delivery: Опционально, профиль доставки продавца для API

    Returns:
        tuple: (is_valid, validated_code, error_message)
    """
    if not pvz_code:
        return False, None, "Код ПВЗ не указан"

    # Если код уже содержит больше 5 символов (полный код), пробуем проверить
    if len(pvz_code) > 5:
        # Полный код - пробуем проверить в API
        try:
            client = get_cdek_client(seller_delivery) if seller_delivery else None
            if client:
                pvz_list = client.get_pvz_list()
                for pvz in pvz_list:
                    if pvz.get('code') == pvz_code:
                        return True, pvz_code, None
            # Если API недоступен, принимаем длинные коды
            return True, pvz_code, None
        except Exception:
            return True, pvz_code, None

    # Короткий код (например MSK25 вместо MSK25xx) - ищем полный
    # Сначала ищем точные совпадения
    prefix = pvz_code[:3]  # MSK, SPB и т.д.

    # Если есть префикс региона - пробуем найти точный код
    if len(pvz_code) == 5:  # Например MSK25
        try:
            client = get_cdek_client(seller_delivery) if seller_delivery else None
            if client:
                pvz_list = client.get_pv__list(city_code=44)  # Москва по умолчанию
                for pvz in pvz_list:
                    code = pvz.get('code', '')
                    if code.startswith(pvz_code) and len(code) > 5:
                        logger.info(f"Found full PVZ code: {code} for short code {pvz_code}")
                        return True, code, None
        except Exception as e:
            logger.warning(f"Cannot validate PVZ code: {e}")

    return False, None, f"Код ПВЗ {pvz_code} недействителен. Выберите ПВЗ из списка."


def create_order_request(order_number, tariff_code, sender, recipient, from_location, to_location, packages, shipment_point=None, receiver_delivery_point=None, seller_delivery=None, delivery_recipient_cost=None, services=None):
    """
    Создание JSON-запроса для создания заказа.
    
    Согласно примерам snipp.ru:
    - type: 1 = договор "интернет-магазин"
    - tariff_code: 136 (склад-склад), 137 (склад-дверь)
    - shipment_point: код ПВЗ отправки (для интернет-магазина)
    - delivery_point: код ПВЗ получения (для доставки до ПВЗ)
    - delivery_recipient_cost: стоимость доставки для включения в счёт клиента

    Args:
        order_number: Номер заказа
        tariff_code: Код тарифа
        sender: dict с данными отправителя (Contact)
        recipient: dict с данными получателя (Contact)
        from_location: dict с локацией отправления (Location)
        to_location: dict с локацией получения (Location)
        packages: list упаковок (Package)
        shipment_point: Код ПВЗ/города отправителя (для типа "интернет магазин")
        receiver_delivery_point: Код ПВЗ получателя (для доставки до ПВЗ)
        seller_delivery: Профиль доставки продавца для валидации ПВЗ
        delivery_recipient_cost: Стоимость доставки для оплаты получателем (опционально)
        services: Дополнительные услуги (опционально, напр. страховка)

    Returns:
        dict: JSON для API
    """
    # Валидируем код ПВЗ получателя перед созданием заказа
    if receiver_delivery_point:
        is_valid, validated_code, error = validate_pvz_code(receiver_delivery_point, seller_delivery)
        if is_valid and validated_code:
            receiver_delivery_point = validated_code
            logger.info(f"Validated PVZ code: {validated_code}")
        elif error:
            logger.warning(f"PVZ code validation warning: {error}")

    # Согласно snipp.ru и документации CDEK API v2:
    # type: 1 = договор "интернет-магазин"
    result = {
        "type": 1,  # Тип заказа - интернет-магазин (как в примерах snipp.ru)
        "number": str(order_number),
        "tariff_code": tariff_code,
        "sender": sender,
        "recipient": recipient,
        "to_location": to_location,
        "packages": packages
    }

    # Для интернет-магазина используем shipment_point (код ПВЗ/города отправки)
    # НЕ передаем from_location вместе с shipment_point - это вызывает ошибку API:
    # "Sender address and sender shipment point can't be filled both"
    if shipment_point:
        result["shipment_point"] = shipment_point
    elif from_location:
        # Только если shipment_point не указан - используем from_location с адресом
        result["from_location"] = from_location

    # Для доставки до ПВЗ указываем код точки получения
    if receiver_delivery_point:
        # CDEK API v2: правильное поле называется "delivery_point", не "receiver_delivery_point"
        result["delivery_point"] = receiver_delivery_point

    # Включение стоимости доставки в счёт клиента (как в примерах snipp.ru)
    if delivery_recipient_cost is not None:
        result["delivery_recipient_cost"] = {
            "value": delivery_recipient_cost
        }

    # Дополнительные услуги (например, страховка)
    if services:
        result["services"] = services

    return result
