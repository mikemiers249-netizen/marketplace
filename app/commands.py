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
@with_appcontext
def reset_public_schema_command():
    """
    Дропает и пересоздаёт схему `public` в PostgreSQL.
    БЕЗОПАСНО ТОЛЬКО ДЛЯ СВЕЖЕЙ БД без данных!

    Использование (внутри контейнера):
        FLASK_APP=wsgi.py flask reset-public-schema
        flask db upgrade
    """
    # Проверяем, не мигрирована ли БД
    inspector = db.inspect(db.engine)
    if "alembic_version" in inspector.get_table_names():
        click.echo(
            "ABORT: alembic_version table already exists. "
            "БД уже мигрирована — дропать схему НЕЛЬЗЯ, потеряешь данные."
        )
        raise click.Abort()

    click.echo("Dropping schema public...")
    db.session.execute(db.text("DROP SCHEMA public CASCADE"))
    db.session.execute(db.text("CREATE SCHEMA public"))
    db.session.execute(db.text("GRANT ALL ON SCHEMA public TO postgres"))
    db.session.execute(db.text("GRANT ALL ON SCHEMA public TO public"))
    db.session.commit()
    click.echo("Schema public recreated. Now run: flask db upgrade")
