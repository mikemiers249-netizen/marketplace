"""Opt-in admin sandbox. No checkout/payment/refund execution endpoints."""
from datetime import date
from decimal import Decimal

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user

from app import db
from app.models.products import Category, Product
from app.models.users import Admin, Seller
from app.models.tariffs_v2 import TariffV2Audit, TariffV2Category, TariffV2Rule, TariffV2Seller, TariffV2StorageInvoice
from app.models.tariffs_v2_commerce import TariffV2Checkout
from app.utils.tariffs_v2 import TariffError, benefits_end, integer, quote_line, storage_quote, stored_utc, validate_rule

bp = Blueprint('tariffs_v2', __name__, url_prefix='/main_admin/tariffs-v2')


@bp.before_request
def guard():
    if not current_app.config.get('TARIFFS_V2_ENABLED', False):
        abort(404)
    authenticated = (session.get('main_admin_authenticated') is True or
                     (current_user.is_authenticated and isinstance(current_user, Admin)
                      and current_user.has_permission('tariffs')))
    if not authenticated:
        abort(403)


def audit(action, subject, details):
    actor = ('main_admin:' + str(session.get('main_admin_user', 'admin'))
             if session.get('main_admin_authenticated') is True else 'admin:' + str(current_user.id))
    db.session.add(TariffV2Audit(actor=actor, action=action, subject=str(subject), details=details))


def finish():
    db.session.commit()
    flash('Настройки тестовой системы сохранены. Действующие тарифы не изменены.', 'success')
    return redirect(url_for('tariffs_v2.index'))


@bp.errorhandler(TariffError)
def invalid(error):
    db.session.rollback()
    return jsonify(error=str(error)), 400


@bp.get('/')
def index():
    return render_template('admin/tariffs_v2.html', title='Новые тарифы — тестовый режим',
        rules=TariffV2Rule.query.order_by(TariffV2Rule.id).all(),
        categories=Category.query.order_by(Category.name).all(),
        assignments={row.category_id: row.rule_id for row in TariffV2Category.query.all()},
        sellers=Seller.query.order_by(Seller.id).all(),
        owner_email=current_app.config.get('TARIFFS_V2_OWNER_EMAIL', '').strip().casefold(),
        policies={row.seller_id: row for row in TariffV2Seller.query.all()},
        storage_invoices=TariffV2StorageInvoice.query.order_by(TariffV2StorageInvoice.id.desc()).limit(50).all(),
        checkout_reviews=TariffV2Checkout.query.filter_by(status='needs_review').order_by(TariffV2Checkout.id.desc()).limit(50).all(),
        history=TariffV2Audit.query.order_by(TariffV2Audit.id.desc()).limit(25).all())


@bp.post('/rules')
def create_rule():
    name = request.form.get('name', '').strip()
    kind = request.form.get('kind', '')
    if not name or len(name) > 200:
        raise TariffError('Укажите название тарифа длиной до 200 символов.')
    amount = validate_rule(kind, request.form.get('amount'))
    rule = TariffV2Rule(name=name, kind=kind, amount=amount)
    db.session.add(rule)
    db.session.flush()
    audit('create_rule', rule.id, dict(name=name, kind=kind, amount=str(amount)))
    return finish()


@bp.post('/categories/<int:category_id>')
def assign_category(category_id):
    db.get_or_404(Category, category_id)
    rule_id = integer(request.form.get('rule_id'))
    rule = db.get_or_404(TariffV2Rule, rule_id)
    if not rule.active:
        raise TariffError('Отключенный тариф нельзя назначить категории.')
    assignment = db.session.get(TariffV2Category, category_id)
    if assignment is None:
        assignment = TariffV2Category(category_id=category_id)
        db.session.add(assignment)
    before = assignment.rule_id
    assignment.rule_id = rule.id
    audit('assign_category', category_id, dict(before=before, after=rule.id))
    return finish()


@bp.post('/sellers/<int:seller_id>')
def seller_policy(seller_id):
    db.get_or_404(Seller, seller_id)
    days = integer(request.form.get('shipping_days', '5'), 1, 5)
    rule_id = request.form.get('override_rule_id', '').strip()
    rule = db.get_or_404(TariffV2Rule, integer(rule_id)) if rule_id else None
    if rule and not rule.active:
        raise TariffError('Отключенный тариф нельзя назначить продавцу.')
    until = request.form.get('benefits_last_day', '').strip()
    try:
        until = stored_utc(benefits_end(date.fromisoformat(until))) if until else None
    except (ValueError, OverflowError):
        raise TariffError('Укажите корректную последнюю дату льгот.') from None
    policy = db.session.get(TariffV2Seller, seller_id)
    if policy is None:
        policy = TariffV2Seller(seller_id=seller_id)
        db.session.add(policy)
    fields = dict(override_rule_id=rule.id if rule else None, shipping_days=days,
                  free_storage=request.form.get('free_storage') == 'on',
                  free_buyer_fee=request.form.get('free_buyer_fee') == 'on',
                  benefits_until=until)
    for name, value in fields.items():
        setattr(policy, name, value)
    audit('seller_policy', seller_id, {k: v.isoformat() if hasattr(v, 'isoformat') else v
                                     for k, v in fields.items()})
    return finish()


@bp.get('/preview/<int:product_id>')
def preview(product_id):
    product = db.get_or_404(Product, product_id)
    quote = quote_line(product, request.args.get('quantity', '1'))
    storage = storage_quote(product)
    return jsonify(order=quote.json(), storage={
        key: str(value) if isinstance(value, Decimal) else value.isoformat() if isinstance(value, date) else value
        for key, value in storage.items()},
                   mode='preview', production_checkout_changed=False)
