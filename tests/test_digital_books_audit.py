import hashlib
import io
import logging
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from flask import g
from app import create_app, db
from config import TestingConfig
from app.models.users import Buyer, Seller
from app.models.digital import DigitalBook, DigitalPurchase
from app.models.payments import RobokassaPayment
from app.models.products import Product, Category, Parameter, CategoryParameter
from app.models.orders import CartItem
from app.utils.digital_books import storage, epub_chapters


def epub_file():
    data = io.BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr('book.opf', '<package><manifest><item id="a" href="chapter.xhtml"/></manifest><spine><itemref idref="a"/></spine></package>')
        archive.writestr('chapter.xhtml', '<html><body><h1>Глава первая</h1><p>Текст книги &lt;img onerror=alert(1)&gt;</p><script>alert(2)</script></body></html>')
    return data.getvalue()


class DigitalBooksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            MAIL_WORKER_ENABLED = False
            MAIL_PASSWORD = None
            LOG_FILE = str(Path(self.tmp.name) / 'test.log')
            UPLOAD_FOLDER = str(Path(self.tmp.name) / 'uploads')
            ROBOKASSA_ENABLED = True
            ROBOKASSA_MERCHANT_LOGIN = 'pilot'
            ROBOKASSA_SELLER_EMAIL = 'ronnie83@mail.ru'
            ROBOKASSA_TEST_MODE = False
            ROBOKASSA_PASSWORD1 = 'one'
            ROBOKASSA_PASSWORD2 = 'two'
            ROBOKASSA_TEST_PASSWORD1 = 'test-one'
            ROBOKASSA_TEST_PASSWORD2 = 'test-two'
        self.app = create_app(Config)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.buyer = Buyer(login='pilot', email='ronnie83@mail.ru', password_hash='test')
        self.other = Buyer(login='other', email='other@example.test', password_hash='test')
        self.seller = Seller(login='pilot', email='ronnie83@mail.ru', password_hash='test', store_name='Store', store_slug='store')
        db.session.add_all([self.buyer, self.other, self.seller])
        self.category = Category(name='Книги', slug='books', is_digital_book=True)
        db.session.add(self.category)
        db.session.flush()
        storage().mkdir(parents=True)
        (storage() / 'test.epub').write_bytes(epub_file())
        self.book = DigitalBook(seller=self.seller, title='Тестовая книга', author='Автор',
                                price=120, file_name='test.epub', format='epub', published=True, moderation_status='approved')
        db.session.add(self.book)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and self.tmp.name in handler.baseFilename:
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.tmp.cleanup()

    def login(self, user=None, admin=False):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session.clear()
            if user:
                session['_user_id'] = user.get_id()
                session['_fresh'] = True
            if admin:
                session['main_admin_authenticated'] = True

    def test_order_limits_apply_only_to_physical_goods(self):
        from decimal import Decimal
        from app.models.orders import Order, OrderItem
        from app.utils.order_limits import minimum_order_error
        from app.blueprints.main import _seller_blocked_by_daily_limit
        self.book.category = self.category
        self.seller.minimum_order_amount = Decimal('100')
        self.seller.daily_orders_limit = 1
        db.session.commit()
        product = self.book.commerce_product
        digital_item = CartItem(product=product, quantity=1)
        physical = Product(seller=self.seller, category=self.category, name='Physical',
                           slug='physical', article='physical', price=50, current_discount=0,
                           stock_quantity=10, status='approved')
        db.session.add(physical)
        db.session.flush()
        physical_item = CartItem(product=physical, quantity=1)
        self.assertIsNone(minimum_order_error([digital_item]))
        self.assertIn('50.00', minimum_order_error([digital_item, physical_item]))
        order = Order(order_number='limit-test', buyer=self.buyer, seller=self.seller,
                      total_price=120, status='received')
        db.session.add(order)
        db.session.add(OrderItem(order=order, product=product, quantity=1, price_at_order=120))
        db.session.commit()
        with self.app.test_request_context():
            g.pop(f'_seller_blocked_cache_{self.seller.id}', None)
            self.assertFalse(_seller_blocked_by_daily_limit(self.seller))
        db.session.add(OrderItem(order=order, product=physical, quantity=1, price_at_order=50))
        db.session.commit()
        with self.app.test_request_context():
            g.pop(f'_seller_blocked_cache_{self.seller.id}', None)
            self.assertTrue(_seller_blocked_by_daily_limit(self.seller))
        self.login(self.buyer)
        with patch('app.blueprints.main._resolve_tariff_state', return_value={'state': 'paid'}):
            response = self.client.post(f'/digital/books/{self.book.id}/cart')
            self.assertEqual(response.status_code, 302)
            self.assertIsNotNone(db.session.get(CartItem, (self.buyer.id, product.id)))
            response = self.client.post('/cart/add', data={'product_id': physical.id})
            self.assertEqual(response.status_code, 404)
        order.status = 'canceled'
        db.session.commit()
        with self.app.test_request_context():
            g.pop(f'_seller_blocked_cache_{self.seller.id}', None)
            self.assertFalse(_seller_blocked_by_daily_limit(self.seller))

    def test_free_test_access_is_scoped_and_does_not_confirm_payment(self):
        from app.blueprints.digital import purchased
        from app.models.orders import Order
        self.login(self.buyer)
        self.assertIn('Получить для теста', self.client.get(f'/digital/books/{self.book.id}').get_data(as_text=True))
        response = self.client.post(f'/digital/books/{self.book.id}/test-access')
        purchase = DigitalPurchase.query.one()
        self.assertEqual(response.location, f'/digital/read/{purchase.id}')
        self.assertTrue(purchased(purchase))
        self.assertFalse(purchase.order.payment_confirmed)
        self.assertEqual(purchase.order.grand_total, 0)
        self.assertEqual(purchase.order.bonus_accrued, 0)
        self.assertEqual(RobokassaPayment.query.count(), 0)
        self.assertEqual(self.client.get(response.location).status_code, 200)
        self.assertIn(self.book.title, self.client.get('/digital/library').get_data(as_text=True))
        self.client.post(f'/digital/books/{self.book.id}/test-access')
        self.assertEqual(DigitalPurchase.query.count(), 1)
        self.assertEqual(Order.query.count(), 1)
        purchase.order.total_price = 1
        self.assertFalse(purchased(purchase))
        purchase.order.total_price = 0
        purchase.order.customer_note = ''
        self.assertFalse(purchased(purchase))

    def test_free_test_access_denies_other_users_and_unpublished_books(self):
        for user in (None, self.other, self.seller):
            self.login(user)
            self.assertEqual(self.client.post(f'/digital/books/{self.book.id}/test-access').status_code, 404)
        self.login(self.buyer)
        self.book.published = False
        db.session.commit()
        self.assertEqual(self.client.post(f'/digital/books/{self.book.id}/test-access').status_code, 404)
        self.assertEqual(DigitalPurchase.query.count(), 0)

    def pay(self, test=False):
        self.login(self.buyer)
        self.app.config['ROBOKASSA_TEST_MODE'] = test
        response = self.client.post(f'/digital/books/{self.book.id}/buy')
        self.assertEqual(response.status_code, 302)
        invoice = RobokassaPayment.query.one()
        secret = 'test-two' if test else 'two'
        signature = hashlib.md5(f'120.00:{invoice.id}:{secret}'.encode()).hexdigest()
        response = self.client.post('/payments/robokassa/result', data={
            'OutSum': '120.00', 'InvId': str(invoice.id), 'SignatureValue': signature})
        self.assertEqual(response.status_code, 200)
        return DigitalPurchase.query.one()

    @patch('app.blueprints.main._visible_seller_ids_with_limit', return_value=[1])
    def test_common_catalogue_includes_only_approved_pilot_books(self, visible):
        self.book.category = self.category
        db.session.commit()
        self.login(self.buyer)
        for path in ('/catalogue/', f'/catalogue/{self.category.id}/'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Тестовая книга', response.get_data(as_text=True))
        self.assertNotIn('Тестовая книга', self.client.get('/catalogue/?price_min=121').get_data(as_text=True))
        self.book.moderation_status = 'pending'
        db.session.commit()
        self.assertNotIn('Тестовая книга', self.client.get('/catalogue/').get_data(as_text=True))
        self.book.moderation_status = 'approved'
        db.session.commit()
        for user in (self.other, None):
            self.login(user)
            self.assertIn('Тестовая книга', self.client.get('/catalogue/').get_data(as_text=True))

    @patch('app.blueprints.main._visible_seller_ids_with_limit', return_value=[1])
    def test_common_catalogue_sorts_and_paginates_books(self, visible):
        self.book.category = self.category
        self.login(self.buyer)
        for index in range(22):
            db.session.add(DigitalBook(seller=self.seller, title=f'Book {index}', price=index + 1,
                category=self.category, file_name='test.epub', format='epub', published=True, moderation_status='approved'))
        db.session.commit()
        first = self.client.get('/catalogue/?sort=price_asc').get_data(as_text=True)
        self.assertIn('Book 0</a>', first)
        self.assertNotIn('Book 21</a>', first)
        second = self.client.get('/catalogue/?sort=price_asc&page=2').get_data(as_text=True)
        self.assertIn('Book 21</a>', second)
        self.assertIn('Тестовая книга', second)
        self.assertEqual(self.client.get('/catalogue/?sort=popular').status_code, 200)

    def test_public_storefront_keeps_library_and_management_private(self):
        for user in (None, self.other):
            self.login(user)
            self.assertEqual(self.client.get('/digital/books').status_code, 200)
            self.assertEqual(self.client.get('/digital/books/1').status_code, 200)
            self.assertEqual(self.client.get('/digital/library').status_code, 200 if user else 404)
            for path in ('/digital/manage',
                         '/digital/preview/1', '/digital/read/1', '/digital/file/1'):
                self.assertEqual(self.client.get(path).status_code, 404, path)
            if user is None:
                self.assertEqual(self.client.post('/digital/books/1/buy').status_code, 404)
        self.assertEqual(Product.query.count(), 0)

    def commerce_book(self):
        self.book.category = self.category
        db.session.commit()
        self.login(self.buyer)
        db.session.add(CartItem(
            buyer_id=self.buyer.id, product_id=self.book.commerce_product.id, quantity=1))
        db.session.commit()
        return self.book.commerce_product

    @patch('app.blueprints.main._visible_seller_ids_with_limit', return_value=[1])
    def test_mixed_feed_uses_common_product_ids_without_repeats(self, visible):
        # Deliberately make book.id differ from its ID in the common registry.
        for index in range(7):
            db.session.add(Product(name=f'Physical {index}', slug=f'physical-{index}',
                seller_id=self.seller.id, category_id=self.category.id,
                article=f'PHYSICAL-{index}', price=10, stock_quantity=1, status='approved'))
        db.session.commit()
        self.book.category = self.category
        db.session.commit()
        self.login(self.buyer)
        product = self.book.commerce_product
        self.assertNotEqual(product.id, self.book.id)
        self.assertEqual(product.system_sku, self.book.system_sku)
        original_id, original_sku = product.id, product.system_sku
        self.book.title = 'Updated book'
        db.session.commit()
        self.assertEqual(self.book.commerce_product.id, original_id)
        self.assertEqual(self.book.commerce_product.system_sku, original_sku)
        catalogue = self.client.get('/catalogue/').get_data(as_text=True)
        self.assertIn(f'data-product-id="{product.id}"', catalogue)
        seen = set()
        while True:
            response = self.client.post('/api/home-products', json={'excluded_ids': sorted(seen)})
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            rendered_ids = [int(value) for value in re.findall(
                r'<(?:article|div) class="product-card" data-product-id="(\d+)"', data['html'])]
            self.assertEqual(rendered_ids, data['ids'])
            self.assertFalse(seen.intersection(rendered_ids))
            seen.update(rendered_ids)
            if not data['has_more']:
                break
        self.assertEqual(seen, {p.id for p in Product.query.all()})
        self.assertIn(product.id, seen)

    def test_book_cart_button_adds_and_removes_without_redirect(self):
        self.book.category = self.category
        db.session.commit()
        self.login(self.buyer)
        product = self.book.commerce_product
        headers = {'X-Requested-With': 'XMLHttpRequest'}
        with patch('app.blueprints.main._resolve_tariff_state', return_value={'state': 'active'}), \
             patch('app.blueprints.main._seller_blocked_by_daily_limit', return_value=False):
            response = self.client.post(f'/digital/books/{self.book.id}/cart', headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['success'])
            self.assertEqual(response.json['cart_count'], 1)
            response = self.client.post(f'/digital/books/{self.book.id}/cart', headers=headers)
            self.assertEqual(response.json['cart_count'], 1)
        self.assertEqual(CartItem.query.count(), 1)
        page = self.client.get(f'/digital/books/{self.book.id}').get_data(as_text=True)
        self.assertIn('btn-outline-danger', page)
        self.assertIn('data-in-cart="true"', page)
        response = self.client.post('/cart/remove', data={'product_id': product.id}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cart_count'], 0)
        self.assertEqual(CartItem.query.count(), 0)
        page = self.client.get(f'/digital/books/{self.book.id}').get_data(as_text=True)
        self.assertIn('data-in-cart="false"', page)

    def test_book_cart_ajax_reports_error_without_navigation(self):
        self.book.category = self.category
        db.session.commit()
        self.login(self.buyer)
        with patch('app.blueprints.main._resolve_tariff_state', return_value={'state': 'locked'}):
            response = self.client.post(f'/digital/books/{self.book.id}/cart',
                headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('error', response.json)
        self.assertEqual(CartItem.query.count(), 0)

    def commerce_checkout(self, data=None):
        with patch('app.blueprints.main._visible_seller_ids', return_value=[self.seller.id]):
            response = self.client.post('/order-review/submit', data=data or {})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.json['success'], response.json)
        return RobokassaPayment.query.order_by(RobokassaPayment.id.desc()).first()

    def confirm_invoice(self, invoice):
        amount = f'{invoice.amount:.2f}'
        signature = hashlib.md5(f'{amount}:{invoice.id}:two'.encode()).hexdigest()
        response = self.client.post('/payments/robokassa/result', data={
            'OutSum': amount, 'InvId': str(invoice.id), 'SignatureValue': signature})
        self.assertEqual(response.status_code, 200)

    def test_shared_discounts_promocode_bonus_and_digital_delivery(self):
        from app.models.orders import Promotion, PromotionProduct, Bonus
        from app.models.promo import PromoCode
        from app.models.loyalty import LoyaltyRate, SellerLoyalty, BuyerBonus
        from app.models.communications import Settings
        product = self.commerce_book()
        product.current_discount = 10
        promotion = Promotion(name='Book sale', scheme='discount', discount_percent=20,
                              status='active', is_template=False)
        db.session.add(promotion)
        db.session.flush()
        db.session.add(PromotionProduct(promotion_id=promotion.id, product_id=product.id))
        code = PromoCode(seller_id=self.seller.id, code='BOOK10', discount_type='rub',
                         discount_value=10, recipient_type='public', usage_type='multiple',
                         max_uses=10, validity_type='forever')
        rate = LoyaltyRate(title='Two points', points_per_ruble=2)
        db.session.add_all([code, rate])
        db.session.flush()
        db.session.add_all([SellerLoyalty(seller_id=self.seller.id, rate_id=rate.id, payback_percent=50),
                           BuyerBonus(buyer_id=self.buyer.id, seller_id=self.seller.id, balance=100)])
        Settings.set('loyalty_enabled', True, 'bool')
        Settings.set('promo_enabled', True, 'bool')
        db.session.commit()
        page = self.client.get(f'/digital/books/{self.book.id}').get_data(as_text=True)
        self.assertIn('96.00', page)
        self.assertIn('Новинка', page)
        self.assertIn('Book sale', page)
        invoice = self.commerce_checkout({'promo_per_seller': f'{self.seller.id}:{code.id}',
                                          'bonus_per_seller': f'{self.seller.id}:20'})
        self.assertEqual(float(invoice.amount), 66)
        self.assertEqual(invoice.order.total_price, 96)
        self.assertEqual(len(invoice.order.digital_purchases), 1)
        self.assertNotIn(self.book.title, self.client.get('/digital/library').get_data(as_text=True))
        self.login(None)
        self.confirm_invoice(invoice)
        self.assertEqual(invoice.order.status, 'received')
        self.login(self.buyer)
        self.assertIn(self.book.title, self.client.get('/digital/library').get_data(as_text=True))
        self.assertEqual(Bonus.query.filter_by(order_id=invoice.order_id, type='accrued').count(), 1)
        balance = BuyerBonus.query.one().balance
        self.confirm_invoice(invoice)
        self.assertEqual(BuyerBonus.query.one().balance, balance)
        self.assertEqual(code.used_count, 1)

    def test_shared_n_plus_one_and_mixed_order_preserve_physical_stock(self):
        from app.models.orders import Promotion, PromotionProduct, SellerPromotion, CartItem
        from app.models.users import DeliveryService, BuyerDelivery, SellerDelivery
        digital = self.commerce_book()
        physical_category = Category(name='Physical', slug='physical')
        physical = Product(seller=self.seller, category=physical_category, name='Physical item',
            slug='physical-item', article='PHYS', price=200, stock_quantity=4, status='approved')
        promo = Promotion(name='Two for one', scheme='one_plus_one', discount_percent=99,
                          status='active', is_template=True)
        db.session.add_all([physical, promo])
        db.session.flush()
        db.session.add_all([SellerPromotion(seller_id=self.seller.id, promotion_id=promo.id),
            PromotionProduct(promotion_id=promo.id, product_id=digital.id),
            PromotionProduct(promotion_id=promo.id, product_id=physical.id),
            CartItem(buyer_id=self.buyer.id, product_id=physical.id, quantity=1)])
        service = DeliveryService(name='Самовывоз', code='pickup')
        db.session.add(service)
        db.session.flush()
        address = BuyerDelivery(buyer_id=self.buyer.id, delivery_service_id=service.id, address='Пункт самовывоза')
        db.session.add_all([address, SellerDelivery(seller_id=self.seller.id, delivery_service_id=service.id, is_active=True)])
        db.session.commit()
        invoice = self.commerce_checkout({f'delivery[{self.seller.id}]': str(address.id)})
        self.assertEqual(float(invoice.amount), 201.2)
        self.assertEqual(physical.stock_quantity, 3)
        self.assertEqual(digital.stock_quantity, 1)
        self.login(None)
        self.confirm_invoice(invoice)
        self.assertEqual(invoice.order.status, 'processing')
        self.login(self.buyer)
        self.assertIn(self.book.title, self.client.get('/digital/library').get_data(as_text=True))

    def test_shared_cancelled_payment_restores_bonus_and_promo(self):
        from app.models.orders import Bonus
        from app.models.promo import PromoCode
        from app.models.loyalty import LoyaltyRate, SellerLoyalty, BuyerBonus
        from app.models.communications import Settings
        from app.blueprints.payments import release_reservation
        self.commerce_book()
        code = PromoCode(seller_id=self.seller.id, code='CANCEL', discount_type='rub',
            discount_value=10, recipient_type='public', usage_type='single', max_uses=1, validity_type='forever')
        rate = LoyaltyRate(title='Points', points_per_ruble=1)
        db.session.add_all([code, rate])
        db.session.flush()
        db.session.add_all([SellerLoyalty(seller_id=self.seller.id, rate_id=rate.id, payback_percent=50),
            BuyerBonus(buyer_id=self.buyer.id, seller_id=self.seller.id, balance=50)])
        Settings.set('loyalty_enabled', True, 'bool')
        Settings.set('promo_enabled', True, 'bool')
        db.session.commit()
        invoice = self.commerce_checkout({'promo_per_seller': f'{self.seller.id}:{code.id}',
            'bonus_per_seller': f'{self.seller.id}:20'})
        release_reservation(invoice.order)
        db.session.commit()
        self.assertEqual(BuyerBonus.query.one().balance, 50)
        self.assertEqual(code.used_count, 0)
        self.assertEqual(Bonus.query.filter_by(type='accrued').count(), 0)
        self.assertNotIn(self.book.title, self.client.get('/digital/library').get_data(as_text=True))

    def test_two_books_use_shared_floor_promotion_and_one_invoice(self):
        from app.models.orders import Promotion, SellerPromotion, CartItem
        product = self.commerce_book()
        second = DigitalBook(seller=self.seller, category=self.category, title='Second book', price=80,
            file_name='test.epub', format='epub', published=True, moderation_status='approved')
        promo = Promotion(name='Every second', scheme='second_with_discount', discount_percent=50,
                          status='active', is_template=True)
        db.session.add_all([second, promo])
        db.session.flush()
        db.session.add_all([SellerPromotion(seller_id=self.seller.id, promotion_id=promo.id),
            CartItem(buyer_id=self.buyer.id, product_id=second.commerce_product.id, quantity=1)])
        db.session.commit()
        invoice = self.commerce_checkout()
        self.assertEqual(float(invoice.amount), 160)
        self.assertEqual(len(invoice.order.digital_purchases), 2)
        self.assertIsNone(invoice.order.delivery_service_id)
        self.confirm_invoice(invoice)
        self.assertEqual(len(DigitalPurchase.query.all()), 2)
        self.assertEqual(invoice.order.status, 'received')

    @patch('app.blueprints.main._visible_seller_ids_with_limit', return_value=[1])
    def test_shared_product_is_public_but_guest_cannot_buy(self, visible):
        product = self.commerce_book()
        for user in (self.other, None):
            self.login(user)
            self.assertIn(self.book.title, self.client.get('/catalogue/').get_data(as_text=True))
            if user is None:
                self.assertEqual(self.client.post(f'/digital/books/{self.book.id}/cart').status_code, 404)
            response = self.client.get(f'/product/{product.id}', follow_redirects=True)
            self.assertEqual(response.status_code, 200)
        self.login(self.buyer)
        self.app.config['DIGITAL_BOOKS_PILOT_ENABLED'] = False
        self.assertNotIn(self.book.title, self.client.get('/catalogue/').get_data(as_text=True))

    def test_buyer_seller_and_admin_capabilities_are_separate(self):
        self.login(self.buyer)
        self.assertEqual(self.client.get('/digital/books').status_code, 200)
        self.assertEqual(self.client.get('/digital/library').status_code, 200)
        self.assertEqual(self.client.get('/digital/manage').status_code, 404)
        self.login(self.seller)
        self.assertEqual(self.client.get('/digital/manage').status_code, 200)
        self.assertEqual(self.client.get('/digital/preview/1').status_code, 200)
        self.assertEqual(self.client.post('/digital/books/1/buy').status_code, 404)
        self.login(admin=True)
        self.assertEqual(self.client.get('/digital/manage').status_code, 200)

    def test_library_uses_profile_cards_and_filters_reading_status(self):
        purchase = self.pay()
        with patch('app.blueprints.digital.page_count', return_value=10):
            page = self.client.get('/digital/library')
            self.assertEqual(page.status_code, 200)
            self.assertIn('profile-sidebar', page.text)
            self.assertIn('product-card-inner', page.text)
            self.assertIn('Начать читать', page.text)
            self.assertIn(self.book.title, self.client.get('/digital/library?status=unread').text)
            self.assertNotIn(self.book.title, self.client.get('/digital/library?status=reading').text)
            self.client.post(f'/digital/progress/{purchase.id}', json={'chapter': 0, 'position': 1})
            page = self.client.get('/digital/library?status=reading')
            self.assertIn('10%', page.text)
            self.assertIn('1 страниц из 10', page.text)
            self.assertNotIn(self.book.title, self.client.get('/digital/library?status=unread').text)
            self.client.post(f'/digital/progress/{purchase.id}', json={'chapter': 9, 'position': 1})
            page = self.client.get('/digital/library?status=finished')
            self.assertIn('100%', page.text)
            self.assertIn('10 страниц из 10', page.text)
            self.assertNotIn(self.book.title, self.client.get('/digital/library?status=reading').text)

    def test_payment_unlocks_only_owner_and_saves_progress(self):
        purchase = self.pay()
        page = self.client.get(f'/digital/read/{purchase.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Текст книги', page.text)
        self.assertIn('id="reading-field"', page.text)
        self.assertNotIn('reader-page-number', page.text)
        self.assertNotIn('Следующая страница', page.text)
        self.assertNotIn('<img onerror', page.text)
        image = self.client.get(f'/digital/page/{purchase.id}/0')
        self.assertEqual(image.mimetype, 'image/jpeg')
        self.assertNotIn('alert(2)', page.text)
        self.assertIn('&lt;img', page.text)
        self.assertNotIn('<img onerror', page.text)
        response = self.client.post(f'/digital/progress/{purchase.id}', json={'chapter': 0, 'position': .42})
        self.assertEqual(response.status_code, 200)
        db.session.refresh(purchase)
        self.assertAlmostEqual(purchase.position, .42)
        self.book.published = False
        db.session.commit()
        self.assertEqual(self.client.get(f'/digital/read/{purchase.id}').status_code, 200)
        self.login(self.other)
        self.assertEqual(self.client.get(f'/digital/read/{purchase.id}').status_code, 404)

    def test_continuous_reader_inlines_epub_footnote_and_keeps_following_text(self):
        data = io.BytesIO()
        with ZipFile(data, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml', '<container><rootfile full-path="book.opf"/></container>')
            archive.writestr('book.opf', '<package><manifest><item id="body" href="body.xhtml"/><item id="notes" href="notes.xhtml"/></manifest><spine><itemref idref="body"/><itemref idref="notes"/></spine></package>')
            archive.writestr('body.xhtml', '<html><body><p>До сноски<a href="notes.xhtml#n1">1</a>После сноски.</p></body></html>')
            archive.writestr('notes.xhtml', '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><aside id="n1" epub:type="footnote"><p>Объяснение термина.</p></aside></body></html>')
        (storage() / 'test.epub').write_bytes(data.getvalue())
        purchase = self.pay()
        response = self.client.get(f'/digital/read/{purchase.id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('До сноски (Сноска: Объяснение термина.) После сноски.', response.text)
        self.assertEqual(response.text.count('Объяснение термина.'), 1)
        response = self.client.post(f'/digital/progress/{purchase.id}', json={'chapter': 0, 'position': .35817})
        self.assertEqual(response.status_code, 200)
        page = self.client.get(f'/digital/read/{purchase.id}')
        self.assertIn('0.35817', page.text)
        self.login(self.other)
        self.assertEqual(self.client.get(f'/digital/read/{purchase.id}').status_code, 404)

    def test_pdf_footer_notes_are_inlined(self):
        from app.utils.book_text import inline_pdf_notes
        text = inline_pdf_notes([('Предложение[1] продолжается.', 500, 800),
                                 ('1 Текст пояснения.', 90, 800)])
        self.assertIn('Предложение (Сноска 1: Текст пояснения.)  продолжается.', text)
        self.assertEqual(text.count('Текст пояснения.'), 1)

    def test_reader_author_comes_from_catalog_not_purchase(self):
        purchase = self.pay()
        self.book.author = 'Имя автора из товара'
        db.session.commit()
        page = self.client.get(f'/digital/read/{purchase.id}')
        self.assertIn('<p class="reader-author">Имя автора из товара</p>', page.text)
        self.assertLess(page.text.index('Имя автора из товара'), page.text.index('<h1>Тестовая книга'))

    def test_updated_book_is_delivered_and_survives_catalog_deletion(self):
        self.book.category = self.category
        db.session.commit()
        purchase = self.pay()
        old_name = purchase.file_name
        with ZipFile(io.BytesIO(epub_file())) as source, ZipFile(storage() / 'updated.epub', 'w') as target:
            for name in source.namelist():
                target.writestr(name, source.read(name).replace('Текст книги'.encode(), 'Новая редакция'.encode()))
        self.book.file_name = 'updated.epub'
        self.book.author = 'Автор последней редакции'
        db.session.commit()
        self.assertEqual(purchase.file_name, 'updated.epub')
        self.assertTrue((storage() / old_name).exists())
        self.assertIn('Новая редакция', self.client.get(f'/digital/read/{purchase.id}').text)
        self.login(self.seller)
        self.assertEqual(self.client.post(f'/digital/books/{self.book.id}/publish').status_code, 302)
        self.login(self.buyer)
        self.assertEqual(self.client.get(f'/digital/read/{purchase.id}').status_code, 200)
        self.login(self.seller)
        self.assertEqual(self.client.post(f'/digital/books/{self.book.id}/delete').status_code, 302)
        self.assertEqual(self.book.moderation_status, 'deleted')
        self.assertEqual(self.book.commerce_product.status, 'deleted')
        self.assertNotIn('Тестовая книга', self.client.get('/digital/manage').text)
        self.assertEqual(self.client.post(f'/digital/books/{self.book.id}/publish').status_code, 404)
        self.login(self.buyer)
        page = self.client.get(f'/digital/read/{purchase.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Новая редакция', page.text)
        library = self.client.get('/digital/library').text
        self.assertIn('Автор последней редакции', library)
        self.assertNotIn('product-system-sku', library)
        self.assertTrue((storage() / 'updated.epub').exists())

    def test_orders_show_cover_even_after_book_removed(self):
        from PIL import Image
        Image.new('RGB', (50, 80), 'blue').save(storage() / 'cover_test.jpg')
        self.book.cover_name = 'cover_test.jpg'
        db.session.commit()
        purchase = self.pay()
        self.book.moderation_status, self.book.published = 'deleted', False
        db.session.commit()
        page = self.client.get('/profile?section=orders')
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'/digital/books/{self.book.id}/cover', page.text)
        self.assertEqual(self.client.get(f'/digital/books/{self.book.id}/cover').status_code, 200)
        page = self.client.get('/digital/library')
        self.assertIn('class="library-author">Автор</div>', page.text)
        self.assertNotIn('class="product-system-sku"', page.text)

    def test_autoscroll_controls_are_initially_stopped(self):
        purchase = self.pay()
        page = self.client.get(f'/digital/read/{purchase.id}')
        self.assertIn('data-reading-auto="-1" aria-label="Уменьшить скорость автопрокрутки" disabled', page.text)
        self.assertIn('data-reading-auto="1"', page.text)
        self.assertIn('data-reading-auto-value aria-live="polite">0%', page.text)

    def test_epub_body_keeps_headings_and_paragraphs_separate(self):
        chapters = epub_chapters(storage() / 'test.epub')
        self.assertEqual(chapters[0]['blocks'][0], ('h1', 'Глава первая'))
        self.assertEqual(chapters[0]['blocks'][1][0], 'p')

    def test_pdf_unreferenced_footer_is_not_body_text(self):
        from app.utils.book_text import inline_pdf_notes
        text = inline_pdf_notes([('Основной текст без сносок.', 500, 800, 12),
                                 ('1 Несвязанное примечание.', 90, 800, 9),
                                 ('Нижний колонтитул', 25, 800, 9)], 12)
        self.assertEqual(text, 'Основной текст без сносок.')

    def test_epub_illustration_stays_between_text_and_is_private(self):
        from PIL import Image
        from app.utils.book_text import reading_sections
        picture = io.BytesIO()
        Image.new('RGB', (120, 80), 'red').save(picture, 'PNG')
        data = io.BytesIO()
        with ZipFile(data, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml', '<container><rootfile full-path="book.opf"/></container>')
            archive.writestr('book.opf', '<package><manifest><item id="a" href="chapter.xhtml"/></manifest><spine><itemref idref="a"/></spine></package>')
            archive.writestr('chapter.xhtml', '<html><head><title>Hidden title</title></head><body><p>До рисунка<img src="pictures/a.png"/>После рисунка</p></body></html>')
            archive.writestr('pictures/a.png', picture.getvalue())
        (storage() / 'test.epub').write_bytes(data.getvalue())
        purchase = self.pay()
        section = reading_sections(purchase)[0]
        self.assertEqual(section['blocks'], ['До рисунка', '', 'После рисунка'])
        self.assertEqual(section['styles'][1]['tag'], 'img')
        page = self.client.get(f'/digital/read/{purchase.id}')
        url = f'/digital/illustration/{purchase.id}/0/1'
        self.assertIn(url, page.text)
        self.assertLess(page.text.index('До рисунка'), page.text.index(url))
        self.assertLess(page.text.index(url), page.text.index('После рисунка'))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(response.data)).size, (120, 80))
        self.assertEqual(self.client.get(f'/digital/illustration/{purchase.id}/0/0').status_code, 404)
        self.login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_pdf_illustration_is_inserted_by_page_coordinates(self):
        from app.utils.book_text import pdf_blocks
        lines = [('Before image.', 600, 800, 12, 40), ('After image.', 200, 800, 12, 40)]
        blocks, styles = pdf_blocks(lines, 12, [dict(tag='img', top=500, bottom=300, left=40, right=240, width=200, height=200)])
        self.assertEqual(blocks, ['Before image.', '', 'After image.'])
        self.assertEqual([style['tag'] for style in styles], ['p', 'img', 'p'])

    def test_pdf_embedded_image_is_extracted_and_served(self):
        import pypdfium2 as pdfium
        from contextlib import closing
        from PIL import Image
        from app.utils.book_text import reading_sections
        with closing(pdfium.PdfDocument.new()) as document:
            page = document.new_page(600, 800)
            picture = pdfium.PdfImage.new(document)
            with closing(pdfium.PdfBitmap.from_pil(Image.new('RGB', (120, 80), 'red'))) as bitmap:
                picture.set_bitmap(bitmap)
                picture.set_matrix(pdfium.PdfMatrix(200, 0, 0, 100, 40, 300))
                page.insert_obj(picture)
                page.gen_content()
                document.save(str(storage() / 'picture.pdf'))
        self.book.file_name, self.book.format = 'picture.pdf', 'pdf'
        db.session.commit()
        purchase = self.pay()
        styles = reading_sections(purchase)[0]['styles']
        self.assertEqual(styles[0]['tag'], 'img')
        response = self.client.get(f'/digital/illustration/{purchase.id}/0/0')
        self.assertEqual(response.status_code, 200)
        image = Image.open(io.BytesIO(response.data))
        self.assertEqual(image.size, (2400, 1200))
        self.assertEqual(image.getpixel((1200, 600)), (255, 0, 0))

    def test_epub_svg_is_bounded_and_drops_active_content(self):
        from app.utils.book_text import epub_picture
        data, width, height, mimetype = epub_picture(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200" onload="alert(1)"><script>alert(2)</script><use href="https://example.test/a.svg#x"/><rect width="400" height="200" fill="red"/></svg>', 'picture.svg')
        self.assertEqual((width, height, mimetype), (400, 200, 'image/svg+xml'))
        self.assertNotIn(b'alert', data.getvalue())
        self.assertNotIn(b'https:', data.getvalue())
        self.assertIn(b'rect', data.getvalue())

    def test_publisher_pdf_footnotes_and_page_continuations(self):
        from app.utils.book_text import inline_pdf_notes, flowing_text, join_source_continuations
        text = inline_pdf_notes([('Рассказ (1) продолжается.', 500, 800),
                                 ('(1) Объяснение термина.', 90, 800)])
        self.assertIn('Рассказ  (Сноска 1: Объяснение термина.)  продолжается.', text)
        self.assertEqual(flowing_text('архи\x02\nвы и священник\x02философ'), 'архивы и священник-философ')
        sections = join_source_continuations([{'index': 0, 'blocks': ['Они остались укоренившими-']},
                                              {'index': 1, 'blocks': ['ся в сознании.', 'Следующий абзац.']}])
        self.assertEqual(sections[0]['blocks'], ['Они остались укоренившимися в сознании.'])
        self.assertEqual(sections[1]['blocks'], ['Следующий абзац.'])

    def test_reader_preserves_pdf_headings_toc_and_paragraphs(self):
        from app.utils.book_text import pdf_blocks, join_source_continuations
        lines = [('СОДЕРЖАНИЕ', 700, 800, 18, 140),
                 ('Введение ....................................1', 660, 800, 10, 40),
                 ('ГЛАВА ПЕРВАЯ Учреждение должностей… 13', 640, 800, 10, 40),
                 ('ГЛАВА II Обзор устройства ................19', 620, 800, 10, 40),
                 ('ВВЕДЕНИЕ', 580, 800, 18, 150),
                 ('Первый абзац начинается', 540, 800, 12, 55),
                 ('и продолжается на следующей строке.', 520, 800, 12, 40),
                 ('Второй абзац.', 500, 800, 12, 55)]
        blocks, styles = pdf_blocks(lines, 12)
        self.assertEqual([s['tag'] for s in styles], ['h2', 'toc', 'toc', 'toc', 'h2', 'p', 'p'])
        self.assertEqual([s['page'] for s in styles if s['tag'] == 'toc'], ['1', '13', '19'])
        self.assertEqual(blocks[-2], 'Первый абзац начинается и продолжается на следующей строке.')
        sections = join_source_continuations([
            {'index': 0, 'blocks': ['Предыдущий текст'], 'styles': [{'tag': 'p'}]},
            {'index': 1, 'blocks': ['заголовок'], 'styles': [{'tag': 'h2'}]}])
        self.assertEqual(sections[1]['blocks'], ['заголовок'])
        blocks, _ = pdf_blocks([('9° Первое издание.', 500, 800, 12, 40),
                                ('10° Второе издание.', 480, 800, 12, 40)], 12)
        self.assertEqual(blocks, ['9° Первое издание.', '10° Второе издание.'])

    def test_reader_recognizes_pdf_subtitles_without_promoting_body(self):
        from app.utils.book_text import pdf_blocks, join_source_continuations
        lines = [('Обычный текст.', 700, 800, 12, 40, 0),
                 ('История города', 660, 800, 12, 40, 1),
                 ('Следующий абзац.', 630, 800, 12, 40, 0),
                 ('Устройство общества', 590, 800, 13.5, 40, 0),
                 ('Предложение целиком выделено жирным.', 560, 800, 12, 40, 1),
                 ('Только первое слово выделено', 540, 800, 12, 40, .2),
                 ('и текст продолжается.', 520, 800, 12, 40, 0)]
        blocks, styles = pdf_blocks(lines, 12)
        self.assertEqual([s['tag'] for s in styles], ['p', 'h3', 'p', 'h3', 'p', 'p'])
        self.assertEqual(blocks[-1], 'Только первое слово выделено и текст продолжается.')
        sections = join_source_continuations([
            {'blocks': ['Незаконченный текст'], 'styles': [{'tag': 'p'}]},
            {'blocks': ['подзаголовок'], 'styles': [{'tag': 'h3'}]}])
        self.assertEqual(sections[1]['blocks'], ['подзаголовок'])

    def test_pdf_text_extraction_flows_footer_note_into_body(self):
        from app.utils.book_text import reading_sections
        stream = b'BT /F1 16 Tf 50 700 Td (Story[1] continues.) Tj ET\nBT /F1 10 Tf 50 70 Td (1 Explanation in footer.) Tj ET'
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
                   b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
        pdf = b'%PDF-1.4\n'
        offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
        xref = len(pdf)
        pdf += b'xref\n0 6\n0000000000 65535 f \n' + b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
        pdf += b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF'
        (storage() / 'text.pdf').write_bytes(pdf)
        self.book.file_name, self.book.format = 'text.pdf', 'pdf'
        sections = reading_sections(self.book)
        self.assertEqual(len(sections), 1)
        self.assertIn('Explanation in footer.', ' '.join(sections[0]['blocks']))
        self.assertIn('Сноска 1:', ' '.join(sections[0]['blocks']))

    def test_unpaid_and_sandbox_invoices_do_not_unlock_library(self):
        self.login(self.buyer)
        first = self.client.post('/digital/books/1/buy')
        second = self.client.post('/digital/books/1/buy')
        self.assertEqual(first.location, second.location)
        self.assertEqual(RobokassaPayment.query.count(), 1)
        self.assertEqual(self.client.get('/digital/read/1').status_code, 404)
        self.assertNotIn('Тестовая книга', self.client.get('/digital/library').text)

    def test_sandbox_success_remains_locked(self):
        purchase = self.pay(test=True)
        self.assertEqual(purchase.order.status, 'payment_demo')
        self.assertEqual(self.client.get('/digital/read/1').status_code, 404)

    def test_private_uploads_cannot_be_downloaded_through_static(self):
        self.login(self.buyer)
        self.assertEqual(self.client.get('/static/uploads/.private/books/test.epub').status_code, 404)

    def test_seller_upload_validates_and_creates_draft(self):
        self.login(self.seller)
        response = self.client.post('/digital/manage', data={'title': 'Новая', 'price': '50.00', 'category_id': self.category.id,
            'book': (io.BytesIO(epub_file()), 'book.epub')}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DigitalBook.query.order_by(DigitalBook.id.desc()).first().published)
        before = DigitalBook.query.count()
        self.client.post('/digital/manage', data={'title': 'Bad', 'price': '50', 'category_id': self.category.id,
            'book': (io.BytesIO(b'<script>attack</script>'), 'bad.pdf')}, content_type='multipart/form-data')
        self.assertEqual(DigitalBook.query.count(), before)

    def test_category_inheritance_parameters_and_generated_article(self):
        child = Category(name='Романы', slug='novels', parent=self.category)
        parent_param = Parameter(name='Язык', code='language', type='string', is_required=True)
        child_param = Parameter(name='Страниц', code='pages', type='numeric')
        db.session.add_all([child, parent_param, child_param])
        db.session.flush()
        db.session.add_all([CategoryParameter(category=self.category, parameter=parent_param),
                            CategoryParameter(category=child, parameter=child_param, is_required=True)])
        db.session.commit()
        self.login(self.seller)
        page = self.client.get(f'/digital/new?category_id={child.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Язык', page.text)
        self.assertIn('Страниц', page.text)
        response = self.client.post('/digital/new', data={'category_id': child.id, 'title': 'Роман',
            'price': '60', f'param_{parent_param.id}': 'Русский', f'param_{child_param.id}': '123',
            'book': (io.BytesIO(epub_file()), 'novel.epub')})
        self.assertEqual(response.status_code, 302)
        book = DigitalBook.query.order_by(DigitalBook.id.desc()).first()
        self.assertEqual(book.category_id, child.id)
        self.assertEqual(book.article, book.system_sku)
        self.assertTrue(book.article.startswith(f'WML-{self.seller.id}-'))
        self.assertEqual(book.parameter_values, {str(parent_param.id): 'Русский', str(child_param.id): 123.0})
        self.assertFalse(book.published)
        book.published = True
        book.moderation_status = 'approved'
        db.session.commit()
        self.login(self.buyer)
        page = self.client.get(f'/digital/books?category_id={self.category.id}')
        self.assertIn('Роман', page.text)
        self.assertIn('Русский', self.client.get(f'/digital/books/{book.id}').text)
        self.assertNotIn('Тестовая книга', page.text)

    def test_digital_form_rejects_missing_parameters_physical_or_inactive_category(self):
        parameter = Parameter(name='ISBN', code='isbn', type='string', is_required=True)
        physical = Category(name='Одежда', slug='clothes')
        db.session.add_all([parameter, physical])
        db.session.flush()
        db.session.add(CategoryParameter(category=self.category, parameter=parameter))
        db.session.commit()
        self.login(self.seller)
        before = DigitalBook.query.count()
        for category_id in (self.category.id, physical.id, 99999):
            response = self.client.post('/digital/new', data={'category_id': category_id,
                'title': 'Bad', 'price': '50', 'book': (io.BytesIO(epub_file()), 'bad.epub')})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(DigitalBook.query.count(), before)
        self.assertEqual(self.client.get(f'/digital/new?category_id={physical.id}').status_code, 404)
        self.category.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get(f'/digital/new?category_id={self.category.id}').status_code, 404)

    def test_category_chooser_routes_physical_and_digital_and_unifies_tabs(self):
        physical = Category(name='Одежда', slug='clothes')
        db.session.add(physical)
        db.session.commit()
        self.login(self.seller)
        with patch('app.blueprints.seller._resolve_tariff_state', return_value={'state': 'paid'}):
            self.assertEqual(self.client.get('/seller/products/new').status_code, 200)
            response = self.client.get(f'/seller/products/new?category_id={self.category.id}')
            self.assertIn('/digital/new?', response.location)
            page = self.client.get(f'/seller/products/new?category_id={physical.id}')
            self.assertEqual(page.status_code, 200)
            self.assertIn(f'let currentCategoryId = {physical.id}', page.text)
            response = self.client.post('/seller/products/new', data={'category_id': self.category.id})
            self.assertEqual(response.status_code, 400)
        page = self.client.get('/seller/products')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Тип товаров', page.text)

    def test_schema_upgrade_preserves_legacy_books_and_is_repeatable(self):
        from sqlalchemy import text, inspect
        from app.utils.digital_catalog import ensure_schema
        with db.engine.begin() as connection:
            # Recreate the previous schema with an existing book and category.
            connection.execute(text('CREATE TABLE old_books_copy AS SELECT id, seller_id, title, author, description, price, file_name, format, published, created_at FROM digital_books'))
            connection.execute(text('DROP TABLE digital_purchases'))
            connection.execute(text('DROP TABLE digital_books'))
            connection.execute(text('ALTER TABLE old_books_copy RENAME TO digital_books'))
            ensure_schema(connection)
            ensure_schema(connection)
            row = connection.execute(text('SELECT article, system_sku, file_name FROM digital_books')).one()
            self.assertEqual(row.article, row.system_sku)
            self.assertEqual(row.file_name, 'test.epub')
            self.assertIn('category_id', {c['name'] for c in inspect(connection).get_columns('digital_books')})

    def test_duplicate_article_and_foreign_attributes_are_not_accepted(self):
        self.book.article = 'EXISTING'
        db.session.commit()
        self.login(self.seller)
        response = self.client.post('/digital/new', data={'category_id': self.category.id,
            'title': 'Duplicate', 'price': '50', 'article': 'EXISTING',
            'book': (io.BytesIO(epub_file()), 'copy.epub')})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Артикул уже существует', response.text)
        self.assertEqual(DigitalBook.query.count(), 1)
        self.login(self.other)
        self.assertEqual(self.client.get(f'/digital/new?category_id={self.category.id}').status_code, 404)

    def test_category_parameter_types_and_allowed_values(self):
        from werkzeug.datastructures import MultiDict
        from app.utils.digital_catalog import read_parameters
        choices = Parameter(name='Жанр', code='genre', type='select', predefined_values=['Роман', 'Драма'])
        count = Parameter(name='Тираж', code='count', type='integer')
        multiple = Parameter(name='Темы', code='topics', type='string', is_multiple=True)
        db.session.add_all([choices, count, multiple])
        db.session.flush()
        for p in (choices, count, multiple):
            db.session.add(CategoryParameter(category=self.category, parameter=p))
        db.session.commit()
        data = MultiDict([(f'param_{choices.id}', 'Роман'), (f'param_{count.id}', '7'),
                          (f'param_{multiple.id}', 'История'), (f'param_{multiple.id}', '')])
        self.assertEqual(read_parameters(self.category, data),
                         {str(choices.id): 'Роман', str(count.id): 7, str(multiple.id): ['История']})
        data[f'param_{choices.id}'] = 'Нельзя'
        with self.assertRaises(ValueError):
            read_parameters(self.category, data)
        data[f'param_{choices.id}'] = 'Драма'
        data[f'param_{count.id}'] = '7.5'
        with self.assertRaises(ValueError):
            read_parameters(self.category, data)

    def test_admin_can_set_book_category_and_open_the_form(self):
        self.login(admin=True)
        response = self.client.post('/main_admin/categories/new', data={'name': 'PDF', 'is_digital_book': 'on'})
        self.assertEqual(response.status_code, 302)
        category = Category.query.filter_by(name='PDF').one()
        self.assertTrue(category.is_digital_book)
        self.assertEqual(self.client.get('/digital/choose').status_code, 200)
        self.assertEqual(self.client.get(f'/digital/new?category_id={category.id}').status_code, 200)
        self.client.post(f'/main_admin/categories/{category.id}/edit', data={'name': 'PDF'})
        self.assertFalse(db.session.get(Category, category.id).is_digital_book)

    @staticmethod
    def cover_image(size=(1200, 1800), color=(20, 80, 180, 128)):
        from PIL import Image
        output = io.BytesIO()
        Image.new('RGBA', size, color).save(output, 'PNG')
        return output.getvalue()

    def test_manager_list_filters_and_paginates_like_physical_products(self):
        for index in range(21):
            db.session.add(DigitalBook(seller=self.seller, title=f'Черновик {index}', price=50,
                                      format='epub', file_name='test.epub', published=False))
        db.session.commit()
        self.login(self.seller)
        page = self.client.get('/digital/manage?status=active')
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.book.title, page.text)
        self.assertNotIn('Черновик 20', page.text)
        page = self.client.get('/digital/manage?status=inactive')
        self.assertNotIn(self.book.title, page.text)
        self.assertEqual(page.text.count('class="product-row"'), 20)
        page = self.client.get('/digital/manage?status=inactive&page=2')
        self.assertEqual(page.text.count('class="product-row"'), 1)
        self.assertIn('Черновик 0', page.text)

    def test_cover_upload_normalizes_image_and_preserves_aspect_ratio(self):
        from PIL import Image
        self.login(self.seller)
        response = self.client.post('/digital/new', data={'category_id': self.category.id,
            'title': 'С обложкой', 'price': '100',
            'book': (io.BytesIO(epub_file()), 'book.epub'),
            'cover': (io.BytesIO(self.cover_image()), 'cover.png')})
        self.assertEqual(response.status_code, 302)
        book = DigitalBook.query.order_by(DigitalBook.id.desc()).first()
        self.assertTrue(book.cover_name.endswith('.jpg'))
        with Image.open(storage() / book.cover_name) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.mode, 'RGB')
            self.assertEqual(image.size, (533, 800))
        self.assertEqual(self.client.get(f'/digital/books/{book.id}/cover').status_code, 200)
        self.login(self.buyer)
        self.assertEqual(self.client.get(f'/digital/books/{book.id}/cover').status_code, 404)
        book.published = True
        book.moderation_status = 'approved'
        db.session.commit()
        response = self.client.get(f'/digital/books/{book.id}/cover')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')
        self.assertIn('no-store', response.headers['Cache-Control'])
        catalog = self.client.get('/digital/books').text
        self.assertIn('products-grid products-grid-4', catalog)
        self.assertIn('class="product-card"', catalog)
        self.assertIn(f'/digital/books/{book.id}', catalog)
        detail = self.client.get(f'/digital/books/{book.id}').text
        self.assertIn('class="product-detail"', detail)
        self.assertIn('class="main-photo"', detail)
        self.assertIn('В корзину', detail)
        response.close()
        for user in (None, self.other):
            self.login(user)
            self.assertEqual(self.client.get(f'/digital/books/{book.id}/cover').status_code, 200)
            self.assertEqual(self.client.get(f'/digital/books/{book.id}').status_code, 200)

    def test_cover_replacement_keeps_one_file_and_checks_ownership(self):
        self.login(self.seller)
        def replace(raw):
            return self.client.post(f'/digital/books/{self.book.id}/cover', data={
                'cover': (io.BytesIO(raw), 'cover.png')})
        self.assertEqual(replace(self.cover_image()).status_code, 302)
        first = self.book.cover_name
        self.assertTrue((storage() / first).exists())
        self.assertEqual(replace(self.cover_image((400, 600))).status_code, 302)
        second = self.book.cover_name
        self.assertNotEqual(first, second)
        self.assertFalse((storage() / first).exists())
        replace(b'<svg><script>bad</script></svg>')
        self.assertEqual(self.book.cover_name, second)
        self.assertTrue((storage() / second).exists())
        self.assertEqual(len(list(storage().glob('cover_*.jpg'))), 1)
        self.login(self.buyer)
        self.assertEqual(replace(self.cover_image()).status_code, 404)

    def test_invalid_or_multiple_covers_do_not_create_books_or_orphan_files(self):
        self.login(self.seller)
        before = set(storage().iterdir())
        for covers in ([ (io.BytesIO(b'not an image'), 'bad.jpg') ],
                       [(io.BytesIO(self.cover_image()), 'one.png'), (io.BytesIO(self.cover_image()), 'two.png')]):
            self.client.post('/digital/new', data={'category_id': self.category.id,
                'title': 'Bad', 'price': '100', 'book': (io.BytesIO(epub_file()), 'book.epub'), 'cover': covers})
            self.assertEqual(DigitalBook.query.count(), 1)
            self.assertEqual(set(storage().iterdir()), before)
        with patch('app.utils.book_covers.MAX_COVER', 3):
            self.client.post(f'/digital/books/{self.book.id}/cover', data={
                'cover': (io.BytesIO(self.cover_image()), 'large.png')})
        self.assertIsNone(self.book.cover_name)

    def test_cover_transaction_failure_removes_new_files(self):
        self.login(self.seller)
        before = set(storage().iterdir())
        with patch.object(db.session, 'commit', side_effect=RuntimeError('test rollback')):
            with self.assertRaises(RuntimeError):
                self.client.post('/digital/new', data={'category_id': self.category.id,
                    'title': 'Rollback', 'price': '100', 'book': (io.BytesIO(epub_file()), 'book.epub'),
                    'cover': (io.BytesIO(self.cover_image()), 'cover.png')})
        self.assertEqual(set(storage().iterdir()), before)
        self.assertEqual(DigitalBook.query.count(), 1)

    def test_pdf_is_private_and_has_a_saved_page_bookmark(self):
        self.book.format = 'pdf'
        self.book.file_name = 'test.pdf'
        from PIL import Image
        Image.new('RGB', (600, 800), 'white').save(storage() / 'test.pdf', 'PDF')
        db.session.commit()
        self.login(self.buyer)
        self.assertEqual(self.client.get('/digital/file/1').status_code, 404)
        purchase = self.pay()
        response = self.client.get(f'/digital/file/{purchase.id}')
        self.assertEqual(response.status_code, 404)
        response = self.client.get(f'/digital/page/{purchase.id}/0')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')
        self.assertIn('no-store', response.headers['Cache-Control'])
        response.close()
        self.assertEqual(self.client.post('/digital/progress/1', json={'chapter': 9, 'position': 0}).status_code, 200)
        self.assertNotIn('<iframe', self.client.get('/digital/read/1').text)
        self.assertEqual(self.client.get('/digital/page/1/9').status_code, 404)

    def test_digital_payment_preserves_physical_cart_and_stock(self):
        from app.models.orders import CartItem
        from app.models.products import Category
        category = Category(name='Physical', slug='physical')
        product = Product(name='Physical', slug='physical', article='physical', category=category,
                          seller=self.seller, price=100, stock_quantity=3, status='approved')
        db.session.add(CartItem(buyer=self.buyer, product=product, quantity=2))
        db.session.commit()
        self.pay()
        self.assertEqual(CartItem.query.one().quantity, 2)
        self.assertEqual(product.stock_quantity, 3)

    def test_edit_and_price_require_owner_and_resubmit_for_moderation(self):
        self.book.category = self.category
        self.book.article = self.book.system_sku = 'WML-EDIT'
        db.session.commit()
        self.login(self.buyer)
        self.assertEqual(self.client.post('/digital/books/1/price', data={'price':'99'}).status_code, 404)
        self.login(self.seller)
        page = self.client.get('/digital/books/1/edit')
        self.assertEqual(page.status_code, 200)
        self.assertIn('WML-EDIT', page.text)
        self.assertEqual(self.client.post('/digital/books/1/price', data={'price':'NaN'}).status_code, 400)
        self.assertEqual(self.client.post('/digital/books/1/price', data={'price':'99.50'}).status_code, 200)
        self.assertFalse(self.book.published)
        self.assertEqual(self.book.moderation_status, 'pending')
        self.assertEqual(self.client.post('/digital/books/1/publish').status_code, 302)
        self.assertFalse(self.book.published)
        response = self.client.post('/digital/books/1/edit', data={'category_id': self.category.id,
            'title':'Изменённая книга','author':'Автор','description':'Описание','price':'110','article':'WML-EDIT'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.book.title, 'Изменённая книга')
        self.assertEqual(self.book.file_name, 'test.epub')

    def test_only_admin_can_approve_and_modified_cover_reenters_moderation(self):
        self.book.moderation_status = 'pending'
        self.book.published = False
        db.session.commit()
        self.login(self.seller)
        self.assertEqual(self.client.post('/digital/moderation/1',data={'action':'approve'}).status_code,404)
        self.login(admin=True)
        self.assertIn('Тестовая книга', self.client.get('/main_admin/products/moderation').text)
        self.assertEqual(self.client.post('/digital/moderation/1',data={'action':'approve'}).status_code,302)
        self.assertTrue(self.book.published)
        self.assertEqual(self.book.moderation_status,'approved')
        self.assertEqual(self.client.get('/digital/preview/1').status_code,200)
        self.assertEqual(self.client.get('/digital/page/1/0?preview=1').mimetype,'image/jpeg')
        self.client.post('/digital/moderation/1', data={'action':'reject','comment':'Проверьте права'})
        self.assertFalse(self.book.published)
        self.assertEqual(self.book.moderation_comment,'Проверьте права')

    def test_page_images_do_not_expose_original_or_cross_account_access(self):
        self.login(self.buyer)
        self.assertEqual(self.client.get('/digital/page/1/0').status_code,404)
        purchase = self.pay()
        response = self.client.get(f'/digital/page/{purchase.id}/0')
        self.assertEqual(response.mimetype,'image/jpeg')
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertEqual(self.client.get(f'/digital/file/{purchase.id}').status_code,404)
        self.assertEqual(self.client.get('/digital/page/1/0?preview=1').status_code,404)
        self.login(self.other)
        self.assertEqual(self.client.get(f'/digital/page/{purchase.id}/0').status_code,404)

    def test_feature_switch_hides_everything(self):
        self.login(admin=True)
        self.app.config['DIGITAL_BOOKS_PILOT_ENABLED'] = False
        self.assertEqual(self.client.get('/digital/manage').status_code, 404)
