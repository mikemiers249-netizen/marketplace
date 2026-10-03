"""Limited retention of technical data; business records are never mass-deleted."""
from datetime import datetime, timedelta
import threading

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import or_, text

from app import db
from app.models.products import ProductEvent
from app.models.email_delivery import EmailToken, EmailOutbox
from app.models.privacy import PrivacyCleanupRun


def cleanup(apply=False):
    now = datetime.utcnow()
    events = ProductEvent.query.filter(ProductEvent.created_at < now - timedelta(days=180))
    outbox = EmailOutbox.query.filter(EmailOutbox.status.in_(['sent', 'failed', 'expired']),
        EmailOutbox.created_at < now - timedelta(days=90))
    tokens = EmailToken.query.filter(EmailToken.expires_at < now - timedelta(days=30),
        ~EmailToken.id.in_(db.session.query(EmailOutbox.token_id).filter(EmailOutbox.token_id.isnot(None))))
    counts = {'analytics_events': events.count(), 'email_queue': outbox.count(), 'email_tokens': tokens.count()}
    if not apply:
        return counts
    # Serialize workers on PostgreSQL; all changes and journal commit together.
    if db.engine.dialect.name == 'postgresql':
        if not db.session.execute(text('SELECT pg_try_advisory_xact_lock(741952803)')).scalar():
            db.session.rollback()
            return {}
    events.delete(synchronize_session=False)
    outbox.delete(synchronize_session=False)
    counts['email_tokens'] = tokens.delete(synchronize_session=False)
    if any(counts.values()):
        db.session.add(PrivacyCleanupRun(categories=counts,
            note='Technical retention cleanup. Orders, accounts, messages and attachments untouched. '
                 'This journal does not replace a signed destruction act or backup handling.'))
    db.session.commit()
    return counts


@click.command('privacy-cleanup')
@click.option('--apply', is_flag=True, help='Apply the displayed technical retention policy.')
@with_appcontext
def cleanup_command(apply):
    click.echo(('Applied: ' if apply else 'Preview only: ') + str(cleanup(apply)))


def start_worker(app):
    if not app.config.get('PRIVACY_CLEANUP_ENABLED', True):
        return
    def run():
        while True:
            with app.app_context():
                try:
                    cleanup(apply=True)
                except Exception as exc:
                    db.session.rollback()
                    app.logger.warning('Privacy retention: %s', type(exc).__name__)
                finally:
                    db.session.remove()
            threading.Event().wait(86400)
    threading.Thread(target=run, name='privacy-retention', daemon=True).start()
