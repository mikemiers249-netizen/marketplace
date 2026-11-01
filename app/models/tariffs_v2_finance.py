"""Sandbox order disputes and refund ledger, separate from legacy orders."""
from app import db


class TariffV2Cancellation(db.Model):
    __tablename__ = 'tariff_v2_cancellations'
    line_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkout_lines.id'), primary_key=True)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.CheckConstraint('quantity >= 0', name='v2_cancel_quantity'),)


class TariffV2CheckoutRefund(db.Model):
    __tablename__ = 'tariff_v2_checkout_refunds'
    __table_args__ = (
        db.CheckConstraint('amount > 0 AND seller_charge >= 0 AND seller_charge <= amount', name='v2_checkout_refund_amount'),
        db.CheckConstraint("status IN ('pending','succeeded','failed')", name='v2_checkout_refund_status'),
        db.CheckConstraint('is_test = true', name='v2_checkout_refund_test'),
    )
    id = db.Column(db.Integer, primary_key=True)
    line_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkout_lines.id'), nullable=False, index=True)
    request_key = db.Column(db.String(160), unique=True, nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    seller_charge = db.Column(db.Numeric(14, 2), nullable=False)
    reason = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, nullable=False)
    provider_reference = db.Column(db.String(160), unique=True)
    confirmed_at = db.Column(db.DateTime)
    invoice_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_invoices.id'))
    is_test = db.Column(db.Boolean, nullable=False, default=True)
    line = db.relationship('TariffV2CheckoutLine')


class TariffV2Claim(db.Model):
    __tablename__ = 'tariff_v2_claims'
    __table_args__ = (
        db.CheckConstraint("opened_by IN ('buyer','seller')", name='v2_claim_opener'),
        db.CheckConstraint("status IN ('waiting','disputed','approved','rejected')", name='v2_claim_status'),
    )
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_sandbox_orders.id'), nullable=False)
    active_order_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_sandbox_orders.id'), unique=True)
    opened_by = db.Column(db.String(10), nullable=False)
    explanation = db.Column(db.Text, nullable=False)
    opened_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(16), nullable=False, default='waiting')
    response = db.Column(db.Text)
    responded_at = db.Column(db.DateTime)
    admin_explanation = db.Column(db.Text)
    decided_at = db.Column(db.DateTime)
    order = db.relationship('TariffV2SandboxOrder', foreign_keys=[order_id])
