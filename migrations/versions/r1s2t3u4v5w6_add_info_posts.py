"""add info_posts table for dashboard info section

Revision ID: r1s2t3u4v5w6
Revises: q1r2s3t4u5v6
Create Date: 2026-07-26 22:25:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'r1s2t3u4v5w6'
down_revision = 'q1r2s3t4u5v6'
branch_labels = None
depends_on = None


def upgrade():
    # Новости/посты раздела "Информация" в дашбордах админа и продавца.
    op.create_table(
        'info_posts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('media_type', sa.String(length=20), nullable=True),
        sa.Column('media_url', sa.String(length=500), nullable=True),
        sa.Column('audience', sa.String(length=20), nullable=False, server_default='all'),
        sa.Column('is_published', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('sort_date', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_info_posts_audience', 'info_posts', ['audience'])
    op.create_index('ix_info_posts_is_published', 'info_posts', ['is_published'])
    op.create_index('ix_info_posts_sort_date', 'info_posts', ['sort_date'])


def downgrade():
    op.drop_index('ix_info_posts_sort_date', table_name='info_posts')
    op.drop_index('ix_info_posts_is_published', table_name='info_posts')
    op.drop_index('ix_info_posts_audience', table_name='info_posts')
    op.drop_table('info_posts')
