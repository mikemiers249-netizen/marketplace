"""merge heads 8c3d4e5f6a70 and v3w4x5y6z7a0

Revision ID: w4x5y6z7a0b1
Revises: 8c3d4e5f6a70, v3w4x5y6z7a0
Create Date: 2026-07-29 20:35:00.000000

Merge двух параллельных веток миграций. Никаких изменений в БД
не требуется — обе ветки заканчиваются, остаётся общая голова.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'w4x5y6z7a0b1'
down_revision = ('8c3d4e5f6a70', 'v3w4x5y6z7a0')
branch_labels = None
depends_on = None


def upgrade():
    # Чисто merge-ревизия: никаких DDL.
    pass


def downgrade():
    # Откатить merge нельзя без разделения веток обратно.
    pass
