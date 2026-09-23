import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import create_app, db
from config import TestingConfig
from app.models.users import Buyer, Seller
from app.models.orders import Order, OrderItem
from app.models.products import Category, Product
from app.models.communications import TariffBlock, TariffRow
from app.models.tariffs import SellerTariffSubscription, TariffTransaction
from app.utils.tariff_billing import sales_turnover, renewal_quote


class TariffBillingTests(unittest.TestCase):
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
        self.start = datetime(2026, 9, 1)
        self.end = datetime(2026, 10, 1)
        self.now = datetime(2026, 9, 23)
        self.buyer = Buyer(login='buyer', email='buyer@example.test')
        self.buyer.set_password('test-password')
        self.seller = Seller(login='seller', email='seller@example.test',
                             store_name='Store', store_slug='store')
        self.seller.set_password('test-password')
        block = TariffBlock(title='Tariffs', section='sellers')
        self.row = TariffRow(block=block, name='Sales 5%', kind='cards_turnover',
                             percent_rate=5, billing_period='monthly', period_days=30,
                             is_active=True, is_published=True)
        self.sub = SellerTariffSubscription(seller=self.seller, row=self.row,
                    activated_at=self.start, expires_at=self.end,
                    source='global_auto', status='active', is_paid=False)
        db.session.add_all([self.buyer, self.seller, self.sub])
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

    def order(self, number, amount, status='delivered', delivered=None, received=None,
              created=None, seller_id=None):
        order = Order(order_number=number, buyer_id=self.buyer.id,
                      seller_id=seller_id or self.seller.id, total_price=amount,
                      delivery_price=999, status=status,
                      created_at=created or self.start - timedelta(days=60),
                      delivered_at=delivered, received_at=received)
        db.session.add(order)
        db.session.flush()
        return order

    def test_first_completion_date_boundaries_and_statuses(self):
        self.order('start', 100, delivered=self.start)
        self.order('both', 200, status='received', delivered=self.start + timedelta(days=1),
                   received=self.start + timedelta(days=2))
        self.order('received-only', 300, status='received', received=self.start + timedelta(days=3))
        self.order('old-delivery', 999, status='received', delivered=self.start - timedelta(days=1),
                   received=self.start + timedelta(days=1), created=self.start)
        self.order('end', 999, delivered=self.end)
        self.order('missing-dates', 999, created=self.start)
        self.order('cancelled', 999, status='canceled', delivered=self.start)
        self.order('shipped', 999, status='shipped', delivered=self.start)
        other_seller = Seller(login='other', email='other@example.test',
                              store_name='Other', store_slug='other')
        other_seller.set_password('test-password')
        db.session.add(other_seller)
        db.session.flush()
        self.order('other-seller', 999, delivered=self.start, seller_id=other_seller.id)
        self.assertEqual(sales_turnover(self.seller.id, self.start, self.end), 600)
        self.assertEqual(self.row.compute_billed_amount(self.seller, self.start, self.end), 30)

    def test_received_before_delivered_uses_earliest_and_no_double_count(self):
        self.order('reverse', 100, status='received', received=self.start - timedelta(days=1),
                   delivered=self.start + timedelta(days=1))
        self.order('cross-period', 200, status='received', delivered=self.start + timedelta(days=1),
                   received=self.end + timedelta(days=1))
        self.assertEqual(sales_turnover(self.seller.id, self.start, self.end), 200)
        self.assertEqual(sales_turnover(self.seller.id, self.end, self.end + timedelta(days=30)), 0)

    def test_quote_uses_last_billing_and_stops_at_now(self):
        boundary = self.start + timedelta(days=10)
        self.sub.last_billed_at = boundary
        self.order('already-billed', 900, delivered=boundary - timedelta(seconds=1))
        self.order('new-period', 200, delivered=boundary)
        self.order('future', 900, delivered=self.now + timedelta(days=1))
        quote = renewal_quote(self.sub, self.now)
        self.assertEqual((quote['turnover'], quote['amount']), (200, 10))
        self.assertEqual(quote['period_start'], boundary)
        self.assertEqual(quote['period_end'], self.now)
        self.assertEqual(TariffTransaction.query.count(), 0)
        self.assertEqual(self.sub.expires_at, self.end)

    def test_expired_and_fixed_quotes(self):
        self.order('completed', 200, delivered=self.start)
        self.order('after-expiry', 900, delivered=self.end + timedelta(seconds=1))
        quote = renewal_quote(self.sub, self.end + timedelta(days=5))
        self.assertEqual(quote['amount'], 10)
        self.assertEqual(quote['period_end'], self.end)
        self.row.kind = 'cards'
        self.row.price_amount = 750
        self.assertEqual(renewal_quote(self.sub, self.now)['amount'], 750)

    def test_category_sums_only_matching_lines_once(self):
        cat = Category(name='Target', slug='target')
        other = Category(name='Other', slug='other')
        db.session.add_all([cat, other])
        db.session.flush()
        order = self.order('mixed', 1000, delivered=self.start)
        for index, category, price in [(1, cat, 100), (2, cat, 200), (3, other, 700)]:
            product = Product(name=str(index), slug=str(index), article=str(index),
                              price=price, category_id=category.id, seller_id=self.seller.id)
            db.session.add(product)
            db.session.flush()
            db.session.add(OrderItem(order_id=order.id, product_id=product.id,
                                    price_at_order=price, quantity=1))
        self.row.kind = 'category_sale'
        self.row.subject_category_id = cat.id
        db.session.flush()
        self.assertEqual(renewal_quote(self.sub, self.now)['amount'], 15)
        self.assertEqual(self.row.compute_billed_amount(self.seller, self.start, self.end), 15)

    def test_admin_page_displays_quote_without_billing(self):
        self.order('admin-preview', 200, delivered=self.start)
        db.session.commit()
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        response = self.client.get('/main_admin/tariffs')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Стоимость продления на текущий момент', html)
        self.assertIn('10.00 ₽', html)
        self.assertEqual(TariffTransaction.query.count(), 0)

    def test_extension_matches_quote_and_excludes_previously_billed_sales(self):
        self.sub.last_billed_at = self.start + timedelta(days=10)
        self.order('old', 1000, delivered=self.start)
        self.order('current', 200, delivered=self.start + timedelta(days=12))
        db.session.commit()
        with self.client.session_transaction() as session:
            session['_user_id'] = self.seller.get_id()
            session['_fresh'] = True
        with patch('app.blueprints.seller.datetime', wraps=datetime) as clock:
            clock.utcnow.return_value = self.now
            response = self.client.post(f'/seller/tariffs/subscriptions/{self.sub.id}/extend')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TariffTransaction.query.one().amount, 10)
        self.assertEqual(self.sub.last_billed_at, self.now)
        self.assertEqual(renewal_quote(self.sub, self.now + timedelta(seconds=1))['amount'], 0)

    def test_admin_status_updates_record_first_completion(self):
        order = self.order('status-change', 200, status='shipped')
        db.session.commit()
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        with patch('app.blueprints.admin.datetime', wraps=datetime) as clock:
            clock.utcnow.return_value = self.now
            self.client.post(f'/main_admin/orders/{order.id}/status', data={'status': 'delivered'})
            clock.utcnow.return_value = self.now + timedelta(days=1)
            self.client.post(f'/main_admin/orders/{order.id}/status', data={'status': 'received'})
            self.client.post(f'/main_admin/orders/{order.id}/status', data={'status': 'delivered'})
        self.assertEqual(order.delivered_at, self.now)
        self.assertEqual(order.received_at, self.now + timedelta(days=1))
        self.assertEqual(sales_turnover(self.seller.id, self.start, self.now + timedelta(seconds=1)), 200)
