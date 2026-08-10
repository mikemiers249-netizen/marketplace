"""Add is_template to promotions and create seller_promotions

Revision ID: c7d8e9f0a1b2
Revises: b2c3d4e5f6a7
Create Date: 2026-07-16 21:42:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d8e9f0a1b2'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    # 1) Флаг шаблона у Promotion
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_template', sa.Boolean(), nullable=True, server_default='0'))

    # 2) Связка продавец ↔ шаблонная акция (SellerPromotion)
    op.create_table(
        'seller_promotions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('seller_id', sa.Integer(), sa.ForeignKey('sellers.id'), nullable=False),
        sa.Column('promotion_id', sa.Integer(), sa.ForeignKey('promotions.id'), nullable=False),
        sa.Column('override_discount_percent', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('seller_id', 'promotion_id', name='uq_seller_promotion'),
    )
    op.create_index('ix_seller_promotions_seller_id', 'seller_promotions', ['seller_id'])
    op.create_index('ix_seller_promotions_promotion_id', 'seller_promotions', ['promotion_id'])


def downgrade():
    op.drop_index('ix_seller_promotions_promotion_id', table_name='seller_promotions')
    op.drop_index('ix_seller_promotions_seller_id', table_name='seller_promotions')
    op.drop_table('seller_promotions')

    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.drop_column('is_template')
