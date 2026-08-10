"""add edu_materials table for education section

Revision ID: u3v4w5x6y7z8
Revises: t3u4v5w6x7y8
Create Date: 2026-07-28 21:35:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'u3v4w5x6y7z8'
down_revision = 't3u4v5w6x7y8'
branch_labels = None
depends_on = None


def upgrade():
    # Учебные материалы в разделе «Информация → Обучение».
    # У каждого материала: название, длинный HTML-контент «с вёрсткой»,
    # опциональная обложка-фон, тег для фильтра и аудитория.
    op.create_table(
        'edu_materials',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('cover_url', sa.String(length=500), nullable=True),
        sa.Column('tag', sa.String(length=50), nullable=True),
        sa.Column('audience', sa.String(length=20), nullable=False, server_default='all'),
        sa.Column('is_published', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.create_index('ix_edu_materials_tag', 'edu_materials', ['tag'])
    op.create_index('ix_edu_materials_audience', 'edu_materials', ['audience'])
    op.create_index('ix_edu_materials_is_published', 'edu_materials', ['is_published'])
    op.create_index('ix_edu_materials_created_at', 'edu_materials', ['created_at'])


def downgrade():
    op.drop_index('ix_edu_materials_created_at', table_name='edu_materials')
    op.drop_index('ix_edu_materials_is_published', table_name='edu_materials')
    op.drop_index('ix_edu_materials_audience', table_name='edu_materials')
    op.drop_index('ix_edu_materials_tag', table_name='edu_materials')
    op.drop_table('edu_materials')
