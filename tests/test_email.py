import re
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from pathlib import Path

from config import TestingConfig
from app import create_app, db
from app.models.users import Buyer, Seller
from app.models.orders import Order
from app.models.email_delivery import EmailIdentity, EmailToken, EmailOutbox
from app.utils.email_service import request_link, consume_token, deliver_batch, needs_verification, identity_for


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            MAIL_SERVER = 'smtp.example.test'
            MAIL_USERNAME = 'sender@example.test'
            MAIL_PASSWORD = 'test-only'
            MAIL_DEFAULT_SENDER = 'sender@example.test'
            MAIL_USE_SSL = True
            MAIL_USE_TLS = False
            PUBLIC_BASE_URL = 'https://market.example.test'
            MAIL_WORKER_ENABLED = False
            UPLOAD_FOLDER = str(Path(self.directory.name) / 'uploads')
            LOG_FILE = str(Path(self.directory.name) / 'test.log')
        self.app = create_app(Config)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.buyer = Buyer(login='buyer', email='buyer@example.test', is_active=True)
        self.buyer.set_password('old-password')
        self.seller = Seller(login='seller', email='buyer@example.test', store_name='Store', store_slug='store', is_active=True)
        self.seller.set_password('seller-password')
        db.session.add_all([self.buyer, self.seller])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and str(self.directory.name) in handler.baseFilename:
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.directory.cleanup()

    def link(self, purpose='verify', user=None):
        self.assertTrue(request_link(user or self.buyer, purpose, required=True))
        db.session.commit()
        row = EmailOutbox.query.order_by(EmailOutbox.id.desc()).first()
        url = re.search(r'https://\S+', row.body).group()
        return url.split('/')[-1], '/' + url.split('/', 3)[3]

    def test_verify_single_use_and_scanner_get(self):
        raw, path = self.link()
        self.assertTrue(needs_verification(self.buyer))
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIsNone(EmailToken.query.first().used_at)
        self.assertEqual(self.client.post(path).status_code, 200)
        self.assertFalse(needs_verification(self.buyer))
        self.assertFalse(consume_token(raw, 'verify'))
        self.assertEqual(self.client.get(path).status_code, 400)

    def test_expired_tampered_wrong_purpose_and_address(self):
        raw, _ = self.link()
        self.assertFalse(consume_token(raw + 'bad', 'verify'))
        self.assertFalse(consume_token(raw, 'reset', 'new-password'))
        self.buyer.email = 'changed@example.test'
        db.session.commit()
        self.assertFalse(consume_token(raw, 'verify'))
        self.buyer.email = 'buyer@example.test'
        EmailToken.query.first().expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertFalse(consume_token(raw, 'verify'))

    def test_reset_only_correct_role_and_single_use(self):
        raw, path = self.link('reset', self.seller)
        self.assertEqual(self.client.post(path, data={'password': 'new-password', 'password_confirm': 'new-password'}).status_code, 200)
        self.assertTrue(self.seller.check_password('new-password'))
        self.assertTrue(self.buyer.check_password('old-password'))
        self.assertFalse(consume_token(raw, 'reset', 'another-password'))

    def test_password_change_invalidates_reset(self):
        raw, _ = self.link('reset')
        self.buyer.set_password('different-password')
        db.session.commit()
        self.assertFalse(consume_token(raw, 'reset', 'new-password'))

    def test_cooldown_and_neutral_response(self):
        self.link()
        self.assertFalse(request_link(self.buyer, 'verify'))
        self.assertEqual(EmailOutbox.query.count(), 1)
        for email in ('buyer@example.test', 'missing@example.test'):
            response = self.client.post('/auth/email/forgot', data={'email': email, 'user_type': 'buyer'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(EmailOutbox.query.count(), 2)

    def test_delivery_retry_and_redaction(self):
        self.link()
        with patch('app.utils.email_service.smtp_connection', side_effect=OSError('secret')):
            self.assertEqual(deliver_batch(), 0)
        row = EmailOutbox.query.first()
        db.session.refresh(row)
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.last_error, 'OSError')
        self.assertEqual(row.attempts, 1)
        row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
        with patch('app.utils.email_service.smtp_connection') as smtp:
            self.assertEqual(deliver_batch(), 1)
            smtp.return_value.__enter__.return_value.send_message.assert_called_once()
        db.session.refresh(row)
        self.assertEqual(row.status, 'sent')
        self.assertEqual(row.body, '')
        self.assertEqual(deliver_batch(), 0)

    def test_lease_and_expiration(self):
        self.link()
        row = EmailOutbox.query.first()
        row.status = 'sending'
        row.next_attempt_at = datetime.utcnow() + timedelta(minutes=5)
        db.session.commit()
        with patch('app.utils.email_service.smtp_connection') as smtp:
            self.assertEqual(deliver_batch(), 0)
            row.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
            EmailToken.query.first().expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
            self.assertEqual(deliver_batch(), 0)
            smtp.assert_not_called()
        db.session.refresh(row)
        self.assertEqual(row.status, 'expired')

    def test_legacy_login_and_new_registration(self):
        self.assertFalse(needs_verification(self.buyer))
        response = self.client.post('/auth/signup', data={'login': 'new-user', 'email': 'new@example.test', 'password': 'new-password', 'password_confirm': 'new-password', 'phone': '123456789'})
        self.assertEqual(response.status_code, 302)
        user = Buyer.query.filter_by(login='new-user').one()
        self.assertTrue(needs_verification(user))
        response = self.client.post('/auth/login', data={'login': 'new-user', 'password': 'new-password'})
        self.assertIn('/auth/email/', response.location)

    def test_csrf_and_no_referrer(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        _, path = self.link()
        self.assertEqual(self.client.post(path).status_code, 400)
        response = self.client.get(path)
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_disabled_mail(self):
        self.app.config['MAIL_PASSWORD'] = None
        self.assertFalse(request_link(self.buyer, 'verify'))
        self.assertEqual(deliver_batch(), 0)

    def test_order_notification_transaction(self):
        order = Order(order_number='TEST-1', buyer_id=self.buyer.id,
                      seller_id=self.seller.id, total_price=100, status='processing')
        db.session.add(order)
        db.session.commit()
        order.status = 'shipped'
        db.session.flush()
        self.assertEqual(EmailOutbox.query.count(), 2)
        db.session.rollback()
        self.assertEqual(EmailOutbox.query.count(), 0)
        order.status = 'delivered'
        db.session.commit()
        self.assertEqual(EmailOutbox.query.count(), 2)
        db.session.commit()
        self.assertEqual(EmailOutbox.query.count(), 2)

    def test_seller_signup_and_public_pages(self):
        response = self.client.post('/auth/seller/signup', data={'login': 'new-seller', 'email': 'seller@example.test', 'password': 'new-password', 'password_confirm': 'new-password', 'store_name': 'New Store'})
        self.assertEqual(response.status_code, 302)
        user = Seller.query.filter_by(login='new-seller').one()
        self.assertTrue(needs_verification(user))
        for path in ('/auth/email/', '/auth/email/forgot', '/auth/login', '/auth/seller/login'):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_reset_revokes_existing_and_legacy_sessions(self):
        versioned = self.buyer.get_id()
        legacy = f'Buyer:{self.buyer.id}'
        with self.app.test_request_context():
            self.assertIsNotNone(self.app.login_manager._user_callback(versioned))
            self.assertIsNotNone(self.app.login_manager._user_callback(legacy))
        raw, _ = self.link('reset')
        self.assertTrue(consume_token(raw, 'reset', 'new-password'))
        with self.app.test_request_context():
            self.assertIsNone(self.app.login_manager._user_callback(versioned))
            self.assertIsNone(self.app.login_manager._user_callback(legacy))
            self.assertIsNotNone(self.app.login_manager._user_callback(self.buyer.get_id()))


if __name__ == '__main__':
    unittest.main()
