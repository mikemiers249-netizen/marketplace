"""Provider-backed bill payment in the isolated test flow; signed callback only."""
from datetime import timedelta
import hmac
import json
import secrets

from flask import current_app

from app import db
from app.models.users import Seller
from app.models.tariffs_v2 import TariffV2Invoice, TariffV2GatewayNumber, TariffV2Audit
from app.models.tariffs_v2_operations import TariffV2BillPayment
from app.utils.tariffs_v2 import TariffError, stored_utc, utc, money, confirm_invoice_payment
from app.utils.tariffs_v2_payments import require_test_adapter, allocate_gateway_number, verify_result

FLOW = 'tariffs_v2_bill_test'


def issue_bill_payment(invoice_id, seller_id, at=None):
    require_test_adapter()
    instant = utc(at)
    db.session.query(Seller).filter_by(id=seller_id).with_for_update().one()
    invoice = TariffV2Invoice.query.filter_by(id=invoice_id, seller_id=seller_id).with_for_update().first()
    if not invoice or invoice.amount <= 0 or invoice.paid_at:
        raise TariffError('Счет недоступен для оплаты.')
    previous = TariffV2BillPayment.query.filter_by(active_invoice_id=invoice.id).with_for_update().first()
    login = current_app.config['TARIFFS_V2_ROBOKASSA_TEST_LOGIN']
    if previous:
        if utc(previous.expires_at) > instant and previous.amount == invoice.amount and previous.merchant_login == login:
            return previous
        previous.status = 'expired'
        previous.active_invoice_id = None
        db.session.flush()
    # Same Receipt encoding/signing contract as the verified working checkout.
    amount = money(invoice.amount)
    receipt = json.dumps({'items': [{'name': f'Доступ к площадке: счет №{invoice.id} за {invoice.period:%m.%Y}',
        'quantity': 1, 'sum': float(amount), 'cost': float(amount), 'tax': 'none',
        'payment_method': 'full_payment', 'payment_object': 'service'}]}, ensure_ascii=False, separators=(',', ':'))
    row = TariffV2BillPayment(invoice_id=invoice.id, active_invoice_id=invoice.id, seller_id=seller_id,
        merchant_login=login, amount=amount, token=secrets.token_hex(24), receipt_json=receipt,
        status='pending', is_test=True, created_at=stored_utc(instant), expires_at=stored_utc(instant + timedelta(minutes=30)))
    db.session.add(row)
    db.session.flush()
    row.gateway_id = allocate_gateway_number('bill', row.id)
    db.session.add(TariffV2Audit(actor=f'seller:{seller_id}', action='bill_payment_created',
        subject=str(row.id), details={'invoice_id': invoice.id, 'amount': str(amount), 'is_test': True}))
    return row


def accept_bill_result(data, at=None):
    number_id, amount, token = verify_result(data, FLOW)
    number = db.session.get(TariffV2GatewayNumber, number_id)
    row = db.session.get(TariffV2BillPayment, number.reference_id) if number and number.purpose == 'bill' else None
    if (not row or row.gateway_id != number_id or not row.is_test or row.amount != amount or
            row.merchant_login != current_app.config['TARIFFS_V2_ROBOKASSA_TEST_LOGIN'] or not hmac.compare_digest(row.token, token)):
        raise TariffError('Оплата не соответствует тестовому счету продавца.')
    db.session.query(Seller).filter_by(id=row.seller_id).with_for_update().one()
    invoice = TariffV2Invoice.query.filter_by(id=row.invoice_id).with_for_update().one()
    db.session.refresh(row, with_for_update=True)
    if row.paid_at:
        return row
    row.paid_at = stored_utc(at)
    row.active_invoice_id = None
    if invoice.seller_id != row.seller_id or invoice.amount != amount or invoice.paid_at:
        row.status, row.review_reason = 'needs_review', 'bill_changed_or_already_paid'
    else:
        # A confirmed late payment still settles the frozen bill. Expiration only
        # prevents a new browser attempt; money already received is never ignored.
        confirm_invoice_payment(invoice.id, amount, f'v2-bill-test:{row.id}:{row.token}', at)
        row.status = 'paid'
        for other in TariffV2BillPayment.query.filter_by(invoice_id=invoice.id, status='pending').with_for_update():
            if other.id != row.id:
                other.status = 'expired'
                other.active_invoice_id = None
    db.session.add(TariffV2Audit(actor='system:robokassa_test', action='bill_payment_result',
        subject=str(row.id), details={'status': row.status, 'invoice_id': invoice.id}))
    return row
