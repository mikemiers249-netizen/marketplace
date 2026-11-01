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
    from app.utils.tariffs_v2 import MSK, utc
    from datetime import timedelta
    app.register_blueprint(bp)

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
    tables = [table for table in db.metadata.sorted_tables if table.name.startswith('tariff_v2_')]
    db.metadata.create_all(bind=db.engine, tables=tables)
    click.echo('Tariffs v2 sandbox tables ready. No payments, workers or cleanup enabled.')
