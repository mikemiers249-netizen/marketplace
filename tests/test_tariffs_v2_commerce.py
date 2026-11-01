import unittest
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text

from app import db
from app.models.tariffs_v2 import TariffV2Coverage, TariffV2GatewayNumber, TariffV2Invoice
from app.models.tariffs_v2_commerce import TariffV2Listing
from app.utils.tariffs_v2 import TariffError, next_month, moscow_date, stored_utc, storage_quote
from app.utils.tariffs_v2_commerce import is_visible, set_publication
from app.utils.tariffs_v2_payments import accept_storage_result, allocate_gateway_number, gateway_params
import test_tariffs_v2_payments as payments


class PublicationTests(unittest.TestCase):
    setUp = payments.StoragePaymentTests.setUp
    tearDown = payments.StoragePaymentTests.tearDown
    login = payments.StoragePaymentTests.login
    issue = payments.StoragePaymentTests.issue
    callback = payments.StoragePaymentTests.callback

    def publish(self):
        return set_publication(self.product.id, self.seller.id, True, self.now)

    def approve(self):
        self.product.status = 'approved'
        db.session.commit()

    def pay(self):
        invoice = self.issue()
        accept_storage_result(self.callback(invoice), self.now)
        db.session.commit()
        return invoice

    def test_payment_does_not_bypass_moderation(self):
        self.pay()
        with self.assertRaises(TariffError):
            self.publish()
        self.assertFalse(is_visible(self.product, self.now))
        self.assertEqual(self.product.status, 'draft')

    def test_prepaid_publication_and_hiding_preserve_legacy_state_and_coverage(self):
        self.approve()
        with self.assertRaises(TariffError):
            self.publish()
        self.pay()
        self.publish()
        db.session.flush()
        self.assertTrue(is_visible(self.product, self.now))
        set_publication(self.product.id, self.seller.id, False, self.now)
        self.assertFalse(is_visible(self.product, self.now))
        self.assertEqual(TariffV2Coverage.query.count(), 1)
        self.assertEqual(storage_quote(self.product, self.now)['due'], 0)
        self.assertEqual(self.product.status, 'approved')
        self.assertEqual(self.product.stock_quantity, 10)

    def test_next_month_and_category_price_increase_hide_without_deleting(self):
        self.approve()
        self.pay()
        self.publish()
        db.session.flush()
        self.fixed.amount = Decimal('620')
        self.assertFalse(is_visible(self.product, self.now))
        self.fixed.amount = Decimal('310')
        from app.utils.tariffs_v2 import midnight_utc
        following = midnight_utc(next_month(moscow_date(self.now)))
        self.assertFalse(is_visible(self.product, following))
        self.assertTrue(db.session.get(TariffV2Listing, self.product.id).published)
        self.assertEqual(self.product.status, 'approved')

    def test_hiding_does_not_cancel_future_storage_charge(self):
        self.approve()
        self.pay()
        self.publish()
        set_publication(self.product.id, self.seller.id, False, self.now)
        from app.utils.tariffs_v2 import midnight_utc
        following = midnight_utc(next_month(moscow_date(self.now)))
        self.assertEqual(storage_quote(self.product, following)['due'], Decimal('310'))

    def test_debt_and_inactive_shop_hide_existing_listing(self):
        self.approve()
        self.mapping.rule = self.rule
        self.publish()
        db.session.flush()
        self.seller.is_active = False
        self.assertFalse(is_visible(self.product, self.now))
        self.seller.is_active = True
        invoice = TariffV2Invoice(seller_id=self.seller.id, period=moscow_date(self.now).replace(day=1),
            issued_at=stored_utc(self.now - timedelta(days=5)), due_at=stored_utc(self.now), amount=100)
        db.session.add(invoice)
        db.session.flush()
        self.assertFalse(is_visible(self.product, self.now))
        with self.assertRaises(TariffError):
            self.publish()
        set_publication(self.product.id, self.seller.id, False, self.now)

    def test_publication_route_requires_owner_and_csrf(self):
        self.approve()
        self.mapping.rule = self.rule
        db.session.commit()
        path = f'/tariffs-v2/seller/storage/{self.product.id}/publication'
        self.login(self.buyer)
        self.assertEqual(self.client.post(path, data={'published': '1'}).status_code, 403)
        self.login(self.owner)
        self.assertEqual(self.client.post(path, data={'published': '1'}).status_code, 400)
        self.login(self.seller)
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post(path, data={'published': '1'}).status_code, 400)
        self.assertIsNone(db.session.get(TariffV2Listing, self.product.id))

    def test_payment_numbers_are_shared_and_callback_rejects_other_purpose(self):
        checkout_number = allocate_gateway_number('checkout', 1)
        invoice = self.issue()
        self.assertNotEqual(invoice.gateway_id, checkout_number)
        self.assertNotEqual(invoice.gateway_id, invoice.id)
        self.assertEqual(gateway_params(invoice, self.now)['InvId'], str(invoice.gateway_id))
        data = self.callback(invoice)
        db.session.get(TariffV2GatewayNumber, invoice.gateway_id).purpose = 'checkout'
        db.session.get(TariffV2GatewayNumber, invoice.gateway_id).reference_id = 2
        db.session.flush()
        with self.assertRaises(TariffError):
            accept_storage_result(data, self.now)
        self.assertEqual(TariffV2Coverage.query.count(), 0)

    def test_catalog_shows_total_and_split_without_changing_stock(self):
        self.approve()
        self.mapping.rule = self.rule
        self.publish()
        db.session.commit()
        response = self.client.get('/tariffs-v2/catalog')
        self.assertEqual(response.status_code, 200)
        self.assertIn('10300.00', response.get_data(as_text=True))
        self.assertIn('300.00', response.get_data(as_text=True))
        self.assertEqual(self.product.stock_quantity, 10)
        self.product.status = 'rejected'
        db.session.commit()
        self.assertNotIn('<h2>Product</h2>', self.client.get('/tariffs-v2/catalog').get_data(as_text=True))

    def test_local_upgrade_backfills_old_provider_number_and_is_repeatable(self):
        invoice = self.issue()
        old_number = invoice.gateway_id
        db.session.execute(text('UPDATE tariff_v2_storage_invoices SET gateway_id=NULL'))
        db.session.execute(text('DELETE FROM tariff_v2_gateway_numbers'))
        db.session.commit()
        for _ in range(2):
            result = self.app.test_cli_runner().invoke(args=['tariffs-v2-init-sandbox'])
            self.assertEqual(result.exit_code, 0, result.output)
        db.session.expire_all()
        self.assertEqual(invoice.gateway_id, old_number)
        self.assertEqual(TariffV2GatewayNumber.query.count(), 1)
        self.assertGreater(allocate_gateway_number('checkout', 1), old_number)
        accept_storage_result(self.callback(invoice), self.now)
        self.assertEqual(invoice.status, 'paid')

    def test_upgrade_collision_fails_without_reassigning_existing_number(self):
        invoice = self.issue()
        db.session.execute(text('UPDATE tariff_v2_storage_invoices SET gateway_id=NULL'))
        db.session.get(TariffV2GatewayNumber, invoice.id).purpose = 'checkout'
        db.session.commit()
        result = self.app.test_cli_runner().invoke(args=['tariffs-v2-init-sandbox'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('collision', result.output)
        db.session.expire_all()
        self.assertIsNone(invoice.gateway_id)
        self.assertEqual(TariffV2GatewayNumber.query.one().purpose, 'checkout')
