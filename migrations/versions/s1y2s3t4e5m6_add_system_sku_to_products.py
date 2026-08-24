"""add system_sku to products

Revision ID: s1y2s3t4e5m6
Revises: d2e3f4a5b6c7
Create Date: 2026-08-24 13:25:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 's1y2s3t4e5m6'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    # Внутренний системный артикул. Генерируется на сохранении нового товара
    # в формате WML-{seller_id}-{timestamp}. Уникален, но nullable=True —
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
