"""make promotions.start_date and end_date nullable

Тимшаблон акции (is_template=True) может быть бессрочным — без дат старта/окончания.
Даты задаёт уже продавец при подключении (SellerPromotion). Раньше NOT NULL мешал
создавать такие шаблоны — падало на INSERT.

Revision ID: 4a1b2c3d4e5f
Revises: c7d8e9f0a1b2
Create Date: 2026-07-16 21:57:00
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4a1b2c3d4e5f'
down_revision = 'c7d8e9f0a1b2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.alter_column('start_date', existing_type=sa.DateTime(), nullable=True)
        batch_op.alter_column('end_date', existing_type=sa.DateTime(), nullable=True)


def downgrade():
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.alter_column('start_date', existing_type=sa.DateTime(), nullable=False)
        batch_op.alter_column('end_date', existing_type=sa.DateTime(), nullable=False)
