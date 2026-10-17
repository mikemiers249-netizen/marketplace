"""Bounded, privacy-conscious buyer diagnostics via the existing mail outbox."""
import hashlib
import re
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import Blueprint, current_app, request, g
from flask_login import current_user
from sqlalchemy import select, func
from app import db
from app.models.email_delivery import EmailOutbox
from app.models.users import Buyer
from app.utils.email_service import configured

RECIPIENT = 'ronnie83@mail.ru'
bp = Blueprint('buyer_errors', __name__)


def report(kind, status=0, source='', line=0, error=None, page=''):
    try:
        if not configured():
            return
        buyer_id = current_user.id if isinstance(current_user, Buyer) else None
        # Never include query strings, request bodies, cookies, exception messages or JS payloads.
        path = request.path if request.endpoint != 'buyer_errors.client_error' else page or source
        path = urlsplit(path).path[:250]
        fingerprint = hashlib.sha256(f'{kind}:{status}:{path}:{source}:{line}'.encode()).hexdigest()[:16]
        subject = f'Wimli — ошибка покупателя [{fingerprint}]'
        now = datetime.utcnow()
        local = now.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=3)))
        body = (f'Дата и время: {local:%d.%m.%Y %H:%M:%S} МСК\n'
                f'Тип: {kind}\nHTTP: {status or "—"}\nСтраница: {path}\n'
                f'Покупатель ID: {buyer_id or "Гость"}\nМетод: {request.method}\n'
                f'Источник: {source or "Сервер"}\nСтрока: {line or "—"}\n')
        if error:
            frames = traceback.extract_tb(error.__traceback__)[-8:]
            body += 'Исключение: ' + type(error).__name__ + '\n'
            body += '\n'.join(f'{f.filename.rsplit("/", 1)[-1]}:{f.lineno} ({f.name})' for f in frames)
        table = EmailOutbox.__table__
        # Separate transaction: diagnostics must not commit or roll back the customer's order.
        with db.engine.begin() as conn:
            recent = conn.execute(select(table.c.id).where(
                table.c.subject == subject, table.c.created_at > now - timedelta(minutes=10)).limit(1)).first()
            count = conn.execute(select(func.count()).select_from(table).where(
                table.c.recipient == RECIPIENT, table.c.subject.like('Wimli — ошибка покупателя%'),
                table.c.created_at > now - timedelta(hours=1))).scalar()
            if not recent and count < 30:
                conn.execute(table.insert().values(recipient=RECIPIENT, subject=subject, body=body,
                    created_at=now, status='pending', attempts=0, next_attempt_at=now))
    except Exception:
        current_app.logger.warning('Could not queue buyer diagnostic email')


@bp.route('/diagnostics/client-error', methods=['POST'])
def client_error():
    if request.content_length is None or request.content_length > 2048:
        return '', 413
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return '', 400
    kind = data.get('kind')
    if kind not in ('javascript', 'promise', 'network', 'application'):
        return '', 400
    source = data.get('source', '')
    if not isinstance(source, str) or len(source) > 500:
        return '', 400
    parsed = urlsplit(source)
    if parsed.netloc or not parsed.path.startswith('/') or not re.fullmatch(r'[\w/.-]*', parsed.path):
        return '', 400
    line = data.get('line', 0)
    if not isinstance(line, int) or not 0 <= line <= 1000000:
        return '', 400
    page = data.get('page', parsed.path)
    if not isinstance(page, str) or not re.fullmatch(r'/[\w/.-]*', page) or len(page) > 250:
        return '', 400
    report(kind, source=parsed.path, line=line, page=page)
    return '', 204


def install(app):
    app.register_blueprint(bp)

    @app.after_request
    def report_response(response):
        if request.endpoint == 'buyer_errors.client_error':
            return response
        buyer = isinstance(current_user, Buyer)
        public = request.blueprint in ('main', 'payments', 'auth')
        if response.status_code >= 400 and (buyer or public) and request.endpoint != 'static':
            # Expected login redirects and ordinary missing pages for bots are not incidents.
            if response.status_code >= 500 or buyer or request.blueprint == 'payments':
                report('HTTP', response.status_code, error=getattr(g, 'buyer_error', None))
        return response
