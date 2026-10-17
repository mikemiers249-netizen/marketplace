"""Durable Robokassa invoices with separate sandbox and live transactions."""
from alembic import op
from app.models.payments import RobokassaPayment

revision = 'b14c36d8e205'
down_revision = 'a93f28c6d104'
branch_labels = None
depends_on = None


def upgrade():
    RobokassaPayment.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    RobokassaPayment.__table__.drop(bind=op.get_bind(), checkfirst=True)
