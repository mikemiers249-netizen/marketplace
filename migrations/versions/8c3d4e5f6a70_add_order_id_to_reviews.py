"""add order_id to reviews (one review per order line item)

Revision ID: 8c3d4e5f6a70
Revises: 7b2c3d4e5f60
Create Date: 2026-07-28 23:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8c3d4e5f6a70'
down_revision = '7b2c3d4e5f60'
branch_labels = None
depends_on = None


def upgrade():
    # Nullable, чтобы старые отзывы (без привязки к заказу) продолжали работать
    # и сохранить обратную совместимость со старой формой отправки отзыва.
    with op.batch_alter_table('reviews', schema=None) as batch_op:
        batch_op.add_column(sa.Column('order_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_reviews_order_id_orders',
            'orders', ['order_id'], ['id']
        )
        batch_op.create_index('ix_reviews_order_id', ['order_id'], unique=False)


def downgrade():
    with op.batch_alter_table('reviews', schema=None) as batch_op:
        batch_op.drop_index('ix_reviews_order_id')
        batch_op.drop_constraint('fk_reviews_order_id_orders', type_='foreignkey')
        batch_op.drop_column('order_id')
