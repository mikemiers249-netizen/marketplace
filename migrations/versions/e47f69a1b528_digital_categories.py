"""Categories, attributes and articles for electronic books."""
from alembic import op
from app.utils.digital_catalog import ensure_schema

revision = 'e47f69a1b528'
down_revision = 'd36e58f0a417'
branch_labels = None
depends_on = None


def upgrade():
    ensure_schema(op.get_bind())


def downgrade():
    # Keep category assignments, articles and purchases on rollback.
    pass
