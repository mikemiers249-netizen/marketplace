"""Idempotent checkout attempts before an order is accepted."""
from alembic import op
from app.models.payments import RobokassaCheckout

revision = 'c25d47e9f306'
down_revision = 'b14c36d8e205'
branch_labels = None
depends_on = None


def upgrade():
    RobokassaCheckout.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    RobokassaCheckout.__table__.drop(bind=op.get_bind(), checkfirst=True)
