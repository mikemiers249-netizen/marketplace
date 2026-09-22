"""Transactional email queue and account-bound, single-use links."""
import hashlib
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit

from flask import current_app
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from app import db
from app.models.email_delivery import EmailIdentity, EmailToken, EmailOutbox
from app.models.users import Buyer, Seller


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def configured():
    cfg = current_app.config
    base = urlsplit(cfg.get('PUBLIC_BASE_URL', ''))
    return bool(cfg.get('MAIL_SERVER') and cfg.get('MAIL_DEFAULT_SENDER')
                and cfg.get('MAIL_USERNAME') and cfg.get('MAIL_PASSWORD')
                and base.scheme == 'https' and base.netloc
                and not base.username and not base.query and not base.fragment
                and bool(cfg.get('MAIL_USE_SSL')) != bool(cfg.get('MAIL_USE_TLS')))


def identity_for(user, create=False, required=False):
    kind = 'seller' if isinstance(user, Seller) else 'buyer'
    identity = EmailIdentity.query.filter_by(user_type=kind, user_id=user.id).first()
    if identity is None and create:
        identity = EmailIdentity(user_type=kind, user_id=user.id, email=user.email,
                                 required=required)
        try:
            with db.session.begin_nested():
                db.session.add(identity)
                db.session.flush()
        except IntegrityError:
            identity = EmailIdentity.query.filter_by(user_type=kind, user_id=user.id).one()
    if identity and identity.email != user.email:
        identity.email = user.email
        identity.verified_at = None
        identity.last_verify_at = None
        identity.last_reset_at = None
    return identity


def needs_verification(user):
    identity = identity_for(user)
    return bool(identity and identity.required and not identity.verified_at)


def enqueue(recipient, subject, body, token_id=None):
    if not configured():
        return False
    db.session.add(EmailOutbox(recipient=recipient, subject=subject, body=body,
                               token_id=token_id))
    return True


def request_link(user, purpose, required=False):
    if purpose not in ('verify', 'reset'):
        raise ValueError('Unknown email purpose')
    if not configured() or not user.is_active:
        return False
    identity = identity_for(user, create=True, required=required)
    if purpose == 'verify' and identity.verified_at:
        return False
    now = datetime.utcnow()
    field = 'last_verify_at' if purpose == 'verify' else 'last_reset_at'
    column = getattr(EmailIdentity, field)
    # Atomic cooldown shared by every web worker, including concurrent requests.
    claimed = EmailIdentity.query.filter(EmailIdentity.id == identity.id, or_(
        column.is_(None), column <= now - timedelta(seconds=60)
    )).update({field: now}, synchronize_session=False)
    if not claimed:
        return False
    raw = secrets.token_urlsafe(32)
    token = EmailToken(digest=digest(raw), identity_id=identity.id, purpose=purpose,
                       email=user.email, password_digest=digest(user.password_hash),
                       expires_at=now + timedelta(minutes=30))
    db.session.add(token)
    db.session.flush()
    endpoint = 'email_account.verify' if purpose == 'verify' else 'email_account.reset'
    base = current_app.config['PUBLIC_BASE_URL'].rstrip('/')
    path = current_app.url_map.bind(urlsplit(base).netloc).build(endpoint, {'token': raw})
    link = base + path
    action = 'Подтвердить почту' if purpose == 'verify' else 'Восстановить пароль'
    enqueue(user.email, f'Wimli — {action}',
            f'{action}:\n{link}\n\nСсылка действует 30 минут и используется один раз.\n'
            'Если вы не запрашивали это письмо, проигнорируйте его.\n\nWimli', token.id)
    return True


def resolve_token(raw, purpose):
    token = EmailToken.query.filter_by(digest=digest(raw), purpose=purpose).first()
    if not token or token.used_at or token.expires_at <= datetime.utcnow():
        return None, None, None
    identity = db.session.get(EmailIdentity, token.identity_id)
    model = Seller if identity.user_type == 'seller' else Buyer
    user = db.session.get(model, identity.user_id)
    if not user or not user.is_active or user.email != token.email or digest(user.password_hash) != token.password_digest:
        return None, None, None
    return token, identity, user


def consume_token(raw, purpose, password=None):
    token, identity, user = resolve_token(raw, purpose)
    if not token:
        return False
    now = datetime.utcnow()
    claimed = EmailToken.query.filter_by(id=token.id, used_at=None).filter(
        EmailToken.expires_at > now).update({'used_at': now}, synchronize_session=False)
    if not claimed:
        return False
    if purpose == 'verify':
        identity.email = user.email
        identity.verified_at = now
    else:
        user.set_password(password)
        identity.sessions_revoked_at = now
    # Invalidate sibling links after successful use.
    EmailToken.query.filter_by(identity_id=identity.id, purpose=purpose, used_at=None).update(
        {'used_at': now}, synchronize_session=False)
    db.session.commit()
    return True


def smtp_connection():
    cfg = current_app.config
    context = ssl.create_default_context()
    if cfg['MAIL_USE_SSL']:
        connection = smtplib.SMTP_SSL(cfg['MAIL_SERVER'], cfg['MAIL_PORT'], timeout=15, context=context)
    else:
        connection = smtplib.SMTP(cfg['MAIL_SERVER'], cfg['MAIL_PORT'], timeout=15)
        connection.starttls(context=context)
    try:
        connection.login(cfg['MAIL_USERNAME'], cfg['MAIL_PASSWORD'])
    except Exception:
        connection.close()
        raise
    return connection


def deliver_batch(limit=20):
    if not configured():
        return 0
    now = datetime.utcnow()
    ids = [row.id for row in EmailOutbox.query.filter(
        EmailOutbox.status.in_(['pending', 'sending']), EmailOutbox.next_attempt_at <= now
    ).order_by(EmailOutbox.id).limit(limit).all()]
    count = 0
    for message_id in ids:
        lease = secrets.token_hex(16)
        claimed = EmailOutbox.query.filter(
            EmailOutbox.id == message_id, EmailOutbox.status.in_(['pending', 'sending']),
            EmailOutbox.next_attempt_at <= now
        ).update({'status': 'sending', 'lease_id': lease,
                  'next_attempt_at': now + timedelta(minutes=10),
                  'attempts': EmailOutbox.attempts + 1}, synchronize_session=False)
        db.session.commit()
        if not claimed:
            continue
        row = db.session.get(EmailOutbox, message_id)
        try:
            if row.token_id:
                token = db.session.get(EmailToken, row.token_id)
                if token.used_at or token.expires_at <= datetime.utcnow():
                    row.status, row.body = 'expired', ''
                    db.session.commit()
                    continue
            message = EmailMessage()
            message['From'] = current_app.config['MAIL_DEFAULT_SENDER']
            message['To'] = row.recipient
            message['Subject'] = row.subject
            message['Message-ID'] = f'<wimli-{row.id}@{urlsplit(current_app.config["PUBLIC_BASE_URL"]).hostname}>'
            message.set_content(row.body)
            with smtp_connection() as smtp:
                smtp.send_message(message)
            changes = {'status': 'sent', 'sent_at': datetime.utcnow(), 'body': '', 'last_error': None}
            count += 1
        except Exception as exc:
            # Never log credentials, recipients, message bodies or token URLs.
            changes = {'status': 'failed' if row.attempts >= 6 else 'pending',
                       'last_error': type(exc).__name__,
                       'next_attempt_at': datetime.utcnow() + timedelta(seconds=min(3600, 30 * 2 ** row.attempts))}
            current_app.logger.warning('Email delivery %s: %s', message_id, type(exc).__name__)
        EmailOutbox.query.filter_by(id=message_id, lease_id=lease).update(changes, synchronize_session=False)
        db.session.commit()
    return count
