"""
Кастомные Flask CLI-команды.

Используется только в аварийных случаях, когда БД в Coolify
застряла в сломанном состоянии (например, после частично
выполненного db.create_all()).

После того, как схема очищена и миграции прогнаны, эту команду
больше вызывать не нужно.
"""

import click
from flask.cli import with_appcontext

from app import db


@click.command("reset-public-schema")
@click.option("--yes", "-y", is_flag=True, help="Подтверждение — без этого не выполнится")
@with_appcontext
def reset_public_schema_command(yes):
    """
    Дропает и пересоздаёт схему `public` в PostgreSQL.
    БЕЗОПАСНО ТОЛЬКО ДЛЯ СВЕЖЕЙ БД без данных!

    Использование:
        FLASK_APP=wsgi.py flask reset-public-schema --yes
        flask db upgrade
    """
    if not yes:
        click.echo(
            "ABORT: требуется --yes для подтверждения. "
            "БД будет полностью очищена!"
        )
        raise click.Abort()

    # Дропаем схему. Используем engine напрямую, потому что после
    # DROP SCHEMA db.session становится бесполезным.
    click.echo("Dropping schema public...")
    with db.engine.begin() as conn:
        conn.execute(db.text("DROP SCHEMA public CASCADE"))
        conn.execute(db.text("CREATE SCHEMA public"))
        conn.execute(db.text("GRANT ALL ON SCHEMA public TO postgres"))
        conn.execute(db.text("GRANT ALL ON SCHEMA public TO public"))
    click.echo("Schema public recreated. Now run: flask db upgrade")
