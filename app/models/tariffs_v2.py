"""Isolated v2 data. Importing these models never changes the live tariff flow."""
from datetime import datetime

from app import db


class TariffV2Rule(db.Model):
    __tablename__ = 'tariff_v2_rules'
    __table_args__ = (
        db.CheckConstraint("kind IN ('fixed', 'percent')", name='v2_rule_kind'),
        db.CheckConstraint('amount >= 0', name='v2_rule_amount'),
        db.CheckConstraint("kind != 'percent' OR amount <= 100", name='v2_percent_range'),
    )
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class TariffV2Category(db.Model):
    __tablename__ = 'tariff_v2_categories'
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id', ondelete='CASCADE'), primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_rules.id'), nullable=False)
    category = db.relationship('Category')
    rule = db.relationship('TariffV2Rule')


class TariffV2Seller(db.Model):
    __tablename__ = 'tariff_v2_sellers'
    __table_args__ = (
        db.CheckConstraint('shipping_days BETWEEN 1 AND 5', name='v2_shipping_days'),
        db.CheckConstraint("payment_mode IN ('cod', 'full_online')", name='v2_payment_mode'),
    )
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), primary_key=True)
    override_rule_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_rules.id'))
    free_storage = db.Column(db.Boolean, nullable=False, default=False)
    free_buyer_fee = db.Column(db.Boolean, nullable=False, default=False)
    # Null means no expiry. Otherwise exclusive UTC instant (MSK midnight after last day).
    benefits_until = db.Column(db.DateTime)
    shipping_days = db.Column(db.Integer, nullable=False, default=5)
    payment_mode = db.Column(db.String(16), nullable=False, default='cod')
    seller = db.relationship('Seller')
    override_rule = db.relationship('TariffV2Rule')


class TariffV2Coverage(db.Model):
    """Confirmed prepayment event; never interpret a pending invoice as payment."""
    __tablename__ = 'tariff_v2_coverage'
    __table_args__ = (
        db.CheckConstraint('paid_amount >= 0', name='v2_coverage_amount'),
        db.CheckConstraint('ends_on > starts_on', name='v2_coverage_period'),
    )
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='SET NULL'), index=True)
    original_product_id = db.Column(db.Integer, nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    starts_on = db.Column(db.Date, nullable=False)
    ends_on = db.Column(db.Date, nullable=False)  # exclusive
    monthly_rate = db.Column(db.Numeric(14, 2), nullable=False)
    paid_amount = db.Column(db.Numeric(14, 2), nullable=False)
    payment_reference = db.Column(db.String(160), nullable=False, unique=True)
    paid_at = db.Column(db.DateTime, nullable=False)
    is_test = db.Column(db.Boolean, nullable=False, default=False)


class TariffV2GatewayNumber(db.Model):
    """One provider invoice namespace shared by every v2 payment purpose."""
    __tablename__ = 'tariff_v2_gateway_numbers'
    __table_args__ = (
        db.UniqueConstraint('purpose', 'reference_id', name='v2_gateway_reference'),
        db.CheckConstraint("purpose IN ('storage', 'checkout')", name='v2_gateway_purpose'),
        {'sqlite_autoincrement': True},
    )
    id = db.Column(db.Integer, primary_key=True)
    purpose = db.Column(db.String(20), nullable=False)
    reference_id = db.Column(db.Integer, nullable=False)


class TariffV2StorageInvoice(db.Model):
    """Frozen sandbox invoice. It cannot represent a live provider payment."""
    __tablename__ = 'tariff_v2_storage_invoices'
    __table_args__ = (
        db.CheckConstraint('amount > 0', name='v2_storage_invoice_amount'),
        db.CheckConstraint("status IN ('pending','paid','expired','needs_review')", name='v2_storage_invoice_status'),
        db.CheckConstraint('is_test = true', name='v2_storage_invoice_test_only'),
    )
    id = db.Column(db.Integer, primary_key=True)
    gateway_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_gateway_numbers.id'), unique=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, nullable=False)
    active_product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='SET NULL'), unique=True)
    product_name = db.Column(db.String(255), nullable=False)
    merchant_login = db.Column(db.String(128), nullable=False)
    category_id = db.Column(db.Integer)
    fingerprint = db.Column(db.String(64), nullable=False)
    token = db.Column(db.String(64), nullable=False, unique=True)
    monthly_rate = db.Column(db.Numeric(14, 2), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    starts_on = db.Column(db.Date, nullable=False)
    ends_on = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    paid_at = db.Column(db.DateTime)
    status = db.Column(db.String(24), nullable=False, default='pending')
    review_reason = db.Column(db.String(80))
    is_test = db.Column(db.Boolean, nullable=False, default=True)


class TariffV2OrderLine(db.Model):
    """Immutable quote snapshot. Financial history must outlive catalog changes."""
    __tablename__ = 'tariff_v2_order_lines'
    __table_args__ = (
        db.UniqueConstraint('order_id', 'line_key', name='v2_order_line_key'),
        db.CheckConstraint('quantity > 0', name='v2_line_quantity'),
        db.CheckConstraint('fee >= 0 AND refunded_fee >= 0 AND refunded_fee <= fee', name='v2_line_fee'),
    )
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    line_key = db.Column(db.String(80), nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    # Deliberately no FK to Product/Category: snapshots survive their deletion.
    product_id = db.Column(db.Integer, nullable=False)
    category_id = db.Column(db.Integer)
    product_name = db.Column(db.String(255), nullable=False)
    rule_id = db.Column(db.Integer)
    rule_name = db.Column(db.String(200))
    kind = db.Column(db.String(16), nullable=False)
    rate = db.Column(db.Numeric(14, 2), nullable=False)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    fee = db.Column(db.Numeric(14, 2), nullable=False)
    refunded_fee = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    online_amount = db.Column(db.Numeric(14, 2), nullable=False)
    cod_amount = db.Column(db.Numeric(14, 2), nullable=False)
    shipping_days = db.Column(db.Integer, nullable=False)
    handed_to_seller_at = db.Column(db.DateTime)
    shipping_deadline = db.Column(db.DateTime)
    shipped_at = db.Column(db.DateTime)


class TariffV2Refund(db.Model):
    """Local intent and verified result, not an instruction to call a provider."""
    __tablename__ = 'tariff_v2_refunds'
    __table_args__ = (
        db.CheckConstraint('amount > 0', name='v2_refund_amount'),
        db.CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name='v2_refund_status'),
    )
    id = db.Column(db.Integer, primary_key=True)
    line_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_order_lines.id'), nullable=False)
    request_key = db.Column(db.String(160), nullable=False, unique=True)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    reason = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='pending')
    provider_reference = db.Column(db.String(160), unique=True)
    confirmed_at = db.Column(db.DateTime)
    invoice_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_invoices.id'))
    line = db.relationship('TariffV2OrderLine')


class TariffV2Invoice(db.Model):
    __tablename__ = 'tariff_v2_invoices'
    __table_args__ = (
        db.UniqueConstraint('seller_id', 'period', name='v2_seller_invoice_month'),
        db.CheckConstraint('amount >= 0', name='v2_invoice_amount'),
    )
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    period = db.Column(db.Date, nullable=False)  # first day of the issue month, MSK
    issued_at = db.Column(db.DateTime, nullable=False)
    due_at = db.Column(db.DateTime, nullable=False)  # MSK 6th 00:00, exclusive boundary
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    paid_at = db.Column(db.DateTime)
    payment_reference = db.Column(db.String(160), unique=True)
    refunds = db.relationship('TariffV2Refund', backref='invoice')


class TariffV2Audit(db.Model):
    __tablename__ = 'tariff_v2_audit'
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(100), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    details = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
