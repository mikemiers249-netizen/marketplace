"""Add optional seller minimum order amount."""
from alembic import op
import sqlalchemy as sa

revision = 't2u3v4w5x6y7'
down_revision = 's1y2s3t4u5m6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sellers', sa.Column('minimum_order_amount', sa.Numeric(12, 2), nullable=True))


def downgrade():
    op.drop_column('sellers', 'minimum_order_amount')
