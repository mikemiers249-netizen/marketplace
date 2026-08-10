"""discount promotion template + per-item discount

Добавляет в Promotion флаг apply_same_discount (для схемы discount, когда
продавец использует один общий процент вместо per-item), и в PromotionProduct —
колонку discount_percent для индивидуальной скидки конкретного товара внутри
акции (NULL = использовать общий процент акции).

Revision ID: d1e2f3a4b5c6
Revises: 4a1b2c3d4e5f
Create Date: 2026-07-17 23:20:00
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = '4a1b2c3d4e5f'
branch_labels = None
depends_on = None


def upgrade():
    # Promotion.apply_same_discount: для схемы 'discount' — общий процент на все
    # товары акции (по умолчанию True — для совместимости с уже существующими
    # классическими акциями, где процент один).
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'apply_same_discount',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ))

    # PromotionProduct.discount_percent: индивидуальный процент для конкретного
    # товара внутри акции. NULL = использовать общий процент акции.
    with op.batch_alter_table('promotion_products', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'discount_percent',
            sa.Integer(),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('promotion_products', schema=None) as batch_op:
        batch_op.drop_column('discount_percent')

    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.drop_column('apply_same_discount')
