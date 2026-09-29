"""Review reward programs, gift gallery, counting receipts and awarded rewards."""
from alembic import op
from app.models.review_rewards import ReviewRewardProgram, ReviewGift, RewardReviewReceipt, ReviewReward

revision = 'a93f28c6d104'
down_revision = 't2u3v4w5x6y7'
branch_labels = None
depends_on = None


def upgrade():
    for model in (ReviewRewardProgram, ReviewGift, RewardReviewReceipt, ReviewReward):
        model.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    for model in (ReviewReward, RewardReviewReceipt, ReviewGift, ReviewRewardProgram):
        model.__table__.drop(bind=op.get_bind(), checkfirst=True)
