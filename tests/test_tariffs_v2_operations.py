import hashlib
import json
from datetime import timedelta
from decimal import Decimal
import unittest
from unittest.mock import Mock
from urllib.parse import unquote

from sqlalchemy import text
from werkzeug.datastructures import MultiDict

from app import db
from app.models.products import Product
from app.models.tariffs_v2 import TariffV2Invoice, TariffV2Seller, TariffV2GatewayNumber
from app.models.tariffs_v2_operations import TariffV2FiscalReceipt, TariffV2RefundDispatch, TariffV2BillPayment
from app.utils.tariffs_v2 import TariffError, moscow_date, stored_utc, blocked_for_new_sales, issue_monthly_invoice
from app.utils.tariffs_v2_bills import FLOW, issue_bill_payment, accept_bill_result
from app.utils.tariffs_v2_payments import payment_params
from app.utils.tariffs_v2_finance import cancel_line
from app.utils.tariffs_v2_provider import Credentials, RobokassaClient, REFUND_URL, OPERATION_URL, REFUND_STATE_URL
from app.utils.tariffs_v2_receipts import refund_receipt_items
from app.utils.tariffs_v2_refund_queue import enqueue_refund, dispatch_refund, poll_refund, stage_pending_refunds
import test_tariffs_v2_finance as foundation

REQUEST_ID = '68cd7fa6-1338-4745-ba5c-28d16cbcdb3d'


class OperationsTests(unittest.TestCase):
    setUp = foundation.FinanceTests.setUp
    tearDown = foundation.FinanceTests.tearDown
    login = foundation.FinanceTests.login
    add = foundation.FinanceTests.add
    checkout = foundation.FinanceTests.checkout
    callback = foundation.FinanceTests.callback
    pay = foundation.FinanceTests.pay
    ready = foundation.FinanceTests.ready
    next_month = foundation.FinanceTests.next_month

    def bill(self):
        invoice = TariffV2Invoice(seller_id=self.seller.id, period=moscow_date(self.now).replace(day=1),
            issued_at=stored_utc(self.now), due_at=stored_utc(self.now), amount=300)
        db.session.add(invoice)
        db.session.commit()
        return invoice

    def bill_callback(self, payment, amount=None):
        amount = amount or format(payment.amount, '.2f')
        body = f'{amount}:{payment.gateway_id}:v2-test-two:Shp_flow={FLOW}:Shp_token={payment.token}'
        return MultiDict(dict(InvId=str(payment.gateway_id), OutSum=amount, Shp_flow=FLOW,
            Shp_token=payment.token, SignatureValue=hashlib.sha256(body.encode()).hexdigest()))

    def queued(self):
        order = self.ready()
        refund = cancel_line(order.lines[0].id, self.seller.id, 1, self.now)
        db.session.commit()
        row = enqueue_refund(refund.id, self.now)
        db.session.commit()
        return row

    def provider_client(self, dispatch):
        self.provider_state = 'processing'
        def send(method, url, body=None):
            if url.startswith(OPERATION_URL):
                return (f'<OperationStateResponse><Result><Code>0</Code></Result><State><Code>100</Code></State>'
                    f'<Info><OutSum>{dispatch.payment_amount}</OutSum><OpKey>operation-123</OpKey></Info><UserFields>'
                    f'<Field><Name>Shp_flow</Name><Value>{dispatch.payment_flow}</Value></Field>'
                    f'<Field><Name>Shp_token</Name><Value>{dispatch.payment_token}</Value></Field>'
                    '</UserFields></OperationStateResponse>').encode()
            if url == REFUND_URL:
                return json.dumps({'success': True, 'requestId': REQUEST_ID}).encode()
            if url.startswith(REFUND_STATE_URL):
                return json.dumps({'requestId': REQUEST_ID, 'amount': float(dispatch.amount), 'label': self.provider_state}).encode()
            raise AssertionError('Unexpected provider request')
        return RobokassaClient(Credentials(dispatch.merchant_login, 'fake-two', 'fake-three', test_mode=False), Mock(side_effect=send))

    def test_bill_form_has_signed_frozen_receipt_and_reuses_pending_payment(self):
        invoice = self.bill()
        payment = issue_bill_payment(invoice.id, self.seller.id, self.now)
        db.session.commit()
        self.assertEqual(issue_bill_payment(invoice.id, self.seller.id, self.now).id, payment.id)
        params = payment_params(payment, FLOW, 'test', self.now)
        receipt = json.loads(unquote(params['Receipt']))
        self.assertEqual(receipt['items'][0]['payment_object'], 'service')
        self.assertEqual(receipt['items'][0]['sum'], 300)
        expected = hashlib.sha256(f"v2-test-shop:300.00:{payment.gateway_id}:{params['Receipt']}:v2-test-one:Shp_flow={FLOW}:Shp_token={payment.token}".encode()).hexdigest()
        self.assertEqual(params['SignatureValue'], expected)
        self.assertEqual(params['IsTest'], '1')
        self.assertIsNone(invoice.paid_at)

    def test_bill_signed_confirmation_unblocks_once_including_late_payment(self):
        invoice = self.bill()
        payment = issue_bill_payment(invoice.id, self.seller.id, self.now)
        db.session.commit()
        self.assertTrue(blocked_for_new_sales(self.seller.id, self.now))
        late = self.now + timedelta(minutes=31)
        for _ in range(2):
            accept_bill_result(self.bill_callback(payment), late)
            db.session.commit()
        self.assertEqual(payment.status, 'paid')
        self.assertIsNotNone(invoice.paid_at)
        self.assertFalse(blocked_for_new_sales(self.seller.id, late))
        self.assertEqual(TariffV2BillPayment.query.count(), 1)

    def test_second_paid_attempt_is_reviewed_instead_of_settling_twice(self):
        invoice = self.bill()
        first = issue_bill_payment(invoice.id, self.seller.id, self.now)
        db.session.commit()
        second = issue_bill_payment(invoice.id, self.seller.id, self.now + timedelta(minutes=31))
        db.session.commit()
        accept_bill_result(self.bill_callback(second), self.now + timedelta(minutes=32))
        db.session.commit()
        accepted_reference = invoice.payment_reference
        accept_bill_result(self.bill_callback(first), self.now + timedelta(minutes=33))
        db.session.commit()
        self.assertEqual(first.status, 'needs_review')
        self.assertEqual(invoice.payment_reference, accepted_reference)

    def test_late_first_payment_retires_newer_unpaid_form(self):
        invoice = self.bill()
        first = issue_bill_payment(invoice.id, self.seller.id, self.now)
        db.session.commit()
        second = issue_bill_payment(invoice.id, self.seller.id, self.now + timedelta(minutes=31))
        db.session.commit()
        accept_bill_result(self.bill_callback(first), self.now + timedelta(minutes=32))
        db.session.commit()
        self.assertEqual(first.status, 'paid')
        self.assertEqual(second.status, 'expired')
        self.assertIsNone(second.active_invoice_id)

    def test_bill_callback_scope_amount_and_owner_enforced(self):
        invoice = self.bill()
        with self.assertRaises(TariffError):
            issue_bill_payment(invoice.id, self.owner.id, self.now)
        payment = issue_bill_payment(invoice.id, self.seller.id, self.now)
        db.session.commit()
        with self.assertRaises(TariffError):
            accept_bill_result(self.bill_callback(payment, '301.00'), self.now)
        self.login(self.owner)
        self.assertEqual(self.client.get(f'/tariffs-v2/seller/bill-payments/{payment.id}').status_code, 404)
        self.login(self.seller)
        self.assertEqual(self.client.get(f'/tariffs-v2/seller/bill-payments/{payment.id}').status_code, 200)
        response = self.client.post('/tariffs-v2/robokassa/result', data=self.bill_callback(payment))
        self.assertEqual(response.status_code, 200, response.data)

    def test_mixed_receipt_contains_owner_goods_and_only_one_platform_service(self):
        self.add()
        self.add(self.own_product)
        row = self.checkout()
        receipt = json.loads(row.receipt_json)
        self.assertEqual(sum(Decimal(str(item['sum'])) for item in receipt['items']), Decimal('1300'))
        self.assertEqual([item['name'] for item in receipt['items']], ['Owner goods', 'Услуга доступа к площадке'])
        self.assertNotIn('Product', row.receipt_json)
        self.assertEqual(receipt['items'][0]['sum'], 1000)
        self.assertEqual(receipt['items'][0]['payment_object'], 'commodity')
        self.assertEqual(receipt['items'][1]['sum'], 300)
        self.assertEqual(row.cod_amount, 10000)

    def test_other_seller_cannot_enable_full_goods_collection_through_policy(self):
        db.session.add(TariffV2Seller(seller_id=self.seller.id, payment_mode='full_online'))
        db.session.commit()
        self.add()
        row = self.checkout()
        self.assertEqual(row.online_amount, 300)
        self.assertEqual(row.cod_amount, 10000)
        self.assertEqual(len(json.loads(row.receipt_json)['items']), 1)
        self.assertNotIn('Product', row.receipt_json)

    def test_multiple_other_products_are_aggregated_into_one_access_service(self):
        extra = Product(seller=self.seller, category=self.category, name='Other secret item', slug='extra',
            article='extra', price=1000, current_discount=0, stock_quantity=5, status='approved')
        db.session.add(extra)
        db.session.flush()
        from app.utils.tariffs_v2_commerce import set_publication
        set_publication(extra.id, self.seller.id, True, self.now)
        db.session.commit()
        self.add()
        self.add(extra)
        row = self.checkout()
        receipt = json.loads(row.receipt_json)
        self.assertEqual(len(receipt['items']), 1)
        self.assertEqual(receipt['items'][0]['sum'], 330)
        self.assertNotIn('secret', row.receipt_json)

    def test_store_pause_blocks_new_checkout_but_accepts_existing_signed_payment(self):
        self.add()
        row = self.checkout()
        self.seller.payments_paused = True
        db.session.commit()
        from app.utils.tariffs_v2_commerce import is_visible
        self.assertFalse(is_visible(self.product, self.now))
        self.pay(row)
        self.assertEqual(row.status, 'complete')
        self.assertEqual(row.orders[0].status, 'ready')

    def test_refund_uses_original_service_receipt_and_does_not_include_seller_goods(self):
        row = self.queued()
        self.assertEqual(row.receipt_items[0]['Name'], 'Услуга доступа к площадке')
        self.assertEqual(row.receipt_items[0]['Cost'], 300)
        self.assertEqual(row.receipt_items[0]['PaymentObject'], 'service')
        self.assertEqual(enqueue_refund(row.refund_id, self.now).id, row.id)
        self.assertEqual(stage_pending_refunds(self.now)['refunds_enqueued'], 0)

    def test_owner_refund_retains_actual_goods_nomenclature(self):
        order = self.ready(quantity=2, own=True)
        refund = cancel_line(order.lines[0].id, self.owner.id, 1, self.now)
        db.session.commit()
        items = refund_receipt_items(refund)
        self.assertEqual(items[0]['Name'], 'Owner goods')
        self.assertEqual(items[0]['Quantity'], 1)
        self.assertEqual(items[0]['Cost'], '1000.00')
        self.assertEqual(items[0]['PaymentObject'], 'commodity')
        self.assertEqual(items[0]['PaymentMethod'], 'full_prepayment')

    def test_seller_refund_cannot_exceed_his_fee_even_in_aggregated_receipt(self):
        row = self.queued()
        row.refund.amount = Decimal('301')
        db.session.flush()
        with self.assertRaises(TariffError):
            refund_receipt_items(row.refund)

    def test_gateway_constraint_upgrade_preserves_numbers_and_supports_bills(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from app.tariffs_v2_schema import upgrade_gateway_purpose
        self.add()
        checkout = self.checkout()
        number = checkout.gateway_id
        db.session.commit()
        # Only this throwaway in-memory database is rebuilt. The production
        # PostgreSQL migration uses ALTER CONSTRAINT and never disables FKs.
        with db.engine.connect() as connection:
            connection.execute(text('PRAGMA foreign_keys=OFF'))
            connection.commit()
            try:
                with connection.begin():
                    ops = Operations(MigrationContext.configure(connection))
                    with ops.batch_alter_table('tariff_v2_gateway_numbers', recreate='always', table_kwargs={'sqlite_autoincrement': True}) as batch:
                        batch.drop_constraint('v2_gateway_purpose', type_='check')
                        batch.create_check_constraint('v2_gateway_purpose', "purpose IN ('storage','checkout')")
                    upgrade_gateway_purpose(connection)
                    upgrade_gateway_purpose(connection)
                self.assertEqual(connection.execute(text('PRAGMA foreign_key_check')).all(), [])
                connection.commit()
            finally:
                connection.execute(text('PRAGMA foreign_keys=ON'))
                connection.commit()
        db.session.expire_all()
        self.assertEqual(checkout.gateway_id, number)
        invoice = self.bill()
        payment = issue_bill_payment(invoice.id, self.seller.id, self.now)
        db.session.commit()
        self.assertGreater(payment.gateway_id, number)
        self.assertEqual(db.session.get(TariffV2GatewayNumber, payment.gateway_id).purpose, 'bill')

    def test_refund_debt_appears_only_after_finished_and_duplicate_poll_is_safe(self):
        row = self.queued()
        client = self.provider_client(row)
        dispatch_refund(row.id, client, self.now, allow_simulated=True)
        self.assertEqual(row.status, 'submitted')
        self.assertEqual(row.refund.status, 'pending')
        self.assertIsNone(issue_monthly_invoice(self.seller.id, self.next_month()))
        poll_refund(row.id, client, self.now, allow_simulated=True)
        self.assertEqual(row.status, 'submitted')
        self.provider_state = 'finished'
        for _ in range(2):
            poll_refund(row.id, client, self.now, allow_simulated=True)
        self.assertEqual(row.refund.line.refunded_fee, 300)
        self.assertEqual(issue_monthly_invoice(self.seller.id, self.next_month()).amount, 300)
        self.assertEqual(sum(call.args[0] == 'POST' for call in client.send.call_args_list), 1)

    def test_unknown_post_result_never_resends_and_never_creates_debt(self):
        row = self.queued()
        client = self.provider_client(row)
        original_send = client.send.side_effect
        client.send.side_effect = lambda method, url, body=None: (_ for _ in ()).throw(TimeoutError()) if method == 'POST' else original_send(method, url, body)
        dispatch_refund(row.id, client, self.now, allow_simulated=True)
        self.assertEqual(row.status, 'unknown')
        dispatch_refund(row.id, client, self.now + timedelta(minutes=10), allow_simulated=True)
        self.assertEqual(sum(call.args[0] == 'POST' for call in client.send.call_args_list), 1)
        self.assertEqual(row.refund.status, 'pending')
        self.assertIsNone(issue_monthly_invoice(self.seller.id, self.next_month()))

    def test_recovery_of_interrupted_sending_parks_for_review(self):
        row = self.queued()
        row.status, row.sending_at = 'sending', stored_utc(self.now)
        db.session.commit()
        client = self.provider_client(row)
        dispatch_refund(row.id, client, self.now + timedelta(minutes=3), allow_simulated=True)
        self.assertEqual(row.status, 'unknown')
        client.send.assert_not_called()

    def test_test_queue_cannot_use_actual_transport(self):
        row = self.queued()
        client = RobokassaClient(Credentials(row.merchant_login, 'fake', 'fake', test_mode=False))
        with self.assertRaises(TariffError):
            dispatch_refund(row.id, client, self.now, allow_simulated=True)

    def test_failed_poll_keeps_pending_and_canceled_refund_never_billed(self):
        row = self.queued()
        client = self.provider_client(row)
        dispatch_refund(row.id, client, self.now, allow_simulated=True)
        original_send = client.send.side_effect
        client.send.side_effect = TimeoutError()
        poll_refund(row.id, client, self.now, allow_simulated=True)
        self.assertEqual(row.status, 'submitted')
        self.assertEqual(row.refund.status, 'pending')
        client.send.side_effect = original_send
        self.provider_state = 'canceled'
        poll_refund(row.id, client, self.now, allow_simulated=True)
        self.assertEqual(row.refund.status, 'failed')
        self.assertIsNone(issue_monthly_invoice(self.seller.id, self.next_month()))
