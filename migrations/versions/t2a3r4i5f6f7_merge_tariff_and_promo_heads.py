"""merge heads t1a2r3i4f5f6 and x1y2z3a4b5c6

Revision ID: t2a3r4i5f6f7
Revises: t1a2r3i4f5f6, x1y2z3a4b5c6
Create Date: 2026-07-31 22:20:00.000000

Соединяем две параллельные ветки миграций (тарифы и правки промо) в
одну общую голову. Никаких DDL не требуется.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't2a3r4i5f6f7'
down_revision = ('t1a2r3i4f5f6', 'x1y2z3a4b5c6')
branch_labels = None
depends_on = None


def upgrade():
    # Merge-ревизия: никаких DDL.
    pass


def downgrade():
    # Откатить merge нельзя без разделения веток обратно.
    pass
