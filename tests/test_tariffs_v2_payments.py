import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import g
from werkzeug.datastructures import MultiDict

from app import db
from app.models.orders import Order
from app.models.payments import RobokassaPayment
from app.models.tariffs_v2 import TariffV2Audit, TariffV2Coverage, TariffV2StorageInvoice
from app.utils.tariffs_v2 import TariffError, storage_quote
from app.utils.tariffs_v2_payments import (
    FLOW, accept_storage_result, configured, gateway_params, issue_storage_invoice,
)
import test_tariffs_v2 as foundation


class StoragePaymentTests(unittest.TestCase):
    def setUp(self):
        foundation.TariffsV2Tests.setUp(self)
        self.app.config.update(
            TARIFFS_V2_PAYMENT_TEST_ENABLED=True,
            TARIFFS_V2_ROBOKASSA_TEST_LOGIN='v2-test-shop',
            TARIFFS_V2_ROBOKASSA_TEST_PASSWORD1='v2-test-one',
            TARIFFS_V2_ROBOKASSA_TEST_PASSWORD2='v2-test-two',
            TARIFFS_V2_ROBOKASSA_HASH='sha256',
        )
        self.mapping.rule = self.fixed
        self.product.status = 'draft'
        db.session.commit()
        self.now = datetime.now(timezone.utc)

    tearDown = foundation.TariffsV2Tests.tearDown

    def login(self, user):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = user.get_id()
            session['_fresh'] = True

    def issue(self, at=None):
        row = issue_storage_invoice(self.product.id, self.seller.id, at or self.now)
        db.session.commit()
        return row

    def callback(self, invoice, amount=None, password=None):
        amount = amount or format(invoice.amount, '.2f')
        password = password or self.app.config['TARIFFS_V2_ROBOKASSA_TEST_PASSWORD2']
        text = f'{amount}:{invoice.gateway_id}:{password}:Shp_flow={FLOW}:Shp_token={invoice.token}'
        signature = hashlib.new(self.app.config['TARIFFS_V2_ROBOKASSA_HASH'], text.encode()).hexdigest()
        return MultiDict(dict(OutSum=amount, InvId=str(invoice.gateway_id), SignatureValue=signature,
                              Shp_flow=FLOW, Shp_token=invoice.token))

    def test_invoice_is_frozen_and_double_click_reuses_it(self):
        invoice = self.issue()
        second = self.issue()
        self.assertEqual(invoice.id, second.id)
        self.assertEqual(TariffV2StorageInvoice.query.count(), 1)
        self.assertEqual(invoice.amount, storage_quote(self.product, self.now)['due'])
        self.assertTrue(invoice.is_test)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_changed_rate_replaces_pending_invoice_without_overwriting_history(self):
        first = self.issue()
        amount = first.amount
        self.fixed.amount = Decimal('620')
        db.session.commit()
        second = self.issue()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.status, 'expired')
        self.assertIsNone(first.active_product_id)
        self.assertEqual(first.amount, amount)
        self.assertEqual(second.amount, amount * 2)

    def test_zero_storage_amount_never_creates_gateway_invoice(self):
        self.product.seller = self.owner
        db.session.commit()
        result = issue_storage_invoice(self.product.id, self.owner.id, self.now)
        self.assertIsNone(result)
        self.assertEqual(TariffV2StorageInvoice.query.count(), 0)

    def test_dedicated_test_credentials_are_required_no_legacy_fallback(self):
        self.app.config.update(TARIFFS_V2_ROBOKASSA_TEST_PASSWORD2='',
                               ROBOKASSA_TEST_PASSWORD2='legacy-test-secret',
                               ROBOKASSA_PASSWORD2='legacy-live-secret')
        self.assertFalse(configured())
        with self.assertRaises(TariffError):
            self.issue()
        self.assertEqual(TariffV2StorageInvoice.query.count(), 0)

    def test_gateway_forces_test_mode_and_signs_scope_and_token(self):
        invoice = self.issue()
        params = gateway_params(invoice, self.now)
        self.assertEqual(params['IsTest'], '1')
        self.assertEqual(params['Shp_flow'], FLOW)
        self.assertEqual(params['Shp_token'], invoice.token)
        text = f"v2-test-shop:{params['OutSum']}:{invoice.gateway_id}:v2-test-one:Shp_flow={FLOW}:Shp_token={invoice.token}"
        self.assertEqual(params['SignatureValue'], hashlib.sha256(text.encode()).hexdigest())
        self.assertNotIn('v2-test-one', str(params))
        self.assertNotIn('v2-test-two', str(params))

    def test_signed_result_records_test_coverage_once_and_never_publishes(self):
        invoice = self.issue()
        data = self.callback(invoice)
        for _ in range(2):
            response = self.client.post('/tariffs-v2/robokassa/result', data=data)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.get_data(as_text=True), f'OK{invoice.gateway_id}')
        self.assertEqual(invoice.status, 'paid')
        self.assertIsNone(invoice.active_product_id)
        self.assertEqual(TariffV2Coverage.query.count(), 1)
        self.assertTrue(TariffV2Coverage.query.one().is_test)
        self.assertEqual(storage_quote(self.product)['due'], 0)
        self.assertEqual(self.product.status, 'draft')
        self.assertEqual(Order.query.count(), 0)
        self.assertEqual(RobokassaPayment.query.count(), 0)
        self.assertEqual(TariffV2Audit.query.filter_by(action='storage_test_payment_confirmed').count(), 1)

    def test_test_coverage_is_ignored_outside_sandbox(self):
        invoice = self.issue()
        accept_storage_result(self.callback(invoice), self.now)
        db.session.commit()
        self.assertEqual(storage_quote(self.product, self.now)['due'], 0)
        self.app.config['TARIFFS_V2_SANDBOX'] = False
        self.assertEqual(storage_quote(self.product, self.now)['due'], invoice.amount)

    def test_browser_success_redirect_never_confirms_payment(self):
        invoice = self.issue()
        response = self.client.get('/tariffs-v2/robokassa/success', query_string=self.callback(invoice))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invoice.status, 'pending')
        self.assertEqual(TariffV2Coverage.query.count(), 0)
        self.assertEqual(self.client.post('/tariffs-v2/robokassa/fail', data=self.callback(invoice)).status_code, 200)
        self.assertEqual(invoice.status, 'pending')

    def test_seller_cannot_confirm_payment_using_password_one(self):
        invoice = self.issue()
        response = self.client.post('/tariffs-v2/robokassa/result',
                                     data=self.callback(invoice, password='v2-test-one'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TariffV2Coverage.query.count(), 0)
        self.assertIsNone(invoice.paid_at)

    def test_wrong_amount_or_fractional_kopeck_is_rejected(self):
        invoice = self.issue()
        for amount in (str(invoice.amount + Decimal('1')), str(invoice.amount + Decimal('0.001')),
                       'NaN', '1e2', '-1'):
            response = self.client.post('/tariffs-v2/robokassa/result', data=self.callback(invoice, amount=amount))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_duplicate_fields_extra_scope_and_live_flag_rejected(self):
        invoice = self.issue()
        bad = []
        duplicate = self.callback(invoice)
        duplicate.add('InvId', str(invoice.id))
        bad.append(duplicate)
        extra = self.callback(invoice)
        extra['Shp_extra'] = 'x'
        bad.append(extra)
        live = self.callback(invoice)
        live['IsTest'] = '0'
        bad.append(live)
        for data in bad:
            self.assertEqual(self.client.post('/tariffs-v2/robokassa/result', data=data).status_code, 400)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_token_swap_cannot_pay_another_invoice(self):
        invoice = self.issue()
        data = self.callback(invoice)
        data['Shp_token'] = '0' * 48
        self.assertEqual(self.client.post('/tariffs-v2/robokassa/result', data=data).status_code, 400)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_expired_paid_invoice_is_acknowledged_but_needs_review(self):
        invoice = self.issue()
        paid_at = self.now + timedelta(minutes=31)
        result = accept_storage_result(self.callback(invoice), paid_at)
        db.session.commit()
        self.assertEqual(result.status, 'needs_review')
        self.assertEqual(result.review_reason, 'expired')
        self.assertIsNotNone(result.paid_at)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_invoice_cannot_cross_moscow_midnight(self):
        before = foundation.instant('2026-01-31T20:55:00')
        invoice = self.issue(before)
        self.assertEqual(invoice.expires_at, foundation.instant('2026-01-31T21:00:00').replace(tzinfo=None))
        with self.assertRaises(TariffError):
            gateway_params(invoice, foundation.instant('2026-01-31T21:00:00'))
        accept_storage_result(self.callback(invoice), foundation.instant('2026-01-31T21:00:00'))
        self.assertEqual(invoice.status, 'needs_review')
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_changed_tariff_after_payment_form_needs_review(self):
        invoice = self.issue()
        original = invoice.amount
        self.fixed.amount = 620
        db.session.commit()
        accept_storage_result(self.callback(invoice), self.now)
        self.assertEqual(invoice.status, 'needs_review')
        self.assertEqual(invoice.review_reason, 'quote_changed')
        self.assertEqual(invoice.amount, original)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_deleted_card_callback_does_not_restore_it(self):
        invoice = self.issue()
        self.product.status = 'deleted'
        db.session.commit()
        accept_storage_result(self.callback(invoice), self.now)
        self.assertEqual(invoice.status, 'needs_review')
        self.assertEqual(invoice.review_reason, 'product_unavailable')
        self.assertEqual(self.product.status, 'deleted')
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_another_merchant_cannot_reuse_invoice(self):
        invoice = self.issue()
        self.app.config['TARIFFS_V2_ROBOKASSA_TEST_LOGIN'] = 'different-test-shop'
        with self.assertRaises(TariffError):
            gateway_params(invoice, self.now)
        with self.assertRaises(TariffError):
            accept_storage_result(self.callback(invoice), self.now)

    def test_invoice_and_payment_pages_are_seller_owned(self):
        invoice = self.issue()
        self.assertEqual(self.client.get('/tariffs-v2/seller/storage').status_code, 403)
        self.login(self.buyer)
        self.assertEqual(self.client.get('/tariffs-v2/seller/storage').status_code, 403)
        self.login(self.owner)
        self.assertEqual(self.client.get(f'/tariffs-v2/seller/invoices/{invoice.id}').status_code, 404)
        self.assertEqual(self.client.post(f'/tariffs-v2/seller/storage/{self.product.id}/invoice').status_code, 404)
        self.login(self.seller)
        response = self.client.get(f'/tariffs-v2/seller/invoices/{invoice.id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="IsTest" value="1"', response.get_data(as_text=True))

    def test_seller_can_create_invoice_and_view_storage_page(self):
        self.login(self.seller)
        self.assertEqual(self.client.get('/tariffs-v2/seller/storage').status_code, 200)
        response = self.client.post(f'/tariffs-v2/seller/storage/{self.product.id}/invoice')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/tariffs-v2/seller/invoices/', response.location)
        again = self.client.post(f'/tariffs-v2/seller/storage/{self.product.id}/invoice')
        self.assertEqual(response.location, again.location)
        self.assertEqual(TariffV2StorageInvoice.query.count(), 1)

    def test_csrf_protects_invoice_creation_but_signed_callback_is_exempt(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.login(self.seller)
        response = self.client.post(f'/tariffs-v2/seller/storage/{self.product.id}/invoice')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TariffV2StorageInvoice.query.count(), 0)
        invoice = self.issue()
        response = self.client.post('/tariffs-v2/robokassa/result', data=self.callback(invoice))
        self.assertEqual(response.status_code, 200)

    def test_v2_callback_cannot_confirm_legacy_payments(self):
        invoice = self.issue()
        response = self.client.post('/payments/robokassa/result', data=self.callback(invoice))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(TariffV2Coverage.query.count(), 0)
        old_shape = MultiDict(dict(InvId=str(invoice.id), OutSum=str(invoice.amount), SignatureValue='0' * 64))
        self.assertEqual(self.client.post('/tariffs-v2/robokassa/result', data=old_shape).status_code, 400)

    def test_payment_switch_off_prevents_invoice_creation(self):
        self.app.config['TARIFFS_V2_PAYMENT_TEST_ENABLED'] = False
        self.login(self.seller)
        response = self.client.post(f'/tariffs-v2/seller/storage/{self.product.id}/invoice')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(TariffV2StorageInvoice.query.count(), 0)

    def test_phase_one_sandbox_schema_can_be_upgraded_twice(self):
        from sqlalchemy import inspect, text
        db.session.execute(text('ALTER TABLE tariff_v2_coverage DROP COLUMN is_test'))
        db.session.commit()
        self.assertNotIn('is_test', {column['name'] for column in inspect(db.engine).get_columns('tariff_v2_coverage')})
        for _ in range(2):
            result = self.app.test_cli_runner().invoke(args=['tariffs-v2-init-sandbox'])
            self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('is_test', {column['name'] for column in inspect(db.engine).get_columns('tariff_v2_coverage')})

    def test_admin_can_see_review_queue_without_payment_secrets(self):
        invoice = self.issue()
        accept_storage_result(self.callback(invoice), self.now + timedelta(minutes=31))
        db.session.commit()
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        page = self.client.get('/main_admin/tariffs-v2/')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Нужна проверка', html)
        self.assertNotIn(invoice.token, html)
        self.assertNotIn('v2-test-two', html)


if __name__ == '__main__':
    unittest.main()
