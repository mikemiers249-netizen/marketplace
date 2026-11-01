"""Explicit local sandbox bootstrap; no production schema or workers."""
import click
from pathlib import Path
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
    from app.models import tariffs_v2_finance  # noqa: F401
    from app.models import tariffs_v2_lifecycle  # noqa: F401
    from app.blueprints.tariffs_v2_payments import bp as payments_bp
    from app.blueprints.tariffs_v2_commerce import bp as commerce_bp
    from app.blueprints.tariffs_v2_finance import bp as finance_bp
    from app.utils.tariffs_v2 import MSK, utc
    from datetime import timedelta
    app.register_blueprint(bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(commerce_bp)
    app.register_blueprint(finance_bp)

    @app.template_filter('v2_last_benefit_day')
    def last_benefit_day(value):
        return (utc(value).astimezone(MSK).date() - timedelta(days=1)).isoformat()

    @app.template_filter('v2_moscow_time')
    def moscow_time(value):
        return utc(value).astimezone(MSK).strftime('%d.%m.%Y %H:%M') if value else ''

    app.cli.add_command(initialize)
    app.cli.add_command(finance_tick)
    app.cli.add_command(storage_tick)
    app.cli.add_command(replica_init)
    app.cli.add_command(replica_create)
    app.cli.add_command(replica_purge)


@click.command('tariffs-v2-init-sandbox')
@with_appcontext
def initialize():
    """Create only v2 tables in the explicitly enabled local sandbox."""
    if not (current_app.config.get('TARIFFS_V2_ENABLED') and
            current_app.config.get('TARIFFS_V2_SANDBOX') and db.engine.dialect.name == 'sqlite'):
        raise click.ClickException('Only an isolated v2 SQLite sandbox may be initialized.')
    from app.models import tariffs_v2  # noqa: F401
    from app.models import tariffs_v2_commerce  # noqa: F401
    from app.models import tariffs_v2_finance  # noqa: F401
    from app.models import tariffs_v2_lifecycle  # noqa: F401
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


@click.command('tariffs-v2-finance-tick')
@with_appcontext
def finance_tick():
    """Explicit isolated job: create refund intents and monthly bills, no provider calls."""
    from app.utils.tariffs_v2_finance import run_finance_tick
    from app.utils.tariffs_v2 import TariffError
    try:
        counts = run_finance_tick()
        db.session.commit()
    except TariffError as error:
        db.session.rollback()
        raise click.ClickException(str(error)) from error
    click.echo(f'Sandbox finance checked: {counts}. No external payments or refunds executed.')


@click.command('tariffs-v2-storage-check')
@with_appcontext
def storage_tick():
    """Record expiry plans; NEVER delete cards or files."""
    from app.utils.tariffs_v2_storage_lifecycle import check_storage_expiry
    from app.utils.tariffs_v2 import TariffError
    try:
        counts = check_storage_expiry()
        db.session.commit()
    except TariffError as error:
        db.session.rollback()
        raise click.ClickException(str(error)) from error
    click.echo(f'Sandbox storage dry run: {counts}. No cards or files deleted.')


@click.command('tariffs-v2-replica-init')
@with_appcontext
def replica_init():
    """Register an empty dedicated replica directory, never a legacy upload root."""
    from app.utils.tariffs_v2_replicas import initialize_store
    from app.utils.tariffs_v2 import TariffError
    try:
        initialize_store()
        db.session.commit()
    except (TariffError, OSError) as error:
        db.session.rollback()
        raise click.ClickException(str(error)) from error
    click.echo('Dedicated replica store initialized. No production files copied or changed.')


@click.command('tariffs-v2-replica-create')
@click.option('--product-id', type=int, required=True)
@click.option('--source', type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@with_appcontext
def replica_create(product_id, source):
    """Copy one explicitly named file as independent bytes into the replica store."""
    from app.utils.tariffs_v2_replicas import create_replica, store_root
    from app.utils.tariffs_v2 import TariffError
    try:
        store_root()
        if source.stat().st_size > 20 * 1024 * 1024:
            raise TariffError('Файл для репетиции должен быть не больше 20 МБ.')
        row = create_replica(product_id, [source.read_bytes()])
        db.session.commit()
    except (TariffError, OSError) as error:
        db.session.rollback()
        raise click.ClickException(str(error)) from error
    click.echo(f'Independent replica #{row.id} created. Source file unchanged.')


@click.command('tariffs-v2-replica-purge')
@click.option('--replica-id', type=int, required=True)
@click.option('--execute', is_flag=True, help='Remove registered replica files only after current eligibility checks.')
@with_appcontext
def replica_purge(replica_id, execute):
    """Rehearse expiry cleanup, with an explicit execution flag and durable recovery."""
    from app.utils.tariffs_v2_replicas import purge_replica, store_root
    from app.models.tariffs_v2_lifecycle import TariffV2Replica
    from app.utils.tariffs_v2 import TariffError
    try:
        store_root()
        row = db.session.get(TariffV2Replica, replica_id)
        if not row:
            raise TariffError('Тестовая копия не найдена.')
        if execute:
            row = purge_replica(replica_id)
        click.echo(f'Replica #{row.id}: {row.status}. ' + ('Replica execution finished.' if execute else 'Preview only; no files deleted.'))
    except (TariffError, OSError) as error:
        db.session.rollback()
        raise click.ClickException(str(error)) from error
