"""add promo code fields to orders

Revision ID: x1y2z3a4b5c6
Revises: w4x5y6z7a0b1
Create Date: 2026-07-29 21:25:00.000000

Добавляем в orders поля для применённого промокода:
  * promo_code_id     — FK на promo_codes.id (nullable, на случай если код
                        удалят после заказа, останется nullable, поэтому
                        без ON DELETE, чтобы запись не ломалась);
  * promo_code_text   — зафиксированный код (на случай, если продавец
                        удалит/переименует промокод, в заказе останется
                        именно тот, что был применён);
  * promo_discount    — размер скидки в рублях, посчитанный по промокоду
                        и применённый к total_price (по аналогии с
                        order.bonus_used, который вычитается при оплате).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'x1y2z3a4b5c6'
down_revision = 'w4x5y6z7a0b1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('promo_code_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('promo_code_text', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('promo_discount', sa.Float(), nullable=True))
        batch_op.create_foreign_key(
            'fk_orders_promo_code_id_promo_codes',
            'promo_codes',
            ['promo_code_id'], ['id'],
        )


def downgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_constraint('fk_orders_promo_code_id_promo_codes', type_='foreignkey')
        batch_op.drop_column('promo_discount')
        batch_op.drop_column('promo_code_text')
        batch_op.drop_column('promo_code_id')
