import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app, db
from config import TestingConfig
from app.models.users import Buyer, Seller, BuyerDelivery, SellerDelivery, DeliveryService
from app.models.products import Product, Category
from app.models.orders import CartItem, Order


class DeliveryDetailsTests(unittest.TestCase):
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
        self.buyer = Buyer(login='buyer', email='buyer@example.test', password_hash='test',
                           name='Иван Иванов', phone='+79991234567')
        self.seller = Seller(login='seller', email='seller@example.test', password_hash='test',
                             store_name='Store', store_slug='store')
        self.cdek = DeliveryService(name='CDEK', code='cdek')
        self.ozon = DeliveryService(name='Ozon', code='ozon')
        self.product = Product(name='Product', slug='product', article='one', seller=self.seller,
                               category=Category(name='Category', slug='category'), price=100,
                               current_discount=0, stock_quantity=10, status='approved')
        self.delivery = BuyerDelivery(buyer=self.buyer, delivery_service=self.cdek, pvz_address='ПВЗ 1')
        db.session.add_all([self.buyer, self.seller, self.cdek, self.ozon, self.product,
                           self.delivery, CartItem(buyer=self.buyer, product=self.product, quantity=1)])
        db.session.flush()
        db.session.add(SellerDelivery(seller_id=self.seller.id, delivery_service_id=self.cdek.id, is_active=True))
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

    def submit(self, delivery_id=None):
        data = {f'delivery[{self.seller.id}]': str(delivery_id or self.delivery.id)}
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}), patch('app.blueprints.payments.configured', return_value=False):
            return self.client.post('/order-review/submit', data=data)

    def test_fields_prefill_and_save(self):
        html = self.client.get('/profile?section=addresses').get_data(as_text=True)
        self.assertIn('ФИО получателя', html)
        self.assertIn('value="Иван Иванов"', html)
        self.assertIn('value="+79991234567"', html)
        self.assertIn(f'name="city_{self.ozon.id}"', html)
        response = self.client.post('/profile/addresses/save', data={
            f'address_{self.cdek.id}': 'ПВЗ 1', f'city_{self.cdek.id}': 'Москва',
            f'recipient_name_{self.cdek.id}': 'Другой Получатель',
            f'recipient_phone_{self.cdek.id}': '+79997654321',
            f'address_{self.ozon.id}': 'ПВЗ 2', f'city_{self.ozon.id}': 'Казань'})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.delivery)
        self.assertEqual(self.delivery.pvz_city, 'Москва')
        self.assertEqual(self.delivery.recipient_name, 'Другой Получатель')
        self.assertEqual(self.delivery.recipient_phone, '+79997654321')
        self.assertEqual(self.buyer.name, 'Иван Иванов')
        html = self.client.get('/profile?section=addresses').get_data(as_text=True)
        self.assertIn('value="Другой Получатель"', html)

    def test_missing_profile_details_stay_empty(self):
        self.buyer.name = self.buyer.phone = None
        db.session.commit()
        import re
        html = self.client.get('/profile?section=addresses').get_data(as_text=True)
        for field in ('recipient_name', 'recipient_phone'):
            match = re.search(r'<input[^>]*name="' + field + '_' + str(self.cdek.id) + r'"[^>]*>', html)
            self.assertIn('value=""', match.group())

    def test_incomplete_selected_cdek_redirects_without_creating_order(self):
        for city, name, phone in [('Москва', '', '123'), ('Москва', 'Иван', ' '), (' ', 'Иван', '123')]:
            self.delivery.pvz_city, self.delivery.recipient_name, self.delivery.recipient_phone = city, name, phone
            db.session.commit()
            response = self.submit()
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json['redirect'], '/profile?section=addresses')
            self.assertEqual(Order.query.count(), 0)
            self.assertEqual(self.product.stock_quantity, 10)
        self.delivery.pvz_city, self.delivery.recipient_name, self.delivery.recipient_phone = 'Москва', 'Иван', '123'
        db.session.commit()
        self.assertEqual(self.submit().status_code, 503)  # Delivery passed; payments disabled by test.

    def test_ozon_requires_city_without_recipient_contacts(self):
        self.delivery.delivery_service = self.ozon
        db.session.add(SellerDelivery(seller_id=self.seller.id, delivery_service_id=self.ozon.id, is_active=True))
        db.session.commit()
        self.assertEqual(self.submit().status_code, 400)
        self.delivery.pvz_city = 'Казань'
        db.session.commit()
        self.assertEqual(self.delivery.missing_required_fields, [])
        self.assertEqual(self.submit().status_code, 503)

    def test_foreign_delivery_and_missing_selection_rejected(self):
        other = Buyer(login='other', email='other@example.test', password_hash='test')
        foreign = BuyerDelivery(buyer=other, delivery_service=self.cdek, pvz_address='ПВЗ',
                                pvz_city='Москва', recipient_name='Иван', recipient_phone='123')
        db.session.add(foreign)
        db.session.commit()
        self.assertEqual(self.submit(foreign.id).json['redirect'], '/profile?section=addresses')
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}):
            response = self.client.post('/order/create')
        self.assertEqual(response.json['redirect'], '/profile?section=addresses')
        self.assertEqual(Order.query.count(), 0)

    def test_review_flags_incomplete_option_and_submits_selected_delivery(self):
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}):
            response = self.client.get('/order-review')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-incomplete="1"', html)
        self.assertIn('form="checkout-form"', html)
        self.assertIn('dataset.incomplete', html)

    def test_complete_delivery_is_snapshotted_before_payment(self):
        self.app.config.update(ROBOKASSA_ENABLED=True, ROBOKASSA_MERCHANT_LOGIN='shop-test',
                               ROBOKASSA_SELLER_EMAIL='seller@example.test', ROBOKASSA_TEST_MODE=False,
                               ROBOKASSA_PASSWORD1='test-one', ROBOKASSA_PASSWORD2='test-two')
        self.delivery.pvz_city = 'Москва'
        self.delivery.recipient_name = 'Получатель Посылки'
        self.delivery.recipient_phone = '+79997654321'
        db.session.commit()
        data = {f'delivery[{self.seller.id}]': str(self.delivery.id)}
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}):
            response = self.client.post('/order-review/submit', data=data)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.json['success'])
        order = Order.query.one()
        self.assertEqual(order.delivery_service_id, self.cdek.id)
        self.assertEqual(order.delivery_address, 'Москва, ПВЗ 1, Получатель Посылки, +79997654321')
        self.delivery.recipient_phone = ''
        db.session.commit()
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}):
            response = self.client.post('/order-review/submit', data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['redirect'], '/profile?section=addresses')
        self.assertEqual(Order.query.count(), 1)

    def test_digital_goods_do_not_require_delivery(self):
        from app.models.digital import DigitalBook
        book = DigitalBook(seller=self.seller, title='Book', price=10, file_name='book.epub', format='epub')
        db.session.add(book)
        db.session.flush()
        self.product.digital_book_id = book.id
        db.session.commit()
        self.assertEqual(self.submit().status_code, 503)


if __name__ == '__main__':
    unittest.main()
