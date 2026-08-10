"""Add apply_same_discount to seller_promotions

Позволяет продавцу для шаблонной discount-акции выбирать:
  - единый процент скидки на все свои товары в этой акции;
  - индивидуальный процент для каждого товара (per-item, через PromotionProduct).

NULL у нового поля = наследовать Promotion.apply_same_discount.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-17 23:30:00
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('seller_promotions', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'apply_same_discount',
            sa.Boolean(),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('seller_promotions', schema=None) as batch_op:
        batch_op.drop_column('apply_same_discount')
