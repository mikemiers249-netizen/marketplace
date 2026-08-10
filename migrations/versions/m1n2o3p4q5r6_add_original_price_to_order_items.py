"""add original_price to order_items

Revision ID: m1n2o3p4q5r6
Revises: l1o2y3a4l5t6
Create Date: 2026-07-21 22:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'm1n2o3p4q5r6'
down_revision = 'l1o2y3a4l5t6'
branch_labels = None
depends_on = None


def upgrade():
    # Сохраняем оригинальную цену (до скидки) в позиции заказа.
    # Нужно, чтобы в карточке заказа продавца корректно показывать
    # расшифровку: цена до скидки, размер скидки, цена после скидки.
    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('original_price', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.drop_column('original_price')
