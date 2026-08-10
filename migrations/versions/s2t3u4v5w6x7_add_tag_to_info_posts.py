"""add tag column to info_posts

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
Create Date: 2026-07-26 22:55:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 's2t3u4v5w6x7'
down_revision = 'r1s2t3u4v5w6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('info_posts', sa.Column('tag', sa.String(length=50), nullable=True))
    op.create_index('ix_info_posts_tag', 'info_posts', ['tag'])


def downgrade():
    op.drop_index('ix_info_posts_tag', table_name='info_posts')
    op.drop_column('info_posts', 'tag')
