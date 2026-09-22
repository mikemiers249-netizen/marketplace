"""Queue runner shared by Gunicorn workers and the Flask CLI."""
import threading
import click
from flask.cli import with_appcontext
from app import db
from app.utils.email_service import deliver_batch, configured, smtp_connection


def start_worker(app):
    def run():
        while True:
            with app.app_context():
                try:
                    deliver_batch()
                except Exception as exc:
                    db.session.rollback()
                    app.logger.warning('Email queue: %s', type(exc).__name__)
                finally:
                    db.session.remove()
            threading.Event().wait(10)
    if app.config.get('MAIL_WORKER_ENABLED', True):
        threading.Thread(target=run, name='email-outbox', daemon=True).start()


@click.command('mail-check')
@with_appcontext
def mail_check():
    """Check encrypted SMTP login without sending a message."""
    if not configured():
        raise click.ClickException('SMTP configuration is incomplete (see docs/email.md).')
    try:
        with smtp_connection() as smtp:
            smtp.noop()
    except Exception as exc:
        raise click.ClickException(type(exc).__name__) from None
    click.echo('SMTP TLS and authentication OK. No email sent.')


@click.command('mail-deliver')
@with_appcontext
def mail_deliver():
    """Deliver one batch; suitable for development or an external scheduler."""
    click.echo(f'Delivered: {deliver_batch()}')
