"""add roadmap_events table for project roadmap section

Revision ID: t3u4v5w6x7y8
Revises: s2t3u4v5w6x7
Create Date: 2026-07-27 22:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't3u4v5w6x7y8'
down_revision = 's2t3u4v5w6x7'
branch_labels = None
depends_on = None


def upgrade():
    # События «Траектории развития проекта» в дашбордах админа и продавца.
    # Хранят календарную дату + категорию (для цвета маркера) + описание
    # (раскрывается под лентой событий по клику).
    op.create_table(
        'roadmap_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('event_date', sa.Date(), nullable=False),
        sa.Column('category', sa.String(length=20), nullable=False, server_default='event'),
        sa.Column('audience', sa.String(length=20), nullable=False, server_default='all'),
        sa.Column('is_published', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('ix_roadmap_events_event_date', 'roadmap_events', ['event_date'])
    op.create_index('ix_roadmap_events_category', 'roadmap_events', ['category'])
    op.create_index('ix_roadmap_events_audience', 'roadmap_events', ['audience'])
    op.create_index('ix_roadmap_events_is_published', 'roadmap_events', ['is_published'])


def downgrade():
    op.drop_index('ix_roadmap_events_is_published', table_name='roadmap_events')
    op.drop_index('ix_roadmap_events_audience', table_name='roadmap_events')
    op.drop_index('ix_roadmap_events_category', table_name='roadmap_events')
    op.drop_index('ix_roadmap_events_event_date', table_name='roadmap_events')
    op.drop_table('roadmap_events')
