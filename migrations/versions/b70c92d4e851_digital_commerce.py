"""Use the shared commerce engine for digital books."""
revision = 'b70c92d4e851'
down_revision = 'a69b81c3d740'
branch_labels = None
depends_on = None


def upgrade():
    from alembic import op
    from app.utils.digital_catalog import ensure_schema
    ensure_schema(op.get_bind())


def downgrade():
    # Keep product/order/file references and purchased rights intact.
    pass
