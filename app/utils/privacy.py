"""Minimise authentication data retained in product analytics."""
import hashlib
import hmac
import re
import click
from flask.cli import with_appcontext
from flask import current_app, request


def analytics_session_id():
    raw = request.cookies.get(current_app.config.get('SESSION_COOKIE_NAME', 'session'))
    if not raw:
        return None
    return pseudonymize(raw)


def pseudonymize(raw):
    key = str(current_app.config['SECRET_KEY']).encode('utf-8')
    return hmac.new(key, b'product-analytics-v1\x00' + raw.encode('utf-8'),
                    hashlib.sha256).hexdigest()


@click.command('privacy-pseudonymize-events')
@with_appcontext
def pseudonymize_events():
    """Replace legacy authentication-cookie values without removing events."""
    from app import db
    from app.models.products import ProductEvent
    count = 0
    for event in ProductEvent.query.filter(ProductEvent.session_id.isnot(None)).yield_per(500):
        if not re.fullmatch(r'[0-9a-f]{64}', event.session_id):
            event.session_id = pseudonymize(event.session_id)
            count += 1
    db.session.commit()
    click.echo(f'Pseudonymized {count} legacy analytics identifiers.')
