"""Add private electronic books and purchase/progress tables."""
revision = 'd36e58f0a417'
down_revision = 'c25d47e9f306'
branch_labels = None
depends_on = None


def upgrade():
    from alembic import op
    from app.models.digital import DigitalBook, DigitalPurchase
    DigitalBook.__table__.create(bind=op.get_bind(), checkfirst=True)
    DigitalPurchase.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    # Purchased books and reading progress must never be removed by a rollback.
    pass
