"""merge branches: combine d1l2i3m4i5t6 (daily_orders_limit) into d2e3f4a5b6c7

Revision ID: m1e2r3g4e5_6
Revises: d1l2i3m4i5t6, d2e3f4a5b6c7
Create Date: 2026-08-24 14:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm1e2r3g4e5_6'
down_revision = ('d1l2i3m4i5t6', 'd2e3f4a5b6c7')
branch_labels = None
depends_on = None


def upgrade():
    # Merge-ревизия: объединяет три параллельные ветки миграций в одну.
    # Реальных изменений в БД не делает — обе ветки уже отражены
    # через db.create_all() в db-init. Нужна только чтобы Alembic
    # имел единственный head и 'flask db upgrade head' не падал с
    # "Multiple head revisions".
    pass


def downgrade():
    pass
