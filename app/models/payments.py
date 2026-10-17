"""Durable invoices; a test transaction never pays a real order."""
from datetime import datetime
from app import db


class RobokassaPayment(db.Model):
    __tablename__ = 'robokassa_payments'
    __table_args__ = (db.UniqueConstraint('order_id', 'is_test'),)

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    is_test = db.Column(db.Boolean, nullable=False)
    status = db.Column(db.String(24), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime)
    order = db.relationship('Order', backref='robokassa_payments')


class RobokassaCheckout(db.Model):
    """One active, idempotent checkout per buyer (buyer row serializes writes)."""
    __tablename__ = 'robokassa_checkouts'
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('robokassa_payments.id'), nullable=False, unique=True)
    fingerprint = db.Column(db.String(64), nullable=False)
    invoice = db.relationship('RobokassaPayment')
