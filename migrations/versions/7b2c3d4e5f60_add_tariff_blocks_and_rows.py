"""add tariff blocks and rows

Revision ID: 7b2c3d4e5f60
Revises:
Create Date: 2026-07-28 22:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7b2c3d4e5f60'
down_revision = 'u3v4w5x6y7z8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tariff_blocks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('section', sa.String(length=20), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('is_published', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tariff_blocks_section', 'tariff_blocks', ['section'])
    op.create_index('ix_tariff_blocks_sort_order', 'tariff_blocks', ['sort_order'])
    op.create_index('ix_tariff_blocks_is_published', 'tariff_blocks', ['is_published'])

    op.create_table(
        'tariff_rows',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('block_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('price', sa.String(length=100), nullable=True),
        sa.Column('period', sa.String(length=50), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('is_published', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['block_id'], ['tariff_blocks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tariff_rows_block_id', 'tariff_rows', ['block_id'])
    op.create_index('ix_tariff_rows_sort_order', 'tariff_rows', ['sort_order'])
    op.create_index('ix_tariff_rows_is_published', 'tariff_rows', ['is_published'])


def downgrade():
    op.drop_index('ix_tariff_rows_is_published', table_name='tariff_rows')
    op.drop_index('ix_tariff_rows_sort_order', table_name='tariff_rows')
    op.drop_index('ix_tariff_rows_block_id', table_name='tariff_rows')
    op.drop_table('tariff_rows')

    op.drop_index('ix_tariff_blocks_is_published', table_name='tariff_blocks')
    op.drop_index('ix_tariff_blocks_sort_order', table_name='tariff_blocks')
    op.drop_index('ix_tariff_blocks_section', table_name='tariff_blocks')
    op.drop_table('tariff_blocks')
