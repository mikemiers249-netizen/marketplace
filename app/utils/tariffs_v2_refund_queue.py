"""Durable dispatch lifecycle. Public jobs remain disconnected until release.

Dispatch/poll own commits: 'sending' is durable BEFORE POST. A crash/timeout
cannot silently turn into another POST. Only a verified finished state books debt.
"""
from datetime import timedelta
import json

from flask import current_app

from app import db
from app.models.tariffs_v2 import TariffV2Audit
from app.models.tariffs_v2_finance import TariffV2CheckoutRefund
from app.models.tariffs_v2_operations import TariffV2RefundDispatch
from app.utils.tariffs_v2 import TariffError, stored_utc, utc
from app.utils.tariffs_v2_commerce import CHECKOUT_FLOW
from app.utils.tariffs_v2_finance import lock_order, confirm_checkout_refund
from app.utils.tariffs_v2_provider import (
    ProviderError, RefundRejected, validated_receipt_items, transport,
)


def enqueue_refund(refund_id, at=None):
    refund = db.session.get(TariffV2CheckoutRefund, refund_id)
    if not refund:
        raise TariffError('Возврат не найден.')
    order = lock_order(refund.line.order_id)
    db.session.refresh(refund, with_for_update=True)
    from app.utils.tariffs_v2_receipts import refund_receipt_items
    receipt_items = refund_receipt_items(refund)
    normalized = json.loads('[' + ','.join(validated_receipt_items(receipt_items, refund.amount)) + ']')
    existing = TariffV2RefundDispatch.query.filter_by(refund_id=refund.id).first()
    if existing:
        if existing.receipt_items != normalized:
            raise TariffError('Номенклатура уже поставленного в очередь возврата изменилась.')
        return existing
    checkout = order.checkout
    if (refund.status != 'pending' or not checkout.gateway_id or not checkout.paid_at or
            refund.is_test != checkout.is_test or refund.amount > checkout.online_amount):
        raise TariffError('Возврат не связан с подтвержденным онлайн-платежом.')
    row = TariffV2RefundDispatch(refund_id=refund.id, checkout_id=checkout.id,
        gateway_id=checkout.gateway_id, merchant_login=checkout.merchant_login,
        payment_amount=checkout.online_amount, amount=refund.amount,
        payment_flow=CHECKOUT_FLOW if checkout.is_test else 'tariffs_v2_checkout',
        payment_token=checkout.token, receipt_items=normalized, is_test=checkout.is_test,
        status='queued', created_at=stored_utc(at))
    db.session.add(row)
    db.session.flush()
    record('refund_enqueued', row, {})
    return row


def record(action, row, details):
    db.session.add(TariffV2Audit(actor='system:refund_queue', action=action,
        subject=str(row.id), details=details))


def locked(dispatch_id):
    row = db.session.get(TariffV2RefundDispatch, dispatch_id)
    if not row:
        raise TariffError('Запись очереди не найдена.')
    lock_order(row.refund.line.order_id)
    db.session.refresh(row.refund, with_for_update=True)
    db.session.refresh(row, with_for_update=True)
    return row


def authorized(row, client, allow_simulated):
    checkout = row.refund.line.order.checkout
    if (row.checkout_id != checkout.id or row.gateway_id != checkout.gateway_id or
            row.payment_amount != checkout.online_amount or row.amount != row.refund.amount or
            row.merchant_login != checkout.merchant_login or row.payment_token != checkout.token or
            row.is_test != checkout.is_test or row.is_test != row.refund.is_test or
            row.merchant_login != client.credentials.merchant):
        raise TariffError('Реквизиты возврата не соответствуют сохраненному платежу.')
    if row.is_test:
        if not (allow_simulated and current_app.config.get('TARIFFS_V2_SANDBOX') and
                db.engine.dialect.name == 'sqlite' and client.send is not transport):
            raise TariffError('Тестовый возврат нельзя отправить в рабочий API.')
    elif not (current_app.config.get('TARIFFS_V2_REFUND_API_ENABLED') and
              current_app.config.get('TARIFFS_V2_LIVE_OPERATIONS_ENABLED') and not allow_simulated):
        raise TariffError('Рабочая отправка возвратов еще не включена.')


def dispatch_refund(dispatch_id, client, at=None, *, allow_simulated=False):
    row = locked(dispatch_id)
    authorized(row, client, allow_simulated)
    if row.status == 'sending':
        if row.sending_at and utc(at) >= utc(row.sending_at) + timedelta(minutes=2):
            row.status, row.error_code = 'unknown', 'interrupted_submission'
            record('refund_submission_unknown', row, {})
            db.session.commit()
        else:
            db.session.commit()
        return row
    if row.status != 'queued':
        db.session.commit()
        return row
    if row.refund.status != 'pending':
        raise TariffError('Исходный возврат уже завершен.')
    try:
        operation = client.operation(row.gateway_id, row.payment_amount,
            {'Shp_flow': row.payment_flow, 'Shp_token': row.payment_token})
        client.refund_token(operation, row.amount, row.receipt_items)
    except ProviderError:
        row.error_code, row.checked_at = 'operation_not_verified', stored_utc(at)
        db.session.commit()
        return row
    row.operation_key, row.status, row.sending_at = operation, 'sending', stored_utc(at)
    row.error_code = None
    record('refund_submission_started', row, {})
    db.session.commit()
    try:
        identifier = client.create_refund(row.operation_key, row.amount, row.receipt_items)
    except RefundRejected:
        row = locked(dispatch_id)
        row.status, row.error_code = 'rejected', 'provider_rejected'
        confirm_checkout_refund(row.refund_id, f'robokassa-rejected:{row.id}', row.amount, False, at)
        record('refund_submission_rejected', row, {})
    except Exception:
        # Even a local crash after sending must park this request, not resend.
        row = locked(dispatch_id)
        row.status, row.error_code = 'unknown', 'submission_outcome_unknown'
        record('refund_submission_unknown', row, {})
    else:
        row = locked(dispatch_id)
        row.provider_request_id, row.status = identifier, 'submitted'
        record('refund_submitted', row, {'request_id': identifier})
    row.checked_at = stored_utc(at)
    db.session.commit()
    return row


def poll_refund(dispatch_id, client, at=None, *, allow_simulated=False):
    row = locked(dispatch_id)
    authorized(row, client, allow_simulated)
    if row.status != 'submitted' or not row.provider_request_id:
        db.session.commit()
        return row
    if row.refund.status != 'pending':
        raise TariffError('Возврат был завершен другим механизмом; требуется сверка.')
    try:
        status = client.refund_state(row.provider_request_id, row.amount)
    except ProviderError:
        row.error_code, row.checked_at = 'status_check_failed', stored_utc(at)
        db.session.commit()
        return row
    row.checked_at, row.error_code = stored_utc(at), None
    if status in ('finished', 'canceled'):
        confirm_checkout_refund(row.refund_id, row.provider_request_id, row.amount, status == 'finished', at)
        row.status = status
        record('refund_provider_result', row, {'state': status, 'amount': str(row.amount)})
    db.session.commit()
    return row


def stage_pending_refunds(at=None):
    counts = {'refunds_enqueued': 0, 'refunds_blocked': 0}
    rows = TariffV2CheckoutRefund.query.filter_by(status='pending').order_by(TariffV2CheckoutRefund.id).all()
    for refund in rows:
        if TariffV2RefundDispatch.query.filter_by(refund_id=refund.id).first():
            continue
        try:
            enqueue_refund(refund.id, at)
            counts['refunds_enqueued'] += 1
        except TariffError:
            counts['refunds_blocked'] += 1
    return counts
