"""Baseline schema: creates all tables that existed before migrations were added.

Revision ID: 000000000000
Revises:
Create Date: 2026-08-11 22:15:00.000000

Это «базовая» миграция, которая создаёт ВСЕ таблицы по текущему
состоянию моделей. Она нужна потому, что в существующей истории
миграций (b6ddf6f76033, add_is_test_mode, ...) ни одна миграция
не создаёт таблицу seller_deliveries — она предполагается уже
существующей. Раньше её создавал db.create_all() в create_app(),
но на проде это запрещено (конфликтует с миграциями).

down_revision = None делает её самой первой в цепочке.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "000000000000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Создаём ВСЕ таблицы по текущему состоянию моделей.
    # Импортируем модели ДО create_all, чтобы они зарегистрировались
    # в Base.metadata.
    from app import db
    # Импорт именно модулей (не пакета) — иначе __init__ пакета
    # может не выполниться.
    from app.models import users  # noqa
    from app.models import products  # noqa
    from app.models import orders  # noqa
    from app.models import communications  # noqa
    from app.models import loyalty  # noqa
    from app.models import promo  # noqa
    from app.models import tariffs  # noqa

    bind = op.get_bind()
    db.metadata.create_all(bind=bind)


def downgrade():
    from app import db
    from app.models import users  # noqa
    from app.models import products  # noqa
    from app.models import orders  # noqa
    from app.models import communications  # noqa
    from app.models import loyalty  # noqa
    from app.models import promo  # noqa
    from app.models import tariffs  # noqa

    bind = op.get_bind()
    db.metadata.drop_all(bind=bind)
