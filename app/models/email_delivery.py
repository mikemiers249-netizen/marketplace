"""Persistent email state; separate tables keep existing accounts compatible."""
from datetime import datetime
from app import db


class EmailIdentity(db.Model):
    __tablename__ = 'email_identities'
    id = db.Column(db.Integer, primary_key=True)
    user_type = db.Column(db.String(10), nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    email = db.Column(db.String(120), nullable=False)
    verified_at = db.Column(db.DateTime)
    required = db.Column(db.Boolean, nullable=False, default=False)
    last_verify_at = db.Column(db.DateTime)
    last_reset_at = db.Column(db.DateTime)
    sessions_revoked_at = db.Column(db.DateTime)
    __table_args__ = (db.UniqueConstraint('user_type', 'user_id'),)


class EmailToken(db.Model):
    __tablename__ = 'email_tokens'
    id = db.Column(db.Integer, primary_key=True)
    digest = db.Column(db.String(64), unique=True, nullable=False)
    identity_id = db.Column(db.Integer, db.ForeignKey('email_identities.id'), nullable=False)
    purpose = db.Column(db.String(10), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    password_digest = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)


class EmailOutbox(db.Model):
    __tablename__ = 'email_outbox'
    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(12), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    lease_id = db.Column(db.String(64))
    sent_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(100))
    token_id = db.Column(db.Integer, db.ForeignKey('email_tokens.id'))
