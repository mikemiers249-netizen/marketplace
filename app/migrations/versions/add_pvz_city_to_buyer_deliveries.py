"""Add pvz_city and pvz_city_code to BuyerDelivery

Revision ID: add_pvz_city_to_buyer_deliveries
Revises:
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = 'add_pvz_city_to_buyer_deliveries'
down_revision = None  # Заменить на актуальный parent revision
branch_labels = None
depends_on = None


def upgrade():
    # Добавляем поле pvz_city
    op.add_column('buyer_deliveries', sa.Column('pvz_city', sa.String(length=100), nullable=True))
    # Добавляем поле pvz_city_code
    op.add_column('buyer_deliveries', sa.Column('pvz_city_code', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('buyer_deliveries', 'pvz_city_code')
    op.drop_column('buyer_deliveries', 'pvz_city')
