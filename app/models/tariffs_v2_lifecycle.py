"""Lifecycle events and non-destructive storage expiry plans for the sandbox."""
from app import db


class TariffV2OrderCompletion(db.Model):
    __tablename__ = 'tariff_v2_order_completions'
    __table_args__ = (db.CheckConstraint("kind IN ('buyer_cancelled','received')", name='v2_completion_kind'),)
    order_id = db.Column(db.Integer, db.ForeignKey('tariff_v2_sandbox_orders.id'), primary_key=True)
    kind = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False)


class TariffV2StorageExpiry(db.Model):
    __tablename__ = 'tariff_v2_storage_expiry'
    __table_args__ = (db.CheckConstraint("status IN ('grace','expired','resolved')", name='v2_storage_expiry_status'),)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    unpaid_since = db.Column(db.Date, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(16), nullable=False)
    checked_at = db.Column(db.DateTime, nullable=False)


class TariffV2ReplicaStore(db.Model):
    __tablename__ = 'tariff_v2_replica_store'
    id = db.Column(db.Integer, primary_key=True)
    root = db.Column(db.Text, nullable=False)
    token = db.Column(db.String(48), nullable=False)


class TariffV2Replica(db.Model):
    __tablename__ = 'tariff_v2_replicas'
    __table_args__ = (db.CheckConstraint("status IN ('active','purging','purged')", name='v2_replica_status'),)
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=False)
    active_product_id = db.Column(db.Integer, unique=True)
    seller_id = db.Column(db.Integer, nullable=False)
    directory = db.Column(db.String(48), nullable=False, unique=True)
    files = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(16), nullable=False, default='active')
    created_at = db.Column(db.DateTime, nullable=False)
    purge_started_at = db.Column(db.DateTime)
    purged_at = db.Column(db.DateTime)
