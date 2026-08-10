"""add status and moderated_at to reviews

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-15 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('reviews', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('moderated_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_reviews_status', ['status'], unique=False)

    # Заполняем status на основе старого is_approved для совместимости
    op.execute("UPDATE reviews SET status = 'approved' WHERE is_approved = 1")
    op.execute("UPDATE reviews SET status = 'pending' WHERE is_approved = 0 OR is_approved IS NULL")


def downgrade():
    with op.batch_alter_table('reviews', schema=None) as batch_op:
        batch_op.drop_index('ix_reviews_status')
        batch_op.drop_column('moderated_at')
        batch_op.drop_column('status')
