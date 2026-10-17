import hashlib
import logging
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path
from flask import g
from app import create_app, db
from config import TestingConfig
from app.models.users import Buyer, Seller
from app.models.orders import Order
from app.models.payments import RobokassaPayment


class RobokassaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            MAIL_WORKER_ENABLED = False
            MAIL_PASSWORD = None
            LOG_FILE = str(Path(self.directory.name) / 'test.log')
            ROBOKASSA_ENABLED = True
            ROBOKASSA_MERCHANT_LOGIN = 'shop-test'
            ROBOKASSA_SELLER_EMAIL = 'seller@example.test'
            ROBOKASSA_TEST_MODE = False
            ROBOKASSA_PASSWORD1 = 'test-pass-one'
            ROBOKASSA_PASSWORD2 = 'test-pass-two'
            ROBOKASSA_TEST_PASSWORD1 = 'sandbox-one'
            ROBOKASSA_TEST_PASSWORD2 = 'sandbox-two'
        self.app = create_app(Config)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.buyer = Buyer(login='buyer', email='buyer@example.test', password_hash='test')
        self.other_buyer = Buyer(login='other', email='other@example.test', password_hash='test')
        self.seller = Seller(login='seller', email='seller@example.test', password_hash='test',
                             store_name='Store', store_slug='store')
        self.other_seller = Seller(login='other-seller', email='other-seller@example.test',
                                   password_hash='test', store_name='Other', store_slug='other')
        db.session.add_all([self.buyer, self.other_buyer, self.seller, self.other_seller])
        db.session.flush()
        self.order = Order(order_number='ORDER-1', buyer=self.buyer, seller=self.seller,
                           total_price=100, delivery_price=20, bonus_used=10,
                           promo_discount=5, status='processing')
        db.session.add(self.order)
        db.session.commit()
        self.client = self.app.test_client()
        self.login(self.buyer)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and self.directory.name in handler.baseFilename:
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.directory.cleanup()

    def login(self, user):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session['_user_id'] = user.get_id()
            session['_fresh'] = True

    def start(self):
        return self.client.post('/payment/process', data={'order_id': self.order.id})

    def callback(self, invoice, amount='105.000000', secret='test-pass-two'):
        inv_id = str(invoice.id)
        sig = hashlib.md5(f'{amount}:{inv_id}:{secret}'.encode()).hexdigest()
        return {'InvId': inv_id, 'OutSum': amount, 'SignatureValue': sig}

    def invoice(self):
        self.assertEqual(self.start().status_code, 200)
        return RobokassaPayment.query.one()

    def test_exact_discounted_amount_and_no_fake_payment(self):
        response = self.start()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="OutSum" value="105.00"', response.data)
        self.assertNotIn(b'test-pass', response.data)
        self.assertEqual(self.order.status, 'processing')
        self.assertEqual(RobokassaPayment.query.one().amount, Decimal('105.00'))
        self.assertEqual(self.start().status_code, 200)
        self.assertEqual(RobokassaPayment.query.count(), 1)

    def test_only_signed_callback_pays_and_retry_is_idempotent(self):
        invoice = self.invoice()
        data = self.callback(invoice)
        self.client.post('/payments/robokassa/result', data={**data, 'SignatureValue': 'bad'})
        self.assertEqual(self.order.status, 'processing')
        anonymous = self.app.test_client()
        for _ in range(2):
            response = anonymous.post('/payments/robokassa/result', data=data)
            self.assertEqual(response.data, f'OK{invoice.id}'.encode())
        self.assertEqual(self.order.status, 'processing')
        self.assertTrue(self.order.payment_confirmed)
        self.assertIsNotNone(invoice.paid_at)
        self.assertEqual(self.start().status_code, 409)

    def test_wrong_amount_is_rejected_even_when_signed(self):
        invoice = self.invoice()
        for amount in ('1.00', 'NaN', '-105', '105.001'):
            response = self.client.post('/payments/robokassa/result', data=self.callback(invoice, amount))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.order.status, 'processing')

    def test_buyer_and_seller_scope_cannot_be_forged(self):
        self.login(self.other_buyer)
        self.assertEqual(self.start().status_code, 404)
        self.assertEqual(self.client.get(f'/payment?order_id={self.order.id}').status_code, 404)
        self.login(self.buyer)
        self.order.seller = self.other_seller
        db.session.commit()
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(RobokassaPayment.query.count(), 0)

    def test_disabled_or_incomplete_configuration_fails_closed(self):
        for setting, value in [('ROBOKASSA_ENABLED', False), ('ROBOKASSA_SELLER_EMAIL', ''),
                               ('ROBOKASSA_PASSWORD2', '')]:
            old = self.app.config[setting]
            self.app.config[setting] = value
            self.assertEqual(self.start().status_code, 503)
            self.app.config[setting] = old

    def test_success_and_fail_do_not_change_order(self):
        invoice = self.invoice()
        data = self.callback(invoice, secret='test-pass-one')
        self.assertEqual(self.client.get('/payments/robokassa/success', query_string=data).status_code, 302)
        self.client.get('/payments/robokassa/fail')
        self.assertEqual(self.order.status, 'processing')
        self.assertIsNone(invoice.paid_at)

    def test_test_mode_cannot_pay_real_order(self):
        self.app.config['ROBOKASSA_TEST_MODE'] = True
        invoice = self.invoice()
        self.assertTrue(invoice.is_test)
        response = self.client.post('/payments/robokassa/result', data=self.callback(invoice, secret='sandbox-two'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(self.order.status, 'processing')
        self.app.config['ROBOKASSA_TEST_MODE'] = False
        live_invoice_response = self.start()
        self.assertEqual(live_invoice_response.status_code, 200)
        self.assertEqual(RobokassaPayment.query.count(), 2)

    def test_cancelled_or_changed_order_records_payment_for_review(self):
        invoice = self.invoice()
        self.order.status = 'cancelled'
        db.session.commit()
        response = self.client.post('/payments/robokassa/result', data=self.callback(invoice))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invoice.status, 'needs_review')
        self.assertEqual(self.order.status, 'cancelled')

    def test_invoice_amount_is_frozen(self):
        self.invoice()
        self.order.total_price = 200
        db.session.commit()
        self.assertEqual(self.start().status_code, 409)

    def test_real_callback_has_csrf_exemption_but_start_does_not(self):
        invoice = self.invoice()
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.start().status_code, 400)
        self.assertEqual(self.client.post('/payments/robokassa/result',
                                         data=self.callback(invoice)).status_code, 200)

    def test_malformed_and_injected_callback_fields(self):
        invoice = self.invoice()
        data = self.callback(invoice)
        for extra in ({'InvId': '-1'}, {'InvId': '999999999999999999'}, {'Shp_x': 'injected'}):
            response = self.client.post('/payments/robokassa/result', data={**data, **extra})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.order.status, 'processing')

    def test_checkout_renders_scoped_order_and_total(self):
        response = self.client.get(f'/payment?order_id={self.order.id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'105.00', response.data)
        self.assertIn(b'ORDER-1', response.data)

    def test_order_list_has_no_payment_action(self):
        response = self.client.get('/profile?section=orders')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(f'/payment?order_id={self.order.id}'.encode(), response.data)
        self.order.seller = self.other_seller
        db.session.commit()
        response = self.client.get('/profile?section=orders')
        self.assertNotIn(f'/payment?order_id={self.order.id}'.encode(), response.data)

    def test_completed_sandbox_has_success_marker_instead_of_retry_button(self):
        self.app.config['ROBOKASSA_TEST_MODE'] = True
        invoice = self.invoice()
        self.client.post('/payments/robokassa/result', data=self.callback(invoice, secret='sandbox-two'))
        page = self.client.get(f'/payment?order_id={self.order.id}')
        self.assertIn('Тестовая оплата пройдена'.encode(), page.data)
        self.assertNotIn(b'id="payment-form"', page.data)
        self.assertNotIn(b'action="/payment/process"', page.data)
        profile = self.client.get('/profile?section=orders')
        self.assertIn('Тестовая оплата пройдена'.encode(), profile.data)
        self.assertNotIn(f'/payment?order_id={self.order.id}'.encode(), profile.data)

    def cart_product(self, other_seller=False):
        from app.models.products import Category, Product
        from app.models.orders import CartItem
        from app.models.users import DeliveryService
        category = Category(name='Goods', slug='goods')
        product = Product(name='Goods', slug='goods', article='goods',
                          seller=self.other_seller if other_seller else self.seller,
                          category=category, price=100, stock_quantity=20,
                          status='approved', current_discount=0)
        db.session.add_all([category, product, DeliveryService(id=1, name='Delivery', code='test')])
        db.session.add(CartItem(buyer=self.buyer, product=product, quantity=1))
        db.session.commit()
        return product

    def pay_from_review(self, product, route='/order-review/submit'):
        with patch('app.blueprints.main._visible_seller_ids', return_value={product.seller_id}):
            response = self.client.post(route)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.json['redirect'].startswith('https://auth.robokassa.ru/Merchant/Index.aspx?'))
        return response

    def test_no_order_is_accepted_or_notified_until_live_callback(self):
        from app.models.orders import CartItem
        product = self.cart_product()
        with patch('app.utils.emails.send_new_order_notification_to_seller') as notify, \
             patch('app.utils.emails.create_order_conversation') as conversation:
            self.pay_from_review(product)
            invoice = RobokassaPayment.query.one()
            quote = invoice.order
            self.assertEqual(quote.status, 'awaiting_payment')
            self.assertFalse(quote.payment_confirmed)
            self.assertEqual(product.stock_quantity, 19)
            self.assertEqual(CartItem.query.count(), 1)
            notify.assert_not_called()
            conversation.assert_not_called()
            for page in ('/profile?section=orders', '/profile/orders'):
                self.assertNotIn(f'/profile/order/{quote.id}'.encode(), self.client.get(page).data)
            self.login(self.seller)
            self.assertEqual(self.client.get(f'/seller/orders/{quote.id}').status_code, 404)
            self.login(self.buyer)
            data = self.callback(invoice, amount='100.000000')
            for _ in range(2):
                response = self.app.test_client().post('/payments/robokassa/result', data=data)
                self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(quote.status, 'processing')
            self.assertTrue(quote.payment_confirmed)
            self.assertEqual(product.stock_quantity, 19)
            self.assertEqual(CartItem.query.count(), 0)
            notify.assert_called_once_with(quote.id)
            conversation.assert_called_once()
            self.login(self.buyer)
            self.assertIn(f'/profile/order/{quote.id}'.encode(), self.client.get('/profile?section=orders').data)

    def test_mail_hooks_never_announce_unpaid_or_demo_orders(self):
        self.app.config['ROBOKASSA_TEST_MODE'] = True
        product = self.cart_product()
        with patch('app.utils.email_events.configured', return_value=True), \
             patch('app.utils.email_events.enqueue') as status_mail, \
             patch('app.utils.admin_notifications.configured', return_value=True), \
             patch('app.utils.admin_notifications.notification_settings', return_value={
                 'enabled': True, 'recipient': 'admin@example.test', 'events': ['order_created']}), \
             patch('app.utils.admin_notifications.enqueue') as admin_mail:
            self.pay_from_review(product)
            invoice = RobokassaPayment.query.one()
            data = self.callback(invoice, amount='100.00', secret='sandbox-two')
            self.client.post('/payments/robokassa/result', data=data)
            status_mail.assert_not_called()
            admin_mail.assert_not_called()

    def test_late_paid_expired_reservation_is_not_an_accepted_order(self):
        from datetime import datetime, timedelta
        product = self.cart_product()
        self.pay_from_review(product)
        invoice = RobokassaPayment.query.one()
        invoice.order.created_at = datetime.utcnow() - timedelta(minutes=31)
        db.session.commit()
        response = self.client.post('/payments/robokassa/result',
                                     data=self.callback(invoice, amount='100.00'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invoice.status, 'needs_review')
        self.assertEqual(invoice.order.status, 'payment_expired')
        self.assertEqual(product.stock_quantity, 20)

    def test_double_click_and_legacy_checkout_reuse_same_invoice(self):
        product = self.cart_product()
        first = self.pay_from_review(product)
        second = self.pay_from_review(product)
        legacy = self.pay_from_review(product, route='/order/create')
        self.assertEqual(first.json['redirect'], second.json['redirect'])
        self.assertEqual(first.json['redirect'], legacy.json['redirect'])
        self.assertEqual(RobokassaPayment.query.count(), 1)
        self.assertEqual(product.stock_quantity, 19)

    def test_checkout_blocks_unconfigured_sellers_without_an_order(self):
        product = self.cart_product(other_seller=True)
        with patch('app.blueprints.main._visible_seller_ids', return_value={product.seller_id}):
            response = self.client.post('/order-review/submit')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.query.count(), 1)
        self.assertEqual(RobokassaPayment.query.count(), 0)
        self.assertEqual(product.stock_quantity, 20)

    def test_sandbox_does_not_create_an_accepted_order_and_releases_stock(self):
        from app.models.orders import CartItem
        self.app.config['ROBOKASSA_TEST_MODE'] = True
        product = self.cart_product()
        self.pay_from_review(product)
        invoice = RobokassaPayment.query.one()
        with patch('app.utils.emails.send_new_order_notification_to_seller') as notify:
            data = self.callback(invoice, amount='100.00', secret='sandbox-two')
            self.assertEqual(self.client.post('/payments/robokassa/result', data=data).status_code, 200)
            self.assertEqual(self.client.post('/payments/robokassa/result', data=data).status_code, 200)
            notify.assert_not_called()
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.order.status, 'payment_demo')
        self.assertFalse(invoice.order.payment_confirmed)
        self.assertEqual(product.stock_quantity, 20)
        self.assertEqual(CartItem.query.count(), 1)
        self.assertNotIn(f'/profile/order/{invoice.order_id}'.encode(), self.client.get('/profile?section=orders').data)

    def test_unpaid_checkout_expiration_returns_reserved_stock(self):
        from datetime import datetime, timedelta
        product = self.cart_product()
        self.pay_from_review(product)
        invoice = RobokassaPayment.query.one()
        invoice.order.created_at = datetime.utcnow() - timedelta(minutes=31)
        db.session.commit()
        self.client.get('/payment')
        self.assertEqual(invoice.status, 'expired')
        self.assertEqual(invoice.order.status, 'payment_expired')
        self.assertEqual(product.stock_quantity, 20)
        self.client.get('/payment')
        self.assertEqual(product.stock_quantity, 20)

    def test_browser_success_is_not_payment_acceptance(self):
        product = self.cart_product()
        self.pay_from_review(product)
        invoice = RobokassaPayment.query.one()
        data = self.callback(invoice, amount='100.00', secret='test-pass-one')
        self.client.get('/payments/robokassa/success', query_string=data)
        self.client.get('/payments/robokassa/fail')
        self.assertEqual(invoice.order.status, 'awaiting_payment')
        self.assertFalse(invoice.order.payment_confirmed)

    def test_review_button_is_pay_not_place_order(self):
        product = self.cart_product()
        with patch('app.blueprints.main._visible_seller_ids', return_value={product.seller_id}):
            response = self.client.get('/order-review')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Оплатить'.encode(), response.data)
        self.assertNotIn('Сделать заказ'.encode(), response.data)
