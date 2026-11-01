"""Sandbox checkout routes, isolated from production orders and cart."""
import secrets
from flask import Blueprint, abort, current_app, flash, render_template, request, redirect, url_for, jsonify
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app import csrf, db
from app.models.users import Buyer, Seller, BuyerDelivery

from app.models.products import Product
from app.models.tariffs_v2_commerce import TariffV2Listing, TariffV2Cart, TariffV2Checkout, TariffV2SandboxOrder
from app.models.tariffs_v2_finance import TariffV2Cancellation, TariffV2CheckoutRefund
from app.utils.tariffs_v2 import TariffError, quote_line, utc
from app.utils.tariffs_v2_commerce import (
    CHECKOUT_FLOW, IncompleteDelivery, accept_checkout_result, create_checkout, is_visible, set_cart_quantity, ship_order,
)
from app.utils.tariffs_v2_payments import configured, payment_params

bp = Blueprint('tariffs_v2_commerce', __name__, url_prefix='/tariffs-v2')


@bp.before_request
def sandbox_only():
    if not (current_app.config.get('TARIFFS_V2_ENABLED') and current_app.config.get('TARIFFS_V2_SANDBOX')):
        abort(404)


@bp.get('/catalog')
def catalog():
    page = request.args.get('page', 1, type=int)
    if page < 1:
        abort(404)
    products = Product.query.join(TariffV2Listing, TariffV2Listing.product_id == Product.id).filter(
        TariffV2Listing.published.is_(True), Product.status == 'approved').order_by(Product.id).paginate(
            page=page, per_page=24, error_out=False)
    instant = utc()
    rows = [dict(product=product, quote=quote_line(product, 1, instant))
            for product in products.items if is_visible(product, instant)]
    return render_template('main/tariffs_v2_catalog.html', rows=rows, pagination=products,
                           buyer_mode=current_user.is_authenticated and isinstance(current_user, Buyer))


def require_role(role):
    if not current_user.is_authenticated or not isinstance(current_user, role):
        abort(403)


@bp.errorhandler(TariffError)
def invalid(error):
    db.session.rollback()
    return jsonify(error=str(error)), 400


@bp.errorhandler(IncompleteDelivery)
def incomplete_delivery(error):
    db.session.rollback()
    flash(str(error), 'warning')
    return redirect('/profile?section=addresses')


@bp.errorhandler(IntegrityError)
def conflict(error):
    db.session.rollback()
    return jsonify(error='Данные изменяются другим запросом. Обновите страницу.'), 409


@bp.post('/cart/<int:product_id>')
def cart_update(product_id):
    require_role(Buyer)
    set_cart_quantity(current_user.id, product_id, request.form.get('quantity'))
    db.session.commit()
    return redirect(url_for('tariffs_v2_commerce.cart'))


@bp.get('/cart')
def cart():
    require_role(Buyer)
    items = TariffV2Cart.query.filter_by(buyer_id=current_user.id).order_by(TariffV2Cart.product_id).all()
    rows = []
    sellers = {}
    for item in items:
        try:
            quote = quote_line(item.product, item.quantity)
            error = None if is_visible(item.product) else 'Товар больше недоступен.'
        except TariffError as exc:
            quote, error = None, str(exc)
        rows.append(dict(item=item, quote=quote, error=error))
        if not item.product.digital_book_id:
            sellers[item.product.seller_id] = item.product.seller
    addresses = BuyerDelivery.query.filter_by(buyer_id=current_user.id).order_by(BuyerDelivery.id).all()
    return render_template('main/tariffs_v2_cart.html', rows=rows, sellers=sellers, addresses=addresses,
                           request_key=secrets.token_hex(24))


@bp.post('/checkout')
def checkout():
    require_role(Buyer)
    deliveries = {}
    for key in request.form:
        if key.startswith('delivery_'):
            try:
                deliveries[int(key[9:])] = int(request.form[key])
            except ValueError:
                raise TariffError('Выберите адрес доставки.') from None
    row = create_checkout(current_user.id, request.form.get('request_key'), deliveries)
    db.session.commit()
    return redirect(url_for('tariffs_v2_commerce.checkout_view', checkout_id=row.id))


@bp.get('/checkouts/<int:checkout_id>')
def checkout_view(checkout_id):
    require_role(Buyer)
    row = TariffV2Checkout.query.filter_by(id=checkout_id, buyer_id=current_user.id).first_or_404()
    params = None
    if row.online_amount > 0 and row.status == 'pending' and utc() < utc(row.expires_at) and configured():
        params = payment_params(row, CHECKOUT_FLOW, f'Тестовый заказ площадки #{row.id}')
    line_ids = [line.id for order in row.orders for line in order.lines]
    cancelled = {item.line_id: item.quantity for item in TariffV2Cancellation.query.filter(TariffV2Cancellation.line_id.in_(line_ids))}
    refunds = TariffV2CheckoutRefund.query.filter(TariffV2CheckoutRefund.line_id.in_(line_ids)).order_by(TariffV2CheckoutRefund.id).all()
    return render_template('main/tariffs_v2_checkout.html', checkout=row, params=params, cancelled=cancelled, refunds=refunds)


@bp.post('/checkout/robokassa/result')
@csrf.exempt
def result():
    row = accept_checkout_result(request.form)
    db.session.commit()
    return f'OK{row.gateway_id}', 200, {'Content-Type': 'text/plain; charset=utf-8'}


@bp.get('/seller/orders')
def seller_orders():
    require_role(Seller)
    orders = TariffV2SandboxOrder.query.filter_by(seller_id=current_user.id).filter(
        TariffV2SandboxOrder.status.in_(('ready', 'shipped', 'received', 'cancelled'))).order_by(
            TariffV2SandboxOrder.id.desc()).limit(100).all()
    line_ids = [line.id for order in orders for line in order.lines]
    cancelled = {item.line_id: item.quantity for item in TariffV2Cancellation.query.filter(TariffV2Cancellation.line_id.in_(line_ids))}
    return render_template('main/tariffs_v2_orders.html', orders=orders, cancelled=cancelled)


@bp.post('/seller/orders/<int:order_id>/shipped')
def shipped(order_id):
    require_role(Seller)
    ship_order(order_id, current_user.id, request.form.get('tracking'))
    db.session.commit()
    return redirect(url_for('tariffs_v2_commerce.seller_orders'))
