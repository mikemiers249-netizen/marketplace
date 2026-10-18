import hashlib
import io
import logging
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
from app.models.products import Product
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
        db.session.flush()
        storage().mkdir(parents=True)
        (storage() / 'test.epub').write_bytes(epub_file())
        self.book = DigitalBook(seller=self.seller, title='Тестовая книга', author='Автор',
                                price=120, file_name='test.epub', format='epub', published=True)
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

    def test_guest_and_other_accounts_cannot_discover_or_use_pilot(self):
        for user in (None, self.other):
            self.login(user)
            for path in ('/digital/books', '/digital/library', '/digital/manage',
                         '/digital/preview/1', '/digital/read/1', '/digital/file/1'):
                self.assertEqual(self.client.get(path).status_code, 404, path)
            self.assertEqual(self.client.post('/digital/books/1/buy').status_code, 404)
        self.assertEqual(Product.query.count(), 0)

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

    def test_payment_unlocks_only_owner_and_saves_progress(self):
        purchase = self.pay()
        page = self.client.get(f'/digital/read/{purchase.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Текст книги', page.text)
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
        response = self.client.post('/digital/manage', data={'title': 'Новая', 'price': '50.00',
            'book': (io.BytesIO(epub_file()), 'book.epub')}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DigitalBook.query.order_by(DigitalBook.id.desc()).first().published)
        before = DigitalBook.query.count()
        self.client.post('/digital/manage', data={'title': 'Bad', 'price': '50',
            'book': (io.BytesIO(b'<script>attack</script>'), 'bad.pdf')}, content_type='multipart/form-data')
        self.assertEqual(DigitalBook.query.count(), before)

    def test_pdf_is_private_and_has_a_saved_page_bookmark(self):
        self.book.format = 'pdf'
        self.book.file_name = 'test.pdf'
        (storage() / 'test.pdf').write_bytes(b'%PDF-1.4\n%%EOF')
        db.session.commit()
        self.login(self.buyer)
        self.assertEqual(self.client.get('/digital/file/1').status_code, 404)
        purchase = self.pay()
        response = self.client.get(f'/digital/file/{purchase.id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/pdf')
        self.assertIn('no-store', response.headers['Cache-Control'])
        response.close()
        self.assertEqual(self.client.post('/digital/progress/1', json={'chapter': 9, 'position': 0}).status_code, 200)
        self.assertIn('#page=10', self.client.get('/digital/read/1').text)

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

    def test_feature_switch_hides_everything(self):
        self.login(admin=True)
        self.app.config['DIGITAL_BOOKS_PILOT_ENABLED'] = False
        self.assertEqual(self.client.get('/digital/manage').status_code, 404)
