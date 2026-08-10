"""add grace period and billing fields

Revision ID: t4a5r6i7f8f9
Revises: h1u2c3a4s5c6
Create Date: 2026-08-01 14:55:00.000000

Что меняем:
  • seller_tariff_subscriptions.grace_until — крайний срок оплаты
    (expires_at + 5 дней). NULL = грейса нет (бессрочно или правило
    глобальное без активации).
  • seller_tariff_subscriptions.last_billed_at — крайняя дата списания
    по правилу (для percent-тарифов с billing_period='monthly').
  • tariff_rows.period_days — длительность периода для глобального
    правила (по умолчанию 30, для kind='cards' берётся из
    duration_days, для глобальных — period_days). NULL = использовать
    30 как фолбэк.
  • tariff_rows.kind / percent_rate / billing_period уже есть; новых
    не требуется.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't4a5r6i7f8f9'
down_revision = 'h1u2c3a4s5c6'
branch_labels = None
depends_on = None


def upgrade():
    # Подписки: грейс-период и точка отсчёта биллинга
    with op.batch_alter_table('seller_tariff_subscriptions') as batch_op:
        batch_op.add_column(sa.Column('grace_until', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('last_billed_at', sa.DateTime(), nullable=True))

    # Глобальные правила: период в днях (по умолчанию 30, фолбэк на уровне модели)
    with op.batch_alter_table('tariff_rows') as batch_op:
        batch_op.add_column(sa.Column('period_days', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('tariff_rows') as batch_op:
        batch_op.drop_column('period_days')

    with op.batch_alter_table('seller_tariff_subscriptions') as batch_op:
        batch_op.drop_column('last_billed_at')
        batch_op.drop_column('grace_until')
