import unittest
from datetime import timedelta
from pathlib import Path

from app import db
from app.models.tariffs_v2 import TariffV2Audit, TariffV2Coverage, TariffV2Seller
from app.models.tariffs_v2_commerce import TariffV2Reservation
from app.models.tariffs_v2_finance import TariffV2CheckoutRefund
from app.models.tariffs_v2_lifecycle import TariffV2OrderCompletion, TariffV2StorageExpiry
from app.utils.tariffs_v2 import (
    TariffError, midnight_utc, moscow_date, next_month, record_storage_payment, storage_quote, stored_utc,
)
from app.utils.tariffs_v2_commerce import ship_order
from app.utils.tariffs_v2_finance import (
    buyer_cancel_order, receive_order, cancel_line, open_claim, process_shipping_overdue,
)
from app.utils.tariffs_v2_storage_lifecycle import storage_expiry_plan, check_storage_expiry
import test_tariffs_v2_finance as foundation


class LifecycleTests(unittest.TestCase):
    setUp = foundation.FinanceTests.setUp
    tearDown = foundation.FinanceTests.tearDown
    login = foundation.FinanceTests.login
    add = foundation.FinanceTests.add
    checkout = foundation.FinanceTests.checkout
    callback = foundation.FinanceTests.callback
    pay = foundation.FinanceTests.pay
    ready = foundation.FinanceTests.ready
    confirm = foundation.FinanceTests.confirm
    admin = foundation.FinanceTests.admin

    def test_buyer_cancel_retains_fee_releases_stock_and_is_idempotent(self):
        order = self.ready()
        buyer_cancel_order(order.id, self.buyer.id, self.now)
        db.session.flush()
        buyer_cancel_order(order.id, self.buyer.id, self.now)
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(order.cod_amount, 0)
        self.assertEqual(order.checkout.cod_amount, 0)
        self.assertEqual(TariffV2Reservation.query.one().status, 'released')
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 0)
        self.assertEqual(TariffV2OrderCompletion.query.count(), 1)
        self.assertEqual(order.lines[0].refunded_fee, 0)
        with self.assertRaises(TariffError):
            cancel_line(order.lines[0].id, self.seller.id, 1, self.now)

    def test_buyer_cancel_own_goods_creates_principal_refund_without_debt(self):
        order = self.ready(own=True)
        buyer_cancel_order(order.id, self.buyer.id, self.now)
        db.session.flush()
        refund = TariffV2CheckoutRefund.query.one()
        self.assertEqual(refund.amount, 1000)
        self.assertEqual(refund.seller_charge, 0)
        self.assertEqual(refund.reason, 'buyer_cancelled')
        self.confirm(refund)
        self.assertEqual(order.lines[0].refunded_fee, 0)

    def test_partial_seller_cancel_then_buyer_cancel_does_not_refund_remaining_fee(self):
        order = self.ready(quantity=2)
        seller_refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        db.session.flush()
        buyer_cancel_order(order.id, self.buyer.id, self.now)
        db.session.flush()
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 1)
        self.assertEqual(seller_refund.amount, 300)
        self.assertEqual(seller_refund.seller_charge, 300)

    def test_partial_owner_cancel_then_buyer_cancel_accounts_for_both_portions(self):
        order = self.ready(quantity=2, own=True)
        cancel_line(order.lines[0].id, self.owner.id, 1, self.now)
        db.session.flush()
        buyer_cancel_order(order.id, self.buyer.id, self.now)
        db.session.flush()
        refunds = TariffV2CheckoutRefund.query.all()
        self.assertEqual(len(refunds), 2)
        self.assertEqual(sum(item.amount for item in refunds), 2000)
        self.assertEqual(sum(item.seller_charge for item in refunds), 0)

    def test_cancel_rejects_outsider_shipped_and_overdue(self):
        order = self.ready()
        with self.assertRaises(TariffError):
            buyer_cancel_order(order.id, self.second_buyer.id, self.now)
        with self.assertRaises(TariffError):
            buyer_cancel_order(order.id, self.buyer.id, self.now + timedelta(days=5))
        ship_order(order.id, self.seller.id, '', self.now)
        with self.assertRaises(TariffError):
            buyer_cancel_order(order.id, self.buyer.id, self.now)

    def test_receipt_requires_shipping_is_buyer_only_and_does_not_refund(self):
        order = self.ready()
        with self.assertRaises(TariffError):
            receive_order(order.id, self.buyer.id, self.now)
        ship_order(order.id, self.seller.id, '', self.now)
        with self.assertRaises(TariffError):
            receive_order(order.id, self.second_buyer.id, self.now)
        receive_order(order.id, self.buyer.id, self.now)
        db.session.flush()
        receive_order(order.id, self.buyer.id, self.now)
        self.assertEqual(order.status, 'received')
        self.assertEqual(TariffV2OrderCompletion.query.count(), 1)
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 0)
        with self.assertRaises(TariffError):
            open_claim(order.id, 'buyer', self.buyer.id, 'После получения', self.now)
        self.assertEqual(process_shipping_overdue(self.now + timedelta(days=10)), [])

    def test_open_dispute_prevents_conflicting_receipt_or_cancellation(self):
        order = self.ready()
        open_claim(order.id, 'buyer', self.buyer.id, 'Не отправлен', self.now)
        db.session.flush()
        with self.assertRaises(TariffError):
            buyer_cancel_order(order.id, self.buyer.id, self.now)
        ship_order(order.id, self.seller.id, '', self.now)
        with self.assertRaises(TariffError):
            receive_order(order.id, self.buyer.id, self.now)

    def setup_renewal(self):
        self.mapping.rule = self.fixed
        self.product.created_at = stored_utc(self.now - timedelta(days=10))
        record_storage_payment(self.product, storage_quote(self.product, self.now)['due'], 'storage-test', self.now)
        db.session.commit()
        return midnight_utc(next_month(moscow_date(self.now)))

    def test_storage_renewal_deadline_is_three_moscow_calendar_days(self):
        start = self.setup_renewal()
        boundary = start + timedelta(days=3)
        plan = storage_expiry_plan(self.product, boundary - timedelta(microseconds=1))
        self.assertEqual(plan['status'], 'grace')
        self.assertEqual(plan['expires_at'], stored_utc(boundary))
        self.assertEqual(storage_expiry_plan(self.product, boundary)['status'], 'expired')
        self.assertEqual(storage_expiry_plan(self.product, start)['unpaid_since'], moscow_date(start))

    def test_storage_check_is_idempotent_and_never_deletes_product_or_files(self):
        start = self.setup_renewal()
        sentinel = Path(self.tmp.name) / 'must-remain.epub'
        sentinel.write_bytes(b'preserve uploaded file')
        at = start + timedelta(days=4)
        original_stock = self.product.stock_quantity
        for _ in range(2):
            result = check_storage_expiry(at)
            db.session.commit()
            self.assertEqual(result['expired'], 1)
        self.assertEqual(TariffV2StorageExpiry.query.count(), 1)
        self.assertEqual(TariffV2StorageExpiry.query.one().status, 'expired')
        self.assertEqual(self.product.status, 'approved')
        self.assertEqual(self.product.stock_quantity, original_stock)
        self.assertEqual(sentinel.read_bytes(), b'preserve uploaded file')
        events = TariffV2Audit.query.filter_by(action='storage_expiry_plan_changed').all()
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].details['deletion_executed'])

    def test_storage_payment_resolves_plan_and_hidden_card_still_charged(self):
        start = self.setup_renewal()
        from app.models.tariffs_v2_commerce import TariffV2Listing
        db.session.get(TariffV2Listing, self.product.id).published = False
        check_storage_expiry(start)
        db.session.commit()
        self.assertGreater(storage_expiry_plan(self.product, start)['due'], 0)
        record_storage_payment(self.product, storage_quote(self.product, start)['due'], 'renewal-test', start)
        db.session.commit()
        check_storage_expiry(start)
        db.session.commit()
        self.assertEqual(TariffV2StorageExpiry.query.one().status, 'resolved')

    def test_new_card_gets_correct_grace_without_daily_reset(self):
        start = self.setup_renewal()
        later = start + timedelta(days=14, hours=12)
        self.product.created_at = stored_utc(later)
        db.session.commit()
        self.assertEqual(storage_expiry_plan(self.product, later)['unpaid_since'], moscow_date(later))
        check_storage_expiry(later)
        db.session.commit()
        self.assertEqual(storage_expiry_plan(self.product, later + timedelta(days=2))['expires_at'],
            stored_utc(midnight_utc(moscow_date(later) + timedelta(days=3))))

    def test_midmonth_topup_and_payment_start_new_episode_without_backdating(self):
        start = self.setup_renewal()
        record_storage_payment(self.product, storage_quote(self.product, start)['due'], 'current-month', start)
        db.session.commit()
        later = start + timedelta(days=14, hours=12)
        self.fixed.amount = 620
        db.session.commit()
        check_storage_expiry(later)
        db.session.commit()
        self.assertEqual(TariffV2StorageExpiry.query.one().unpaid_since, moscow_date(later))
        payment_at = later + timedelta(days=1)
        record_storage_payment(self.product, storage_quote(self.product, payment_at)['due'], 'top-up', payment_at)
        db.session.commit()
        self.fixed.amount = 930
        db.session.commit()
        observed = later + timedelta(days=2)
        plan = storage_expiry_plan(self.product, observed)
        self.assertEqual(plan['unpaid_since'], moscow_date(observed))

    def test_free_storage_expiry_starts_grace_at_benefit_boundary(self):
        start = self.setup_renewal()
        end = start + timedelta(days=10)
        db.session.add(TariffV2Seller(seller_id=self.seller.id, free_storage=True, benefits_until=stored_utc(end)))
        db.session.commit()
        self.assertEqual(storage_expiry_plan(self.product, end - timedelta(microseconds=1))['status'], 'resolved')
        plan = storage_expiry_plan(self.product, end)
        self.assertEqual(plan['unpaid_since'], moscow_date(end))
        self.assertEqual(plan['expires_at'], stored_utc(end + timedelta(days=3)))

    def test_new_category_assignment_does_not_backdate_storage_debt(self):
        start = self.setup_renewal()
        changed = start + timedelta(days=19)
        db.session.add(TariffV2Audit(actor='admin:test', action='assign_category', subject=str(self.category.id),
            details={'after': self.fixed.id}, created_at=stored_utc(changed)))
        db.session.commit()
        plan = storage_expiry_plan(self.product, changed + timedelta(days=1))
        self.assertEqual(plan['unpaid_since'], moscow_date(changed))

    def test_missing_tariff_not_treated_as_storage_debt_and_owner_free(self):
        db.session.delete(self.mapping)
        db.session.commit()
        result = check_storage_expiry(self.now)
        db.session.commit()
        self.assertEqual(result['errors'], 1)
        self.assertEqual(TariffV2StorageExpiry.query.count(), 0)
        self.assertEqual(storage_expiry_plan(self.own_product, self.now)['status'], 'resolved')

    def test_lifecycle_routes_ownership_csrf_and_rendering(self):
        order = self.ready()
        self.login(self.seller)
        self.assertEqual(self.client.post(f'/tariffs-v2/orders/{order.id}/buyer-cancel').status_code, 403)
        self.login(self.second_buyer)
        self.assertEqual(self.client.post(f'/tariffs-v2/orders/{order.id}/received').status_code, 404)
        self.login(self.buyer)
        ship_order(order.id, self.seller.id, '', self.now)
        db.session.commit()
        response = self.client.post(f'/tariffs-v2/orders/{order.id}/received')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(f'/tariffs-v2/checkouts/{order.checkout_id}').status_code, 200)
        self.admin()
        self.assertEqual(self.client.get('/main_admin/tariffs-v2/storage-expiry').status_code, 200)
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/main_admin/tariffs-v2/storage-expiry/check').status_code, 400)

    def test_storage_cli_is_explicit_sandbox_only(self):
        self.setup_renewal()
        result = self.app.test_cli_runner().invoke(args=['tariffs-v2-storage-check'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('No cards or files deleted', result.output)
        self.app.config['TARIFFS_V2_SANDBOX'] = False
        result = self.app.test_cli_runner().invoke(args=['tariffs-v2-storage-check'])
        self.assertNotEqual(result.exit_code, 0)
