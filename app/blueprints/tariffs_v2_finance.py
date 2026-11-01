"""Local finance UI. Simulations are explicit; no provider requests are sent."""
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for, session
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app import db
from app.models.users import Buyer, Seller
from app.models.tariffs_v2 import TariffV2Invoice
from app.models.tariffs_v2_commerce import TariffV2SandboxOrder, TariffV2Checkout
from app.models.tariffs_v2_finance import TariffV2Claim, TariffV2CheckoutRefund
from app.models.tariffs_v2_lifecycle import TariffV2StorageExpiry
from app.utils.tariffs_v2 import TariffError, utc
from app.utils.tariffs_v2_finance import (
    buyer_cancel_order, receive_order, cancel_line, open_claim, respond_claim, decide_claim,
    retry_refund, run_finance_tick, simulate_refund, simulate_invoice_payment,
)

bp = Blueprint('tariffs_v2_finance', __name__)


@bp.before_request
def guard():
    if not (current_app.config.get('TARIFFS_V2_ENABLED') and current_app.config.get('TARIFFS_V2_SANDBOX')):
        abort(404)
    if request.path.startswith('/main_admin/'):
        from app.blueprints.tariffs_v2 import guard as admin_guard
        return admin_guard()
    if not current_user.is_authenticated or not isinstance(current_user, (Buyer, Seller)):
        abort(403)


@bp.errorhandler(TariffError)
def invalid(error):
    db.session.rollback()
    return jsonify(error=str(error)), 400


@bp.errorhandler(IntegrityError)
def conflict(error):
    db.session.rollback()
    return jsonify(error='Операция уже изменена другим запросом. Обновите страницу.'), 409


def role():
    return 'buyer' if isinstance(current_user, Buyer) else 'seller'


def admin_actor():
    return ('main_admin:' + str(session.get('main_admin_user', 'admin')) if session.get('main_admin_authenticated') is True
            else 'admin:' + str(current_user.id))


def owned_order(order_id):
    query = TariffV2SandboxOrder.query.filter_by(id=order_id)
    if isinstance(current_user, Buyer):
        query = query.join(TariffV2Checkout).filter(TariffV2Checkout.buyer_id == current_user.id)
    else:
        query = query.filter(TariffV2SandboxOrder.seller_id == current_user.id)
    return query.first_or_404()


@bp.get('/tariffs-v2/orders/<int:order_id>/claims')
def claims(order_id):
    order = owned_order(order_id)
    if order.checkout.status != 'complete':
        abort(404)
    return render_template('main/tariffs_v2_claims.html', order=order, role=role(),
        claims=TariffV2Claim.query.filter_by(order_id=order.id).order_by(TariffV2Claim.id).all())


@bp.post('/tariffs-v2/orders/<int:order_id>/claims')
def create_claim(order_id):
    owned_order(order_id)
    open_claim(order_id, role(), current_user.id, request.form.get('explanation'))
    db.session.commit()
    return redirect(url_for('tariffs_v2_finance.claims', order_id=order_id))


@bp.post('/tariffs-v2/claims/<int:claim_id>/respond')
def respond(claim_id):
    claim = db.get_or_404(TariffV2Claim, claim_id)
    owned_order(claim.order_id)
    answer = request.form.get('agrees')
    if answer not in ('yes', 'no'):
        raise TariffError('Выберите ответ на претензию.')
    respond_claim(claim.id, role(), current_user.id, answer == 'yes', request.form.get('explanation'))
    db.session.commit()
    return redirect(url_for('tariffs_v2_finance.claims', order_id=claim.order_id))


@bp.post('/tariffs-v2/orders/<int:order_id>/lines/<int:line_id>/cancel')
def cancel(order_id, line_id):
    if not isinstance(current_user, Seller):
        abort(403)
    order = owned_order(order_id)
    if not any(line.id == line_id for line in order.lines):
        abort(404)
    cancel_line(line_id, current_user.id, request.form.get('quantity'))
    db.session.commit()
    return redirect(url_for('tariffs_v2_commerce.seller_orders'))


@bp.post('/tariffs-v2/orders/<int:order_id>/buyer-cancel')
def buyer_cancel(order_id):
    if not isinstance(current_user, Buyer):
        abort(403)
    order = owned_order(order_id)
    buyer_cancel_order(order.id, current_user.id)
    db.session.commit()
    return redirect(url_for('tariffs_v2_commerce.checkout_view', checkout_id=order.checkout_id))


@bp.post('/tariffs-v2/orders/<int:order_id>/received')
def received(order_id):
    if not isinstance(current_user, Buyer):
        abort(403)
    order = owned_order(order_id)
    receive_order(order.id, current_user.id)
    db.session.commit()
    return redirect(url_for('tariffs_v2_commerce.checkout_view', checkout_id=order.checkout_id))


@bp.get('/tariffs-v2/seller/bills')
def bills():
    if not isinstance(current_user, Seller):
        abort(403)
    rows = TariffV2Invoice.query.filter_by(seller_id=current_user.id).order_by(TariffV2Invoice.id.desc()).all()
    details = {}
    for refund in TariffV2CheckoutRefund.query.filter(TariffV2CheckoutRefund.invoice_id.in_([row.id for row in rows])):
        details.setdefault(refund.invoice_id, []).append(refund)
    return render_template('main/tariffs_v2_bills.html', invoices=rows, details=details)


@bp.post('/tariffs-v2/seller/bills/<int:invoice_id>/simulate-payment')
def pay_bill(invoice_id):
    if not isinstance(current_user, Seller):
        abort(403)
    simulate_invoice_payment(invoice_id, current_user.id)
    db.session.commit()
    flash('Смоделирована тестовая оплата. Настоящие деньги не списывались.', 'info')
    return redirect(url_for('tariffs_v2_finance.bills'))


@bp.get('/main_admin/tariffs-v2/finance')
def admin_finance():
    return render_template('admin/tariffs_v2_finance.html',
        claims=TariffV2Claim.query.order_by(TariffV2Claim.id.desc()).limit(100).all(),
        refunds=TariffV2CheckoutRefund.query.order_by(TariffV2CheckoutRefund.id.desc()).limit(100).all(),
        invoices=TariffV2Invoice.query.order_by(TariffV2Invoice.id.desc()).limit(100).all(), now=utc())


@bp.post('/main_admin/tariffs-v2/claims/<int:claim_id>/decide')
def decide(claim_id):
    decision = request.form.get('approved')
    if decision not in ('yes', 'no'):
        raise TariffError('Выберите решение.')
    decide_claim(claim_id, decision == 'yes', request.form.get('explanation'), admin_actor())
    db.session.commit()
    return redirect(url_for('tariffs_v2_finance.admin_finance'))


@bp.post('/main_admin/tariffs-v2/refunds/<int:refund_id>/simulate')
def refund_simulation(refund_id):
    success = request.form.get('succeeded')
    if success not in ('yes', 'no'):
        raise TariffError('Выберите результат симуляции.')
    simulate_refund(refund_id, success == 'yes', admin_actor())
    db.session.commit()
    flash('Смоделирован результат возврата. Платежная система не вызывалась.', 'info')
    return redirect(url_for('tariffs_v2_finance.admin_finance'))


@bp.post('/main_admin/tariffs-v2/finance/run')
def run():
    run_finance_tick()
    db.session.commit()
    flash('Проверены сроки отгрузки и сформированы тестовые месячные счета. Внешние операции не выполнялись.', 'info')
    return redirect(url_for('tariffs_v2_finance.admin_finance'))


@bp.post('/main_admin/tariffs-v2/refunds/<int:refund_id>/retry')
def retry(refund_id):
    retry_refund(refund_id, admin_actor())
    db.session.commit()
    return redirect(url_for('tariffs_v2_finance.admin_finance'))


@bp.get('/main_admin/tariffs-v2/storage-expiry')
def storage_expiry():
    rows = TariffV2StorageExpiry.query.order_by(TariffV2StorageExpiry.expires_at, TariffV2StorageExpiry.product_id).limit(200).all()
    return render_template('admin/tariffs_v2_storage_expiry.html', rows=rows)


@bp.post('/main_admin/tariffs-v2/storage-expiry/check')
def check_storage():
    from app.utils.tariffs_v2_storage_lifecycle import check_storage_expiry
    result = check_storage_expiry()
    db.session.commit()
    flash(f'Проверены сроки хранения: {result}. Карточки и файлы не удалялись.', 'info')
    return redirect(url_for('tariffs_v2_finance.storage_expiry'))
