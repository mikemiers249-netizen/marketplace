"""Digital moderation and removal of legacy direct publication."""
from alembic import op
from app.utils.digital_catalog import ensure_schema
revision = 'a69b81c3d740'
down_revision = 'f58a70b2c639'
branch_labels = None
depends_on = None


def upgrade():
    ensure_schema(op.get_bind())


def downgrade():
    op.drop_column('digital_books', 'moderation_comment')
    op.drop_column('digital_books', 'moderation_status')
