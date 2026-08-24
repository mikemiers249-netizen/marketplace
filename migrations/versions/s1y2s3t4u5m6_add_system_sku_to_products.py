"""add system_sku to products

Revision ID: s1y2s3t4u5m6
Revises: m1e2r3g4e5_6
Create Date: 2026-08-24 14:18:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 's1y2s3t4u5m6'
down_revision = 'm1e2r3g4e5_6'
branch_labels = None
depends_on = None


def upgrade():
    # Внутренний системный артикул. Генерируется на сохранении нового товара
    # в формате WML-{seller_id}-{ms_timestamp}. Уникален, но nullable=True —
    # на случай старых товаров, добавленных до выкатки фичи.
    op.add_column(
        'products',
        sa.Column('system_sku', sa.String(length=64), nullable=True)
    )
    op.create_index(
        op.f('ix_products_system_sku'),
        'products',
        ['system_sku'],
        unique=True
    )


def downgrade():
    op.drop_index(op.f('ix_products_system_sku'), table_name='products')
    op.drop_column('products', 'system_sku')
