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
    """
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


@click.command("fix-password-length")
@with_appcontext
def fix_password_length_command():
    """
    Увеличивает длину колонки password_hash до 256 символов.
    Нужно после изменения модели, если таблица уже создана.
    """
    click.echo("Altering password_hash columns to VARCHAR(256)...")
    with db.engine.begin() as conn:
        for table in ("buyers", "sellers", "admins"):
            try:
                conn.execute(db.text(
                    f"ALTER TABLE {table} ALTER COLUMN password_hash TYPE VARCHAR(256)"
                ))
                click.echo(f"  ok: {table}")
            except Exception as e:
                click.echo(f"  warn: {table}: {e}")
    click.echo("Done.")


@click.command("reset-public-schema")
@click.option("--yes", "-y", is_flag=True, help="Подтверждение — без этого не выполнится")
@with_appcontext
def reset_public_schema_command(yes):
    """
    Дропает и пересоздаёт схему `public` в PostgreSQL.
    БЕЗОПАСНО ТОЛЬКО ДЛЯ СВЕЖЕЙ БД без данных!
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
    click.echo("Schema public recreated. Now run: flask db-init && flask db stamp heads")


@click.command("grant-test-tariff")
@click.argument("seller_id", type=int)
@click.option("--days", default=30, help="Срок подписки в днях")
@with_appcontext
def grant_test_tariff_command(seller_id, days):
    """
    Аварийно выдаёт seller'у тестовую global-подписку.

    Используется для отладки — минует логику активации и эмулирует
    уже существующую подписку source='global_auto', is_paid=False,
    expires_at=now+days.

    FLASK_APP=wsgi.py flask grant-test-tariff 1 --days 30
    """
    from app.models.tariffs import SellerTariffSubscription
    from app.models.communications import TariffRow, TariffBlock
    from app.models.users import Seller
    from datetime import datetime, timedelta

    seller = Seller.query.get(seller_id)
    if not seller:
        click.echo(f"Seller {seller_id} not found")
        return

    # Берём первый подходящий глобальный тариф
    row = (
        TariffRow.query
        .join(TariffBlock, TariffBlock.id == TariffRow.block_id)
        .filter(TariffRow.is_published.is_(True))
        .filter(TariffRow.is_active.is_(True))
        .first()
    )
    if not row:
        click.echo("No published TariffRow found. Create one in admin first.")
        return

    now = datetime.utcnow()
    sub = SellerTariffSubscription(
        seller_id=seller_id,
        row_id=row.id,
        source=SellerTariffSubscription.SOURCE_GLOBAL_AUTO,
        is_paid=False,
        status=SellerTariffSubscription.STATUS_ACTIVE,
        activated_at=now,
        expires_at=now + timedelta(days=days),
    )
    sub.recompute_grace(5)
    db.session.add(sub)
    db.session.commit()
    click.echo(f"Granted test subscription id={sub.id} to seller={seller_id}, "
               f"row={row.id} ({row.name}), expires_at={sub.expires_at}")


@click.command("clear-seller-subs")
@click.option("--seller-id", type=int, default=None,
              help="Чистить только этого продавца (по умолчанию — всех)")
@click.option("--yes", "-y", is_flag=True,
              help="Не спрашивать подтверждения")
@with_appcontext
def clear_seller_subs_command(seller_id, yes):
    """
    Удалить ВСЕ подписки (и связанные транзакции) для продавца.

    Используется для отладки: подчистка БД от мусорных подписок после
    многократных нажатий 'Активировать' / grant-test-tariff.

    FLASK_APP=wsgi.py flask clear-seller-subs --seller-id 1 --yes
    """
    from app.models.tariffs import SellerTariffSubscription, TariffTransaction

    sub_q = SellerTariffSubscription.query
    if seller_id is not None:
        sub_q = sub_q.filter(SellerTariffSubscription.seller_id == seller_id)
    subs = sub_q.order_by(SellerTariffSubscription.id.asc()).all()

    if not subs:
        click.echo("No subscriptions found.")
        return

    click.echo(f"Found {len(subs)} subscription(s):")
    for sub in subs:
        click.echo(f"  id={sub.id} seller={sub.seller_id} row={sub.row_id} "
                   f"source={sub.source} is_paid={sub.is_paid} status={sub.status} "
                   f"expires_at={sub.expires_at}")

    if not yes:
        click.echo("ABORT: pass --yes to actually delete.")
        raise click.Abort()

    # Сначала удалим все связанные TariffTransaction (FK subscription_id
    # может быть ON DELETE SET NULL, но удалим явно, чтобы не оставлять
    # мусор).
    sub_ids = [s.id for s in subs]
    deleted_txs = TariffTransaction.query.filter(
        TariffTransaction.subscription_id.in_(sub_ids)
    ).delete(synchronize_session=False)

    deleted_subs = 0
    for sub in subs:
        db.session.delete(sub)
        deleted_subs += 1
    db.session.commit()
    click.echo(f"Deleted {deleted_subs} subscription(s) and {deleted_txs} transaction(s).")

@click.command("clean-test-subs")
@click.option("--seller-id", type=int, default=None,
              help="Чистить только этого продавца (по умолчанию — всех)")
@click.option("--yes", "-y", is_flag=True,
              help="Не спрашивать подтверждения")
@with_appcontext
def clean_test_subs_command(seller_id, yes):
    """
    Удалить «тестовые» подписки grant-test-tariff, чтобы прибрать БД.

    Условие «тестовости»:
      • source='global_auto'
      • is_paid=False
      • НЕ привязана к TariffTransaction (то есть не было реального списания)

    Удаляются и сами подписки, и связанные transactions, у которых
    subscription_id указывает на удаляемую запись. Это безопасный
    dry-cleanup: если подписка уже «обросла» транзакциями (не тестовыми),
    она пропускается.

    FLASK_APP=wsgi.py flask clean-test-subs --seller-id 1 --yes
    """
    from app.models.tariffs import SellerTariffSubscription, TariffTransaction

    q = SellerTariffSubscription.query.filter(
        SellerTariffSubscription.source == SellerTariffSubscription.SOURCE_GLOBAL_AUTO,
        SellerTariffSubscription.is_paid.is_(False),
    )
    if seller_id is not None:
        q = q.filter(SellerTariffSubscription.seller_id == seller_id)
    subs = q.order_by(SellerTariffSubscription.id.asc()).all()

    if not subs:
        click.echo("No test subscriptions found.")
        return

    # Отфильтровать те, у которых есть транзакции (значит, не тестовая).
    real_ids = set()
    for sub in subs:
        txs = sub.transactions.all()
        if txs:
            real_ids.add(sub.id)
    subs = [s for s in subs if s.id not in real_ids]

    if not subs:
        click.echo("All global_auto subs have transactions — nothing to clean.")
        return

    click.echo(f"Found {len(subs)} test subscription(s):")
    for sub in subs:
        click.echo(f"  id={sub.id} seller={sub.seller_id} row={sub.row_id} "
                   f"expires_at={sub.expires_at}")

    if not yes:
        click.echo("ABORT: pass --yes to actually delete.")
        raise click.Abort()

    deleted = 0
    for sub in subs:
        # Связанные транзакции удалятся каскадом, если у них
        # subscription_id указывает на этот sub и нет cascade — удалим руками.
        TariffTransaction.query.filter(
            TariffTransaction.subscription_id == sub.id
        ).delete(synchronize_session=False)
        db.session.delete(sub)
        deleted += 1
    db.session.commit()
    click.echo(f"Deleted {deleted} test subscription(s).")
