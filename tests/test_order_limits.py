import logging
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app import create_app, db
from config import TestingConfig
from app.models.users import Seller, Buyer
from app.models.products import Product, Category
from app.models.orders import CartItem, Order
from app.utils.order_limits import minimum_order_error, parse_minimum_order_amount


class OrderLimitsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            MAIL_WORKER_ENABLED = False
            MAIL_PASSWORD = None
            LOG_FILE = str(Path(self.directory.name) / 'test.log')
        self.app = create_app(Config)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.seller = Seller(login='seller', email='seller@example.test', password_hash='test',
                             store_name='Store', store_slug='store', phone='123',
                             minimum_order_amount=Decimal('100'), daily_orders_limit=3)
        self.buyer = Buyer(login='buyer', email='buyer@example.test', password_hash='test')
        category = Category(name='Category', slug='category')
        self.product = Product(name='Product', slug='product', article='one', seller=self.seller,
                               category=category, price=100, current_discount=10,
                               stock_quantity=10, status='approved')
        self.item = CartItem(buyer=self.buyer, product=self.product, quantity=1)
        db.session.add_all([self.seller, self.buyer, category, self.product, self.item])
        db.session.commit()
        self.client = self.app.test_client()

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
        with self.client.session_transaction() as session:
            session['_user_id'] = user.get_id()
            session['_fresh'] = True

    def test_settings_preserve_other_forms_and_validate(self):
        self.login(self.seller)
        response = self.client.post('/seller/settings', data={'minimum_order_amount': '150,50'})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.seller)
        self.assertEqual(self.seller.minimum_order_amount, Decimal('150.50'))
        self.assertEqual(self.seller.store_name, 'Store')
        self.assertEqual(self.seller.phone, '123')
        self.assertEqual(self.seller.daily_orders_limit, 3)
        self.client.post('/seller/settings', data={'store_name': 'Updated'})
        db.session.refresh(self.seller)
        self.assertEqual(self.seller.minimum_order_amount, Decimal('150.50'))
        self.assertEqual(self.seller.daily_orders_limit, 3)
        for value in ['-1', 'NaN', 'Infinity', '1.001', '10000000000', 'bad']:
            self.client.post('/seller/settings', data={'minimum_order_amount': value})
            db.session.refresh(self.seller)
            self.assertEqual(self.seller.minimum_order_amount, Decimal('150.50'))
        self.client.post('/seller/settings', data={'minimum_order_amount': ''})
        db.session.refresh(self.seller)
        self.assertIsNone(self.seller.minimum_order_amount)
        self.assertEqual(self.client.get('/seller/settings').status_code, 200)

    def test_discounted_threshold_and_independent_stores(self):
        self.assertIn('10.00', minimum_order_error([self.item]))
        self.seller.minimum_order_amount = Decimal('90')
        self.assertIsNone(minimum_order_error([self.item]))
        self.seller.minimum_order_amount = Decimal('100')
        second = Seller(id=999, store_name='Other', minimum_order_amount=None)
        other_item = CartItem(product=Product(seller=second, price=1000, current_discount=0), quantity=1)
        self.assertIn('Store', minimum_order_error([self.item, other_item]))
        self.seller.minimum_order_amount = None
        self.assertIsNone(minimum_order_error([self.item]))
        self.assertIsNone(parse_minimum_order_amount('0'))

    def test_checkout_routes_reject_before_creating_orders(self):
        self.login(self.buyer)
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}):
            response = self.client.get('/checkout?delivery=1:1:PVZ:1000')
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/cart'))
            self.assertEqual(Order.query.count(), 0)
            response = self.client.get('/cart')
            self.assertEqual(response.status_code, 200)
            self.assertIn('Минимальный заказ', response.get_data(as_text=True))
            for route, data in [('/order-review/submit', {}),
                                ('/order/create', {'delivery_info': '[{"seller_id":1,"cost":1000}]'})]:
                response = self.client.post(route, data=data)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn('Минимальная сумма', response.json['error'])
                self.assertEqual(Order.query.count(), 0)
                self.assertEqual(self.product.stock_quantity, 10)


if __name__ == '__main__':
    unittest.main()
