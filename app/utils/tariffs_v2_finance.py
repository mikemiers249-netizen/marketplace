"""Refund intents and confirmed outcomes. No network, mail, files or commits.

Provider integration must authenticate a result before calling confirm_checkout_refund.
The separately named simulator is ONLY for the explicitly isolated SQLite sandbox.
"""
from datetime import timedelta
from decimal import Decimal

from flask import current_app

from app import db
from app.models.users import Seller
from app.models.tariffs_v2 import TariffV2Audit, TariffV2Invoice
from app.models.tariffs_v2_commerce import TariffV2CheckoutLine, TariffV2SandboxOrder, TariffV2Reservation
from app.models.tariffs_v2_finance import TariffV2Cancellation, TariffV2CheckoutRefund, TariffV2Claim
from app.models.tariffs_v2_lifecycle import TariffV2OrderCompletion
from app.utils.tariffs_v2 import TariffError, ZERO, money, integer, utc, stored_utc


def audit(action, subject, details, actor='system:sandbox_finance'):
    db.session.add(TariffV2Audit(actor=actor, action=action, subject=str(subject), details=details))


def lock_order(order_id):
    order = db.session.get(TariffV2SandboxOrder, order_id)
    if order is None:
        raise TariffError('Заказ не найден.')
    # Shared with invoice generation: seller before order/line/refund.
    db.session.query(Seller).filter_by(id=order.seller_id).with_for_update().one()
    db.session.refresh(order, with_for_update=True)
    if order.checkout.status != 'complete' or not order.handed_to_seller_at:
        raise TariffError('Заказ еще не передан продавцу.')
    return order


def refund_target(line, quantity, reason, key, at=None):
    """Cumulative target avoids rounding drift and duplicate partial refunds.

    All online money for cancelled units is refunded. Only platform fee becomes
    seller debt; own-store goods principal never becomes commission debt.
    """
    if reason not in ('seller_cancelled', 'seller_partial_cancelled', 'shipping_overdue', 'confirmed_non_delivery'):
        raise TariffError('Основание не предусматривает возврат площадкой.')
    if not key or len(key) > 160:
        raise TariffError('Некорректный ключ возврата.')
    quantity = integer(quantity, 0, line.quantity)
    existing = TariffV2CheckoutRefund.query.filter_by(request_key=key).first()
    if existing:
        if existing.line_id != line.id or existing.reason != reason:
            raise TariffError('Ключ уже использован другой операцией.')
        return existing
    online = Decimal(line.snapshot['online_amount'])
    target = money(online * quantity / line.quantity)
    charge_target = money(line.fee * quantity / line.quantity)
    reserved = TariffV2CheckoutRefund.query.filter_by(line_id=line.id).filter(
        TariffV2CheckoutRefund.status.in_(('pending', 'succeeded'))).all()
    amount = target - sum((row.amount for row in reserved), ZERO)
    charge = charge_target - sum((row.seller_charge for row in reserved), ZERO)
    if amount <= ZERO:
        return None
    if charge < ZERO or charge > amount or target > online:
        raise TariffError('Некорректная сумма возврата.')
    row = TariffV2CheckoutRefund(line_id=line.id, request_key=key, amount=amount,
        seller_charge=charge, reason=reason, status='pending', created_at=stored_utc(at), is_test=True)
    db.session.add(row)
    audit('checkout_refund_requested', line.id, {'amount': str(amount), 'seller_charge': str(charge), 'reason': reason})
    return row


def cancel_line(line_id, seller_id, cumulative_quantity, at=None):
    line = db.session.get(TariffV2CheckoutLine, line_id)
    if not line or line.order.seller_id != seller_id:
        raise TariffError('Позиция недоступна продавцу.')
    order = lock_order(line.order_id)
    if db.session.get(TariffV2OrderCompletion, order.id):
        raise TariffError('Заказ уже завершен покупателем.')
    if order.status not in ('ready', 'cancelled'):
        raise TariffError('Отменить позицию можно до отправки.')
    db.session.refresh(line, with_for_update=True)
    quantity = integer(cumulative_quantity, 1, line.quantity)
    cancelled = db.session.get(TariffV2Cancellation, line.id)
    if cancelled and quantity < cancelled.quantity:
        raise TariffError('Количество отмененных товаров нельзя уменьшать.')
    if not cancelled:
        cancelled = TariffV2Cancellation(line_id=line.id, quantity=0)
        db.session.add(cancelled)
    cancelled.quantity = quantity
    reason = 'seller_cancelled' if quantity == line.quantity else 'seller_partial_cancelled'
    result = refund_target(line, quantity, reason, f'cancel:{line.id}:{quantity}', at)
    reservation = TariffV2Reservation.query.filter_by(line_id=line.id).first()
    if reservation:
        remaining = line.quantity - quantity
        if remaining:
            reservation.quantity = remaining
        else:
            reservation.status = 'released'
    db.session.flush()
    cancelled_quantities = {row.line_id: row.quantity for row in TariffV2Cancellation.query.filter(
        TariffV2Cancellation.line_id.in_([item.id for item in order.lines])).all()}
    order.cod_amount = sum((Decimal(item.snapshot['cod_amount']) - money(
        Decimal(item.snapshot['cod_amount']) * cancelled_quantities.get(item.id, 0) / item.quantity)
        for item in order.lines), ZERO)
    order.checkout.cod_amount = sum((item.cod_amount for item in order.checkout.orders), ZERO)
    if all(cancelled_quantities.get(item.id, 0) == item.quantity for item in order.lines):
        order.status = 'cancelled'
    audit('seller_line_cancelled', line.id, {'quantity': quantity}, f'seller:{seller_id}')
    return result


def buyer_cancel_order(order_id, buyer_id, at=None):
    order = lock_order(order_id)
    if order.checkout.buyer_id != buyer_id:
        raise TariffError('Заказ недоступен покупателю.')
    completion = db.session.get(TariffV2OrderCompletion, order.id)
    if completion:
        if completion.kind == 'buyer_cancelled':
            return order
        raise TariffError('Полученный заказ нельзя отменить до отправки.')
    if order.status != 'ready' or not order.has_physical:
        raise TariffError('Покупатель может отменить физический заказ до отправки.')
    if TariffV2Claim.query.filter_by(active_order_id=order.id).first():
        raise TariffError('Сначала необходимо завершить открытую претензию.')
    if order.shipping_deadline and utc(at) >= utc(order.shipping_deadline):
        raise TariffError('Срок отгрузки истек; отмена не должна заменять возврат по просрочке.')
    # Seller cancellation refunds/debts already requested remain intact. Buyer
    # cancellation refunds only the still-unrefunded goods principal paid online.
    for line in order.lines:
        online = Decimal(line.snapshot['online_amount'])
        principal = online - line.fee
        reserved = TariffV2CheckoutRefund.query.filter_by(line_id=line.id).filter(
            TariffV2CheckoutRefund.status.in_(('pending', 'succeeded'))).all()
        refunded_principal = sum((row.amount - row.seller_charge for row in reserved), ZERO)
        cancelled = db.session.get(TariffV2Cancellation, line.id)
        seller_cancelled = cancelled.quantity if cancelled else 0
        principal_target = money(principal * (line.quantity - seller_cancelled) / line.quantity)
        # Account separately for a seller-cancelled portion, including a failed
        # refund which must be retried using the original seller reason.
        seller_principal = money(principal * seller_cancelled / line.quantity)
        amount = max(ZERO, principal_target - max(ZERO, refunded_principal - seller_principal))
        if amount > ZERO:
            db.session.add(TariffV2CheckoutRefund(line_id=line.id, request_key=f'buyer-cancel:{line.id}',
                amount=amount, seller_charge=ZERO, reason='buyer_cancelled', status='pending',
                created_at=stored_utc(at), is_test=True))
        reservation = TariffV2Reservation.query.filter_by(line_id=line.id).first()
        if reservation:
            reservation.status = 'released'
    order.status = 'cancelled'
    order.cod_amount = ZERO
    order.checkout.cod_amount = sum((item.cod_amount for item in order.checkout.orders), ZERO)
    db.session.add(TariffV2OrderCompletion(order_id=order.id, kind='buyer_cancelled',
        buyer_id=buyer_id, created_at=stored_utc(at)))
    audit('buyer_order_cancelled', order.id, {'fee_retained': True}, f'buyer:{buyer_id}')
    return order


def receive_order(order_id, buyer_id, at=None):
    order = lock_order(order_id)
    if order.checkout.buyer_id != buyer_id:
        raise TariffError('Заказ недоступен покупателю.')
    completion = db.session.get(TariffV2OrderCompletion, order.id)
    if completion:
        if completion.kind == 'received':
            return order
        raise TariffError('Отмененный заказ нельзя подтвердить как полученный.')
    if order.status != 'shipped' or not order.has_physical:
        raise TariffError('Подтвердить получение можно после отправки физического заказа.')
    if TariffV2Claim.query.filter_by(active_order_id=order.id).first():
        raise TariffError('Сначала необходимо завершить открытую претензию.')
    order.status = 'received'
    db.session.add(TariffV2OrderCompletion(order_id=order.id, kind='received',
        buyer_id=buyer_id, created_at=stored_utc(at)))
    audit('buyer_order_received', order.id, {}, f'buyer:{buyer_id}')
    return order


def refund_order(order, reason, prefix, at=None):
    return [row for line in order.lines
            if (row := refund_target(line, line.quantity, reason, f'{prefix}:{line.id}', at)) is not None]


def process_shipping_overdue(at=None):
    """Explicit sandbox job; intents only, never treat an API request as success."""
    instant = utc(at)
    ids = [row.id for row in TariffV2SandboxOrder.query.filter(
        TariffV2SandboxOrder.status == 'ready', TariffV2SandboxOrder.has_physical.is_(True),
        TariffV2SandboxOrder.shipping_deadline <= stored_utc(instant)).order_by(TariffV2SandboxOrder.seller_id).all()]
    results = []
    for order_id in ids:
        order = lock_order(order_id)
        if order.status == 'ready' and order.shipping_deadline and utc(order.shipping_deadline) <= instant:
            results.extend(refund_order(order, 'shipping_overdue', f'overdue:{order.id}', instant))
    return results


def confirm_checkout_refund(refund_id, provider_reference, amount, succeeded=True, at=None):
    if not isinstance(succeeded, bool):
        raise TariffError('Некорректный результат возврата.')
    refund = db.session.get(TariffV2CheckoutRefund, refund_id)
    if not refund:
        raise TariffError('Возврат не найден.')
    lock_order(refund.line.order_id)
    db.session.refresh(refund.line, with_for_update=True)
    db.session.refresh(refund, with_for_update=True)
    if not provider_reference or len(provider_reference) > 160 or money(amount) != refund.amount:
        raise TariffError('Ответ платежной системы не соответствует возврату.')
    status = 'succeeded' if succeeded else 'failed'
    if refund.status != 'pending':
        if refund.status != status or refund.provider_reference != provider_reference:
            raise TariffError('Возврат уже завершен другим результатом.')
        return refund
    if succeeded and refund.line.refunded_fee + refund.seller_charge > refund.line.fee:
        raise TariffError('Сумма возврата превышает сбор.')
    refund.status = status
    refund.provider_reference = provider_reference
    refund.confirmed_at = stored_utc(at)
    if succeeded:
        refund.line.refunded_fee += refund.seller_charge
    audit('checkout_refund_' + status, refund.id, {'amount': str(refund.amount), 'reference': provider_reference})
    return refund


def retry_refund(refund_id, actor, at=None):
    original = db.session.get(TariffV2CheckoutRefund, refund_id)
    if not original:
        raise TariffError('Возврат не найден.')
    lock_order(original.line.order_id)
    db.session.refresh(original, with_for_update=True)
    if original.status != 'failed':
        raise TariffError('Повторить можно только неудавшийся возврат.')
    key = f'retry:{original.id}'
    existing = TariffV2CheckoutRefund.query.filter_by(request_key=key).first()
    if existing:
        return existing
    reserved = TariffV2CheckoutRefund.query.filter_by(line_id=original.line_id).filter(
        TariffV2CheckoutRefund.status.in_(('pending', 'succeeded'))).all()
    if (sum((row.amount for row in reserved), ZERO) + original.amount > Decimal(original.line.snapshot['online_amount']) or
            sum((row.seller_charge for row in reserved), ZERO) + original.seller_charge > original.line.fee):
        raise TariffError('Сумма уже возвращена или зарезервирована другим возвратом.')
    row = TariffV2CheckoutRefund(line_id=original.line_id, request_key=key, amount=original.amount,
        seller_charge=original.seller_charge, reason=original.reason, status='pending',
        created_at=stored_utc(at), is_test=True)
    db.session.add(row)
    audit('checkout_refund_retry', original.id, {}, actor)
    return row


def explanation(value):
    value = (value or '').strip()
    if not value or len(value) > 5000:
        raise TariffError('Укажите объяснение длиной от 1 до 5000 символов.')
    return value


def open_claim(order_id, role, user_id, text, at=None):
    order = lock_order(order_id)
    if role not in ('buyer', 'seller') or (order.checkout.buyer_id if role == 'buyer' else order.seller_id) != user_id:
        raise TariffError('Заказ недоступен участнику.')
    if not order.has_physical or order.status not in ('ready', 'shipped'):
        raise TariffError('Претензия недоступна для этого заказа.')
    if TariffV2Claim.query.filter_by(active_order_id=order.id).first():
        raise TariffError('По заказу уже есть открытая претензия.')
    claim = TariffV2Claim(order_id=order.id, active_order_id=order.id, opened_by=role,
        explanation=explanation(text), opened_at=stored_utc(at), status='waiting')
    db.session.add(claim)
    audit('claim_opened', order.id, {'opened_by': role}, f'{role}:{user_id}')
    return claim


def respond_claim(claim_id, role, user_id, agrees, text, at=None):
    claim = db.session.get(TariffV2Claim, claim_id)
    if not claim:
        raise TariffError('Претензия не найдена.')
    order = lock_order(claim.order_id)
    db.session.refresh(claim, with_for_update=True)
    if role not in ('buyer', 'seller') or role == claim.opened_by or (order.checkout.buyer_id if role == 'buyer' else order.seller_id) != user_id:
        raise TariffError('Ответить может только другая сторона заказа.')
    if claim.status != 'waiting' or not isinstance(agrees, bool):
        raise TariffError('Претензия уже рассмотрена.')
    claim.response = explanation(text)
    claim.responded_at = stored_utc(at)
    if agrees:
        claim.status = 'approved'
        claim.active_order_id = None
        claim.decided_at = stored_utc(at)
        refund_order(order, 'confirmed_non_delivery', f'claim:{claim.id}', at)
    else:
        claim.status = 'disputed'
    audit('claim_response', claim.id, {'agrees': agrees}, f'{role}:{user_id}')
    return claim


def decide_claim(claim_id, approved, text, actor, at=None):
    claim = db.session.get(TariffV2Claim, claim_id)
    if not claim:
        raise TariffError('Претензия не найдена.')
    order = lock_order(claim.order_id)
    db.session.refresh(claim, with_for_update=True)
    if claim.status not in ('waiting', 'disputed') or not isinstance(approved, bool):
        raise TariffError('Претензия уже рассмотрена.')
    if claim.status == 'waiting' and utc(at) < utc(claim.opened_at) + timedelta(days=3):
        raise TariffError('Срок ответа второй стороны еще не истек.')
    claim.admin_explanation = explanation(text)
    claim.decided_at = stored_utc(at)
    claim.status = 'approved' if approved else 'rejected'
    claim.active_order_id = None
    if approved:
        refund_order(order, 'confirmed_non_delivery', f'claim:{claim.id}', at)
    audit('claim_decided', claim.id, {'approved': approved, 'explanation': claim.admin_explanation}, actor)
    return claim


def require_simulator():
    if not (current_app.config.get('TARIFFS_V2_ENABLED') and current_app.config.get('TARIFFS_V2_SANDBOX') and db.engine.dialect.name == 'sqlite'):
        raise TariffError('Симулятор доступен только в локальной SQLite-песочнице.')


def simulate_refund(refund_id, succeeded, actor, at=None):
    require_simulator()
    refund = db.session.get(TariffV2CheckoutRefund, refund_id)
    if not refund or not refund.is_test:
        raise TariffError('Тестовый возврат не найден.')
    row = confirm_checkout_refund(refund.id, f'sandbox-simulation:refund:{refund.id}', refund.amount, succeeded, at)
    audit('refund_result_simulated', row.id, {'succeeded': succeeded}, actor)
    return row


def simulate_invoice_payment(invoice_id, seller_id, at=None):
    require_simulator()
    from app.utils.tariffs_v2 import confirm_invoice_payment
    invoice = db.session.get(TariffV2Invoice, invoice_id)
    if not invoice or invoice.seller_id != seller_id:
        raise TariffError('Счет недоступен продавцу.')
    db.session.query(Seller).filter_by(id=seller_id).with_for_update().one()
    row = confirm_invoice_payment(invoice.id, invoice.amount, f'sandbox-simulation:invoice:{invoice.id}', at)
    audit('invoice_payment_simulated', invoice.id, {'amount': str(invoice.amount)}, f'seller:{seller_id}')
    return row


def run_finance_tick(at=None):
    require_simulator()
    from app.utils.tariffs_v2 import issue_monthly_invoice
    refunds = process_shipping_overdue(at)
    invoices = [invoice for seller_id, in db.session.query(Seller.id).order_by(Seller.id).all()
                if (invoice := issue_monthly_invoice(seller_id, at)) is not None]
    audit('finance_tick', 'sandbox', {'refunds_checked': len(refunds), 'invoices_checked': len(invoices)})
    return {'refunds_checked': len(refunds), 'invoices_checked': len(invoices)}
