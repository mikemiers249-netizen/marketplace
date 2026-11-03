"""Robokassa hosted checkout, restricted to the configured seller.

Only a signed ResultURL notification confirms payment, never the browser return.
"""
import hashlib
import hmac
import json
from urllib.parse import quote
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from app import csrf, db
from app.models.orders import Order, CartItem, Bonus
from app.models.payments import RobokassaPayment, RobokassaCheckout
from app.models.products import Product
from app.models.users import Buyer

bp = Blueprint('payments', __name__, url_prefix='/payments/robokassa')
GATEWAY = 'https://auth.robokassa.ru/Merchant/Index.aspx'
PRIVATE_STATUSES = ('awaiting_payment', 'payment_expired', 'payment_demo')
RESERVATION_TIME = timedelta(minutes=30)


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
    from app.utils.private_payment_tests import PRIVATE_SKUS
    if any(item.product and item.product.system_sku in PRIVATE_SKUS for item in order.items.all()):
        return False
    return bool(email and order.seller and not order.seller.payments_paused
                and (order.seller.email or '').strip().casefold() == email)


@bp.app_context_processor
def payment_context():
    return {'robokassa_can_pay': lambda order: configured() and eligible(order)
            and order.status in ('awaiting_payment', 'processing') and not test_paid(order) and not order.payment_confirmed,
            'robokassa_test_paid': test_paid}


def test_paid(order):
    return current_app.config['ROBOKASSA_TEST_MODE'] and any(
        invoice.is_test and invoice.status == 'paid' for invoice in order.robokassa_payments)


def checkout_page():
    if not isinstance(current_user, Buyer):
        abort(403)
    query = Order.query.filter_by(buyer_id=current_user.id).filter(
        Order.status.in_(('awaiting_payment', 'processing', 'payment_demo')))
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
    invoice = prepare_invoice(order)
    db.session.commit()
    return render_template('main/robokassa_redirect.html', gateway=GATEWAY,
                           params=gateway_params(invoice))


def prepare_invoice(order):
    """Called inside the checkout transaction, before it can become an order."""
    if not configured() or not eligible(order):
        abort(503)
    if order.status not in ('awaiting_payment', 'processing') or order.payment_confirmed:
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
    if not invoice.receipt_json:
        invoice.receipt_json = build_receipt(order, amount)
    return invoice


def build_receipt(order, amount):
    """Freeze actual order lines, allocating order discounts in whole kopecks."""
    def cents(value):
        value = Decimal(str(value or 0))
        if not value.is_finite() or value < 0:
            abort(409, description='Проверьте суммы позиций заказа.')
        return int(value.quantize(Decimal('0.01')) * 100)

    lines = order.items.order_by('id').all()
    if not lines and len(order.digital_purchases) == 1:
        # Legacy direct book purchases may predate a linked catalogue product.
        purchase = order.digital_purchases[0]
        lines = [SimpleNamespace(quantity=1, price_at_order=order.total_price,
                 product=SimpleNamespace(name=purchase.title, digital_book_id=purchase.book_id))]
    weights = []
    for line in lines:
        if not line.product or line.quantity <= 0 or not (line.product.name or '').strip():
            abort(409, description='Не удалось сформировать состав чека.')
        weights.append(cents(Decimal(str(line.price_at_order)) * line.quantity))
    subtotal = sum(weights)
    delivery = cents(order.delivery_price)
    goods = cents(amount) - delivery
    if not lines or subtotal <= 0 or goods < 0 or goods > subtotal or subtotal != cents(order.total_price):
        abort(409, description='Состав чека не совпадает с суммой заказа.')
    # Largest remainders preserve the exact total even for a one-kopeck discount.
    allocated = [weight * goods // subtotal for weight in weights]
    remainder = goods - sum(allocated)
    ranked = sorted(range(len(lines)), key=lambda i: (-(weights[i] * goods % subtotal), i))
    for i in ranked[:remainder]:
        allocated[i] += 1
    items = []
    for line, line_sum in zip(lines, allocated):
        digital = bool(line.product.digital_book_id)
        item = {'name': ' '.join(line.product.name.split())[:128],
                'quantity': line.quantity, 'sum': line_sum / 100,
                'payment_method': 'full_payment' if digital else 'full_prepayment',
                'payment_object': 'intellectual_activity' if digital else 'commodity',
                'tax': 'none'}
        if line_sum % line.quantity == 0:
            item['cost'] = (line_sum // line.quantity) / 100
        items.append(item)
    if delivery:
        items.append({'name': 'Доставка', 'quantity': 1, 'sum': delivery / 100,
                      'cost': delivery / 100, 'payment_method': 'full_prepayment',
                      'payment_object': 'service', 'tax': 'none'})
    # Current integration accepts only the configured seller, without VAT.
    # SNO is deliberately inherited from that merchant's Robokassa account.
    return json.dumps({'items': items}, ensure_ascii=False, separators=(',', ':'))


def gateway_params(invoice):
    out_sum = format(invoice.amount, '.2f')
    is_test = invoice.is_test
    login = current_app.config['ROBOKASSA_MERCHANT_LOGIN']
    if not invoice.receipt_json:
        abort(409, description='Для счёта не сформирована номенклатура чека.')
    receipt = quote(invoice.receipt_json, safe='')
    signature = digest(f'{login}:{out_sum}:{invoice.id}:{receipt}:{password(1, is_test)}')
    params = {'MerchantLogin': login, 'OutSum': out_sum, 'InvId': str(invoice.id),
              'Description': f'Оплата заказа {invoice.order.order_number}'[:100],
              'SignatureValue': signature, 'Receipt': receipt, 'Culture': 'ru', 'Encoding': 'utf-8'}
    if is_test:
        params['IsTest'] = '1'
    return params


def gateway_url(invoice):
    if not eligible(invoice.order):
        abort(409, description='Магазин временно приостановил приём оплаты.')
    return url_for('payments.checkout', invoice_id=invoice.id)


@bp.before_app_request
def protect_private_test_links():
    from app.utils.private_payment_tests import private_view_allowed, PRIVATE_SKUS
    from app.models.digital import DigitalBook
    if request.endpoint == 'payments.result' or private_view_allowed():
        return
    args = request.view_args or {}
    product_id = args.get('product_id')
    if request.endpoint == 'main.cart_add':
        product_id = request.form.get('product_id', type=int)
    book_id = args.get('book_id')
    for model, identity in ((Product, product_id), (DigitalBook, book_id)):
        row = db.session.get(model, identity) if identity else None
        if row and row.system_sku in PRIVATE_SKUS:
            abort(404)


@bp.after_app_request
def protect_private_test_cache(response):
    from app.utils.private_payment_tests import allowed_buyer
    if allowed_buyer():
        response.headers['Cache-Control'] = 'private, no-store'
        response.vary.add('Cookie')
    return response


@bp.get('/private-tests')
def private_test_page():
    abort(404)


@bp.post('/private-tests/<kind>/buy')
def private_test_buy(kind):
    abort(404)


@bp.route('/checkout/<int:invoice_id>')
@login_required
def checkout(invoice_id):
    invoice = db.session.get(RobokassaPayment, invoice_id)
    if not invoice or not isinstance(current_user, Buyer) or invoice.order.buyer_id != current_user.id:
        abort(404)
    if invoice.status != 'pending' or invoice.order.status not in ('awaiting_payment', 'processing'):
        flash('Эта оплата уже завершена или срок её действия истёк.', 'info')
        return redirect(url_for('main.order_review'))
    if not eligible(invoice.order):
        abort(409, description='Магазин временно приостановил приём оплаты.')
    if not configured():
        abort(503)
    if not invoice.receipt_json:
        invoice.receipt_json = build_receipt(invoice.order, invoice.amount)
        db.session.commit()
    return render_template('main/robokassa_redirect.html', gateway=GATEWAY,
                           params=gateway_params(invoice))


def release_reservation(order, status='payment_expired'):
    """Return stock, bonus holds and promo/review rewards without seller penalties."""
    if order.status != 'awaiting_payment':
        return
    items = order.items.all()
    products = {p.id: p for p in Product.query.filter(
        Product.id.in_([i.product_id for i in items])).order_by(Product.id)
        .populate_existing().with_for_update().all()}
    for item in items:
        if item.product_id in products and not products[item.product_id].digital_book_id:
            products[item.product_id].stock_quantity += item.quantity
    from app.utils.loyalty import _add_balance
    spent = Bonus.query.filter_by(order_id=order.id, type='spent', seller_id=order.seller_id).all()
    refund = -sum(float(entry.amount) for entry in spent)
    if refund > 0:
        _add_balance(order.buyer_id, order.seller_id, refund)
        db.session.add(Bonus(buyer_id=order.buyer_id, seller_id=order.seller_id,
                             order_id=order.id, amount=refund, type='payment_refund',
                             reason='Возврат резерва незавершённой оплаты'))
    if order.promo_code:
        order.promo_code.used_count = max(0, (order.promo_code.used_count or 0) - 1)
    from app.utils.review_rewards import restore_order_rewards
    restore_order_rewards(order)
    order.status = status
    order.canceled_at = datetime.utcnow()
    for invoice in order.robokassa_payments:
        if invoice.status == 'pending':
            invoice.status = 'expired'
        active = RobokassaCheckout.query.filter_by(invoice_id=invoice.id).first()
        if active:
            db.session.delete(active)


@bp.before_app_request
def expire_reservations():
    edit_cart = request.endpoint in ('main.cart_add', 'main.cart_update', 'main.cart_remove')
    if not edit_cart and request.endpoint not in ('main.index', 'main.catalog', 'main.product',
                                 'main.cart', 'main.order_review', 'main.checkout_submit',
                                 'main.payment', 'payments.checkout'):
        return
    expired = Order.query.filter(Order.status == 'awaiting_payment',
                                 Order.created_at < datetime.utcnow() - RESERVATION_TIME).order_by(
                                     Order.buyer_id, Order.id).limit(30).all()
    for candidate in expired:
        Buyer.query.filter_by(id=candidate.buyer_id).with_for_update().one()
        order = Order.query.filter_by(id=candidate.id).populate_existing().with_for_update().one()
        release_reservation(order)
    if expired:
        db.session.commit()
    if edit_cart and isinstance(current_user, Buyer):
        Buyer.query.filter_by(id=current_user.id).with_for_update().one()
        active = db.session.get(RobokassaCheckout, current_user.id)
        if active:
            order = Order.query.filter_by(id=active.invoice.order_id).with_for_update().one()
            release_reservation(order)
    if expired or edit_cart:
        db.session.commit()


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


def accept_paid_order(invoice, created_at=None):
    """Accept verified money even after its temporary stock reservation expired.

    Caller holds the buyer/order locks and commits all accounting together.
    """
    order = invoice.order
    if invoice.is_test or invoice.paid_at is None:
        return False
    if order.status == 'processing' and Decimal(str(order.grand_total)) == invoice.amount:
        invoice.status = 'paid'
        return False
    if order.status not in ('awaiting_payment', 'payment_expired') or Decimal(str(order.grand_total)) != invoice.amount:
        invoice.status = 'needs_review'
        current_app.logger.error('Paid Robokassa invoice %s requires review for order %s', invoice.id, order.id)
        return False
    expired = order.status == 'payment_expired'
    items = order.items.all()
    if expired:
        products = {p.id: p for p in Product.query.filter(
            Product.id.in_([i.product_id for i in items])).order_by(Product.id)
            .populate_existing().with_for_update().all()}
        for item in items:
            product = products.get(item.product_id)
            if product and not product.digital_book_id:
                product.stock_quantity -= item.quantity
                if product.stock_quantity < 0:
                    # Money was received: keep the order visible as an obligation
                    # to fulfil, rather than losing the paid order again.
                    current_app.logger.warning('Paid order %s requires replenishment for product %s', order.id, product.id)
        returned = sum(Decimal(str(row.amount)) for row in Bonus.query.filter_by(
            order_id=order.id, seller_id=order.seller_id, type='payment_refund').all())
        reclaimed = -sum(Decimal(str(row.amount)) for row in Bonus.query.filter_by(
            order_id=order.id, seller_id=order.seller_id, type='payment_reclaimed').all())
        amount = returned - reclaimed
        if amount > 0:
            from app.models.loyalty import BuyerBonus
            balance = BuyerBonus.query.filter_by(buyer_id=order.buyer_id,
                seller_id=order.seller_id).populate_existing().with_for_update().first()
            if balance is None:
                balance = BuyerBonus(buyer_id=order.buyer_id, seller_id=order.seller_id, balance=0)
                db.session.add(balance)
            # Preserve the ledger even if refunded points were used meanwhile.
            balance.balance = float(Decimal(str(balance.balance)) - amount)
            db.session.add(Bonus(buyer_id=order.buyer_id, seller_id=order.seller_id,
                order_id=order.id, amount=-float(amount), type='payment_reclaimed',
                reason='Восстановление бонусного списания по подтверждённой оплате'))
        if order.promo_code:
            order.promo_code.used_count = (order.promo_code.used_count or 0) + 1
    invoice.status = 'paid'
    order.status = 'processing'
    order.canceled_at = None
    order.created_at = created_at or invoice.paid_at
    # An expired reservation may have been replaced with a new cart/payment.
    # Its contents belong to that new attempt and must not be removed here.
    if not expired:
        for item in items:
            cart_item = CartItem.query.filter_by(buyer_id=order.buyer_id,
                product_id=item.product_id).with_for_update().first()
            if cart_item:
                cart_item.quantity -= item.quantity
                if cart_item.quantity <= 0:
                    db.session.delete(cart_item)
    from app.utils.emails import create_order_conversation
    create_order_conversation(order)
    from app.utils.digital_commerce import complete_digital_delivery
    complete_digital_delivery(order)
    current_app.logger.info('Paid order accepted: invoice=%s order=%s amount=%s status=%s created_at_utc=%s restored=%s',
        invoice.id, order.id, invoice.amount, order.status, order.created_at.isoformat(), expired)
    return True


def recover_incident_paid_orders():
    """Owner-authorized repair of two already verified, paid incident invoices."""
    for invoice_id, order_id, expected_amount, bank_time in (
            (27, 39, Decimal('1700.00'), datetime(2026, 11, 2, 14, 43, 48)),
            (28, 40, Decimal('200.00'), None)):
        invoice = db.session.get(RobokassaPayment, invoice_id)
        if not invoice:
            continue
        if (invoice.order_id != order_id or invoice.is_test or invoice.paid_at is None
                or invoice.amount != expected_amount):
            current_app.logger.error('Incident repair refused: verified invoice identity/amount differs for %s', invoice_id)
            continue
        Buyer.query.filter_by(id=invoice.order.buyer_id).with_for_update().one()
        order = Order.query.filter_by(id=order_id).populate_existing().with_for_update().one()
        db.session.refresh(invoice)
        if invoice.status == 'needs_review' and order.status == 'payment_expired':
            accepted = accept_paid_order(invoice, created_at=bank_time or invoice.paid_at)
            db.session.commit()
            if accepted:
                from app.utils.emails import send_new_order_notification_to_seller
                send_new_order_notification_to_seller(order.id)
        elif invoice.status == 'paid' and order.status not in PRIVATE_STATUSES:
            current_app.logger.info('Incident paid order already visible: invoice=%s order=%s status=%s created_at_utc=%s',
                invoice.id, order.id, order.status, order.created_at.isoformat())
            db.session.commit()
        else:
            current_app.logger.error('Incident repair requires inspection: invoice=%s invoice_status=%s order=%s order_status=%s',
                invoice.id, invoice.status, order.id, order.status)
            db.session.commit()


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
    Buyer.query.filter_by(id=invoice.order.buyer_id).with_for_update().one()
    order = Order.query.filter_by(id=invoice.order_id).populate_existing().with_for_update().one()
    # Refresh after the order lock to see an earlier concurrent notification.
    db.session.refresh(invoice)
    if invoice.paid_at is None or (not invoice.is_test and invoice.status == 'needs_review'
                                  and order.status == 'payment_expired'):
        invoice.paid_at = invoice.paid_at or datetime.utcnow()
        invoice.status = 'paid'
        accepted = False
        if invoice.is_test:
            release_reservation(order, status='payment_demo')
        else:
            accepted = accept_paid_order(invoice)
        active = RobokassaCheckout.query.filter_by(invoice_id=invoice.id).first()
        if active:
            db.session.delete(active)
        db.session.commit()
        if accepted:
            from app.utils.emails import send_new_order_notification_to_seller
            send_new_order_notification_to_seller(order.id)
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
        flash('Тестовая оплата пройдена. Деньги не списывались, заказ продавцу не передан.', 'info')
        return redirect(url_for('main.order_review'))
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
    from app.utils.buyer_errors import report
    report('Оплата не завершена')
    flash('Оплата не завершена. Повторить оплату можно на странице проверки заказа.', 'info')
    return redirect(url_for('main.order_review'))
