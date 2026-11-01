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
from app.models.products import Product, Category, Parameter, CategoryParameter
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
        self.assertNotIn('Текст книги', page.text)
        self.assertIn('/digital/page/', page.text)
        image = self.client.get(f'/digital/page/{purchase.id}/0')
        self.assertEqual(image.mimetype, 'image/jpeg')
        self.assertNotIn('alert(2)', page.text)
        self.assertNotIn('&lt;img', page.text)
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
        self.assertIn('Купить и читать', detail)
        for user in (None, self.other):
            self.login(user)
            self.assertEqual(self.client.get(f'/digital/books/{book.id}/cover').status_code, 404)
            self.assertEqual(self.client.get(f'/digital/books/{book.id}').status_code, 404)

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
