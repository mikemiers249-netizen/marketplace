"""Persistent image cover for digital books."""
from alembic import op
import sqlalchemy as sa

revision = 'f58a70b2c639'
down_revision = 'e47f69a1b528'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if 'cover_name' not in {c['name'] for c in sa.inspect(bind).get_columns('digital_books')}:
        op.add_column('digital_books', sa.Column('cover_name', sa.String(80), nullable=True))


def downgrade():
    # Keep image references on rollback, as with purchased book files.
    pass
