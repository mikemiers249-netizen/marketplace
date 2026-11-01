"""Isolated checkout. Callers authenticate users and commit the whole transaction."""
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta

from flask import current_app
from sqlalchemy import or_, func
from app import db
from app.models.products import Product
from app.models.users import Seller, Buyer, BuyerDelivery, SellerDelivery
from app.models.tariffs_v2 import TariffV2Audit, TariffV2GatewayNumber
from app.models.tariffs_v2_commerce import (
    TariffV2Listing, TariffV2Cart, TariffV2Checkout, TariffV2CheckoutRequest,
    TariffV2SandboxOrder, TariffV2CheckoutLine, TariffV2Reservation,
)
from app.utils.tariffs_v2 import (
    TariffError, ZERO, blocked_for_new_sales, integer, quote_line, storage_quote, stored_utc,
    utc, midnight_utc, moscow_date,
)
from app.utils.tariffs_v2_payments import allocate_gateway_number, require_test_adapter, verify_result

CHECKOUT_FLOW = 'tariffs_v2_checkout_test'


class IncompleteDelivery(TariffError):
    pass


def digital_available(product):
    return (not product.digital_book_id or bool(product.digital_book and
        product.digital_book.seller_id == product.seller_id and product.digital_book.published and
        product.digital_book.moderation_status == 'approved'))


def publication_error(product, at=None):
    from app.utils.tariffs_v2_replicas import replica_is_purged
    if replica_is_purged(product.id):
        return 'Тестовая копия карточки удаляется или удалена. Для публикации нужна новая копия и проверка предоплаты.'
    if product.status != 'approved':
        return 'Карточка должна пройти модерацию.'
    if not product.seller or not product.seller.is_active:
        return 'Магазин недоступен.'
    if not digital_available(product):
        return 'Электронный товар недоступен или не одобрен модерацией.'
    if blocked_for_new_sales(product.seller_id, at):
        return 'Новые продажи заблокированы до оплаты месячного счета.'
    try:
        quote_line(product, 1, at)
        if not storage_quote(product, at)['may_publish']:
            return 'Для публикации требуется предоплата хранения.'
    except TariffError as error:
        return str(error)
    return None


def is_visible(product, at=None):
    listing = db.session.get(TariffV2Listing, product.id)
    return bool(listing and listing.published and publication_error(product, at) is None)


def set_publication(product_id, seller_id, published, at=None):
    if not isinstance(published, bool):
        raise TariffError('Укажите состояние публикации.')
    db.session.query(Seller).filter_by(id=seller_id).with_for_update().one()
    product = Product.query.filter_by(id=product_id, seller_id=seller_id).with_for_update().first()
    if product is None or product.status == 'deleted':
        raise TariffError('Карточка недоступна продавцу.')
    if published:
        error = publication_error(product, at)
        if error:
            raise TariffError(error)
    row = db.session.get(TariffV2Listing, product_id)
    if row is None:
        row = TariffV2Listing(product_id=product_id)
        db.session.add(row)
    row.published = published
    row.changed_at = stored_utc(at)
    db.session.add(TariffV2Audit(actor=f'seller:{seller_id}', action='sandbox_publication',
        subject=str(product_id), details={'published': published}))
    return row


def available_quantity(product, at=None, excluding=None):
    query = db.session.query(func.coalesce(func.sum(TariffV2Reservation.quantity), 0)).filter(
        TariffV2Reservation.product_id == product.id,
        or_(TariffV2Reservation.status == 'committed',
            (TariffV2Reservation.status == 'pending') & (TariffV2Reservation.expires_at > stored_utc(at))))
    if excluding is not None:
        query = query.filter(TariffV2Reservation.checkout_id != excluding)
    return max(0, (product.stock_quantity or 0) - query.scalar())


def release_checkout(checkout):
    checkout.status = 'expired'
    checkout.active_buyer_id = None
    for reservation in TariffV2Reservation.query.filter_by(checkout_id=checkout.id, status='pending'):
        reservation.status = 'released'
    db.session.flush()


def set_cart_quantity(buyer_id, product_id, quantity, at=None):
    quantity = integer(quantity, low=0)
    db.session.query(Buyer).filter_by(id=buyer_id).with_for_update().one()
    product = Product.query.filter_by(id=product_id).with_for_update().first()
    row = db.session.get(TariffV2Cart, (buyer_id, product_id))
    if quantity:
        if product is None or not is_visible(product, at):
            raise TariffError('Товар недоступен в тестовом каталоге.')
        if product.digital_book_id and quantity != 1:
            raise TariffError('Электронный товар можно купить в одном экземпляре.')
    pending = TariffV2Checkout.query.filter_by(active_buyer_id=buyer_id).first()
    if quantity and not product.digital_book_id and available_quantity(product, at, pending.id if pending else None) < quantity:
        raise TariffError('Недостаточно товара в тестовом каталоге.')
    if pending:
        release_checkout(pending)
    if quantity == 0:
        if row:
            db.session.delete(row)
    elif row:
        row.quantity = quantity
    else:
        db.session.add(TariffV2Cart(buyer_id=buyer_id, product_id=product_id, quantity=quantity))


def delivery_snapshot(buyer_id, seller_id, delivery_id):
    row = BuyerDelivery.query.filter_by(id=delivery_id, buyer_id=buyer_id).first()
    if row is None or not row.delivery_service or not row.delivery_service.is_active:
        raise TariffError('Выберите доступный адрес доставки в своем профиле.')
    if not SellerDelivery.query.filter_by(seller_id=seller_id, delivery_service_id=row.delivery_service_id, is_active=True).first():
        raise TariffError('Магазин не поддерживает выбранную доставку.')
    if row.missing_required_fields:
        raise IncompleteDelivery('Заполните адрес в профиле: ' + ', '.join(row.missing_required_fields))
    return dict(service_id=row.delivery_service_id, service_name=row.delivery_service.name,
        service_code=row.delivery_service.code, address=row.address, pvz_code=row.pvz_code,
        pvz_address=row.pvz_address, city=row.pvz_city, recipient_name=row.recipient_name,
        recipient_phone=row.recipient_phone, payment='separate')


def daily_limit_reached(seller, at=None, excluding=None):
    if not seller.daily_orders_limit or seller.daily_orders_limit <= 0:
        return False
    start = stored_utc(midnight_utc(moscow_date(at)))
    end = start + timedelta(days=1)
    query = TariffV2SandboxOrder.query.join(TariffV2Checkout).filter(
        TariffV2SandboxOrder.seller_id == seller.id, TariffV2SandboxOrder.has_physical.is_(True),
        TariffV2SandboxOrder.status != 'cancelled',
        or_((TariffV2Checkout.status == 'complete') & (TariffV2Checkout.completed_at >= start) &
                (TariffV2Checkout.completed_at < end),
            (TariffV2Checkout.status == 'pending') & (TariffV2Checkout.expires_at > stored_utc(at)) &
                (TariffV2Checkout.created_at >= start) & (TariffV2Checkout.created_at < end)))
    if excluding is not None:
        query = query.filter(TariffV2Checkout.id != excluding)
    return query.count() >= seller.daily_orders_limit


def create_checkout(buyer_id, request_key, deliveries, at=None):
    if not re.fullmatch(r'[0-9a-f]{48}', request_key or ''):
        raise TariffError('Обновите форму оформления заказа.')
    instant = utc(at)
    db.session.query(Buyer).filter_by(id=buyer_id).with_for_update().one()
    previous_request = db.session.get(TariffV2CheckoutRequest, (buyer_id, request_key))
    if previous_request:
        return db.session.get(TariffV2Checkout, previous_request.checkout_id)
    previous = TariffV2Checkout.query.filter_by(active_buyer_id=buyer_id).with_for_update().first()
    if previous and utc(previous.expires_at) <= instant:
        release_checkout(previous)
        previous = None
    items = TariffV2Cart.query.filter_by(buyer_id=buyer_id).order_by(TariffV2Cart.product_id).all()
    if not items:
        raise TariffError('Тестовая корзина пуста.')
    seller_ids = {item.product.seller_id for item in items}
    db.session.query(Seller).filter(Seller.id.in_(seller_ids)).order_by(Seller.id).with_for_update().all()
    products = Product.query.filter(Product.id.in_([item.product_id for item in items])).order_by(Product.id).with_for_update().all()
    products = {product.id: product for product in products}
    groups = {}
    for item in items:
        product = products.get(item.product_id)
        if not product or not is_visible(product, instant):
            raise TariffError('Один из товаров больше недоступен в тестовом каталоге.')
        if product.digital_book_id and item.quantity != 1:
            raise TariffError('Электронный товар можно купить в одном экземпляре.')
        if not product.digital_book_id and available_quantity(product, instant, previous.id if previous else None) < item.quantity:
            raise TariffError('Недостаточно товара для оформления заказа.')
        group = groups.setdefault(product.seller_id, dict(lines=[], physical_total=ZERO, delivery={}))
        quote = quote_line(product, item.quantity, instant)
        group['lines'].append((product, quote))
        if not product.digital_book_id:
            group['physical_total'] += quote.goods_amount
    fingerprint_data = []
    for seller_id, group in sorted(groups.items()):
        physical = any(not product.digital_book_id for product, _ in group['lines'])
        if physical:
            seller = group['lines'][0][0].seller
            if daily_limit_reached(seller, instant, previous.id if previous else None):
                raise TariffError('Дневной лимит физических заказов магазина исчерпан.')
            if group['physical_total'] < (seller.minimum_order_amount or ZERO):
                raise TariffError(f'Минимальная сумма физического заказа: {seller.minimum_order_amount} ₽.')
            group['delivery'] = delivery_snapshot(buyer_id, seller_id, deliveries.get(seller_id))
        group['physical'] = physical
        fingerprint_data.append(dict(seller_id=seller_id, delivery=group['delivery'],
            lines=[dict(quote=quote.json(), digital_book_id=product.digital_book_id) for product, quote in group['lines']]))
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if previous and previous.fingerprint == fingerprint:
        db.session.add(TariffV2CheckoutRequest(buyer_id=buyer_id, request_key=request_key, checkout_id=previous.id))
        return previous
    if previous:
        release_checkout(previous)
    online = sum((quote.online_amount for group in groups.values() for _, quote in group['lines']), ZERO)
    cod = sum((quote.cod_amount for group in groups.values() for _, quote in group['lines']), ZERO)
    if online > ZERO:
        require_test_adapter()
    checkout = TariffV2Checkout(buyer_id=buyer_id, active_buyer_id=buyer_id,
        merchant_login=current_app.config.get('TARIFFS_V2_ROBOKASSA_TEST_LOGIN') or '',
        token=secrets.token_hex(24), fingerprint=fingerprint, online_amount=online, cod_amount=cod,
        status='pending', created_at=stored_utc(instant), expires_at=stored_utc(instant + timedelta(minutes=30)), is_test=True)
    db.session.add(checkout)
    db.session.flush()
    if online > ZERO:
        checkout.gateway_id = allocate_gateway_number('checkout', checkout.id)
    for seller_id, group in sorted(groups.items()):
        quotes = [quote for _, quote in group['lines']]
        order = TariffV2SandboxOrder(checkout=checkout, seller_id=seller_id,
            goods_amount=sum((quote.goods_amount for quote in quotes), ZERO),
            fee=sum((quote.fee for quote in quotes), ZERO), online_amount=sum((quote.online_amount for quote in quotes), ZERO),
            cod_amount=sum((quote.cod_amount for quote in quotes), ZERO), delivery=group['delivery'],
            has_physical=group['physical'], shipping_days=quotes[0].shipping_days, status='waiting_payment')
        db.session.add(order)
        db.session.flush()
        for product, quote in group['lines']:
            line = TariffV2CheckoutLine(order=order, product_id=product.id, digital_book_id=product.digital_book_id,
                quantity=quote.quantity, fee=quote.fee, refunded_fee=ZERO, snapshot=quote.json())
            db.session.add(line)
            db.session.flush()
            if not product.digital_book_id:
                db.session.add(TariffV2Reservation(checkout_id=checkout.id, line_id=line.id,
                    product_id=product.id, quantity=quote.quantity, expires_at=checkout.expires_at, status='pending'))
    db.session.add(TariffV2CheckoutRequest(buyer_id=buyer_id, request_key=request_key, checkout_id=checkout.id))
    db.session.flush()
    if online == ZERO:
        complete_checkout(checkout, instant)
    return checkout


def complete_checkout(checkout, at):
    checkout.status = 'complete'
    checkout.active_buyer_id = None
    checkout.completed_at = stored_utc(at)
    for order in checkout.orders:
        order.status = 'ready'
        order.handed_to_seller_at = stored_utc(at)
        if order.has_physical:
            order.shipping_deadline = stored_utc(utc(at) + timedelta(days=order.shipping_days))
        for line in order.lines:
            cart = db.session.get(TariffV2Cart, (checkout.buyer_id, line.product_id))
            if cart:
                if cart.quantity <= line.quantity:
                    db.session.delete(cart)
                else:
                    cart.quantity -= line.quantity
    for reservation in TariffV2Reservation.query.filter_by(checkout_id=checkout.id):
        reservation.status = 'committed'
    db.session.add(TariffV2Audit(actor='system:sandbox_checkout', action='checkout_complete',
        subject=str(checkout.id), details={'online': str(checkout.online_amount), 'cod': str(checkout.cod_amount)}))


def accept_checkout_result(data, at=None):
    number_id, amount, token = verify_result(data, CHECKOUT_FLOW)
    number = db.session.get(TariffV2GatewayNumber, number_id)
    checkout = db.session.get(TariffV2Checkout, number.reference_id) if number and number.purpose == 'checkout' else None
    if (not checkout or checkout.gateway_id != number_id or not checkout.is_test or
            checkout.merchant_login != current_app.config['TARIFFS_V2_ROBOKASSA_TEST_LOGIN'] or
            not hmac.compare_digest(checkout.token, token) or checkout.online_amount != amount):
        raise TariffError('Тестовый платеж не соответствует заказу.')
    db.session.query(Buyer).filter_by(id=checkout.buyer_id).with_for_update().one()
    db.session.query(Seller).filter(Seller.id.in_([order.seller_id for order in checkout.orders])).order_by(Seller.id).with_for_update().all()
    lines = [line for order in checkout.orders for line in order.lines]
    products = Product.query.filter(Product.id.in_([line.product_id for line in lines])).order_by(Product.id).with_for_update().all()
    products = {product.id: product for product in products}
    db.session.refresh(checkout, with_for_update=True)
    if checkout.paid_at is not None:
        return checkout
    instant = utc(at)
    reason = None
    if checkout.status != 'pending' or instant >= utc(checkout.expires_at):
        reason = 'expired'
    else:
        for order in checkout.orders:
            seller = db.session.get(Seller, order.seller_id)
            if order.has_physical and daily_limit_reached(seller, instant, checkout.id):
                reason = 'daily_limit_reached'
                break
        for line in lines:
            if reason:
                break
            product = products.get(line.product_id)
            if (not product or product.status != 'approved' or not product.seller.is_active or not digital_available(product) or
                    product.seller_id != line.order.seller_id or product.digital_book_id != line.digital_book_id):
                reason = 'product_unavailable'
                break
            if not line.digital_book_id:
                reservation = TariffV2Reservation.query.filter_by(checkout_id=checkout.id, line_id=line.id).first()
                if (not reservation or reservation.status != 'pending' or reservation.quantity != line.quantity or
                        reservation.product_id != product.id or utc(reservation.expires_at) <= instant or
                        available_quantity(product, instant, checkout.id) < line.quantity):
                    reason = 'reservation_unavailable'
                    break
    checkout.paid_at = stored_utc(instant)
    if reason:
        release_checkout(checkout)
        checkout.status = 'needs_review'
        checkout.review_reason = reason
        db.session.add(TariffV2Audit(actor='system:robokassa_test', action='checkout_payment_review',
            subject=str(checkout.id), details={'reason': reason}))
    else:
        complete_checkout(checkout, instant)
    return checkout


def ship_order(order_id, seller_id, tracking='', at=None):
    order = TariffV2SandboxOrder.query.filter_by(id=order_id, seller_id=seller_id).with_for_update().first()
    if not order or order.status not in ('ready', 'shipped') or not order.has_physical:
        raise TariffError('Заказ недоступен для отметки об отправке.')
    tracking = (tracking or '').strip()
    if len(tracking) > 100:
        raise TariffError('Трек-номер слишком длинный.')
    if order.status == 'ready':
        order.status = 'shipped'
        order.shipped_at = stored_utc(at)
        order.tracking_number = tracking or None
    return order
