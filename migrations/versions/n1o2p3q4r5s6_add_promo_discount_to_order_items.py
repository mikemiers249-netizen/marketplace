"""add promo_discount to order_items

Revision ID: n1o2p3q4r5s6
Revises: m1n2o3p4q5r6
Create Date: 2026-07-21 22:50:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'n1o2p3q4r5s6'
down_revision = 'm1n2o3p4q5r6'
branch_labels = None
depends_on = None


def upgrade():
    # Скидка по промо-акции (в рублях на позицию, NULL = не заполнено).
    # Записывается при оформлении заказа через compute_item_discount_breakdown:
    # функция считает, какая часть скидки пришла от базовой скидки товара, а
    # какая — от промо (discount / second_with_discount / 1+1 и т.д.).
    # Правило "не суммируется — берём максимум" сохраняется: пишется
    # либо current_discount, либо promo_discount, но не оба.
    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('promo_discount', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.drop_column('promo_discount')
