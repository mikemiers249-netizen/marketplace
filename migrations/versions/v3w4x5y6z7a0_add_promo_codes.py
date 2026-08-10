"""add promo_codes table for seller discount codes

Revision ID: v3w4x5y6z7a0
Revises: u3v4w5x6y7z8
Create Date: 2026-07-29 20:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'v3w4x5y6z7a0'
down_revision = 'u3v4w5x6y7z8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'promo_codes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('seller_id', sa.Integer(), nullable=False),
        sa.Column('discount_type', sa.String(length=16), nullable=False),
        sa.Column('discount_value', sa.Float(), nullable=False),
        sa.Column('min_order_amount', sa.Float(), nullable=True),
        sa.Column('recipient_type', sa.String(length=16), nullable=False),
        sa.Column('buyer_id', sa.Integer(), nullable=True),
        sa.Column('usage_type', sa.String(length=16), nullable=False),
        sa.Column('max_uses', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('used_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('validity_type', sa.String(length=16), nullable=False),
        sa.Column('valid_days', sa.Integer(), nullable=True),
        sa.Column('valid_until', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['seller_id'], ['sellers.id']),
        sa.ForeignKeyConstraint(['buyer_id'], ['buyers.id']),
        sa.UniqueConstraint('seller_id', 'code', name='uq_promo_seller_code'),
    )
    op.create_index('ix_promo_codes_code', 'promo_codes', ['code'])
    op.create_index('ix_promo_codes_seller_id', 'promo_codes', ['seller_id'])
    op.create_index('ix_promo_codes_buyer_id', 'promo_codes', ['buyer_id'])


def downgrade():
    op.drop_index('ix_promo_codes_buyer_id', table_name='promo_codes')
    op.drop_index('ix_promo_codes_seller_id', table_name='promo_codes')
    op.drop_index('ix_promo_codes_code', table_name='promo_codes')
    op.drop_table('promo_codes')
