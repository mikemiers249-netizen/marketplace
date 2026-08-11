"""
Кастомные Flask CLI-команды.
"""

import click
from flask.cli import with_appcontext

from app import db


@click.command("db-init")
@with_appcontext
def db_init_command():
    """
    Создаёт ВСЕ таблицы по текущему состоянию моделей
    через db.create_all(). Безопасно для пустой БД.

    Использование:
        FLASK_APP=wsgi.py flask db-init
    """
    # Импортируем все модули моделей, чтобы они зарегистрировались
    # в Base.metadata.
    from app.models import users  # noqa
    from app.models import products  # noqa
    from app.models import orders  # noqa
    from app.models import communications  # noqa
    from app.models import loyalty  # noqa
    from app.models import promo  # noqa
    from app.models import tariffs  # noqa

    click.echo("Creating all tables via db.create_all()...")
    db.create_all()
    click.echo("Done. Tables created.")


@click.command("reset-public-schema")
@click.option("--yes", "-y", is_flag=True, help="Подтверждение — без этого не выполнится")
@with_appcontext
def reset_public_schema_command(yes):
    """
    Дропает и пересоздаёт схему `public` в PostgreSQL.
    БЕЗОПАСНО ТОЛЬКО ДЛЯ СВЕЖЕЙ БД без данных!

    Использование:
        FLASK_APP=wsgi.py flask reset-public-schema --yes
    """
    if not yes:
        click.echo(
            "ABORT: требуется --yes для подтверждения. "
            "БД будет полностью очищена!"
        )
        raise click.Abort()

    click.echo("Dropping schema public...")
    with db.engine.begin() as conn:
        try:
            conn.execute(db.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            ))
        except Exception as e:
            click.echo(f"  warn: pg_terminate_backend failed: {e}")

        conn.execute(db.text("DROP SCHEMA public CASCADE"))
        conn.execute(db.text("CREATE SCHEMA public"))
        conn.execute(db.text("GRANT ALL ON SCHEMA public TO postgres"))
        conn.execute(db.text("GRANT ALL ON SCHEMA public TO public"))

    db.engine.dispose()
    click.echo("Schema public recreated. Now run: flask db-init && flask db upgrade heads")
