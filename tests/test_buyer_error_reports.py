import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import create_app, db
from config import TestingConfig
from app.models.email_delivery import EmailOutbox
from app.models.users import Buyer, Seller, DeliveryService
from app.models.orders import Order, OrderItem
from app.models.products import Product, Category
from app.utils.emails import order_notification_details


class BuyerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            MAIL_WORKER_ENABLED = False
            MAIL_PASSWORD = None
            LOG_FILE = str(Path(self.tmp.name) / 'test.log')
        self.app = create_app(Config)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.buyer = Buyer(login='buyer', first_name='Иван', last_name='Иванов',
                           email='buyer@example.test', password_hash='test')
        db.session.add(self.buyer)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = self.buyer.get_id()
            session['_fresh'] = True

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and self.tmp.name in handler.baseFilename:
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.tmp.cleanup()

    def test_browser_reports_are_bounded_and_do_not_include_payload_secrets(self):
        with patch('app.utils.buyer_errors.configured', return_value=True):
            for _ in range(3):
                response = self.client.post('/diagnostics/client-error', json={
                    'kind': 'javascript', 'source': '/cart?password=never-email-me',
                    'line': 42, 'message': 'card=never-email-me'})
        self.assertEqual(response.status_code, 204)
        mail = EmailOutbox.query.one()
        self.assertEqual(mail.recipient, 'ronnie83@mail.ru')
        self.assertIn('/cart', mail.body)
        self.assertNotIn('never-email-me', mail.body)
        self.assertIn('МСК', mail.body)

    def test_server_exception_report_omits_exception_message(self):
        self.app.config['PROPAGATE_EXCEPTIONS'] = False
        @self.app.route('/test-buyer-crash')
        def crash():
            raise ValueError('secret-password-should-not-be-sent')
        with patch('app.utils.buyer_errors.configured', return_value=True):
            response = self.client.get('/test-buyer-crash')
        self.assertEqual(response.status_code, 500)
        mail = EmailOutbox.query.one()
        self.assertIn('ValueError', mail.body)
        self.assertIn('crash', mail.body)
        self.assertNotIn('secret-password', mail.body)

    def test_external_source_is_rejected(self):
        response = self.client.post('/diagnostics/client-error', json={
            'kind': 'network', 'source': 'https://other.test/private', 'line': 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(EmailOutbox.query.count(), 0)

    def test_buyer_http_error_is_reported_without_query_string(self):
        with patch('app.utils.buyer_errors.configured', return_value=True):
            self.client.get('/missing-buyer-page?SignatureValue=secret')
        mail = EmailOutbox.query.one()
        self.assertIn('404', mail.body)
        self.assertIn('Покупатель ID: 1', mail.body)
        self.assertNotIn('secret', mail.body)

    def test_order_notification_contains_customer_items_delivery_and_moscow_time(self):
        from datetime import datetime
        seller = Seller(store_name='Store', store_slug='store', login='seller', email='seller@example.test', password_hash='test')
        category = Category(name='Goods', slug='goods')
        product = Product(name='Миниатюра', slug='mini', article='a', category=category,
                          seller=seller, price=100, stock_quantity=1, status='approved')
        delivery = DeliveryService(name='OZON', code='ozon')
        order = Order(order_number='TEST123', buyer=self.buyer, seller=seller, total_price=200,
                      delivery_price=50, delivery_service=delivery, pvz_code='PVZ123',
                      delivery_address='Москва', created_at=datetime(2026, 10, 18, 9, 15))
        db.session.add_all([product, order])
        db.session.flush()
        db.session.add(OrderItem(order=order, product=product, quantity=2, price_at_order=100))
        db.session.commit()
        body = order_notification_details(order)
        for expected in ('TEST123', '18.10.2026 12:15:00 МСК', 'Иванов Иван',
                         'Миниатюра', '2 шт.', '250.00', 'OZON', 'PVZ123', 'Москва'):
            self.assertIn(expected, body)
