import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import event

from app import create_app, db
from config import TestingConfig
from app.models.orders import Order
from app.models.products import Category, Product
from app.models.users import Buyer, Seller
from app.models.tariffs_v2 import (
    TariffV2Audit, TariffV2Category, TariffV2Coverage, TariffV2Invoice,
    TariffV2OrderLine, TariffV2Refund, TariffV2Rule, TariffV2Seller,
)
from app.utils.tariffs_v2 import (
    TariffError, benefits_end, blocked_for_new_sales, confirm_invoice_payment,
    confirm_refund, cumulative_partial_fee, hand_to_seller, issue_monthly_invoice, mark_shipped,
    midnight_utc, money, moscow_date, next_month, prorated_storage,
    quote_cart, quote_line, record_storage_payment, refund_decision,
    request_refund, resolve_tariff, shipping_overdue, snapshot_line, storage_quote,
    validate_rule,
)

UTC = timezone.utc


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


class TariffsV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            MAIL_WORKER_ENABLED = False
            MAIL_PASSWORD = None
            TARIFFS_V2_ENABLED = True
            TARIFFS_V2_SANDBOX = True
            LOG_FILE = str(Path(self.tmp.name) / 'test.log')
            UPLOAD_FOLDER = str(Path(self.tmp.name) / 'uploads')
        self.config = Config
        self.app = create_app(Config)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.seller = Seller(login='seller', email='seller@example.test', password_hash='test',
                             store_name='Store', store_slug='store')
        self.owner = Seller(login='owner', email='ronnie83@mail.ru', password_hash='test',
                            store_name='Owner', store_slug='owner')
        self.buyer = Buyer(login='buyer', email='buyer@example.test', password_hash='test')
        self.parent = Category(name='Parent', slug='parent')
        self.category = Category(name='Category', slug='category', parent=self.parent)
        self.rule = TariffV2Rule(name='3 percent', kind='percent', amount=Decimal('3'))
        self.fixed = TariffV2Rule(name='310 a month', kind='fixed', amount=Decimal('310'))
        self.product = Product(seller=self.seller, category=self.category, name='Product',
                               slug='product', article='p1', price=10000,
                               current_discount=0, stock_quantity=10, status='approved')
        db.session.add_all([self.seller, self.owner, self.buyer, self.parent, self.category,
                           self.rule, self.fixed, self.product])
        db.session.flush()
        self.mapping = TariffV2Category(category_id=self.category.id, rule_id=self.rule.id)
        db.session.add(self.mapping)
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

    def admin(self):
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
            session['main_admin_user'] = 'test-admin'

    def line(self, at=None):
        order = Order(order_number='TEST-' + str(Order.query.count()), buyer=self.buyer,
                       seller=self.seller, total_price=10000, status='processing')
        db.session.add(order)
        db.session.flush()
        row = snapshot_line(order, self.product, 1, 'one', at)
        db.session.flush()
        hand_to_seller(row, at or instant('2026-11-01T12:00:00'))
        db.session.flush()
        return row

    def test_moscow_date_and_inclusive_benefits(self):
        self.assertEqual(moscow_date(instant('2026-10-31T20:59:59')), date(2026, 10, 31))
        self.assertEqual(moscow_date(instant('2026-10-31T21:00:00')), date(2026, 11, 1))
        self.assertEqual(benefits_end(date(2026, 11, 5)), instant('2026-11-05T21:00:00'))

    def test_proration_handles_calendar_and_leap_year(self):
        self.assertEqual(prorated_storage(310, date(2026, 1, 16)), Decimal('160'))
        self.assertEqual(prorated_storage(290, date(2028, 2, 29)), Decimal('10'))
        self.assertEqual(prorated_storage(310, date(2026, 1, 1)), Decimal('310'))
        self.assertEqual(next_month(date(2026, 12, 20)), date(2027, 1, 1))
        with self.assertRaises(TariffError):
            prorated_storage(100, date(2026, 1, 10), date(2026, 2, 2))

    def test_invalid_rates_and_amounts(self):
        for value in ('NaN', 'Infinity', '-1', '100000000000'):
            with self.assertRaises(TariffError):
                money(value)
        for kind, rate in [('unknown', '1'), ('percent', '100.01'), ('fixed', '1.001')]:
            with self.assertRaises(TariffError):
                validate_rule(kind, rate)
        for quantity in ('0', '-1', '1.5', True):
            with self.assertRaises(TariffError):
                quote_line(self.product, quantity)

    def test_percent_is_seller_price_only_and_rounds_per_line(self):
        quote = quote_line(self.product, 2)
        self.assertEqual(quote.fee, Decimal('600'))
        self.assertEqual(quote.online_amount, Decimal('600'))
        self.assertEqual(quote.cod_amount, Decimal('20000'))
        self.product.price = 0.05
        self.rule.amount = 10
        self.assertEqual(quote_line(self.product, 3).fee, Decimal('0.02'))

    def test_no_parent_tariff_inheritance(self):
        db.session.delete(self.mapping)
        db.session.add(TariffV2Category(category_id=self.parent.id, rule_id=self.rule.id))
        db.session.flush()
        with self.assertRaises(TariffError):
            quote_line(self.product, 1)

    def test_seller_override_wins_and_category_change_is_immediate(self):
        policy = TariffV2Seller(seller_id=self.seller.id, override_rule_id=self.fixed.id)
        db.session.add(policy)
        db.session.flush()
        self.assertEqual(quote_line(self.product, 1).fee, 0)
        policy.override_rule_id = None
        policy.override_rule = None
        db.session.flush()
        self.assertEqual(quote_line(self.product, 1).fee, 300)
        self.product.category = self.parent
        db.session.flush()
        with self.assertRaises(TariffError):
            quote_line(self.product, 1)

    def test_independent_benefits_and_expiry(self):
        policy = TariffV2Seller(seller_id=self.seller.id, free_buyer_fee=True,
                                free_storage=False, benefits_until=benefits_end(date(2026, 11, 5)))
        db.session.add(policy)
        db.session.flush()
        self.assertEqual(quote_line(self.product, 1, instant('2026-11-05T20:59:59')).fee, 0)
        self.assertEqual(quote_line(self.product, 1, instant('2026-11-05T21:00:00')).fee, 300)
        policy.override_rule = self.fixed
        policy.benefits_until = None
        db.session.flush()
        self.assertEqual(storage_quote(self.product, instant('2026-01-16T12:00:00'))['due'], 160)
        policy.free_storage = True
        self.assertEqual(storage_quote(self.product)['due'], 0)

    def test_mixed_cart_includes_full_owner_payment(self):
        own = Product(seller=self.owner, category=self.category, name='Own', slug='own',
                       article='own', price=500, current_discount=0, stock_quantity=10)
        db.session.add(own)
        db.session.flush()
        quote = quote_cart([SimpleNamespace(product=self.product, quantity=1),
                            SimpleNamespace(product=own, quantity=2)])
        self.assertEqual(quote['online_amount'], '1300.00')
        self.assertEqual(quote['cod_amount'], '10000.00')
        self.assertTrue(quote['requires_gateway'])
        self.assertEqual(quote['lines'][1]['fee'], '0.00')

    def test_zero_fee_cart_needs_no_gateway(self):
        db.session.add(TariffV2Seller(seller_id=self.seller.id, free_buyer_fee=True))
        db.session.flush()
        result = quote_cart([SimpleNamespace(product=self.product, quantity=1)])
        self.assertEqual(result['online_amount'], '0.00')
        self.assertFalse(result['requires_gateway'])

    def test_owner_has_free_storage_and_full_online_even_without_category_rule(self):
        self.product.seller = self.owner
        db.session.delete(self.mapping)
        db.session.flush()
        quote = quote_line(self.product, 1)
        self.assertEqual((quote.fee, quote.online_amount, quote.cod_amount), (0, 10000, 0))
        self.assertEqual(storage_quote(self.product)['due'], 0)

    def test_free_override_survives_inactive_category_rule(self):
        self.rule.active = False
        db.session.flush()
        with self.assertRaises(TariffError):
            quote_line(self.product, 1)
        db.session.add(TariffV2Seller(seller_id=self.seller.id, free_storage=True, free_buyer_fee=True))
        db.session.flush()
        self.assertEqual(quote_line(self.product, 1).fee, 0)

    def test_digital_sales_are_owner_only_even_if_another_seller_uses_full_online(self):
        from app.models.digital import DigitalBook
        book = DigitalBook(seller=self.seller, title='Book', price=10000, file_name='test.epub', format='epub')
        db.session.add(book)
        db.session.flush()
        self.product.digital_book_id = book.id
        db.session.add(TariffV2Seller(seller_id=self.seller.id, payment_mode='full_online'))
        db.session.flush()
        with self.assertRaises(TariffError):
            quote_line(self.product, 1)
        self.product.seller = self.owner
        db.session.flush()
        self.assertEqual(quote_line(self.product, 1).online_amount, 10000)

    def test_prepayment_credit_on_category_change_no_double_charge(self):
        self.mapping.rule = self.fixed
        db.session.flush()
        first = instant('2026-01-16T10:00:00')
        paid = record_storage_payment(self.product, 160, 'storage-1', first)
        db.session.flush()
        self.assertIs(record_storage_payment(self.product, 160, 'storage-1', first), paid)
        self.assertEqual(storage_quote(self.product, first)['due'], 0)
        second = TariffV2Rule(name='620', kind='fixed', amount=620)
        db.session.add(second)
        db.session.flush()
        self.mapping.rule = second
        db.session.flush()
        day = instant('2026-01-21T10:00:00')
        quote = storage_quote(self.product, day)
        self.assertEqual((quote['required'], quote['credit'], quote['due']), (220, 110, 110))
        record_storage_payment(self.product, 110, 'storage-2', day)
        db.session.flush()
        self.assertEqual(storage_quote(self.product, day)['due'], 0)
        self.mapping.rule = self.fixed
        db.session.flush()
        self.assertEqual(storage_quote(self.product, day)['due'], 0)
        self.assertEqual(TariffV2Coverage.query.count(), 2)

    def test_hidden_cards_still_pay_and_next_month_requires_prepayment(self):
        self.mapping.rule = self.fixed
        self.product.status = 'draft'
        db.session.flush()
        record_storage_payment(self.product, 310, 'full-month', instant('2026-01-01T12:00:00'))
        db.session.flush()
        self.assertFalse(storage_quote(self.product, instant('2026-02-01T12:00:00'))['may_publish'])
        self.assertEqual(storage_quote(self.product, instant('2026-02-01T12:00:00'))['due'], 310)

    def test_payment_amount_must_match_storage_quote(self):
        self.mapping.rule = self.fixed
        db.session.flush()
        with self.assertRaises(TariffError):
            record_storage_payment(self.product, 1, 'wrong', instant('2026-01-16T12:00:00'))
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_soft_deleted_card_is_not_billed_or_published(self):
        self.mapping.rule = self.fixed
        self.product.status = 'deleted'
        db.session.flush()
        quote = storage_quote(self.product, instant('2026-01-16T12:00:00'))
        self.assertEqual(quote['due'], 0)
        self.assertEqual(quote['monthly_rate'], 0)
        self.assertFalse(quote['may_publish'])

    def test_catalog_deletion_keeps_prepayment_history_without_reusing_credit(self):
        self.mapping.rule = self.fixed
        db.session.flush()
        coverage = record_storage_payment(self.product, 310, 'archived-card', instant('2026-01-01T12:00:00'))
        db.session.flush()
        original = self.product.id
        coverage_id = coverage.id
        db.session.delete(self.product)
        db.session.flush()
        db.session.expire_all()
        saved = db.session.get(TariffV2Coverage, coverage_id)
        self.assertIsNone(saved.product_id)
        self.assertEqual(saved.original_product_id, original)
        self.assertEqual(saved.paid_amount, 310)

    def test_order_snapshot_survives_price_and_tariff_changes(self):
        row = self.line()
        self.product.price = 20000
        self.rule.amount = 10
        db.session.flush()
        self.assertEqual((row.unit_price, row.fee, row.rate), (10000, 300, 3))

    def test_shipping_deadline_and_optional_tracking(self):
        row = self.line()
        deadline = row.shipping_deadline.replace(tzinfo=UTC)
        self.assertFalse(shipping_overdue(row, deadline - timedelta(microseconds=1)))
        self.assertTrue(shipping_overdue(row, deadline))
        with self.assertRaises(TariffError):
            mark_shipped(row, self.owner.id, deadline - timedelta(seconds=1))
        mark_shipped(row, self.seller.id, deadline - timedelta(seconds=1))
        self.assertFalse(shipping_overdue(row, deadline))
        original = row.shipped_at
        mark_shipped(row, self.seller.id, deadline + timedelta(days=1))
        self.assertEqual(row.shipped_at, original)

    def test_unpaid_order_has_no_shipping_deadline(self):
        row = self.line()
        row.handed_to_seller_at = row.shipping_deadline = None
        self.assertFalse(shipping_overdue(row))
        with self.assertRaises(TariffError):
            mark_shipped(row, self.seller.id)

    def test_dispute_requires_admin_and_silence_does_not_auto_refund(self):
        opened = instant('2026-11-01T12:00:00')
        self.assertEqual(refund_decision('waiting', opened, opened + timedelta(days=2)), 'waiting')
        self.assertEqual(refund_decision('waiting', opened, opened + timedelta(days=3)), 'admin_review')
        self.assertEqual(refund_decision('disputed', opened, opened), 'admin_review')
        self.assertEqual(refund_decision('disputed', opened, opened, True), 'refund')
        self.assertEqual(refund_decision('confirmed', opened, opened), 'refund')

    def test_only_successful_refunds_become_debt(self):
        row = self.line()
        refund = request_refund(row.id, 300, 'seller_cancelled', 'refund-1')
        db.session.flush()
        self.assertIsNone(issue_monthly_invoice(self.seller.id, instant('2026-12-01T12:00:00')))
        self.assertEqual(row.refunded_fee, 0)
        confirmed = instant('2026-11-30T20:59:59')
        confirm_refund(refund.id, 'provider-1', 300, confirmed)
        db.session.flush()
        confirm_refund(refund.id, 'provider-1', 300, confirmed)
        self.assertEqual(row.refunded_fee, 300)
        invoice = issue_monthly_invoice(self.seller.id, instant('2026-11-30T21:00:00'))
        self.assertEqual(invoice.amount, 300)
        self.assertEqual(invoice.period, date(2026, 12, 1))
        db.session.flush()
        self.assertEqual(TariffV2Invoice.query.count(), 1)

    def test_pending_partial_refunds_cannot_exceed_original_fee(self):
        row = self.line()
        refund = request_refund(row.id, 100, 'seller_partial_cancelled', 'partial-1')
        db.session.flush()
        self.assertIs(request_refund(row.id, 100, 'seller_partial_cancelled', 'partial-1'), refund)
        with self.assertRaises(TariffError):
            request_refund(row.id, 201, 'seller_partial_cancelled', 'partial-2')
        with self.assertRaises(TariffError):
            request_refund(row.id, 100, 'seller_cancelled', 'partial-1')
        with self.assertRaises(TariffError):
            confirm_refund(refund.id, 'wrong-amount', 99)

    def test_cumulative_partial_refund_preserves_last_kopeck(self):
        line = SimpleNamespace(quantity=3, fee=Decimal('0.02'))
        targets = [cumulative_partial_fee(line, quantity) for quantity in range(4)]
        self.assertEqual(targets, [Decimal('0'), Decimal('0.01'), Decimal('0.01'), Decimal('0.02')])
        self.assertEqual(sum((targets[i] - targets[i - 1] for i in range(1, 4)), Decimal('0')), line.fee)
        with self.assertRaises(TariffError):
            cumulative_partial_fee(line, 4)

    def test_buyer_cancellation_and_post_receipt_return_do_not_refund_platform_fee(self):
        row = self.line()
        for reason in ('buyer_cancelled', 'not_collected', 'returned_after_receipt'):
            with self.assertRaises(TariffError):
                request_refund(row.id, 300, reason, 'x-' + reason)
        self.assertEqual(TariffV2Refund.query.count(), 0)

    def test_invoice_moscow_fifth_inclusive_and_payment_unblocks(self):
        row = self.line()
        refund = request_refund(row.id, 100, 'shipping_overdue', 'late-1')
        db.session.flush()
        confirm_refund(refund.id, 'returned-1', 100, instant('2026-11-15T12:00:00'))
        db.session.flush()
        invoice = issue_monthly_invoice(self.seller.id, instant('2026-12-01T12:00:00'))
        db.session.flush()
        self.assertIs(issue_monthly_invoice(self.seller.id, instant('2026-12-02T12:00:00')), invoice)
        self.assertFalse(blocked_for_new_sales(self.seller.id, instant('2026-12-05T20:59:59')))
        self.assertTrue(blocked_for_new_sales(self.seller.id, instant('2026-12-05T21:00:00')))
        with self.assertRaises(TariffError):
            confirm_invoice_payment(invoice.id, 99, 'bad')
        confirm_invoice_payment(invoice.id, 100, 'paid-invoice', instant('2026-12-06T12:00:00'))
        db.session.flush()
        self.assertFalse(blocked_for_new_sales(self.seller.id, instant('2026-12-07T12:00:00')))

    def test_current_month_refund_waits_for_next_month_invoice(self):
        row = self.line()
        refund = request_refund(row.id, 100, 'seller_cancelled', 'new-month')
        db.session.flush()
        confirm_refund(refund.id, 'new-month-provider', 100, instant('2026-11-30T21:00:00'))
        db.session.flush()
        self.assertIsNone(issue_monthly_invoice(self.seller.id, instant('2026-12-01T12:00:00')))
        invoice = issue_monthly_invoice(self.seller.id, instant('2027-01-01T12:00:00'))
        self.assertEqual(invoice.amount, 100)

    def test_admin_preview_requires_admin(self):
        self.assertEqual(self.client.get('/main_admin/tariffs-v2/').status_code, 403)
        self.assertEqual(self.client.get(f'/main_admin/tariffs-v2/preview/{self.product.id}').status_code, 403)

    def test_admin_can_create_assign_and_grant_benefits_with_audit(self):
        self.admin()
        response = self.client.post('/main_admin/tariffs-v2/rules', data={
            'name': 'New', 'kind': 'fixed', 'amount': '150'})
        self.assertEqual(response.status_code, 302)
        rule = TariffV2Rule.query.filter_by(name='New').one()
        response = self.client.post(f'/main_admin/tariffs-v2/categories/{self.category.id}', data={'rule_id': rule.id})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(f'/main_admin/tariffs-v2/sellers/{self.seller.id}', data={
            'shipping_days': '2', 'free_storage': 'on', 'free_buyer_fee': 'on',
            'benefits_last_day': '2026-11-05'})
        self.assertEqual(response.status_code, 302)
        policy = db.session.get(TariffV2Seller, self.seller.id)
        self.assertEqual(policy.shipping_days, 2)
        self.assertEqual(policy.benefits_until, instant('2026-11-05T21:00:00').replace(tzinfo=None))
        self.assertEqual(TariffV2Audit.query.count(), 3)
        page = self.client.get('/main_admin/tariffs-v2/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('2026-11-05', page.get_data(as_text=True))

    def test_admin_rejects_invalid_policy_without_partial_update(self):
        self.admin()
        for days in ('0', '6', '2.5'):
            response = self.client.post(f'/main_admin/tariffs-v2/sellers/{self.seller.id}',
                                        data={'shipping_days': days, 'free_storage': 'on'})
            self.assertEqual(response.status_code, 400)
        self.assertIsNone(db.session.get(TariffV2Seller, self.seller.id))
        self.assertEqual(TariffV2Audit.query.count(), 0)

    def test_admin_mutations_require_csrf_even_in_sandbox(self):
        import re
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.admin()
        data = dict(name='Protected', kind='percent', amount='5')
        response = self.client.post('/main_admin/tariffs-v2/rules', data=data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TariffV2Audit.query.count(), 0)
        page = self.client.get('/main_admin/tariffs-v2/').get_data(as_text=True)
        data['csrf_token'] = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post('/main_admin/tariffs-v2/rules', data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TariffV2Audit.query.count(), 1)

    def test_buyer_cannot_change_tariffs(self):
        with self.client.session_transaction() as session:
            session['_user_id'] = self.buyer.get_id()
            session['_fresh'] = True
        response = self.client.post('/main_admin/tariffs-v2/rules',
                                    data=dict(name='Unauthorized', kind='percent', amount='0'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(TariffV2Audit.query.count(), 0)

    def test_preview_does_not_create_orders_invoices_or_payments(self):
        self.admin()
        response = self.client.get(f'/main_admin/tariffs-v2/preview/{self.product.id}?quantity=2')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['production_checkout_changed'])
        self.assertIs(response.json['storage']['may_publish'], True)
        self.assertEqual(response.json['order']['fee'], '600.00')
        self.assertEqual(Order.query.count(), 0)
        self.assertEqual(TariffV2Invoice.query.count(), 0)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_disabled_feature_registers_no_routes_and_queries_no_v2_tables(self):
        class Disabled(self.config):
            TARIFFS_V2_ENABLED = False
        app = create_app(Disabled)
        self.assertFalse(any(rule.endpoint.startswith('tariffs_v2.') for rule in app.url_map.iter_rules()))
        with app.app_context():
            queries = []
            def capture(conn, cursor, statement, parameters, context, executemany):
                queries.append(statement)
            event.listen(db.engine, 'before_cursor_execute', capture)
            try:
                response = app.test_client().get('/main_admin/tariffs-v2/')
                self.assertEqual(response.status_code, 404)
                self.assertFalse(any('tariff_v2_' in query for query in queries))
            finally:
                event.remove(db.engine, 'before_cursor_execute', capture)

    def test_enabled_preview_requires_explicit_sandbox(self):
        class Unsafe(self.config):
            TARIFFS_V2_SANDBOX = False
        with self.assertRaisesRegex(RuntimeError, 'isolated sandbox'):
            create_app(Unsafe)

    def test_sandbox_initialization_is_idempotent_and_has_no_jobs(self):
        runner = self.app.test_cli_runner()
        for _ in range(2):
            result = runner.invoke(args=['tariffs-v2-init-sandbox'])
            self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(TariffV2Refund.query.count(), 0)
        self.assertEqual(TariffV2Coverage.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
