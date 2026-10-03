"""Versioned legal records, optional consents and data-subject requests."""
from datetime import datetime
from app import db


class LegalSnapshot(db.Model):
    __tablename__ = 'legal_snapshots'
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), nullable=False)
    digest = db.Column(db.String(64), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (db.UniqueConstraint('slug', 'digest'),)


class PrivacyConsent(db.Model):
    __tablename__ = 'privacy_consents'
    id = db.Column(db.Integer, primary_key=True)
    user_type = db.Column(db.String(20), nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    visitor_key = db.Column(db.String(64), index=True)
    purpose = db.Column(db.String(40), nullable=False)
    snapshot_id = db.Column(db.Integer, db.ForeignKey('legal_snapshots.id'), nullable=False)
    granted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    withdrawn_at = db.Column(db.DateTime)
    method = db.Column(db.String(100), nullable=False)
    scope_data = db.Column(db.JSON)


class PrivacyCleanupRun(db.Model):
    __tablename__ = 'privacy_cleanup_runs'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    categories = db.Column(db.JSON, nullable=False)
    note = db.Column(db.Text, nullable=False)


class PrivacyRequest(db.Model):
    __tablename__ = 'privacy_requests'
    id = db.Column(db.Integer, primary_key=True)
    user_type = db.Column(db.String(20), nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(30), nullable=False)
    details = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='new', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    due_at = db.Column(db.DateTime, nullable=False)
    reply = db.Column(db.Text)
    completed_at = db.Column(db.DateTime)
