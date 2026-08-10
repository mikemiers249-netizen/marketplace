"""add product_events table for period-aware analytics

Revision ID: q1r2s3t4u5v6
Revises: p1q2r3s4t5u6
Create Date: 2026-07-22 23:25:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'q1r2s3t4u5v6'
down_revision = 'p1q2r3s4t5u6'
branch_labels = None
depends_on = None


def upgrade():
    # Таблица событий по товарам. Источник истины для конверсии
    # "просмотры → заказы" в заданном периоде. Накопительные
    # views_count / cart_adds_count на Product остаются как кэш для UI.
    op.create_table(
        'product_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('product_id', sa.Integer(),
                  sa.ForeignKey('products.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('seller_id', sa.Integer(),
                  sa.ForeignKey('sellers.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('buyer_id', sa.Integer(),
                  sa.ForeignKey('buyers.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('event_type', sa.String(length=20), nullable=False),
        sa.Column('session_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_product_events_product_id', 'product_events', ['product_id'])
    op.create_index('ix_product_events_seller_id', 'product_events', ['seller_id'])
    op.create_index('ix_product_events_buyer_id', 'product_events', ['buyer_id'])
    op.create_index('ix_product_events_event_type', 'product_events', ['event_type'])
    op.create_index('ix_product_events_created_at', 'product_events', ['created_at'])
    op.create_index(
        'ix_product_events_seller_type_time',
        'product_events',
        ['seller_id', 'event_type', 'created_at'],
    )


def downgrade():
    op.drop_index('ix_product_events_seller_type_time', table_name='product_events')
    op.drop_index('ix_product_events_created_at', table_name='product_events')
    op.drop_index('ix_product_events_event_type', table_name='product_events')
    op.drop_index('ix_product_events_buyer_id', table_name='product_events')
    op.drop_index('ix_product_events_seller_id', table_name='product_events')
    op.drop_index('ix_product_events_product_id', table_name='product_events')
    op.drop_table('product_events')
