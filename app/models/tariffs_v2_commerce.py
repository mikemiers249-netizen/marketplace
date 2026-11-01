"""Sandbox catalog/cart/orders: no legacy Order, CartItem or payment side effects."""
from app import db


class TariffV2Listing(db.Model):
    __tablename__ = 'tariff_v2_listings'
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), primary_key=True)
    published = db.Column(db.Boolean, nullable=False, default=False)
    changed_at = db.Column(db.DateTime, nullable=False)


class TariffV2Cart(db.Model):
    __tablename__ = 'tariff_v2_cart'
    __table_args__ = (db.CheckConstraint('quantity > 0', name='v2_cart_quantity'),)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), primary_key=True)
    quantity = db.Column(db.Integer, nullable=False)
    product = db.relationship('Product')


class TariffV2Checkout(db.Model):
    __tablename__ = 'tariff_v2_checkouts'
    __table_args__ = (
        db.CheckConstraint("status IN ('pending','complete','expired','needs_review')", name='v2_checkout_status'),
        db.CheckConstraint('online_amount >= 0 AND cod_amount >= 0', name='v2_checkout_amounts'),
        db.CheckConstraint('is_test = true', name='v2_checkout_test_only'),
    )
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False, index=True)
    active_buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), unique=True)
    gateway_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_gateway_numbers.id'), unique=True)
    merchant_login = db.Column(db.String(128), nullable=False)
    token = db.Column(db.String(48), nullable=False, unique=True)
    fingerprint = db.Column(db.String(64), nullable=False)
    online_amount = db.Column(db.Numeric(14, 2), nullable=False)
    cod_amount = db.Column(db.Numeric(14, 2), nullable=False)
    status = db.Column(db.String(24), nullable=False)
    review_reason = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    paid_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    is_test = db.Column(db.Boolean, nullable=False, default=True)
    orders = db.relationship('TariffV2SandboxOrder', back_populates='checkout', order_by='TariffV2SandboxOrder.id')

    @property
    def amount(self):
        return self.online_amount


class TariffV2CheckoutRequest(db.Model):
    __tablename__ = 'tariff_v2_checkout_requests'
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), primary_key=True)
    request_key = db.Column(db.String(48), primary_key=True)
    checkout_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkouts.id'), nullable=False)


class TariffV2SandboxOrder(db.Model):
    __tablename__ = 'tariff_v2_sandbox_orders'
    __table_args__ = (
        db.UniqueConstraint('checkout_id', 'seller_id', name='v2_checkout_seller'),
        db.CheckConstraint("status IN ('waiting_payment','ready','shipped','received','cancelled')", name='v2_sandbox_order_status'),
        db.CheckConstraint('shipping_days BETWEEN 1 AND 5', name='v2_order_shipping_days'),
    )
    id = db.Column(db.Integer, primary_key=True)
    checkout_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkouts.id'), nullable=False)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    goods_amount = db.Column(db.Numeric(14, 2), nullable=False)
    fee = db.Column(db.Numeric(14, 2), nullable=False)
    online_amount = db.Column(db.Numeric(14, 2), nullable=False)
    cod_amount = db.Column(db.Numeric(14, 2), nullable=False)
    delivery = db.Column(db.JSON, nullable=False)
    has_physical = db.Column(db.Boolean, nullable=False)
    shipping_days = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(24), nullable=False)
    handed_to_seller_at = db.Column(db.DateTime)
    shipping_deadline = db.Column(db.DateTime)
    shipped_at = db.Column(db.DateTime)
    tracking_number = db.Column(db.String(100))
    checkout = db.relationship('TariffV2Checkout', back_populates='orders')
    lines = db.relationship('TariffV2CheckoutLine', back_populates='order', order_by='TariffV2CheckoutLine.id')


class TariffV2CheckoutLine(db.Model):
    """Typed money plus immutable product/price snapshot, surviving catalog deletion."""
    __tablename__ = 'tariff_v2_checkout_lines'
    __table_args__ = (
        db.UniqueConstraint('order_id', 'product_id', name='v2_checkout_product'),
        db.CheckConstraint('quantity > 0', name='v2_checkout_line_quantity'),
        db.CheckConstraint('fee >= 0 AND refunded_fee >= 0 AND refunded_fee <= fee', name='v2_checkout_line_fee'),
    )
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_sandbox_orders.id'), nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    digital_book_id = db.Column(db.Integer)
    quantity = db.Column(db.Integer, nullable=False)
    fee = db.Column(db.Numeric(14, 2), nullable=False)
    refunded_fee = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    snapshot = db.Column(db.JSON, nullable=False)
    order = db.relationship('TariffV2SandboxOrder', back_populates='lines')


class TariffV2Reservation(db.Model):
    __tablename__ = 'tariff_v2_reservations'
    __table_args__ = (
        db.CheckConstraint('quantity > 0', name='v2_reservation_quantity'),
        db.CheckConstraint("status IN ('pending','committed','released')", name='v2_reservation_status'),
    )
    id = db.Column(db.Integer, primary_key=True)
    checkout_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkouts.id'), nullable=False, index=True)
    line_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_checkout_lines.id'), nullable=False, unique=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='SET NULL'), index=True)
    quantity = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(16), nullable=False)
