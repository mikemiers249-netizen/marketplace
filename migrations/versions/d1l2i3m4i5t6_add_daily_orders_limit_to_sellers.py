"""add daily_orders_limit to sellers

Revision ID: d1l2i3m4i5t6
Revises: t4a5r6i7f8f9
Create Date: 2026-08-04 21:35:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1l2i3m4i5t6'
down_revision = 't4a5r6i7f8f9'
branch_labels = None
depends_on = None


def upgrade():
    # Дневной лимит заказов на одного продавца.
    # NULL = безлимит. Иначе целое >= 1.
    with op.batch_alter_table('sellers', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'daily_orders_limit', sa.Integer(), nullable=True
        ))


def downgrade():
    with op.batch_alter_table('sellers', schema=None) as batch_op:
        batch_op.drop_column('daily_orders_limit')
