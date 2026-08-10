"""Add one_plus_one scheme support and min_product_price

Добавляет возможность создавать шаблонные акции формата 1+1 (купи один —
получи второй со скидкой 99%), для которых продавец выбирает участвующие
товары через PromotionProduct. Расчёт скидки — на уровне корзины продавца
по принципу floor(N/2) самых дешёвых участвующих товаров.

Введено:
  • promotions.min_product_price — минимальная цена товара, ниже которой
    продавец не может добавить товар в акцию (NULL = ограничения нет).
  • promotions.discount_percent остаётся обязательным значением скидки
    для новой схемы (по умолчанию админ задаёт 99 %).

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-19 13:55:00
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'min_product_price',
            sa.Float(),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('promotions', schema=None) as batch_op:
        batch_op.drop_column('min_product_price')
