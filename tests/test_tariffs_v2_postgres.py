"""Opt-in real PostgreSQL concurrency tests. Default discovery never connects.

Only scripts/test_tariffs_v2_postgres.py supplies a fresh local test identity.
The web application's SQLite-only enablement guard remains unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from werkzeug.datastructures import MultiDict

from app import create_app, db
from config import TestingConfig
from app.models.products import Category, Product
from app.models.users import Seller, Buyer, BuyerDelivery, SellerDelivery, DeliveryService
from app.models import tariffs_v2, tariffs_v2_commerce, tariffs_v2_finance, tariffs_v2_lifecycle, tariffs_v2_operations  # noqa: F401
from app.models.tariffs_v2 import TariffV2Rule, TariffV2Category, TariffV2Audit, TariffV2StorageInvoice
from app.models.tariffs_v2_commerce import TariffV2Checkout, TariffV2Reservation
from app.models.tariffs_v2_finance import TariffV2CheckoutRefund
from app.utils.tariffs_v2 import TariffError, issue_monthly_invoice, midnight_utc, moscow_date, next_month
from app.utils.tariffs_v2_commerce import CHECKOUT_FLOW, create_checkout, set_cart_quantity, set_publication, accept_checkout_result
from app.utils.tariffs_v2_finance import cancel_line, confirm_checkout_refund
from app.utils.tariffs_v2_payments import issue_storage_invoice

DSN = os.environ.get('WIMLI_TARIFF_V2_PG_DSN')


@unittest.skipUnless(DSN, 'Opt-in disposable PostgreSQL runner only.')
class PostgresConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = make_url(DSN)
        if (url.drivername != 'postgresql+psycopg2' or url.host != '127.0.0.1' or not url.port or url.query or
                not re.fullmatch(r'wimli_tariffs_v2_test_[0-9a-f]{12}', url.database or '') or url.username != url.database):
            raise RuntimeError('Refusing a non-local or non-test PostgreSQL identity.')
        cls.engine = create_engine(url, pool_size=5, max_overflow=0, connect_args={'connect_timeout': 5})
        with cls.engine.connect() as connection:
            row = connection.execute(text('SELECT current_database(), current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user')).one()
            if row[0] != url.database or row[1] != url.username or any(row[2:]):
                cls.engine.dispose()
                raise RuntimeError('Refusing privileged or unexpected PostgreSQL connection.')

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            TARIFFS_V2_ENABLED = False
            TARIFFS_V2_SANDBOX = False
            MAIL_WORKER_ENABLED = False
            MAIL_PASSWORD = None
            LOG_FILE = str(Path(self.tmp.name) / 'test.log')
            UPLOAD_FOLDER = str(Path(self.tmp.name) / 'uploads')
        self.app = create_app(Config)
        self.ctx = self.app.app_context()
        self.ctx.push()
        # Only this test harness replaces a disabled app's engine, before any
        # tables/fixtures exist. No HTTP routes or production config are enabled.
        db.engines[None].dispose()
        db.engines[None] = self.engine
        self.app.config.update(TARIFFS_V2_ENABLED=True, TARIFFS_V2_SANDBOX=True,
            TARIFFS_V2_PAYMENT_TEST_ENABLED=True, TARIFFS_V2_ROBOKASSA_TEST_LOGIN='pg-fake-shop',
            TARIFFS_V2_ROBOKASSA_TEST_PASSWORD1='fake-one', TARIFFS_V2_ROBOKASSA_TEST_PASSWORD2='fake-two',
            TARIFFS_V2_ROBOKASSA_HASH='sha256')
        db.create_all()
        self.now = datetime.now(timezone.utc)
        seller = Seller(login='seller', email='seller@pg.test', password_hash='fake', store_name='Store', store_slug='store')
        buyers = [Buyer(login=f'buyer{i}', email=f'buyer{i}@pg.test', password_hash='fake') for i in range(2)]
        category = Category(name='Category', slug='category')
        rule = TariffV2Rule(name='Three percent', kind='percent', amount=3)
        service = DeliveryService(name='CDEK', code='cdek')
        product = Product(seller=seller, category=category, name='Product', slug='product', article='one',
            price=10000, current_discount=0, stock_quantity=1, status='approved')
        db.session.add_all([seller, *buyers, category, rule, service, product])
        db.session.flush()
        db.session.add(TariffV2Category(category_id=category.id, rule_id=rule.id))
        db.session.add(SellerDelivery(seller_id=seller.id, delivery_service_id=service.id))
        addresses = [BuyerDelivery(buyer=buyer, delivery_service=service, pvz_city='Moscow',
            pvz_address='PVZ', recipient_name='Recipient', recipient_phone='+79991234567') for buyer in buyers]
        db.session.add_all(addresses)
        db.session.flush()
        set_publication(product.id, seller.id, True, self.now)
        self.seller_id, self.product_id = seller.id, product.id
        self.buyer_ids = [buyer.id for buyer in buyers]
        self.deliveries = [{seller.id: address.id} for address in addresses]
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and self.tmp.name in handler.baseFilename:
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.tmp.cleanup()

    def add(self, buyer_index=0):
        set_cart_quantity(self.buyer_ids[buyer_index], self.product_id, 1, self.now)
        db.session.commit()

    def checkout(self, buyer_index=0):
        row = create_checkout(self.buyer_ids[buyer_index], 'a' * 48, self.deliveries[buyer_index], self.now)
        db.session.commit()
        return row

    def callback(self, row):
        amount = format(row.online_amount, '.2f')
        message = f'{amount}:{row.gateway_id}:fake-two:Shp_flow={CHECKOUT_FLOW}:Shp_token={row.token}'
        return MultiDict(dict(InvId=str(row.gateway_id), OutSum=amount, Shp_flow=CHECKOUT_FLOW,
            Shp_token=row.token, SignatureValue=hashlib.sha256(message.encode()).hexdigest()))

    def race(self, functions):
        barrier = threading.Barrier(len(functions))
        def worker(function):
            with self.app.app_context():
                try:
                    barrier.wait(timeout=5)
                    row = function()
                    result = row.id if row is not None else None
                    db.session.commit()
                    return ('ok', result)
                except TariffError as error:
                    db.session.rollback()
                    return ('rejected', str(error))
                finally:
                    db.session.remove()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(worker, function) for function in functions]
            results = [future.result(timeout=20) for future in futures]
        db.session.expire_all()
        return results

    def test_only_one_buyer_can_reserve_last_item(self):
        self.add(0)
        self.add(1)
        results = self.race([lambda i=i: create_checkout(self.buyer_ids[i], 'a' * 48, self.deliveries[i], self.now) for i in range(2)])
        self.assertEqual([item[0] for item in results].count('ok'), 1, results)
        self.assertEqual(TariffV2Checkout.query.count(), 1)
        self.assertEqual(TariffV2Reservation.query.count(), 1)

    def test_same_buyer_double_submit_reuses_one_payment(self):
        self.add()
        results = self.race([lambda: create_checkout(self.buyer_ids[0], 'a' * 48, self.deliveries[0], self.now)] * 2)
        self.assertTrue(all(item[0] == 'ok' for item in results), results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(TariffV2Checkout.query.count(), 1)

    def test_duplicate_callbacks_complete_once(self):
        self.add()
        row = self.checkout()
        data = self.callback(row)
        results = self.race([lambda: accept_checkout_result(data, self.now)] * 2)
        self.assertTrue(all(item[0] == 'ok' for item in results), results)
        self.assertEqual(TariffV2Reservation.query.filter_by(status='committed').count(), 1)
        self.assertEqual(TariffV2Audit.query.filter_by(action='checkout_complete').count(), 1)

    def test_parallel_storage_invoice_creation_reuses_one_number(self):
        mapping = db.session.get(TariffV2Category, db.session.get(Product, self.product_id).category_id)
        mapping.rule.kind, mapping.rule.amount = 'fixed', Decimal('310')
        db.session.commit()
        results = self.race([lambda: issue_storage_invoice(self.product_id, self.seller_id, self.now)] * 2)
        self.assertTrue(all(item[0] == 'ok' for item in results), results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(TariffV2StorageInvoice.query.count(), 1)

    def test_duplicate_refund_confirmation_and_monthly_invoice(self):
        self.add()
        row = self.checkout()
        accept_checkout_result(self.callback(row), self.now)
        db.session.commit()
        refund = cancel_line(row.orders[0].lines[0].id, self.seller_id, 1, self.now)
        db.session.commit()
        refund_id = refund.id
        results = self.race([lambda: confirm_checkout_refund(refund_id, 'pg-result', 300, True, self.now)] * 2)
        self.assertTrue(all(item[0] == 'ok' for item in results), results)
        self.assertEqual(db.session.get(TariffV2CheckoutRefund, refund_id).line.refunded_fee, 300)
        at = midnight_utc(next_month(moscow_date(self.now)))
        results = self.race([lambda: issue_monthly_invoice(self.seller_id, at)] * 2)
        self.assertTrue(all(item[0] == 'ok' for item in results), results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(issue_monthly_invoice(self.seller_id, at).amount, 300)

    def test_parallel_checkouts_respect_daily_limit_with_stock_available(self):
        db.session.get(Product, self.product_id).stock_quantity = 2
        db.session.get(Seller, self.seller_id).daily_orders_limit = 1
        db.session.commit()
        self.add(0)
        self.add(1)
        results = self.race([lambda i=i: create_checkout(self.buyer_ids[i], 'a' * 48, self.deliveries[i], self.now) for i in range(2)])
        self.assertEqual([item[0] for item in results].count('ok'), 1, results)
        self.assertTrue(any(item[0] == 'rejected' and 'Дневной' in item[1] for item in results), results)
        self.assertEqual(TariffV2Checkout.query.count(), 1)

    def test_concurrent_partial_cancellations_keep_one_cumulative_refund_target(self):
        db.session.get(Product, self.product_id).stock_quantity = 3
        db.session.commit()
        set_cart_quantity(self.buyer_ids[0], self.product_id, 3, self.now)
        db.session.commit()
        row = self.checkout()
        accept_checkout_result(self.callback(row), self.now)
        db.session.commit()
        line_id = row.orders[0].lines[0].id
        results = self.race([lambda quantity=quantity: cancel_line(line_id, self.seller_id, quantity, self.now) for quantity in (1, 2)])
        self.assertTrue(any(item[0] == 'ok' for item in results), results)
        self.assertEqual(sum(item.amount for item in TariffV2CheckoutRefund.query.all()), 600)
        self.assertEqual(row.orders[0].cod_amount, 10000)

    def test_additive_release_schema_preserves_invoice_numbers_and_advances_sequence(self):
        from app.tariffs_v2_schema import prepare_schema
        from app.models.tariffs_v2 import TariffV2GatewayNumber
        from app.utils.tariffs_v2_payments import allocate_gateway_number
        mapping = db.session.get(TariffV2Category, db.session.get(Product, self.product_id).category_id)
        mapping.rule.kind, mapping.rule.amount = 'fixed', Decimal('310')
        db.session.commit()
        invoice = issue_storage_invoice(self.product_id, self.seller_id, self.now)
        db.session.commit()
        invoice_id = invoice.id
        db.session.execute(text('UPDATE tariff_v2_storage_invoices SET gateway_id=NULL'))
        db.session.execute(text('DELETE FROM tariff_v2_gateway_numbers'))
        db.session.execute(text('ALTER TABLE tariff_v2_coverage DROP COLUMN is_test'))
        db.session.execute(text('ALTER TABLE tariff_v2_gateway_numbers DROP CONSTRAINT v2_gateway_purpose'))
        db.session.execute(text("ALTER TABLE tariff_v2_gateway_numbers ADD CONSTRAINT v2_gateway_purpose CHECK (purpose IN ('storage','checkout'))"))
        db.session.commit()
        for _ in range(2):
            self.assertFalse(prepare_schema(self.engine)['activation_changed'])
        db.session.expire_all()
        self.assertEqual(invoice.gateway_id, invoice_id)
        self.assertEqual(db.session.get(TariffV2GatewayNumber, invoice.gateway_id).purpose, 'storage')
        self.assertGreater(allocate_gateway_number('bill', 999), invoice.gateway_id)
        db.session.commit()

    def test_parallel_bill_payments_and_notifications_are_idempotent(self):
        from app.models.tariffs_v2 import TariffV2Invoice
        from app.models.tariffs_v2_operations import TariffV2BillPayment
        from app.utils.tariffs_v2_bills import FLOW, issue_bill_payment, accept_bill_result
        invoice = TariffV2Invoice(seller_id=self.seller_id, period=moscow_date(self.now).replace(day=1),
            issued_at=self.now.replace(tzinfo=None), due_at=self.now.replace(tzinfo=None), amount=300)
        db.session.add(invoice)
        db.session.commit()
        invoice_id = invoice.id
        results = self.race([lambda: issue_bill_payment(invoice_id, self.seller_id, self.now)] * 2)
        self.assertEqual(results[0], results[1])
        payment = TariffV2BillPayment.query.one()
        message = f'300.00:{payment.gateway_id}:fake-two:Shp_flow={FLOW}:Shp_token={payment.token}'
        data = MultiDict(dict(InvId=str(payment.gateway_id), OutSum='300.00', Shp_flow=FLOW,
            Shp_token=payment.token, SignatureValue=hashlib.sha256(message.encode()).hexdigest()))
        results = self.race([lambda: accept_bill_result(data, self.now)] * 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(payment.status, 'paid')
        self.assertIsNotNone(invoice.paid_at)

    def test_parallel_refund_queue_staging_creates_one_frozen_dispatch(self):
        from app.utils.tariffs_v2_refund_queue import enqueue_refund
        from app.models.tariffs_v2_operations import TariffV2RefundDispatch
        self.add()
        checkout = self.checkout()
        accept_checkout_result(self.callback(checkout), self.now)
        db.session.commit()
        refund = cancel_line(checkout.orders[0].lines[0].id, self.seller_id, 1, self.now)
        db.session.commit()
        refund_id = refund.id
        results = self.race([lambda: enqueue_refund(refund_id, self.now)] * 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(TariffV2RefundDispatch.query.count(), 1)
        self.assertEqual(TariffV2RefundDispatch.query.one().receipt_items[0]['Name'], 'Услуга доступа к площадке')
