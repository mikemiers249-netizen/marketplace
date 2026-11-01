"""V2 calculations and transaction helpers. No network, mail, jobs or commits.

Callers own the database transaction and authenticate any provider notification.
None of these functions is wired into production checkout in the first phase.
"""
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import current_app
from sqlalchemy import func

from app import db
from app.models.tariffs_v2 import (
    TariffV2Category, TariffV2Coverage, TariffV2Invoice, TariffV2OrderLine,
    TariffV2Refund, TariffV2Seller,
)

MSK = timezone(timedelta(hours=3), 'Europe/Moscow')
UTC = timezone.utc
CENT = Decimal('0.01')
ZERO = Decimal('0.00')
SELLER_REFUND_REASONS = frozenset(('seller_cancelled', 'seller_partial_cancelled',
                                  'shipping_overdue', 'confirmed_non_delivery'))


class TariffError(ValueError):
    pass


def utc(value=None):
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stored_utc(value):
    return utc(value).replace(tzinfo=None)


def moscow_date(value=None):
    return utc(value).astimezone(MSK).date()


def midnight_utc(day):
    return datetime.combine(day, time.min, MSK).astimezone(UTC)


def benefits_end(last_day):
    """An admin-specified date is inclusive in Moscow, not UTC."""
    return midnight_utc(last_day + timedelta(days=1))


def next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def money(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal('9999999999.99'):
            raise ValueError
        return number.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise TariffError('Укажите неотрицательную конечную сумму.') from None


def integer(value, low=1, high=1000000):
    if isinstance(value, bool):
        raise TariffError('Укажите целое число.')
    try:
        result = int(str(value))
    except (ValueError, TypeError):
        raise TariffError('Укажите целое число.') from None
    if not low <= result <= high:
        raise TariffError(f'Укажите число от {low} до {high}.')
    return result


def validate_rule(kind, amount):
    if kind not in ('fixed', 'percent'):
        raise TariffError('Выберите фиксированную плату или процент.')
    result = money(amount)
    if Decimal(str(amount)) != result:
        raise TariffError('Укажите не более двух знаков после запятой.')
    if kind == 'percent' and result > 100:
        raise TariffError('Процент должен быть от 0 до 100.')
    return result


def prorated_storage(monthly_rate, starts_on, ends_on=None):
    """Inclusive start, exclusive end; one Moscow calendar month only."""
    monthly_rate = money(monthly_rate)
    ends_on = ends_on or next_month(starts_on)
    if ends_on <= starts_on or ends_on > next_month(starts_on):
        raise TariffError('Период должен находиться внутри одного календарного месяца.')
    return (monthly_rate * (ends_on - starts_on).days /
            monthrange(starts_on.year, starts_on.month)[1]).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ResolvedTariff:
    rule_id: int | None
    rule_name: str
    kind: str
    amount: Decimal
    free_storage: bool
    free_buyer_fee: bool
    payment_mode: str
    shipping_days: int


@dataclass(frozen=True)
class LineQuote:
    product_id: int
    category_id: int | None
    product_name: str
    seller_id: int
    rule_id: int | None
    rule_name: str
    kind: str
    rate: Decimal
    unit_price: Decimal
    quantity: int
    goods_amount: Decimal
    fee: Decimal
    online_amount: Decimal
    cod_amount: Decimal
    shipping_days: int

    def json(self):
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}


def is_owner_seller(seller):
    owner_email = current_app.config.get('TARIFFS_V2_OWNER_EMAIL', '').strip().casefold()
    return bool(owner_email and (seller.email or '').strip().casefold() == owner_email)


def resolve_tariff(seller, category_id, at=None):
    at = utc(at)
    policy = db.session.get(TariffV2Seller, seller.id)
    owner = is_owner_seller(seller)
    benefits_active = policy and (policy.benefits_until is None or at < utc(policy.benefits_until))
    free_storage = owner or bool(benefits_active and policy.free_storage)
    free_fee = owner or bool(benefits_active and policy.free_buyer_fee)
    mapping = db.session.get(TariffV2Category, category_id) if category_id else None
    rule = policy.override_rule if policy and policy.override_rule_id else (mapping.rule if mapping else None)
    if rule and not rule.active and not (free_storage and free_fee):
        raise TariffError('Назначенный тариф отключен. Требуется решение администратора.')
    if not rule and not (free_storage and free_fee):
        raise TariffError('Для этой категории не назначен тариф.')
    return ResolvedTariff(
        rule.id if rule else None, rule.name if rule else 'Бесплатно',
        rule.kind if rule else 'fixed', money(rule.amount) if rule else ZERO,
        free_storage, free_fee,
        'full_online' if owner else (policy.payment_mode if policy else 'cod'),
        policy.shipping_days if policy else 5,
    )


def quote_line(product, quantity, at=None):
    quantity = integer(quantity)
    if not product.seller:
        raise TariffError('У товара не найден продавец.')
    tariff = resolve_tariff(product.seller, product.category_id, at)
    if product.digital_book_id and not is_owner_seller(product.seller):
        raise TariffError('Продажа электронных товаров доступна только магазину площадки.')
    price = money(product.price)
    goods = money(price * quantity)
    fee = ZERO if tariff.free_buyer_fee or tariff.kind != 'percent' else money(goods * tariff.amount / 100)
    online = money(goods + fee) if tariff.payment_mode == 'full_online' else fee
    return LineQuote(product.id, product.category_id, product.name, product.seller_id,
                     tariff.rule_id, tariff.rule_name, tariff.kind, tariff.amount,
                     price, quantity, goods, fee, online,
                     ZERO if tariff.payment_mode == 'full_online' else goods, tariff.shipping_days)


def quote_cart(cart_items, at=None):
    lines = [quote_line(item.product, item.quantity, at) for item in cart_items]
    if not lines:
        raise TariffError('Корзина пуста.')
    online = sum((line.online_amount for line in lines), ZERO)
    return dict(lines=[line.json() for line in lines], online_amount=str(online),
                cod_amount=str(sum((line.cod_amount for line in lines), ZERO)),
                requires_gateway=online > 0)


def storage_quote(product, at=None):
    """Read-only quote including remaining prepaid value on category change."""
    today = moscow_date(at)
    end = next_month(today)
    tariff = resolve_tariff(product.seller, product.category_id, at)
    deleted = product.status == 'deleted'
    rate = ZERO if deleted or tariff.free_storage or tariff.kind != 'fixed' else tariff.amount
    required = prorated_storage(rate, today, end)
    coverage = TariffV2Coverage.query.filter(
        TariffV2Coverage.product_id == product.id,
        TariffV2Coverage.seller_id == product.seller_id,
        TariffV2Coverage.starts_on <= today,
        TariffV2Coverage.ends_on == end,
    ).all()
    # Payment records carry the additional prepaid amount (not the whole rate again).
    # Remaining credit declines proportionally within the paid interval.
    credit = sum((money(row.paid_amount * Decimal((row.ends_on - today).days) /
                        Decimal((row.ends_on - row.starts_on).days)) for row in coverage), ZERO)
    due = max(ZERO, required - credit)
    return dict(monthly_rate=rate, required=required, credit=credit, due=due,
                starts_on=today, ends_on=end, may_publish=due == ZERO and not deleted)


def record_storage_payment(product, amount, reference, at=None):
    """For a verified callback adapter only; no route exposes manual 'payment'."""
    if not reference or len(reference) > 160:
        raise TariffError('Не указан идентификатор подтвержденного платежа.')
    from app.models.users import Seller
    db.session.query(Seller).filter_by(id=product.seller_id).with_for_update().one()
    existing = TariffV2Coverage.query.filter_by(payment_reference=reference).first()
    amount = money(amount)
    if existing:
        if existing.product_id != product.id or existing.paid_amount != amount:
            raise TariffError('Платеж уже связан с другой операцией.')
        return existing
    quote = storage_quote(product, at)
    if amount != quote['due'] or amount <= 0:
        raise TariffError('Сумма не совпадает с предоплатой карточки.')
    row = TariffV2Coverage(product_id=product.id, original_product_id=product.id, seller_id=product.seller_id,
        starts_on=quote['starts_on'], ends_on=quote['ends_on'],
        monthly_rate=quote['monthly_rate'], paid_amount=amount,
        payment_reference=reference, paid_at=stored_utc(at))
    db.session.add(row)
    return row


def snapshot_line(order, product, quantity, line_key, at=None):
    if product.seller_id != order.seller_id:
        raise TariffError('Продавец позиции не совпадает с продавцом заказа.')
    quote = quote_line(product, quantity, at)
    fields = asdict(quote)
    fields.pop('goods_amount')
    row = TariffV2OrderLine(order_id=order.id, line_key=str(line_key), **fields)
    db.session.add(row)
    return row


def hand_to_seller(line, at=None):
    """Only after verified group payment, or a genuinely zero-online checkout."""
    if line.handed_to_seller_at is None:
        instant = utc(at)
        line.handed_to_seller_at = stored_utc(instant)
        line.shipping_deadline = stored_utc(instant + timedelta(days=line.shipping_days))


def shipping_overdue(line, at=None):
    return bool(line.handed_to_seller_at and line.shipping_deadline and
                line.shipped_at is None and utc(at) >= utc(line.shipping_deadline))


def mark_shipped(line, seller_id, at=None):
    if line.seller_id != seller_id or not line.handed_to_seller_at:
        raise TariffError('Заказ недоступен продавцу для отправки.')
    if shipping_overdue(line, at):
        raise TariffError('Срок отгрузки истек; требуется проверка возврата.')
    if line.shipped_at is None:
        line.shipped_at = stored_utc(at)


def refund_decision(response, opened_at, at=None, admin_approved=False):
    """Silence alone never triggers a payout. A disputed claim needs admin review."""
    if response not in ('waiting', 'confirmed', 'disputed'):
        raise TariffError('Неизвестный ответ по претензии.')
    if response == 'confirmed':
        return 'refund'
    if response == 'disputed':
        return 'refund' if admin_approved else 'admin_review'
    if utc(at) < utc(opened_at) + timedelta(days=3):
        return 'waiting'
    return 'refund' if admin_approved else 'admin_review'


def cumulative_partial_fee(line, cancelled_quantity):
    """Cumulative refund target; preserve rounding and cap at the snapshotted fee.

    The future cancellation adapter subtracts previous successful/pending refunds
    from this target under the same line lock before creating the next intent.
    """
    quantity = integer(cancelled_quantity, 0, line.quantity)
    return money(line.fee * Decimal(quantity) / Decimal(line.quantity))


def request_refund(line_id, amount, reason, request_key):
    if reason not in SELLER_REFUND_REASONS:
        raise TariffError('Это основание не предусматривает возврат сбора площадкой.')
    if not request_key or len(request_key) > 160:
        raise TariffError('Не указан ключ операции возврата.')
    line = TariffV2OrderLine.query.filter_by(id=line_id).with_for_update().one()
    amount = money(amount)
    existing = TariffV2Refund.query.filter_by(request_key=request_key).first()
    if existing:
        if (existing.line_id, existing.amount, existing.reason) != (line_id, amount, reason):
            raise TariffError('Ключ возврата уже использован другой операцией.')
        return existing
    if not line.handed_to_seller_at:
        raise TariffError('Сбор по заказу еще не подтвержден.')
    pending = db.session.query(func.sum(TariffV2Refund.amount)).filter_by(
        line_id=line_id, status='pending').scalar() or ZERO
    if amount <= 0 or amount + line.refunded_fee + pending > line.fee:
        raise TariffError('Сумма превышает доступный к возврату сбор.')
    row = TariffV2Refund(line_id=line_id, amount=amount, reason=reason,
                         request_key=request_key, status='pending')
    db.session.add(row)
    return row


def confirm_refund(refund_id, provider_reference, amount, at=None):
    """Record authenticated successful provider result, never merely an API request."""
    refund = TariffV2Refund.query.filter_by(id=refund_id).with_for_update().one()
    line = TariffV2OrderLine.query.filter_by(id=refund.line_id).with_for_update().one()
    if not provider_reference or len(provider_reference) > 160 or money(amount) != refund.amount:
        raise TariffError('Подтверждение возврата не соответствует операции.')
    if refund.status == 'succeeded':
        if refund.provider_reference != provider_reference:
            raise TariffError('Возврат уже подтвержден другой операцией.')
        return refund
    if refund.status != 'pending' or line.refunded_fee + refund.amount > line.fee:
        raise TariffError('Недопустимое состояние возврата.')
    refund.status = 'succeeded'
    refund.provider_reference = provider_reference
    refund.confirmed_at = stored_utc(at)
    line.refunded_fee += refund.amount
    return refund


def issue_monthly_invoice(seller_id, at=None):
    """Bill only prior-month, confirmed refunds; serialize one invoice per seller/month."""
    from app.models.users import Seller
    instant = utc(at)
    today = moscow_date(instant)
    period = today.replace(day=1)
    db.session.query(Seller).filter_by(id=seller_id).with_for_update().one()
    invoice = TariffV2Invoice.query.filter_by(seller_id=seller_id, period=period).first()
    if invoice:
        return invoice
    refunds = TariffV2Refund.query.join(TariffV2OrderLine).filter(
        TariffV2OrderLine.seller_id == seller_id,
        TariffV2Refund.status == 'succeeded',
        TariffV2Refund.invoice_id.is_(None),
        TariffV2Refund.confirmed_at < stored_utc(midnight_utc(period)),
    ).with_for_update(of=TariffV2Refund).all()
    if not refunds:
        return None
    invoice = TariffV2Invoice(seller_id=seller_id, period=period,
        issued_at=stored_utc(instant), due_at=stored_utc(midnight_utc(period.replace(day=6))),
        amount=sum((row.amount for row in refunds), ZERO))
    db.session.add(invoice)
    db.session.flush()
    for row in refunds:
        row.invoice = invoice
    return invoice


def blocked_for_new_sales(seller_id, at=None):
    return TariffV2Invoice.query.filter(
        TariffV2Invoice.seller_id == seller_id,
        TariffV2Invoice.paid_at.is_(None),
        TariffV2Invoice.amount > 0,
        TariffV2Invoice.due_at <= stored_utc(at),
    ).first() is not None


def confirm_invoice_payment(invoice_id, amount, reference, at=None):
    invoice = TariffV2Invoice.query.filter_by(id=invoice_id).with_for_update().one()
    if not reference or len(reference) > 160 or money(amount) != invoice.amount:
        raise TariffError('Сумма оплаты не соответствует счету.')
    if invoice.paid_at is not None:
        if invoice.payment_reference != reference:
            raise TariffError('Счет уже оплачен другим платежом.')
        return invoice
    invoice.paid_at = stored_utc(at)
    invoice.payment_reference = reference
    return invoice
