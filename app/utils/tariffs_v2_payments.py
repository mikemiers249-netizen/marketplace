"""Test-only durable storage payment adapter. Never reads legacy/live passwords.

Protocol: https://docs.robokassa.ru/ru/pay-interface and
https://docs.robokassa.ru/ru/notifications-and-redirects .
No provider HTTP call, refund, publication, notification or deletion is made here.
"""
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from flask import current_app

from app import db
from app.models.products import Product
from app.models.users import Seller
from app.models.tariffs_v2 import TariffV2Audit, TariffV2Coverage, TariffV2GatewayNumber, TariffV2StorageInvoice
from app.utils.tariffs_v2 import (
    TariffError, midnight_utc, moscow_date, resolve_tariff, storage_quote, stored_utc, utc,
)

FLOW = 'tariffs_v2_storage_test'
GATEWAY = 'https://auth.robokassa.ru/Merchant/Index.aspx'


def configured():
    config = current_app.config
    return bool(config.get('TARIFFS_V2_ENABLED') and config.get('TARIFFS_V2_SANDBOX') and
                config.get('TARIFFS_V2_PAYMENT_TEST_ENABLED') and
                all(config.get('TARIFFS_V2_ROBOKASSA_TEST_' + key) for key in ('LOGIN', 'PASSWORD1', 'PASSWORD2')) and
                config.get('TARIFFS_V2_ROBOKASSA_HASH') in ('md5', 'sha256', 'sha512'))


def require_test_adapter():
    if not configured():
        raise TariffError('Тестовая оплата v2 не настроена. Настоящие платежи не используются.')


def signature(text):
    algorithm = current_app.config.get('TARIFFS_V2_ROBOKASSA_HASH')
    if algorithm not in ('md5', 'sha256', 'sha512'):
        raise TariffError('Неподдерживаемый алгоритм подписи.')
    return hashlib.new(algorithm, text.encode('utf-8')).hexdigest()


def allocate_gateway_number(purpose, reference_id):
    row = TariffV2GatewayNumber(purpose=purpose, reference_id=reference_id)
    db.session.add(row)
    db.session.flush()
    return row.id


def quote_fingerprint(product, at):
    quote = storage_quote(product, at)
    tariff = resolve_tariff(product.seller, product.category_id, at)
    data = dict(product_id=product.id, seller_id=product.seller_id, category_id=product.category_id,
                created_at=str(product.created_at), rule_id=tariff.rule_id,
                monthly_rate=str(quote['monthly_rate']), due=str(quote['due']),
                credit=str(quote['credit']), starts_on=str(quote['starts_on']), ends_on=str(quote['ends_on']))
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return quote, fingerprint


def issue_storage_invoice(product_id, seller_id, at=None):
    require_test_adapter()
    instant = utc(at)
    # Same lock order as storage coverage: seller, product, pending invoice.
    db.session.query(Seller).filter_by(id=seller_id).with_for_update().one()
    product = Product.query.filter_by(id=product_id, seller_id=seller_id).with_for_update().first()
    if product is None or product.status == 'deleted':
        raise TariffError('Карточка недоступна продавцу для оплаты.')
    from app.utils.tariffs_v2_replicas import replica_is_purged
    if replica_is_purged(product.id):
        raise TariffError('Тестовая копия карточки удаляется или удалена. Сначала создайте новую копию.')
    quote, fingerprint = quote_fingerprint(product, instant)
    previous = TariffV2StorageInvoice.query.filter_by(active_product_id=product.id).with_for_update().first()
    if previous is not None:
        if previous.expires_at > stored_utc(instant) and previous.fingerprint == fingerprint:
            return previous
        previous.status = 'expired'
        previous.active_product_id = None
        db.session.flush()
    if quote['due'] == 0:
        return None
    expires = min(instant + timedelta(minutes=30), midnight_utc(moscow_date(instant) + timedelta(days=1)))
    invoice = TariffV2StorageInvoice(seller_id=seller_id, product_id=product.id,
        active_product_id=product.id, product_name=product.name, category_id=product.category_id,
        merchant_login=current_app.config['TARIFFS_V2_ROBOKASSA_TEST_LOGIN'],
        fingerprint=fingerprint, token=secrets.token_hex(24), monthly_rate=quote['monthly_rate'],
        amount=quote['due'], starts_on=quote['starts_on'], ends_on=quote['ends_on'],
        created_at=stored_utc(instant), expires_at=stored_utc(expires), status='pending', is_test=True)
    db.session.add(invoice)
    db.session.flush()
    invoice.gateway_id = allocate_gateway_number('storage', invoice.id)
    db.session.add(TariffV2Audit(actor=f'seller:{seller_id}', action='storage_test_invoice_created',
        subject=str(invoice.id), details=dict(product_id=product.id, amount=str(invoice.amount))))
    return invoice


def gateway_params(invoice, at=None):
    return payment_params(invoice, FLOW,
                          f'Тестовая предоплата хранения карточки #{invoice.product_id}', at)


def payment_params(invoice, flow, description, at=None):
    require_test_adapter()
    if invoice.status != 'pending' or not invoice.is_test or utc(at) >= utc(invoice.expires_at):
        raise TariffError('Этот тестовый счет недоступен для оплаты.')
    amount = format(invoice.amount, '.2f')
    login = current_app.config['TARIFFS_V2_ROBOKASSA_TEST_LOGIN']
    if login != invoice.merchant_login:
        raise TariffError('Счет создан для другого тестового магазина Robokassa.')
    password = current_app.config['TARIFFS_V2_ROBOKASSA_TEST_PASSWORD1']
    if invoice.gateway_id is None:
        raise TariffError('Для счета не назначен платежный номер. Обновите схему песочницы.')
    signed = f'{login}:{amount}:{invoice.gateway_id}:{password}:Shp_flow={flow}:Shp_token={invoice.token}'
    return dict(MerchantLogin=login, OutSum=amount, InvId=str(invoice.gateway_id), IsTest='1',
                Description=description,
                Culture='ru', Encoding='utf-8', Shp_flow=flow, Shp_token=invoice.token,
                SignatureValue=signature(signed))


def gateway_url(invoice):
    return GATEWAY + '?' + urlencode(gateway_params(invoice))


def verify_result(data, flow):
    """Authenticate original body values; return provider number, exact amount, token."""
    require_test_adapter()
    for key in ('InvId', 'OutSum', 'SignatureValue', 'Shp_flow', 'Shp_token'):
        if len(data.getlist(key)) != 1:
            raise TariffError('Неполное или повторяющееся уведомление об оплате.')
    if (set(key for key in data if key.lower().startswith('shp_')) != {'Shp_flow', 'Shp_token'} or
            data['Shp_flow'] != flow or not re.fullmatch(r'[0-9a-f]{48}', data['Shp_token']) or
            (data.getlist('IsTest') and data.getlist('IsTest') != ['1'])):
        raise TariffError('Уведомление не относится к тестовой оплате v2.')
    if not re.fullmatch(r'[1-9][0-9]{0,9}', data['InvId']) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,6})?', data['OutSum']) or len(data['OutSum']) > 32:
        raise TariffError('Некорректный номер счета или сумма.')
    if not re.fullmatch(r'[A-Fa-f0-9]{32,128}', data['SignatureValue']):
        raise TariffError('Некорректная подпись уведомления.')
    password = current_app.config['TARIFFS_V2_ROBOKASSA_TEST_PASSWORD2']
    signed = f"{data['OutSum']}:{data['InvId']}:{password}:Shp_flow={flow}:Shp_token={data['Shp_token']}"
    if not hmac.compare_digest(signature(signed).upper(), data['SignatureValue'].upper()):
        raise TariffError('Подпись уведомления не подтверждена.')
    try:
        received = Decimal(data['OutSum'])
    except InvalidOperation:
        raise TariffError('Некорректная сумма.') from None
    return int(data['InvId']), received, data['Shp_token']


def accept_storage_result(data, at=None):
    """Authenticate exact body values before any mutation; caller commits atomically."""
    number_id, received, token = verify_result(data, FLOW)
    number = db.session.get(TariffV2GatewayNumber, number_id)
    invoice = db.session.get(TariffV2StorageInvoice, number.reference_id) if number and number.purpose == 'storage' else None
    if (invoice is None or invoice.gateway_id != number_id or not invoice.is_test or
            invoice.merchant_login != current_app.config['TARIFFS_V2_ROBOKASSA_TEST_LOGIN'] or
            not hmac.compare_digest(invoice.token, token)):
        raise TariffError('Тестовый счет не найден.')
    if received != invoice.amount:
        raise TariffError('Сумма не совпадает с тестовым счетом.')
    db.session.query(Seller).filter_by(id=invoice.seller_id).with_for_update().one()
    product = Product.query.filter_by(id=invoice.product_id).with_for_update().first()
    db.session.refresh(invoice, with_for_update=True)
    if invoice.paid_at is not None:
        return invoice
    instant = utc(at)
    reason = None
    if invoice.status != 'pending' or instant >= utc(invoice.expires_at):
        reason = 'expired'
    elif product is None or product.seller_id != invoice.seller_id or product.status == 'deleted':
        reason = 'product_unavailable'
    else:
        try:
            _, fingerprint = quote_fingerprint(product, instant)
        except TariffError:
            fingerprint = None
        if fingerprint != invoice.fingerprint:
            reason = 'quote_changed'
    invoice.paid_at = stored_utc(instant)
    invoice.active_product_id = None
    if reason:
        invoice.status = 'needs_review'
        invoice.review_reason = reason
        db.session.add(TariffV2Audit(actor='system:robokassa_test', action='storage_test_payment_review',
            subject=str(invoice.id), details=dict(reason=reason, amount=str(invoice.amount))))
        return invoice
    reference = f'v2-storage-test:{invoice.id}:{invoice.token}'
    db.session.add(TariffV2Coverage(product_id=product.id, original_product_id=product.id,
        seller_id=invoice.seller_id, starts_on=invoice.starts_on, ends_on=invoice.ends_on,
        monthly_rate=invoice.monthly_rate, paid_amount=invoice.amount,
        payment_reference=reference, paid_at=stored_utc(instant), is_test=True))
    invoice.status = 'paid'
    db.session.add(TariffV2Audit(actor='system:robokassa_test', action='storage_test_payment_confirmed',
        subject=str(invoice.id), details=dict(product_id=product.id, amount=str(invoice.amount))))
    return invoice
