"""add received status to orders

Revision ID: a1b2c3d4e5f6
Revises: 3ce89af7a4a8
Create Date: 2026-07-15 21:35:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '3ce89af7a4a8'
branch_labels = None
depends_on = None


def upgrade():
    # Добавляем колонку для времени подтверждения получения покупателем
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('received_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('received_at')
