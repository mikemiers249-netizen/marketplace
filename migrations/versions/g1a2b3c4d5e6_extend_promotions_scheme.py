"""Extend promotions.scheme to VARCHAR(30)

Модель Promotion.scheme объявлена как String(30) (нужно для значений вроде
'one_plus_one' и для запаса под будущие схемы), но историческая миграция
создала колонку с VARCHAR(20). При текущих значениях схем строка
'one_plus_one' (12 символов) помещается, поэтому баг не выстреливал, но
расхождение между моделью и БД мешает будущим изменениям и сбивает с толку
при чтении схемы.

Изменения:
  • promotions.scheme VARCHAR(20) → VARCHAR(30)

Также подтверждаем наличие promotions.min_product_price (добавлено в
предыдущей миграции f3a4b5c6d7e8 для схемы 1+1).

Revision ID: g1a2b3c4d5e6
Revises: f3a4b5c6d7e8
Create Date: 2026-07-19 14:30:00
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g1a2b3c4d5e6'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite требует batch_alter_table для ALTER COLUMN
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.alter_column(
            'scheme',
            existing_type=sa.String(length=20),
            type_=sa.String(length=30),
            existing_nullable=False,
        )


def downgrade():
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.alter_column(
            'scheme',
            existing_type=sa.String(length=30),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
