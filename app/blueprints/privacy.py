"""Legal documents, separate choices and authenticated data-subject requests."""
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import click
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for, jsonify
from flask.cli import with_appcontext

from app import db
from app.models.footer import FooterLink
from app.models.communications import Settings
from app.models.privacy import LegalSnapshot, PrivacyConsent, PrivacyRequest
from app.utils.message_files import actor
from sqlalchemy.exc import IntegrityError

bp = Blueprint('privacy', __name__)
DOCUMENTS = {
    'privacy': 'Политика обработки персональных данных',
    'personal': 'Отдельные согласия на обработку данных',
    'distribution': 'Согласие на распространение контактов',
    'terms': 'Правила использования',
    'seller-terms': 'Правила панели продавца',
    'offer': 'Оферта на продажу товаров',
}
PURPOSE_DOCUMENT = {'analytics': 'personal', 'marketing': 'personal',
                    'public_phone': 'distribution', 'public_email': 'distribution',
                    'terms': 'terms', 'seller_terms': 'seller-terms'}


def legal_document(slug):
    link = FooterLink.query.filter_by(slug=slug, is_active=True).first()
    if link:
        return link
    if slug not in DOCUMENTS:
        return None
    return SimpleNamespace(title=DOCUMENTS[slug], slug=slug, display_mode='page',
                           content=packaged_document(slug))


def packaged_document(slug):
    # Legal sources contain finished HTML, no request-dependent template context.
    return (Path(current_app.root_path) / 'templates' / 'legal' / (slug + '.html')).read_text(encoding='utf-8')


def document_digest(slug):
    document = legal_document(slug)
    return hashlib.sha256(document.content.encode('utf-8')).hexdigest()


def snapshot(slug, content=None):
    if content is None:
        content = legal_document(slug).content
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    row = LegalSnapshot.query.filter_by(slug=slug, digest=digest).first()
    if not row:
        try:
            with db.session.begin_nested():
                row = LegalSnapshot(slug=slug, digest=digest, content=content)
                db.session.add(row)
                db.session.flush()
        except IntegrityError:
            row = LegalSnapshot.query.filter_by(slug=slug, digest=digest).one()
    return row


def subject():
    role, user_id = actor()
    if role:
        return role, user_id, None
    return 'visitor', 0, session.get('privacy_visitor_key')


def consent_query(purpose):
    role, user_id, visitor_key = subject()
    query = PrivacyConsent.query.filter_by(user_type=role, user_id=user_id,
        visitor_key=visitor_key, purpose=purpose, withdrawn_at=None)
    if role == 'visitor' and not visitor_key:
        return query.filter(PrivacyConsent.id == -1)
    return query


def has_consent(purpose):
    return consent_query(purpose).first() is not None


def public_contact_allowed(seller, kind):
    consent = PrivacyConsent.query.filter_by(user_type='seller', user_id=seller.id,
        purpose='public_' + kind, withdrawn_at=None).order_by(PrivacyConsent.id.desc()).first()
    return bool(consent and consent.scope_data and
                consent.scope_data.get('value') == getattr(seller, kind))


def record_choice(purpose, enabled, method, role=None, user_id=None, scope_data=None):
    if role is None:
        role, user_id, visitor_key = subject()
        if role == 'visitor' and not visitor_key:
            visitor_key = secrets.token_hex(24)
            session['privacy_visitor_key'] = visitor_key
    else:
        visitor_key = None
    rows = PrivacyConsent.query.filter_by(user_type=role, user_id=user_id,
        visitor_key=visitor_key, purpose=purpose, withdrawn_at=None).all()
    if enabled:
        if rows and scope_data is not None and rows[-1].scope_data != scope_data:
            for row in rows:
                row.withdrawn_at = datetime.utcnow()
            rows = []
        if not rows:
            db.session.add(PrivacyConsent(user_type=role, user_id=user_id,
                visitor_key=visitor_key, purpose=purpose,
                snapshot_id=snapshot(PURPOSE_DOCUMENT[purpose]).id, method=method,
                scope_data=scope_data))
    else:
        for row in rows:
            row.withdrawn_at = datetime.utcnow()


def validate_terms(slug):
    if request.form.get('agree') not in ('on', '1', 'true'):
        return False
    return request.form.get('terms_version') == document_digest(slug)


def record_order_terms(order):
    # Legacy clients remain usable; never invent acceptance evidence for them.
    version = request.form.get('offer_version')
    if not version:
        return
    if version != document_digest('offer'):
        abort(400, description='Оферта обновилась. Повторите оформление с актуальной страницы.')
    db.session.add(PrivacyConsent(user_type='buyer', user_id=order.buyer_id,
        purpose='purchase_terms', snapshot_id=snapshot('offer').id,
        method='order button; contractual acceptance, not PD consent',
        scope_data={'order_id': order.id, 'order_number': order.order_number}))


@bp.route('/privacy')
def privacy_alias():
    return redirect(url_for('main.footer_page', slug='privacy'), code=308)


@bp.route('/privacy-center', methods=['GET', 'POST'])
def center():
    role, user_id = actor()
    purposes = ['analytics']
    if role in ('buyer', 'seller'):
        purposes.append('marketing')
    if role == 'seller':
        purposes.extend(['public_phone', 'public_email'])
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'choices':
            for purpose in purposes:
                enabled = request.form.get(purpose) == 'on'
                scope_data = None
                if enabled:
                    slug = PURPOSE_DOCUMENT[purpose]
                    if request.form.get(slug + '_version') != document_digest(slug):
                        abort(400, description='Условия изменились. Обновите страницу и повторите выбор.')
                if enabled and purpose.startswith('public_'):
                    from flask_login import current_user
                    name = request.form.get('subject_name', '').strip()
                    value = getattr(current_user, purpose.removeprefix('public_'))
                    if not name or len(name) > 200 or not value:
                        abort(400, description='Для публикации заполните ФИО и выбранный контакт в профиле.')
                    scope_data = {'subject_name': name, 'value': value, 'site': 'https://wimli.ru',
                                  'conditions': 'Only product enquiries; no further distribution or advertising'}
                record_choice(purpose, enabled, 'privacy-center checkbox', scope_data=scope_data)
                if purpose == 'analytics' and not enabled and role == 'buyer':
                    from app.models.products import ProductEvent
                    ProductEvent.query.filter_by(buyer_id=user_id).delete(synchronize_session=False)
            db.session.commit()
            flash('Настройки сохранены. Отказ от дополнительных целей не ограничивает покупки.', 'success')
        elif action == 'request':
            if role not in ('buyer', 'seller'):
                abort(401)
            kind = request.form.get('kind')
            if kind not in ('access', 'correct', 'withdraw', 'delete', 'block'):
                abort(400)
            details = request.form.get('details', '').strip()
            if not details or len(details) > 5000:
                abort(400)
            # Indicative calendar target; actual statutory deadline is checked
            # against the working-day calendar by the operator, never extended.
            days = 10 if kind == 'access' else (30 if kind == 'delete' else 7)
            ticket = PrivacyRequest(user_type=role, user_id=user_id, kind=kind,
                details=details, due_at=datetime.utcnow() + timedelta(days=days))
            db.session.add(ticket)
            from app.utils.email_service import enqueue
            enqueue('admin@wimli.ru', 'Wimli — обращение по персональным данным',
                    'Зарегистрировано обращение. Проверьте /main_admin/privacy-requests и законный срок ответа.')
            if kind == 'withdraw':
                for purpose in purposes:
                    record_choice(purpose, False, 'privacy request withdrawal')
            db.session.commit()
            flash(f'Обращение №{ticket.id} зарегистрировано.', 'success')
        else:
            abort(400)
        return redirect(url_for('privacy.center'))
    tickets = PrivacyRequest.query.filter_by(user_type=role, user_id=user_id).order_by(
        PrivacyRequest.created_at.desc()).all() if role in ('buyer', 'seller') else []
    return render_template('privacy/center.html', title='Конфиденциальность',
        purposes=purposes, choices={p: has_consent(p) for p in purposes},
        role=role, tickets=tickets)


@bp.route('/main_admin/privacy-requests', methods=['GET', 'POST'])
def requests_admin():
    if actor()[0] != 'admin':
        abort(403)
    if request.method == 'POST':
        ticket = db.session.get(PrivacyRequest, request.form.get('id', type=int))
        if not ticket:
            abort(404)
        state = request.form.get('status')
        reply = request.form.get('reply', '').strip()
        if state not in ('in_progress', 'completed', 'refused') or not reply or len(reply) > 10000:
            abort(400)
        ticket.status = state
        ticket.reply = reply
        ticket.completed_at = datetime.utcnow() if state in ('completed', 'refused') else None
        db.session.commit()
        return redirect(url_for('privacy.requests_admin'))
    return render_template('privacy/requests.html', title='Обращения по данным',
        tickets=PrivacyRequest.query.order_by(PrivacyRequest.created_at.desc()).all())


@bp.route('/privacy/export')
def export_account():
    role, user_id = actor()
    if role not in ('buyer', 'seller'):
        abort(401)
    from flask_login import current_user
    from app.models.communications import Message, Review
    from app.models.orders import Order
    from app.models.users import BuyerDelivery, SellerDelivery
    from app.utils.message_files import participant_filter
    def fields(row, names):
        result = {}
        for name in names:
            if hasattr(row, name):
                value = getattr(row, name)
                result[name] = value.isoformat() if isinstance(value, datetime) else value
        return result
    profile = fields(current_user, ['id', 'login', 'email', 'phone', 'created_at', 'name',
        'first_name', 'last_name', 'middle_name', 'address', 'store_name', 'store_slug'])
    orders = Order.query.filter_by(**{role + '_id': user_id}).all()
    messages = Message.query.filter(participant_filter(role, user_id)).all()
    delivery_model = BuyerDelivery if role == 'buyer' else SellerDelivery
    deliveries = delivery_model.query.filter_by(**{role + '_id': user_id}).all()
    data = {
        'profile': profile,
        'orders': [fields(row, ['id', 'order_number', 'created_at', 'status', 'total_price',
            'delivery_price', 'delivery_address', 'pvz_code', 'payment_method']) for row in orders],
        'messages': [fields(row, ['id', 'sender_type', 'sender_id', 'receiver_type', 'receiver_id',
            'text', 'timestamp', 'image_path', 'file_path']) for row in messages],
        'deliveries': [fields(row, ['id', 'delivery_service_id', 'recipient_name', 'phone',
            'address', 'pvz_code', 'pvz_address', 'pvz_city', 'ship_from_address']) for row in deliveries],
        'consents': [fields(row, ['id', 'purpose', 'snapshot_id', 'granted_at', 'withdrawn_at',
            'method', 'scope_data']) for row in PrivacyConsent.query.filter_by(user_type=role, user_id=user_id).all()],
        'requests': [fields(row, ['id', 'kind', 'details', 'status', 'created_at', 'reply',
            'completed_at']) for row in PrivacyRequest.query.filter_by(user_type=role, user_id=user_id).all()],
    }
    if role == 'buyer':
        data['reviews'] = [fields(row, ['id', 'product_id', 'rating', 'text', 'created_at', 'status'])
                           for row in Review.query.filter_by(buyer_id=user_id).all()]
    response = jsonify(data)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Content-Disposition'] = 'attachment; filename="wimli-personal-data.json"'
    return response


@click.command('privacy-install')
@with_appcontext
def install_documents():
    contents = {slug: packaged_document(slug) for slug in DOCUMENTS}
    version = hashlib.sha256(''.join(contents.values()).encode('utf-8')).hexdigest()
    if Settings.get('legal_installed_version') == version:
        click.echo('Legal documents already installed; administrator edits preserved.')
        return
    for index, (slug, title) in enumerate(DOCUMENTS.items()):
        link = FooterLink.query.filter_by(slug=slug).first()
        if link:
            snapshot(slug, link.content)  # Preserve the previous published document.
        else:
            link = FooterLink(slug=slug)
            db.session.add(link)
        link.title = title
        link.content = contents[slug]
        link.display_mode = 'page'
        link.column = 'info'
        link.is_active = True
        link.sort_order = index * 10
    Settings.set('legal_installed_version', version)
    db.session.commit()
    click.echo('Installed versioned Wimli legal documents.')


def init_privacy(app):
    app.register_blueprint(bp)
    app.cli.add_command(install_documents)
    from app.utils.privacy import pseudonymize_events
    app.cli.add_command(pseudonymize_events)
    from app.privacy_cleanup import cleanup_command
    app.cli.add_command(cleanup_command)
    app.jinja_env.globals.update(document_digest=document_digest,
        public_contact_allowed=public_contact_allowed, has_privacy_consent=has_consent)
