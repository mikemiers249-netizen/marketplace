"""Explicit local sandbox bootstrap; no production schema or workers."""
import click
from flask import current_app
from flask.cli import with_appcontext

from app import db


def install(app):
    if not app.config.get('TARIFFS_V2_ENABLED', False):
        return
    # First-stage preview cannot accidentally be enabled against a production DB.
    if not app.config.get('TARIFFS_V2_SANDBOX', False):
        raise RuntimeError('Tariffs v2 currently require an explicitly isolated sandbox.')
    with app.app_context():
        if db.engine.dialect.name != 'sqlite':
            raise RuntimeError('Tariffs v2 first-stage sandbox supports local SQLite only.')
    from app.blueprints.tariffs_v2 import bp
    from app.models import tariffs_v2_commerce  # noqa: F401
    from app.blueprints.tariffs_v2_payments import bp as payments_bp
    from app.blueprints.tariffs_v2_commerce import bp as commerce_bp
    from app.utils.tariffs_v2 import MSK, utc
    from datetime import timedelta
    app.register_blueprint(bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(commerce_bp)

    @app.template_filter('v2_last_benefit_day')
    def last_benefit_day(value):
        return (utc(value).astimezone(MSK).date() - timedelta(days=1)).isoformat()

    app.cli.add_command(initialize)


@click.command('tariffs-v2-init-sandbox')
@with_appcontext
def initialize():
    """Create only v2 tables in the explicitly enabled local sandbox."""
    if not (current_app.config.get('TARIFFS_V2_ENABLED') and
            current_app.config.get('TARIFFS_V2_SANDBOX') and db.engine.dialect.name == 'sqlite'):
        raise click.ClickException('Only an isolated v2 SQLite sandbox may be initialized.')
    from app.models import tariffs_v2  # noqa: F401
    from app.models import tariffs_v2_commerce  # noqa: F401
    tables = [table for table in db.metadata.sorted_tables if table.name.startswith('tariff_v2_')]
    db.metadata.create_all(bind=db.engine, tables=tables)
    # Additive upgrade for a local sandbox created during phase one.
    from sqlalchemy import inspect, text
    columns = {column['name'] for column in inspect(db.engine).get_columns('tariff_v2_coverage')}
    if 'is_test' not in columns:
        db.session.execute(text('ALTER TABLE tariff_v2_coverage ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT 0'))
    columns = {column['name'] for column in inspect(db.engine).get_columns('tariff_v2_storage_invoices')}
    if 'gateway_id' not in columns:
        db.session.execute(text('ALTER TABLE tariff_v2_storage_invoices ADD COLUMN gateway_id INTEGER REFERENCES tariff_v2_gateway_numbers(id)'))
    # Preserve the InvId already sent to the test provider by phase-two invoices.
    for row in db.session.execute(text('SELECT id FROM tariff_v2_storage_invoices WHERE gateway_id IS NULL')).all():
        existing = db.session.execute(text('SELECT purpose, reference_id FROM tariff_v2_gateway_numbers WHERE id=:id'), {'id': row.id}).first()
        if existing and (existing.purpose != 'storage' or existing.reference_id != row.id):
            db.session.rollback()
            raise click.ClickException('Payment number collision: sandbox requires manual review.')
        if not existing:
            db.session.execute(text("INSERT INTO tariff_v2_gateway_numbers(id,purpose,reference_id) VALUES (:id,'storage',:id)"), {'id': row.id})
        db.session.execute(text('UPDATE tariff_v2_storage_invoices SET gateway_id=:id WHERE id=:id'), {'id': row.id})
    db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS v2_storage_gateway_unique ON tariff_v2_storage_invoices(gateway_id)'))
    db.session.commit()
    click.echo('Tariffs v2 sandbox tables ready. No provider calls, workers or cleanup started.')
