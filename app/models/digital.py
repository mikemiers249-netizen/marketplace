"""Additive pilot tables; physical product/order schemas remain unchanged."""
from datetime import datetime
from app import db


class DigitalBook(db.Model):
    __tablename__ = 'digital_books'
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    author = db.Column(db.String(200), nullable=False, default='')
    description = db.Column(db.Text, nullable=False, default='')
    price = db.Column(db.Numeric(14, 2), nullable=False)
    file_name = db.Column(db.String(80), nullable=False)
    cover_name = db.Column(db.String(80), nullable=True)
    format = db.Column(db.String(4), nullable=False)
    published = db.Column(db.Boolean, nullable=False, default=False)
    moderation_status = db.Column(db.String(20), nullable=False, default='draft', server_default='draft')
    moderation_comment = db.Column(db.Text, nullable=False, default='', server_default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    seller = db.relationship('Seller')
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True, index=True)
    category = db.relationship('Category')
    article = db.Column(db.String(50), unique=True, nullable=True, index=True)
    system_sku = db.Column(db.String(64), unique=True, nullable=True, index=True)
    parameter_values = db.Column(db.JSON, nullable=False, default=dict, server_default='{}')


class DigitalPurchase(db.Model):
    __tablename__ = 'digital_purchases'
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False, index=True)
    book_id = db.Column(db.Integer, db.ForeignKey('digital_books.id'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    __table_args__ = (db.UniqueConstraint('order_id', 'book_id', name='uq_digital_purchase_order_book'),)
    title = db.Column(db.String(200), nullable=False)
    file_name = db.Column(db.String(80), nullable=False)
    format = db.Column(db.String(4), nullable=False)
    chapter = db.Column(db.Integer, nullable=False, default=0)
    position = db.Column(db.Float, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    book = db.relationship('DigitalBook')
    order = db.relationship('Order', backref=db.backref('digital_purchases'))
