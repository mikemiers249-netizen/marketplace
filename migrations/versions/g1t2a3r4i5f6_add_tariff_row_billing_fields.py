"""add tariff row billing fields and subscription source

Revision ID: g1t2a3r4i5f6
Revises: t2a3r4i5f6f7
Create Date: 2026-08-01 00:30:00.000000

Добавляет в tariff_rows поля биллинг-движка (kind, is_active, percent_rate,
subject_category_id, billing_period) и в seller_tariff_subscriptions поле
source, чтобы отличать «селлер сам купил» от «глобальный авто-тариф».
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g1t2a3r4i5f6'
down_revision = 't2a3r4i5f6f7'
branch_labels = None
depends_on = None


def upgrade():
    # tariff_rows: новые поля
    with op.batch_alter_table('tariff_rows', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('kind', sa.String(length=20), nullable=False, server_default='cards')
        )
        batch_op.add_column(
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('0'))
        )
        batch_op.add_column(
            sa.Column('percent_rate', sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('subject_category_id', sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('billing_period', sa.String(length=20), nullable=True)
        )
        batch_op.create_index('ix_tariff_rows_kind', ['kind'])
        batch_op.create_index('ix_tariff_rows_is_active', ['is_active'])
        batch_op.create_index('ix_tariff_rows_subject_category_id', ['subject_category_id'])
        batch_op.create_index('ix_tariff_rows_billing_period', ['billing_period'])
        batch_op.create_foreign_key(
            'fk_tariff_rows_subject_category_id_categories',
            'categories',
            ['subject_category_id'],
            ['id'],
        )

    # seller_tariff_subscriptions: source
    with op.batch_alter_table('seller_tariff_subscriptions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('source', sa.String(length=20), nullable=False, server_default='self')
        )
        batch_op.create_index('ix_seller_tariff_subscriptions_source', ['source'])


def downgrade():
    with op.batch_alter_table('seller_tariff_subscriptions', schema=None) as batch_op:
        batch_op.drop_index('ix_seller_tariff_subscriptions_source')
        batch_op.drop_column('source')

    with op.batch_alter_table('tariff_rows', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tariff_rows_subject_category_id_categories', type_='foreignkey')
        batch_op.drop_index('ix_tariff_rows_billing_period')
        batch_op.drop_index('ix_tariff_rows_subject_category_id')
        batch_op.drop_index('ix_tariff_rows_is_active')
        batch_op.drop_index('ix_tariff_rows_kind')
        batch_op.drop_column('billing_period')
        batch_op.drop_column('subject_category_id')
        batch_op.drop_column('percent_rate')
        batch_op.drop_column('is_active')
        batch_op.drop_column('kind')
