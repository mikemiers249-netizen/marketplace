"""Robokassa hosted checkout, restricted to the configured seller.

Only a signed ResultURL notification confirms payment, never the browser return.
"""
import hashlib
import hmac
from datetime import datetime
from decimal import Decimal, InvalidOperation
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from app import csrf, db
from app.models.orders import Order
from app.models.payments import RobokassaPayment
from app.models.users import Buyer

bp = Blueprint('payments', __name__, url_prefix='/payments/robokassa')
GATEWAY = 'https://auth.robokassa.ru/Merchant/Index.aspx'


def money(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0 or number > Decimal('999999999999.99'):
        raise ValueError('Invalid payment amount')
    rounded = number.quantize(Decimal('0.01'))
    if number != rounded:
        raise ValueError('Invalid precision')
    return rounded


def digest(value):
    algorithm = current_app.config['ROBOKASSA_HASH_ALGORITHM'].lower()
    if algorithm not in {'md5', 'sha256', 'sha512'}:
        raise ValueError('Unsupported Robokassa hash algorithm')
    return hashlib.new(algorithm, value.encode('utf-8')).hexdigest()


def password(number, is_test):
    prefix = 'ROBOKASSA_TEST_PASSWORD' if is_test else 'ROBOKASSA_PASSWORD'
    return current_app.config.get(prefix + str(number), '')


def configured():
    config = current_app.config
    test = config['ROBOKASSA_TEST_MODE']
    return bool(config['ROBOKASSA_ENABLED'] and config['ROBOKASSA_MERCHANT_LOGIN']
                and config['ROBOKASSA_SELLER_EMAIL'] and password(1, test) and password(2, test))


def eligible(order):
    email = current_app.config['ROBOKASSA_SELLER_EMAIL'].strip().casefold()
    return bool(email and order.seller and (order.seller.email or '').strip().casefold() == email)


@bp.app_context_processor
def payment_context():
    return {'robokassa_can_pay': lambda order: configured() and eligible(order)
            and order.status == 'processing' and not test_paid(order),
            'robokassa_test_paid': test_paid}


def test_paid(order):
    return current_app.config['ROBOKASSA_TEST_MODE'] and any(
        invoice.is_test and invoice.status == 'paid' for invoice in order.robokassa_payments)


def checkout_page():
    if not isinstance(current_user, Buyer):
        abort(403)
    query = Order.query.filter_by(buyer_id=current_user.id, status='processing')
    order_id = request.args.get('order_id', type=int)
    if order_id:
        orders = [query.filter_by(id=order_id).first_or_404()]
    else:
        orders = query.order_by(Order.created_at.desc()).all()
    return render_template('main/robokassa_checkout.html', orders=orders,
                           ready=configured(), eligible=eligible, test_paid=test_paid,
                           is_test=current_app.config['ROBOKASSA_TEST_MODE'])


def start_payment():
    if not isinstance(current_user, Buyer):
        abort(403)
    if not configured():
        abort(503, description='Онлайн-оплата пока не подключена.')
    order_id = request.form.get('order_id', type=int)
    # Serialize invoice creation for the same order, including concurrent clicks.
    order = Order.query.filter_by(id=order_id, buyer_id=current_user.id).with_for_update().first_or_404()
    if not eligible(order):
        abort(403)
    if order.status != 'processing':
        abort(409, description='Этот заказ сейчас нельзя оплатить.')
    try:
        amount = money(order.grand_total)
    except (ValueError, InvalidOperation):
        abort(409, description='Проверьте сумму заказа.')
    is_test = current_app.config['ROBOKASSA_TEST_MODE']
    invoice = RobokassaPayment.query.filter_by(order_id=order.id, is_test=is_test).first()
    if invoice and (invoice.amount != amount or invoice.status != 'pending'):
        abort(409, description='Счёт изменён или уже оплачен. Обратитесь в поддержку.')
    if not invoice:
        invoice = RobokassaPayment(order=order, amount=amount, is_test=is_test)
        db.session.add(invoice)
        db.session.flush()
    out_sum = format(amount, '.2f')
    login = current_app.config['ROBOKASSA_MERCHANT_LOGIN']
    signature = digest(f'{login}:{out_sum}:{invoice.id}:{password(1, is_test)}')
    params = {'MerchantLogin': login, 'OutSum': out_sum, 'InvId': str(invoice.id),
              'Description': f'Оплата заказа {order.order_number}'[:100],
              'SignatureValue': signature, 'Culture': 'ru', 'Encoding': 'utf-8'}
    if is_test:
        params['IsTest'] = '1'
    db.session.commit()
    return render_template('main/robokassa_redirect.html', gateway=GATEWAY, params=params)


def notification_data():
    data = request.form if request.method == 'POST' else request.args
    for key in ('InvId', 'OutSum', 'SignatureValue'):
        if len(data.getlist(key)) != 1:
            abort(400)
    if any(key.lower().startswith('shp_') for key in data):
        abort(400)
    inv_id = data['InvId']
    if not inv_id.isascii() or not inv_id.isdigit() or len(inv_id) > 10:
        abort(400)
    invoice = db.session.get(RobokassaPayment, int(inv_id))
    if not invoice:
        abort(404)
    try:
        amount = money(data['OutSum'])
    except (ValueError, InvalidOperation):
        abort(400)
    if amount != invoice.amount:
        abort(400)
    return data, invoice


@bp.route('/result', methods=['POST'])
@csrf.exempt
def result():
    data, invoice = notification_data()
    secret = password(2, invoice.is_test)
    if not secret:
        abort(503)
    expected = digest(f"{data['OutSum']}:{data['InvId']}:{secret}")
    if not hmac.compare_digest(expected.upper(), data['SignatureValue'].upper()):
        abort(400)
    order = Order.query.filter_by(id=invoice.order_id).with_for_update().one()
    # Refresh after the order lock to see an earlier concurrent notification.
    db.session.refresh(invoice)
    if invoice.paid_at is None:
        invoice.paid_at = datetime.utcnow()
        invoice.status = 'paid'
        if not invoice.is_test:
            if order.status == 'processing' and Decimal(str(order.grand_total)) == invoice.amount:
                order.status = 'paid'
            else:
                invoice.status = 'needs_review'
                current_app.logger.error('Paid Robokassa invoice %s requires review for order %s',
                                         invoice.id, order.id)
        db.session.commit()
    return f"OK{data['InvId']}", 200, {'Content-Type': 'text/plain; charset=utf-8'}


@bp.route('/success')
@login_required
def success():
    data, invoice = notification_data()
    if not isinstance(current_user, Buyer) or invoice.order.buyer_id != current_user.id:
        abort(404)
    secret = password(1, invoice.is_test)
    expected = digest(f"{data['OutSum']}:{data['InvId']}:{secret}")
    if not secret or not hmac.compare_digest(expected.upper(), data['SignatureValue'].upper()):
        abort(400)
    if invoice.is_test:
        flash('Тестовый платёж: реальные деньги не списывались, заказ не отмечен оплаченным.', 'info')
    elif invoice.status == 'paid':
        flash('Оплата заказа подтверждена.', 'success')
    elif invoice.status == 'needs_review':
        flash('Платёж получен. Для проверки заказа свяжитесь с поддержкой.', 'warning')
    else:
        flash('Ожидаем подтверждение оплаты. Обновите список заказов через несколько секунд.', 'info')
    return redirect(url_for('main.profile_orders'))


@bp.route('/fail')
@login_required
def fail():
    # A browser return is untrusted and must never cancel a paid invoice/order.
    flash('Оплата не завершена. Проверить заказ и повторить оплату можно в списке заказов.', 'info')
    return redirect(url_for('main.profile_orders'))
