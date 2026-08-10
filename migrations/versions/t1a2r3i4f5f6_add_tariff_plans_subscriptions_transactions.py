"""add tariff plans, subscriptions and transactions

Revision ID: t1a2r3i4f5f6
Revises: b6ddf6f76033
Create Date: 2026-07-31 22:15:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't1a2r3i4f5f6'
down_revision = 'b6ddf6f76033'
branch_labels = None
depends_on = None


def upgrade():
    # Каталог тарифов
    op.create_table(
        'tariff_plans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('price', sa.Float(), nullable=False),
        sa.Column('duration_days', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tariff_plans_is_active', 'tariff_plans', ['is_active'])
    op.create_index('ix_tariff_plans_sort_order', 'tariff_plans', ['sort_order'])

    # Подписки селлеров на тарифы
    op.create_table(
        'seller_tariff_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('seller_id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('is_paid', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('activated_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('paused_at', sa.DateTime(), nullable=True),
        sa.Column('disabled_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['seller_id'], ['sellers.id'], ),
        sa.ForeignKeyConstraint(['plan_id'], ['tariff_plans.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_seller_tariff_subscriptions_seller_id',
                    'seller_tariff_subscriptions', ['seller_id'])
    op.create_index('ix_seller_tariff_subscriptions_plan_id',
                    'seller_tariff_subscriptions', ['plan_id'])
    op.create_index('ix_seller_tariff_subscriptions_is_paid',
                    'seller_tariff_subscriptions', ['is_paid'])
    op.create_index('ix_seller_tariff_subscriptions_status',
                    'seller_tariff_subscriptions', ['status'])
    op.create_index('ix_seller_tariff_subscriptions_expires_at',
                    'seller_tariff_subscriptions', ['expires_at'])

    # Транзакции/расчёты за тарифы
    op.create_table(
        'tariff_transactions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('seller_id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['seller_id'], ['sellers.id'], ),
        sa.ForeignKeyConstraint(['plan_id'], ['tariff_plans.id'], ),
        sa.ForeignKeyConstraint(['subscription_id'], ['seller_tariff_subscriptions.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_tariff_transactions_seller_id',
                    'tariff_transactions', ['seller_id'])
    op.create_index('ix_tariff_transactions_plan_id',
                    'tariff_transactions', ['plan_id'])
    op.create_index('ix_tariff_transactions_subscription_id',
                    'tariff_transactions', ['subscription_id'])
    op.create_index('ix_tariff_transactions_paid_at',
                    'tariff_transactions', ['paid_at'])


def downgrade():
    op.drop_index('ix_tariff_transactions_paid_at', table_name='tariff_transactions')
    op.drop_index('ix_tariff_transactions_subscription_id', table_name='tariff_transactions')
    op.drop_index('ix_tariff_transactions_plan_id', table_name='tariff_transactions')
    op.drop_index('ix_tariff_transactions_seller_id', table_name='tariff_transactions')
    op.drop_table('tariff_transactions')

    op.drop_index('ix_seller_tariff_subscriptions_expires_at',
                  table_name='seller_tariff_subscriptions')
    op.drop_index('ix_seller_tariff_subscriptions_status',
                  table_name='seller_tariff_subscriptions')
    op.drop_index('ix_seller_tariff_subscriptions_is_paid',
                  table_name='seller_tariff_subscriptions')
    op.drop_index('ix_seller_tariff_subscriptions_plan_id',
                  table_name='seller_tariff_subscriptions')
    op.drop_index('ix_seller_tariff_subscriptions_seller_id',
                  table_name='seller_tariff_subscriptions')
    op.drop_table('seller_tariff_subscriptions')

    op.drop_index('ix_tariff_plans_sort_order', table_name='tariff_plans')
    op.drop_index('ix_tariff_plans_is_active', table_name='tariff_plans')
    op.drop_table('tariff_plans')
