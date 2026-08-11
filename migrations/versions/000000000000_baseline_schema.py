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

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "000000000000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Берём все модели из BaseUser / db.Model и создаём таблицы.
    # Чтобы это сработало, нужно, чтобы все модели были импортированы.
    from app import db
    from app.models import (
        users, products, orders, communications, loyalty, promo, tariffs,
    )
    db.create_all()


def downgrade():
    from app import db
    from app.models import (
        users, products, orders, communications, loyalty, promo, tariffs,
    )
    db.drop_all()
