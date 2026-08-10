"""Add is_test_mode and other CDEK fields to seller_deliveries

Revision ID: add_is_test_mode
Revises: b6ddf6f76033
Create Date: 2026-03-10 04:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_is_test_mode'
down_revision = 'b6ddf6f76033'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('seller_deliveries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_test_mode', sa.Boolean(), nullable=True, server_default='1'))
        batch_op.add_column(sa.Column('contract_number', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('pvz_code', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('pvz_address', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('pvz_city', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('tariffs', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('seller_deliveries', schema=None) as batch_op:
        batch_op.drop_column('tariffs')
        batch_op.drop_column('pvz_city')
        batch_op.drop_column('pvz_address')
        batch_op.drop_column('pvz_code')
        batch_op.drop_column('contract_number')
        batch_op.drop_column('is_test_mode')
