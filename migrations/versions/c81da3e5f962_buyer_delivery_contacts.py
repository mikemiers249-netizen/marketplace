"""Recipient contact details for buyer delivery profiles."""
from alembic import op
import sqlalchemy as sa

revision = 'c81da3e5f962'
down_revision = 'b70c92d4e851'
branch_labels = None
depends_on = None


def upgrade():
    existing = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('buyer_deliveries')}
    for name, size in [('recipient_name', 100), ('recipient_phone', 30)]:
        if name not in existing:
            op.add_column('buyer_deliveries', sa.Column(name, sa.String(size), nullable=True))


def downgrade():
    op.drop_column('buyer_deliveries', 'recipient_phone')
    op.drop_column('buyer_deliveries', 'recipient_name')
