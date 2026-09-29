from datetime import datetime
from app import db


class ReviewRewardProgram(db.Model):
    __tablename__ = 'review_reward_programs'
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    kind = db.Column(db.String(16), nullable=False, default='bonus')
    mode = db.Column(db.String(16), nullable=False, default='each')
    rules = db.Column(db.JSON, nullable=False, default=list)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    seller = db.relationship('Seller')


class ReviewGift(db.Model):
    __tablename__ = 'review_gifts'
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False, unique=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    product = db.relationship('Product')


class RewardReviewReceipt(db.Model):
    """Immutable counting record; survives removal or repeated moderation of a review."""
    __tablename__ = 'reward_review_receipts'
    id = db.Column(db.Integer, primary_key=True)
    review_id = db.Column(db.Integer, nullable=False, unique=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    order_id = db.Column(db.Integer, nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    ordinal = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('buyer_id', 'order_id', 'product_id', name='uq_reward_review_purchase'),
        db.UniqueConstraint('buyer_id', 'seller_id', 'ordinal', name='uq_reward_review_ordinal'),
    )


class ReviewReward(db.Model):
    __tablename__ = 'review_rewards'
    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer, db.ForeignKey('reward_review_receipts.id'), nullable=False, unique=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    gift_id = db.Column(db.Integer, db.ForeignKey('review_gifts.id'), nullable=True)
    gift_name = db.Column(db.String(200))
    gift_image = db.Column(db.String(255))
    status = db.Column(db.String(16), nullable=False, default='available', index=True)
    # IDs kept as audit values, so historical order removal does not erase rewards.
    redeemed_order_id = db.Column(db.Integer, index=True)
    redeemed_amount = db.Column(db.Numeric(12, 2))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    redeemed_at = db.Column(db.DateTime)
    buyer = db.relationship('Buyer')
    seller = db.relationship('Seller')
    gift = db.relationship('ReviewGift')
    receipt = db.relationship('RewardReviewReceipt')

    @property
    def label(self):
        if self.kind == 'gift':
            return self.gift_name
        return f'{self.amount:g} ' + ('баллов' if self.kind == 'bonus' else '% на следующий заказ')
