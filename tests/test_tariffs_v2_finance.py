import unittest
from datetime import timedelta
from decimal import Decimal

from app import db
from app.models.tariffs_v2 import TariffV2Invoice
from app.models.tariffs_v2_commerce import TariffV2Reservation
from app.models.tariffs_v2_finance import TariffV2CheckoutRefund, TariffV2Claim
from app.utils.tariffs_v2 import (
    TariffError, issue_monthly_invoice, blocked_for_new_sales, midnight_utc, moscow_date, next_month,
)
from app.utils.tariffs_v2_commerce import ship_order
from app.utils.tariffs_v2_finance import (
    cancel_line, confirm_checkout_refund, open_claim, respond_claim, decide_claim,
    process_shipping_overdue, retry_refund, simulate_refund, simulate_invoice_payment,
)
import test_tariffs_v2_checkout as foundation


class FinanceTests(unittest.TestCase):
    setUp = foundation.CheckoutTests.setUp
    tearDown = foundation.CheckoutTests.tearDown
    login = foundation.CheckoutTests.login
    add = foundation.CheckoutTests.add
    checkout = foundation.CheckoutTests.checkout
    callback = foundation.CheckoutTests.callback
    pay = foundation.CheckoutTests.pay

    def ready(self, quantity=1, own=False):
        self.add(self.own_product if own else self.product, quantity)
        row = self.checkout()
        self.pay(row)
        return row.orders[0]

    def next_month(self):
        return midnight_utc(next_month(moscow_date(self.now)))

    def confirm(self, refund, success=True, at=None):
        db.session.flush()
        return confirm_checkout_refund(refund.id, f'test-provider:{refund.id}', refund.amount, success, at or self.now)

    def claim(self, order, role='buyer'):
        row = open_claim(order.id, role, self.buyer.id if role == 'buyer' else self.seller.id, 'Товар не получен', self.now)
        db.session.flush()
        return row

    def admin(self):
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
            session['main_admin_user'] = 'test-admin'

    def test_seller_cancel_creates_intent_not_debt_and_releases_reservation(self):
        order = self.ready()
        refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        db.session.flush()
        self.assertEqual(refund.amount, Decimal('300'))
        self.assertEqual(refund.seller_charge, Decimal('300'))
        self.assertEqual(refund.status, 'pending')
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(order.cod_amount, 0)
        self.assertEqual(order.checkout.cod_amount, 0)
        self.assertEqual(TariffV2Reservation.query.one().status, 'released')
        self.assertIsNone(issue_monthly_invoice(self.seller.id, self.next_month()))
        self.assertEqual(order.lines[0].refunded_fee, 0)

    def test_success_is_idempotent_and_billed_only_after_confirmation_month(self):
        order = self.ready()
        refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        self.confirm(refund)
        self.confirm(refund)
        self.assertEqual(order.lines[0].refunded_fee, 300)
        self.assertIsNone(issue_monthly_invoice(self.seller.id, self.now))
        invoice = issue_monthly_invoice(self.seller.id, self.next_month())
        db.session.flush()
        self.assertEqual(invoice.amount, 300)
        self.assertEqual(refund.invoice_id, invoice.id)
        self.assertEqual(issue_monthly_invoice(self.seller.id, self.next_month()).id, invoice.id)
        self.assertEqual(TariffV2Invoice.query.count(), 1)

    def test_failed_refund_never_billed_and_retry_is_capped_and_idempotent(self):
        order = self.ready()
        refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        self.confirm(refund, False)
        self.assertEqual(order.lines[0].refunded_fee, 0)
        self.assertIsNone(issue_monthly_invoice(self.seller.id, self.next_month()))
        retry = retry_refund(refund.id, 'admin:test', self.now)
        db.session.flush()
        self.assertEqual(retry_refund(refund.id, 'admin:test', self.now).id, retry.id)
        self.confirm(retry)
        self.assertEqual(issue_monthly_invoice(self.seller.id, self.next_month()).amount, 300)

    def test_partial_cancel_cumulative_target_and_repeated_click_do_not_double_refund(self):
        self.product.price = Decimal('0.11')
        self.rule.amount = Decimal('3')
        db.session.commit()
        order = self.ready(quantity=3)
        line = order.lines[0]
        self.assertEqual(line.fee, Decimal('0.01'))
        first = cancel_line(line.id, self.seller.id, 1, self.now)
        self.assertIsNone(first)
        second = cancel_line(line.id, self.seller.id, 2, self.now)
        db.session.flush()
        self.assertEqual(second.amount, Decimal('0.01'))
        self.assertEqual(cancel_line(line.id, self.seller.id, 2, self.now).id, second.id)
        self.confirm(second)
        self.assertIsNone(cancel_line(line.id, self.seller.id, 3, self.now))
        self.assertEqual(line.refunded_fee, line.fee)

    def test_owner_refund_includes_goods_but_creates_no_commission_debt(self):
        order = self.ready(own=True)
        refund = cancel_line(order.lines[0].id, self.owner.id, 1, self.now)
        self.assertEqual(refund.amount, 1000)
        self.assertEqual(refund.seller_charge, 0)
        self.confirm(refund)
        self.assertIsNone(issue_monthly_invoice(self.owner.id, self.next_month()))

    def test_free_cod_cancel_needs_no_provider_operation(self):
        from app.models.tariffs_v2 import TariffV2Seller
        db.session.add(TariffV2Seller(seller_id=self.seller.id, free_buyer_fee=True))
        db.session.commit()
        self.add()
        order = self.checkout().orders[0]
        self.assertIsNone(cancel_line(order.lines[0].id, self.seller.id, 1, self.now))
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 0)

    def test_overdue_uses_snapshot_deadline_only_unshipped_and_no_debt_before_success(self):
        order = self.ready()
        before = self.now + timedelta(days=5, microseconds=-1)
        self.assertEqual(process_shipping_overdue(before), [])
        at = self.now + timedelta(days=5)
        refunds = process_shipping_overdue(at)
        db.session.flush()
        self.assertEqual(len(refunds), 1)
        process_shipping_overdue(at)
        db.session.flush()
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 1)
        self.assertEqual(refunds[0].reason, 'shipping_overdue')
        self.assertIsNone(issue_monthly_invoice(self.seller.id, self.next_month()))

    def test_shipped_with_no_tracking_does_not_get_overdue_refund(self):
        order = self.ready()
        ship_order(order.id, self.seller.id, '', self.now)
        db.session.flush()
        self.assertEqual(process_shipping_overdue(self.now + timedelta(days=10)), [])

    def test_claim_confirmation_creates_refund_and_opener_cannot_confirm_own_claim(self):
        order = self.ready()
        claim = self.claim(order)
        with self.assertRaises(TariffError):
            respond_claim(claim.id, 'buyer', self.buyer.id, True, 'Да', self.now)
        respond_claim(claim.id, 'seller', self.seller.id, True, 'Подтверждаю', self.now)
        db.session.flush()
        self.assertEqual(claim.status, 'approved')
        self.assertEqual(TariffV2CheckoutRefund.query.one().status, 'pending')

    def test_disagreement_requires_admin_and_never_automatically_refunds(self):
        order = self.ready()
        claim = self.claim(order)
        respond_claim(claim.id, 'seller', self.seller.id, False, 'Товар доставлен', self.now)
        db.session.flush()
        self.assertEqual(claim.status, 'disputed')
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 0)
        decide_claim(claim.id, False, 'Доставка подтверждена', 'admin:test', self.now)
        self.assertEqual(claim.status, 'rejected')
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 0)

    def test_silence_after_three_days_requires_explicit_admin_decision(self):
        order = self.ready()
        claim = self.claim(order)
        with self.assertRaises(TariffError):
            decide_claim(claim.id, True, 'Решение', 'admin:test', self.now + timedelta(days=3, microseconds=-1))
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 0)
        decide_claim(claim.id, True, 'Ответ не поступил', 'admin:test', self.now + timedelta(days=3))
        db.session.flush()
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 1)

    def test_active_claim_unique_and_outsiders_rejected(self):
        order = self.ready()
        with self.assertRaises(TariffError):
            open_claim(order.id, 'buyer', self.second_buyer.id, 'Чужая претензия', self.now)
        self.claim(order)
        with self.assertRaises(TariffError):
            self.claim(order)

    def test_claim_overdue_and_cancellation_cannot_exceed_online_money(self):
        order = self.ready(quantity=2)
        first = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        self.assertEqual(order.cod_amount, 10000)
        self.confirm(first)
        process_shipping_overdue(self.now + timedelta(days=5))
        db.session.flush()
        claim = self.claim(order)
        respond_claim(claim.id, 'seller', self.seller.id, True, 'Да', self.now)
        db.session.flush()
        self.assertEqual(sum(row.amount for row in TariffV2CheckoutRefund.query.all()), 600)
        self.assertEqual(TariffV2CheckoutRefund.query.count(), 2)

    def test_bill_blocks_on_sixth_moscow_and_simulation_unblocks(self):
        order = self.ready()
        self.confirm(cancel_line(order.lines[0].id, self.seller.id, 1, self.now))
        invoice = issue_monthly_invoice(self.seller.id, self.next_month())
        self.assertFalse(blocked_for_new_sales(self.seller.id, invoice.due_at - timedelta(microseconds=1)))
        self.assertTrue(blocked_for_new_sales(self.seller.id, invoice.due_at))
        simulate_invoice_payment(invoice.id, self.seller.id, invoice.due_at)
        self.assertFalse(blocked_for_new_sales(self.seller.id, invoice.due_at))

    def test_wrong_success_amount_and_conflicting_terminal_result_rejected(self):
        order = self.ready()
        refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        db.session.flush()
        with self.assertRaises(TariffError):
            confirm_checkout_refund(refund.id, 'provider', 301, True, self.now)
        self.confirm(refund)
        with self.assertRaises(TariffError):
            self.confirm(refund, False)

    def test_simulator_is_sandbox_only(self):
        order = self.ready()
        refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        db.session.flush()
        self.app.config['TARIFFS_V2_SANDBOX'] = False
        with self.assertRaises(TariffError):
            simulate_refund(refund.id, True, 'admin:test', self.now)
        self.assertEqual(refund.status, 'pending')

    def test_participant_pages_and_admin_guard_and_csrf(self):
        order = self.ready()
        self.login(self.buyer)
        response = self.client.get(f'/tariffs-v2/orders/{order.id}/claims')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/main_admin/tariffs-v2/finance').status_code, 403)
        self.login(self.second_buyer)
        self.assertEqual(self.client.get(f'/tariffs-v2/orders/{order.id}/claims').status_code, 404)
        self.login(self.seller)
        self.assertEqual(self.client.get('/tariffs-v2/seller/bills').status_code, 200)
        self.admin()
        self.assertEqual(self.client.get('/main_admin/tariffs-v2/finance').status_code, 200)
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/main_admin/tariffs-v2/finance/run').status_code, 400)

    def test_admin_pages_render_failed_refund_and_claim(self):
        order = self.ready()
        claim = self.claim(order)
        respond_claim(claim.id, 'seller', self.seller.id, True, 'Да', self.now)
        db.session.flush()
        refund = TariffV2CheckoutRefund.query.one()
        simulate_refund(refund.id, False, 'admin:test', self.now)
        db.session.commit()
        self.admin()
        response = self.client.get('/main_admin/tariffs-v2/finance')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('Создать повторную заявку', response.get_data(as_text=True))

    def test_finance_cli_creates_intents_without_confirming_them(self):
        self.ready()
        result = self.app.test_cli_runner().invoke(args=['tariffs-v2-finance-tick'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('No external payments', result.output)
        self.assertEqual(TariffV2Invoice.query.count(), 0)
        self.app.config['TARIFFS_V2_SANDBOX'] = False
        result = self.app.test_cli_runner().invoke(args=['tariffs-v2-finance-tick'])
        self.assertNotEqual(result.exit_code, 0)

    def test_seller_cannot_pay_another_stores_bill_and_bill_details_render(self):
        order = self.ready()
        refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        self.confirm(refund)
        invoice = issue_monthly_invoice(self.seller.id, self.next_month())
        db.session.commit()
        self.login(self.owner)
        self.assertEqual(self.client.post(f'/tariffs-v2/seller/bills/{invoice.id}/simulate-payment').status_code, 400)
        self.assertIsNone(invoice.paid_at)
        self.login(self.seller)
        response = self.client.get('/tariffs-v2/seller/bills')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('возвращенный сбор 300.00', response.get_data(as_text=True))

    def test_seller_route_partial_cancel_and_buyer_refund_status(self):
        order = self.ready(quantity=2)
        self.login(self.seller)
        response = self.client.post(f'/tariffs-v2/orders/{order.id}/lines/{order.lines[0].id}/cancel', data={'quantity': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get('/tariffs-v2/seller/orders').status_code, 200)
        self.login(self.buyer)
        response = self.client.get(f'/tariffs-v2/checkouts/{order.checkout_id}')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('отменено 1', response.get_data(as_text=True))
        self.assertIn('Ожидает подтверждения', response.get_data(as_text=True))
