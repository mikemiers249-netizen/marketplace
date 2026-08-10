"""add views_count and cart_adds_count to products

Revision ID: p1q2r3s4t5u6
Revises: n1o2p3q4r5s6
Create Date: 2026-07-22 23:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'p1q2r3s4t5u6'
down_revision = 'n1o2p3q4r5s6'
branch_labels = None
depends_on = None


def upgrade():
    # Счётчики для аналитики продавца:
    #   views_count    — сколько раз открывали карточку товара
    #   cart_adds_count — сколько раз добавляли в корзину
    # default=0, NOT NULL, чтобы простая агрегация SUM() давала
    # корректные числа даже для старых товаров.
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'views_count', sa.Integer(), nullable=False, server_default='0'
        ))
        batch_op.add_column(sa.Column(
            'cart_adds_count', sa.Integer(), nullable=False, server_default='0'
        ))


def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('cart_adds_count')
        batch_op.drop_column('views_count')
