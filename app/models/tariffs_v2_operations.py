"""Durable bill payments and refund dispatch; no effects on import."""
from app import db


class TariffV2FiscalReceipt(db.Model):
    __tablename__ = 'tariff_v2_fiscal_receipts'
    gateway_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_gateway_numbers.id'), primary_key=True)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    receipt_json = db.Column(db.Text, nullable=False)
    line_items = db.Column(db.JSON, nullable=False)
    is_test = db.Column(db.Boolean, nullable=False)


class TariffV2BillPayment(db.Model):
    __tablename__ = 'tariff_v2_bill_payments'
    __table_args__ = (
        db.CheckConstraint("status IN ('pending','paid','expired','needs_review')", name='v2_bill_payment_status'),
        db.CheckConstraint('amount > 0', name='v2_bill_payment_amount'),
    )
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_invoices.id'), nullable=False)
    active_invoice_id = db.Column(db.Integer, unique=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    gateway_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_gateway_numbers.id'), unique=True)
    merchant_login = db.Column(db.String(128), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    token = db.Column(db.String(48), nullable=False, unique=True)
    receipt_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), nullable=False)
    review_reason = db.Column(db.String(100))
    is_test = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    paid_at = db.Column(db.DateTime)
    invoice = db.relationship('TariffV2Invoice', foreign_keys=[invoice_id])


class TariffV2RefundDispatch(db.Model):
    __tablename__ = 'tariff_v2_refund_dispatch'
    __table_args__ = (
        db.CheckConstraint("status IN ('queued','sending','submitted','finished','canceled','rejected','unknown')", name='v2_refund_dispatch_status'),
        db.CheckConstraint('amount > 0', name='v2_refund_dispatch_amount'),
    )
    id = db.Column(db.Integer, primary_key=True)
    refund_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkout_refunds.id'), nullable=False, unique=True)
    checkout_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkouts.id'), nullable=False)
    gateway_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_gateway_numbers.id'), nullable=False)
    merchant_login = db.Column(db.String(128), nullable=False)
    payment_amount = db.Column(db.Numeric(14, 2), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    payment_flow = db.Column(db.String(64), nullable=False)
    payment_token = db.Column(db.String(48), nullable=False)
    receipt_items = db.Column(db.JSON, nullable=False)
    is_test = db.Column(db.Boolean, nullable=False)
    status = db.Column(db.String(16), nullable=False, default='queued')
    operation_key = db.Column(db.String(200))
    provider_request_id = db.Column(db.String(36), unique=True)
    error_code = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, nullable=False)
    sending_at = db.Column(db.DateTime)
    checked_at = db.Column(db.DateTime)
    refund = db.relationship('TariffV2CheckoutRefund')
