"""Add loyalty program: rates, seller loyalty, per-seller buyer bonus balances

Revision ID: l1o2y3a4l5t6
Revises: g1a2b3c4d5e6
Create Date: 2026-07-20 21:30:00.000000

Создаёт три новые таблицы для программы лояльности:
  • loyalty_rates          — шаблоны курсов начисления (админ)
  • seller_loyalties       — подключение селлера к курсу + свой % списания
  • buyer_bonuses          — кеш-баланс баллов покупателя на каждого селлера

И добавляет колонку bonuses.seller_id для привязки журнальной записи
баллов к конкретному селлеру (nullable — старые записи остаются без
привязки).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'l1o2y3a4l5t6'
down_revision = 'g1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    # 1) bonuses.seller_id — привязка журнальной записи к селлеру
    with op.batch_alter_table('bonuses', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seller_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_bonuses_seller_id', ['seller_id'])
        batch_op.create_foreign_key(
            'fk_bonuses_seller_id_sellers',
            'sellers',
            ['seller_id'],
            ['id'],
        )

    # 2) loyalty_rates — шаблоны курсов
    op.create_table(
        'loyalty_rates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('points_per_ruble', sa.Float(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_loyalty_rates_is_active', 'loyalty_rates', ['is_active'])

    # 3) seller_loyalties — подключение селлера к курсу
    op.create_table(
        'seller_loyalties',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'seller_id',
            sa.Integer(),
            sa.ForeignKey('sellers.id', name='fk_seller_loyalties_seller_id'),
            nullable=False,
        ),
        sa.Column(
            'rate_id',
            sa.Integer(),
            sa.ForeignKey('loyalty_rates.id', name='fk_seller_loyalties_rate_id'),
            nullable=True,
        ),
        sa.Column('payback_percent', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'ix_seller_loyalties_seller_id',
        'seller_loyalties',
        ['seller_id'],
        unique=True,
    )

    # 4) buyer_bonuses — кеш-баланс покупателя на каждого селлера
    op.create_table(
        'buyer_bonuses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'buyer_id',
            sa.Integer(),
            sa.ForeignKey('buyers.id', name='fk_buyer_bonuses_buyer_id'),
            nullable=False,
        ),
        sa.Column(
            'seller_id',
            sa.Integer(),
            sa.ForeignKey('sellers.id', name='fk_buyer_bonuses_seller_id'),
            nullable=False,
        ),
        sa.Column('balance', sa.Float(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('buyer_id', 'seller_id', name='uq_buyer_seller_bonus'),
    )
    op.create_index('ix_buyer_bonuses_buyer_id', 'buyer_bonuses', ['buyer_id'])
    op.create_index('ix_buyer_bonuses_seller_id', 'buyer_bonuses', ['seller_id'])

    # 5) Стартовые курсы начисления (то, что было в UI-заглушке).
    # Помогает не стартовать с пустой страницы у админа.
    op.execute(
        "INSERT INTO loyalty_rates (title, points_per_ruble, description, is_active, sort_order) VALUES "
        "('Базовый', 1, '1 балл за каждый рубль', 1, 1), "
        "('Стандарт', 2, '2 балла за каждый рубль', 1, 2), "
        "('Премиум', 4, '4 балла за каждый рубль', 1, 3)"
    )


def downgrade():
    op.drop_table('buyer_bonuses')
    op.drop_table('seller_loyalties')
    op.drop_index('ix_loyalty_rates_is_active', table_name='loyalty_rates')
    op.drop_table('loyalty_rates')

    with op.batch_alter_table('bonuses', schema=None) as batch_op:
        batch_op.drop_constraint('fk_bonuses_seller_id_sellers', type_='foreignkey')
        batch_op.drop_index('ix_bonuses_seller_id')
        batch_op.drop_column('seller_id')
