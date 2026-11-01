"""Seller-facing local sandbox. Only signed ResultURL can record test coverage."""
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app import csrf, db
from app.models.products import Product
from app.models.users import Seller
from app.models.tariffs_v2 import TariffV2StorageInvoice
from app.models.tariffs_v2_commerce import TariffV2Listing
from app.utils.tariffs_v2 import TariffError, storage_quote, utc
from app.utils.tariffs_v2_payments import accept_storage_result, configured, gateway_params, issue_storage_invoice
from app.utils.tariffs_v2_commerce import is_visible, publication_error, set_publication

bp = Blueprint('tariffs_v2_payments', __name__, url_prefix='/tariffs-v2')


@bp.before_request
def sandbox_only():
    if not (current_app.config.get('TARIFFS_V2_ENABLED') and current_app.config.get('TARIFFS_V2_SANDBOX')):
        abort(404)
    if request.endpoint in ('tariffs_v2_payments.result', 'tariffs_v2_payments.success', 'tariffs_v2_payments.fail'):
        return
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        abort(403)


@bp.errorhandler(TariffError)
def invalid(error):
    db.session.rollback()
    return jsonify(error=str(error)), 400


@bp.get('/seller/storage')
def storage():
    products = Product.query.filter_by(seller_id=current_user.id).filter(
        Product.status != 'deleted').order_by(Product.id).limit(100).all()
    rows = []
    for product in products:
        listing = db.session.get(TariffV2Listing, product.id)
        row = dict(product=product, quote=None, error=None, visible=is_visible(product),
                   published=bool(listing and listing.published), reason=publication_error(product))
        try:
            row['quote'] = storage_quote(product)
        except TariffError as error:
            row['error'] = str(error)
        rows.append(row)
    return render_template('main/tariffs_v2_storage.html', rows=rows, payment_ready=configured())


@bp.post('/seller/storage/<int:product_id>/publication')
def publication(product_id):
    state = request.form.get('published')
    if state not in ('0', '1'):
        raise TariffError('Укажите состояние публикации.')
    set_publication(product_id, current_user.id, state == '1')
    db.session.commit()
    return redirect(url_for('tariffs_v2_payments.storage'))


@bp.post('/seller/storage/<int:product_id>/invoice')
def invoice(product_id):
    product = Product.query.filter_by(id=product_id, seller_id=current_user.id).first_or_404()
    if product.status == 'deleted':
        abort(404)
    if not configured():
        return jsonify(error='Отдельная тестовая оплата v2 не настроена.'), 503
    try:
        row = issue_storage_invoice(product.id, current_user.id)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error='Счет уже изменяется другим запросом. Обновите страницу.'), 409
    if row is None:
        flash('Предоплата для этой карточки не требуется.', 'success')
        return redirect(url_for('tariffs_v2_payments.storage'))
    return redirect(url_for('tariffs_v2_payments.payment', invoice_id=row.id))


@bp.get('/seller/invoices/<int:invoice_id>')
def payment(invoice_id):
    row = TariffV2StorageInvoice.query.filter_by(id=invoice_id, seller_id=current_user.id).first_or_404()
    can_pay = configured() and row.status == 'pending' and utc() < utc(row.expires_at)
    params = gateway_params(row) if can_pay else None
    return render_template('main/tariffs_v2_payment.html', invoice=row, params=params)


@bp.post('/robokassa/result')
@csrf.exempt
def result():
    row = accept_storage_result(request.form)
    db.session.commit()
    return f'OK{row.gateway_id}', 200, {'Content-Type': 'text/plain; charset=utf-8'}


@bp.route('/robokassa/success', methods=['GET', 'POST'])
@csrf.exempt
def success():
    # An untrusted browser redirect never confirms a financial operation.
    return ('Возврат из тестовой Robokassa не подтверждает оплату. '
            'Проверьте состояние счета в тестовом кабинете продавца.'), 200


@bp.route('/robokassa/fail', methods=['GET', 'POST'])
@csrf.exempt
def fail():
    return ('Тестовая оплата не подтверждена. Если срок счета не истек, '
            'его можно открыть повторно в тестовом кабинете продавца.'), 200
