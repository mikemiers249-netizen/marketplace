"""Robokassa protocol for release preparation; not connected to live jobs/routes.

Sources: official /ru/refund-api and /ru/xml-interfaces. Test checkout cannot
use OpStateExt. An uncertain refund creation must never be retried blindly.
"""
import base64
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID
import xml.etree.ElementTree as ET

from app.utils.tariffs_v2 import TariffError, money

OPERATION_URL = 'https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt'
REFUND_URL = 'https://services.robokassa.ru/RefundService/Refund/Create'
REFUND_STATE_URL = 'https://services.robokassa.ru/RefundService/Refund/GetState'
MAX_RESPONSE = 1024 * 1024


class ProviderError(TariffError):
    pass


class RefundOutcomeUnknown(ProviderError):
    """A create request may have reached Robokassa; manual reconciliation required."""


class RefundRejected(ProviderError):
    """Provider explicitly rejected creation; no successful request id exists."""


@dataclass(frozen=True)
class Credentials:
    merchant: str
    password2: str = field(repr=False)
    password3: str = field(repr=False)
    algorithm: str = 'sha256'
    test_mode: bool = True


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(method, url, body=None):
    """Fixed HTTPS endpoints, no redirects/retries, bounded read and timeout."""
    base = url.split('?', 1)[0]
    if (method, base) not in {('GET', OPERATION_URL), ('GET', REFUND_STATE_URL), ('POST', REFUND_URL)}:
        raise ProviderError('Недопустимый адрес API платежной системы.')
    request = Request(url, data=body, method=method,
        headers={'Content-Type': 'application/jwt', 'Accept': 'application/json, application/xml'})
    with build_opener(NoRedirects()).open(request, timeout=15) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ProviderError('Ответ платежной системы слишком большой.')
    return data


def exact_amount(value):
    try:
        amount = money(value)
        if amount <= 0 or Decimal(str(value)) != amount:
            raise ValueError
        return amount
    except (ValueError, TypeError, InvalidOperation):
        raise ProviderError('Некорректная сумма платежной операции.') from None


def parse_json(data):
    if not isinstance(data, (bytes, str)) or len(data) > MAX_RESPONSE:
        raise ProviderError('Некорректный размер ответа платежной системы.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProviderError('Повторяющийся параметр ответа платежной системы.')
            result[key] = value
        return result
    try:
        value = json.loads(data, parse_float=Decimal, object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError):
        raise ProviderError('Некорректный ответ платежной системы.') from None


def request_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ProviderError('Некорректный идентификатор возврата.') from None


def encode64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


class RobokassaClient:
    def __init__(self, credentials, send=transport):
        self.credentials, self.send = credentials, send

    def require_live_operation(self):
        credential = self.credentials
        if credential.test_mode:
            raise ProviderError('OpStateExt и возврат реальной операции недоступны для тестовой оплаты.')
        if not credential.merchant or not credential.password2 or credential.algorithm not in ('md5', 'sha256', 'sha512'):
            raise ProviderError('Не настроены отдельные реквизиты API платежной системы.')

    def operation(self, invoice_id, expected_amount, expected_fields):
        self.require_live_operation()
        if not isinstance(invoice_id, int) or isinstance(invoice_id, bool) or invoice_id <= 0:
            raise ProviderError('Некорректный номер платежа.')
        if (set(expected_fields) != {'Shp_flow', 'Shp_token'} or
                not isinstance(expected_fields['Shp_flow'], str) or not expected_fields['Shp_flow'].startswith('tariffs_v2_') or
                not re.fullmatch(r'[0-9a-f]{48}', expected_fields['Shp_token'])):
            raise ProviderError('Не задано однозначное назначение платежной операции v2.')
        credential = self.credentials
        signature = hashlib.new(credential.algorithm,
            f'{credential.merchant}:{invoice_id}:{credential.password2}'.encode()).hexdigest()
        try:
            data = self.send('GET', OPERATION_URL + '?' + urlencode(dict(
                MerchantLogin=credential.merchant, InvoiceID=invoice_id, Signature=signature)))
        except Exception:
            raise ProviderError('Не удалось проверить платежную операцию; повторите проверку позже.') from None
        if not isinstance(data, bytes) or len(data) > MAX_RESPONSE or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ProviderError('Некорректный XML платежной операции.')
        try:
            decoded = data.decode('utf-8')
            document = ET.fromstring(decoded)
            for element in document.iter():
                element.tag = element.tag.rsplit('}', 1)[-1]
            if document.findtext('Result/Code') != '0' or document.findtext('State/Code') != '100':
                raise ProviderError('Платежная операция не подтверждена провайдером.')
            if exact_amount(document.findtext('Info/OutSum')) != exact_amount(expected_amount):
                raise ProviderError('Сумма операции провайдера отличается от сохраненного платежа.')
            fields = {}
            for element in document.findall('UserFields/Field'):
                key = element.findtext('Name')
                if key in fields:
                    raise ProviderError('Повторяющийся параметр платежной операции.')
                fields[key] = element.findtext('Value')
            if any(fields.get(key) != value for key, value in expected_fields.items()):
                raise ProviderError('Назначение операции не совпадает с сохраненным платежом.')
            key = document.findtext('Info/OpKey')
            if not key or len(key) > 200 or not re.fullmatch(r'[A-Za-z0-9_-]+', key):
                raise ProviderError('Провайдер не вернул корректный идентификатор операции.')
            return key
        except (ET.ParseError, UnicodeError):
            raise ProviderError('Некорректный XML платежной операции.') from None

    def refund_token(self, operation_key, amount, receipt_items):
        self.require_live_operation()
        credential = self.credentials
        if not credential.password3:
            raise ProviderError('Для API возвратов необходим Password3.')
        if not isinstance(operation_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', operation_key):
            raise ProviderError('Некорректный идентификатор операции.')
        amount = exact_amount(amount)
        if not isinstance(receipt_items, list) or not receipt_items:
            raise ProviderError('Для возврата необходимо подготовить состав фискального чека.')
        items, total = [], Decimal('0')
        for item in receipt_items:
            quantity = item.get('Quantity')
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
                raise ProviderError('Некорректное количество в чеке возврата.')
            cost = exact_amount(item.get('Cost'))
            total += cost * quantity
            strings = {key: item.get(key) for key in ('Name', 'Tax', 'PaymentMethod', 'PaymentObject')}
            if any(not isinstance(value, str) or not value or len(value) > 128 for value in strings.values()):
                raise ProviderError('Не заполнены реквизиты чека возврата.')
            prefix = json.dumps(strings, ensure_ascii=False, separators=(',', ':'))[:-1]
            items.append(prefix + f',"Quantity":{quantity},"Cost":{cost:.2f}' + '}')
        if total != amount:
            raise ProviderError('Сумма чека возврата не совпадает с заявкой.')
        header = encode64(b'{"alg":"HS256","typ":"JWT"}')
        payload = '{"OpKey":' + json.dumps(operation_key) + f',"RefundSum":{amount:.2f},"InvoiceItems":[' + ','.join(items) + ']}'
        unsigned = header + '.' + encode64(payload.encode('utf-8'))
        signed = hmac.new(credential.password3.encode(), unsigned.encode(), hashlib.sha256).digest()
        return (unsigned + '.' + encode64(signed)).encode('ascii')

    def create_refund(self, operation_key, amount, receipt_items):
        token = self.refund_token(operation_key, amount, receipt_items)
        try:
            response = parse_json(self.send('POST', REFUND_URL, token))
            if response.get('success') is True:
                return request_id(response.get('requestId'))
            if response.get('success') is False and response.get('requestId') is None:
                raise RefundRejected('Провайдер отказал в создании возврата. Требуется проверка причины.')
            raise RefundOutcomeUnknown('Неоднозначный ответ создания возврата. Автоматический повтор запрещен.')
        except RefundOutcomeUnknown:
            raise
        except RefundRejected:
            raise
        except ProviderError:
            # A malformed response after sending may still mean the request was accepted.
            raise RefundOutcomeUnknown('Результат создания возврата неизвестен. Автоматический повтор запрещен.') from None
        except Exception:
            raise RefundOutcomeUnknown('Связь прервалась после отправки возврата. Нужна сверка; повтор запрещен.') from None

    def refund_state(self, identifier, expected_amount):
        self.require_live_operation()
        identifier = request_id(identifier)
        try:
            response = parse_json(self.send('GET', REFUND_STATE_URL + '?' + urlencode({'id': identifier})))
        except Exception:
            raise ProviderError('Не удалось проверить результат возврата. Заявка остается в ожидании.') from None
        if request_id(response.get('requestId')) != identifier or exact_amount(response.get('amount')) != exact_amount(expected_amount):
            raise ProviderError('Результат не соответствует ожидаемому возврату.')
        status = response.get('label')
        if status not in ('finished', 'processing', 'canceled'):
            raise ProviderError('Неизвестное состояние возврата. Нужна повторная проверка.')
        return status
