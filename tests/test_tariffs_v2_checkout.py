import hashlib
import unittest
from datetime import timedelta
from decimal import Decimal

from werkzeug.datastructures import MultiDict

from app import db
from app.models.users import Buyer, BuyerDelivery, DeliveryService, SellerDelivery
from app.models.products import Product
from app.models.orders import Order, CartItem
from app.models.payments import RobokassaPayment
from app.models.digital import DigitalBook, DigitalPurchase
from app.models.tariffs_v2 import TariffV2Seller, TariffV2Invoice
from app.models.tariffs_v2_commerce import (
    TariffV2Cart, TariffV2Checkout, TariffV2CheckoutRequest, TariffV2Reservation,
    TariffV2SandboxOrder,
)
from app.utils.tariffs_v2 import TariffError, stored_utc, moscow_date
from app.utils.tariffs_v2_commerce import (
    CHECKOUT_FLOW, accept_checkout_result, available_quantity, create_checkout,
    set_cart_quantity, set_publication, ship_order,
)
from app.utils.tariffs_v2_payments import accept_storage_result, payment_params
import test_tariffs_v2_payments as payments


class CheckoutTests(unittest.TestCase):
    tearDown = payments.StoragePaymentTests.tearDown
    login = payments.StoragePaymentTests.login

    def setUp(self):
        payments.StoragePaymentTests.setUp(self)
        self.product.status = 'approved'
        self.mapping.rule = self.rule
        self.own_product = Product(seller=self.owner, category=self.category, name='Owner goods',
            slug='owner-goods', article='owner-goods', price=1000, current_discount=0,
            stock_quantity=5, status='approved')
        self.service = DeliveryService(name='CDEK', code='cdek')
        self.address = BuyerDelivery(buyer=self.buyer, delivery_service=self.service, pvz_city='Москва',
            pvz_address='ПВЗ 1', recipient_name='Получатель', recipient_phone='+79991234567')
        self.second_buyer = Buyer(login='second', email='second@example.test', password_hash='test')
        db.session.add_all([self.own_product, self.service, self.address, self.second_buyer])
        db.session.flush()
        for seller in (self.seller, self.owner):
            db.session.add(SellerDelivery(seller_id=seller.id, delivery_service_id=self.service.id))
        set_publication(self.product.id, self.seller.id, True, self.now)
        set_publication(self.own_product.id, self.owner.id, True, self.now)
        db.session.commit()
        self.deliveries = {self.seller.id: self.address.id, self.owner.id: self.address.id}

    def add(self, product=None, quantity=1, buyer=None):
        set_cart_quantity((buyer or self.buyer).id, (product or self.product).id, quantity, self.now)
        db.session.commit()

    def checkout(self, key='a' * 48, buyer=None, at=None, deliveries=None):
        row = create_checkout((buyer or self.buyer).id, key,
            self.deliveries if deliveries is None else deliveries, at or self.now)
        db.session.commit()
        return row

    def callback(self, row, amount=None, flow=CHECKOUT_FLOW):
        amount = amount or format(row.online_amount, '.2f')
        text = f'{amount}:{row.gateway_id}:v2-test-two:Shp_flow={flow}:Shp_token={row.token}'
        return MultiDict(dict(InvId=str(row.gateway_id), OutSum=amount, Shp_flow=flow,
            Shp_token=row.token, SignatureValue=hashlib.sha256(text.encode()).hexdigest()))

    def pay(self, row, at=None):
        result = accept_checkout_result(self.callback(row), at or self.now)
        db.session.commit()
        return result

    def test_mixed_checkout_collects_owner_goods_and_other_fee_once(self):
        self.add()
        self.add(self.own_product)
        row = self.checkout()
        self.assertEqual(row.online_amount, Decimal('1300'))
        self.assertEqual(row.cod_amount, Decimal('10000'))
        self.assertEqual(len(row.orders), 2)
        self.assertTrue(all(order.status == 'waiting_payment' for order in row.orders))
        self.assertEqual(TariffV2Cart.query.count(), 2)
        self.assertEqual(payment_params(row, CHECKOUT_FLOW, 'test', self.now)['IsTest'], '1')
        self.pay(row)
        self.pay(row)
        self.assertEqual(row.status, 'complete')
        self.assertTrue(all(order.status == 'ready' for order in row.orders))
        self.assertEqual(TariffV2Reservation.query.filter_by(status='committed').count(), 2)
        self.assertEqual(TariffV2Cart.query.count(), 0)
        self.assertEqual(Order.query.count(), 0)
        self.assertEqual(CartItem.query.count(), 0)
        self.assertEqual(RobokassaPayment.query.count(), 0)
        self.assertEqual(self.product.stock_quantity, 10)

    def test_zero_fee_completes_without_configured_adapter_and_double_click_is_safe(self):
        db.session.add(TariffV2Seller(seller_id=self.seller.id, free_buyer_fee=True))
        db.session.commit()
        self.app.config['TARIFFS_V2_PAYMENT_TEST_ENABLED'] = False
        self.add()
        row = self.checkout()
        self.assertEqual(row.online_amount, 0)
        self.assertIsNone(row.gateway_id)
        self.assertIsNone(row.paid_at)
        self.assertEqual(row.status, 'complete')
        self.assertEqual(self.checkout().id, row.id)
        self.assertEqual(TariffV2Checkout.query.count(), 1)

    def test_same_cart_new_request_key_reuses_payment_and_alias_is_idempotent(self):
        self.add()
        row = self.checkout()
        self.assertEqual(self.checkout('b' * 48).id, row.id)
        self.assertEqual(TariffV2CheckoutRequest.query.count(), 2)
        self.pay(row)
        self.assertEqual(self.checkout('b' * 48).id, row.id)

    def test_reservations_prevent_oversell_and_expired_hold_is_ignored(self):
        self.product.stock_quantity = 1
        db.session.commit()
        self.add()
        row = self.checkout()
        self.assertEqual(available_quantity(self.product, self.now), 0)
        with self.assertRaises(TariffError):
            self.add(buyer=self.second_buyer)
        db.session.rollback()
        later = self.now + timedelta(minutes=31)
        self.assertEqual(available_quantity(self.product, later), 1)
        self.pay(row, later)
        self.assertEqual(row.status, 'needs_review')
        self.assertEqual(row.review_reason, 'expired')
        self.assertEqual(row.orders[0].status, 'waiting_payment')
        self.assertEqual(TariffV2Reservation.query.one().status, 'released')

    def test_cart_edit_releases_old_payment_and_old_callback_requires_review(self):
        self.add()
        first = self.checkout()
        self.add(quantity=2)
        self.assertEqual(first.status, 'expired')
        second = self.checkout('b' * 48)
        self.pay(first)
        self.assertEqual(first.status, 'needs_review')
        self.assertEqual(second.status, 'pending')
        self.assertEqual(TariffV2Cart.query.one().quantity, 2)

    def test_frozen_price_survives_rate_and_price_changes_before_payment(self):
        self.add()
        row = self.checkout()
        self.product.price = 20000
        self.rule.amount = Decimal('9')
        self.address.pvz_address = 'Новый адрес'
        db.session.commit()
        self.pay(row)
        self.assertEqual(row.online_amount, 300)
        self.assertEqual(row.orders[0].delivery['pvz_address'], 'ПВЗ 1')
        self.assertEqual(row.orders[0].lines[0].snapshot['unit_price'], '10000.00')

    def test_stock_reduction_and_moderation_revocation_require_review(self):
        self.add()
        row = self.checkout()
        self.product.stock_quantity = 0
        db.session.commit()
        self.pay(row)
        self.assertEqual(row.status, 'needs_review')
        self.assertEqual(row.review_reason, 'reservation_unavailable')

    def test_missing_cdek_fields_and_foreign_address_rejected(self):
        self.add()
        self.address.recipient_phone = ''
        db.session.commit()
        with self.assertRaisesRegex(TariffError, 'Телефон'):
            self.checkout()
        db.session.rollback()
        self.address.recipient_phone = '+79991234567'
        self.address.buyer_id = self.second_buyer.id
        db.session.commit()
        with self.assertRaises(TariffError):
            self.checkout()
        self.assertEqual(TariffV2Checkout.query.count(), 0)

    def test_physical_minimum_uses_goods_only(self):
        self.seller.minimum_order_amount = 10001
        db.session.commit()
        self.add()
        with self.assertRaisesRegex(TariffError, 'Минимальная'):
            self.checkout()

    def test_callback_tampering_and_wrong_payment_purpose_rejected(self):
        self.add()
        row = self.checkout()
        with self.assertRaises(TariffError):
            accept_checkout_result(self.callback(row, amount='301.00'), self.now)
        with self.assertRaises(TariffError):
            accept_storage_result(self.callback(row), self.now)
        data = self.callback(row)
        data.add('InvId', str(row.gateway_id))
        with self.assertRaises(TariffError):
            accept_checkout_result(data, self.now)
        self.assertIsNone(row.paid_at)

    def test_seller_cannot_see_unpaid_orders_or_other_sellers_orders(self):
        self.add()
        self.add(self.own_product)
        row = self.checkout()
        self.login(self.seller)
        response = self.client.get('/tariffs-v2/seller/orders')
        self.assertNotIn('<h2>Заказ #', response.get_data(as_text=True))
        self.pay(row)
        response = self.client.get('/tariffs-v2/seller/orders')
        html = response.get_data(as_text=True)
        self.assertIn('Product', html)
        self.assertNotIn('Owner goods', html)
        owner_order = next(order for order in row.orders if order.seller_id == self.owner.id)
        self.assertEqual(self.client.post(f'/tariffs-v2/seller/orders/{owner_order.id}/shipped').status_code, 400)

    def test_shipping_without_tracking_allowed_after_new_sales_block(self):
        self.add()
        row = self.checkout()
        self.pay(row)
        order = row.orders[0]
        db.session.add(TariffV2Invoice(seller_id=self.seller.id, period=moscow_date(self.now).replace(day=1),
            issued_at=stored_utc(self.now), due_at=stored_utc(self.now), amount=100))
        db.session.commit()
        ship_order(order.id, self.seller.id, '', self.now)
        self.assertEqual(order.status, 'shipped')
        self.assertIsNone(order.tracking_number)
        self.assertEqual(order.shipping_deadline, stored_utc(self.now + timedelta(days=5)))

    def test_buyer_ownership_and_csrf(self):
        self.add()
        row = self.checkout()
        self.login(self.second_buyer)
        self.assertEqual(self.client.get(f'/tariffs-v2/checkouts/{row.id}').status_code, 404)
        self.login(self.seller)
        self.assertEqual(self.client.get('/tariffs-v2/cart').status_code, 403)
        self.login(self.buyer)
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/tariffs-v2/checkout').status_code, 400)

    def test_common_result_route_dispatches_signed_checkout_only(self):
        self.add()
        row = self.checkout()
        response = self.client.post('/tariffs-v2/robokassa/result', data=self.callback(row))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.get_data(as_text=True), f'OK{row.gateway_id}')
        self.assertEqual(row.status, 'complete')

    def test_pages_render_and_cart_is_independent(self):
        self.login(self.buyer)
        response = self.client.post(f'/tariffs-v2/cart/{self.product.id}', data={'quantity': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get('/tariffs-v2/cart').status_code, 200)
        row = self.checkout()
        self.assertEqual(self.client.get(f'/tariffs-v2/checkouts/{row.id}').status_code, 200)
        self.assertEqual(CartItem.query.count(), 0)

    def test_payment_disabled_rolls_back_without_order(self):
        self.add()
        self.app.config['TARIFFS_V2_PAYMENT_TEST_ENABLED'] = False
        with self.assertRaises(TariffError):
            self.checkout()
        db.session.rollback()
        self.assertEqual(TariffV2SandboxOrder.query.count(), 0)

    def make_digital(self):
        book = DigitalBook(seller_id=self.owner.id, title='Book', price=1000, file_name='fake.epub',
            format='epub', published=True, moderation_status='approved')
        db.session.add(book)
        db.session.flush()
        self.own_product.digital_book_id = book.id
        self.own_product.stock_quantity = 0
        self.owner.minimum_order_amount = 999999
        db.session.commit()
        return book

    def test_digital_owner_only_no_address_or_stock_reservation_or_live_access(self):
        self.make_digital()
        self.add(self.own_product)
        with self.assertRaises(TariffError):
            self.add(self.own_product, quantity=2)
        db.session.rollback()
        row = self.checkout(deliveries={})
        self.assertEqual(row.online_amount, 1000)
        self.assertEqual(row.cod_amount, 0)
        self.assertFalse(row.orders[0].has_physical)
        self.assertEqual(TariffV2Reservation.query.count(), 0)
        self.pay(row)
        self.assertEqual(DigitalPurchase.query.count(), 0)
        self.assertIsNone(row.orders[0].shipping_deadline)
        self.own_product.seller = self.seller
        db.session.commit()
        with self.assertRaises(TariffError):
            self.add(self.own_product)

    def test_digital_moderation_revoked_after_invoice_needs_review(self):
        book = self.make_digital()
        self.add(self.own_product)
        row = self.checkout(deliveries={})
        book.moderation_status = 'rejected'
        db.session.commit()
        self.pay(row)
        self.assertEqual(row.status, 'needs_review')

    def test_daily_limit_reserves_pending_slot_and_resets_at_moscow_midnight(self):
        from app.utils.tariffs_v2 import midnight_utc
        self.seller.daily_orders_limit = 1
        db.session.commit()
        self.add()
        row = self.checkout()
        self.add(buyer=self.second_buyer)
        second_address = BuyerDelivery(buyer=self.second_buyer, delivery_service=self.service,
            pvz_city='Москва', pvz_address='ПВЗ 2', recipient_name='Второй', recipient_phone='+79991234568')
        db.session.add(second_address)
        db.session.commit()
        with self.assertRaisesRegex(TariffError, 'Дневной'):
            self.checkout(buyer=self.second_buyer, deliveries={self.seller.id: second_address.id})
        db.session.rollback()
        self.pay(row)
        with self.assertRaisesRegex(TariffError, 'Дневной'):
            self.checkout(buyer=self.second_buyer, deliveries={self.seller.id: second_address.id})
        db.session.rollback()
        next_day = midnight_utc(moscow_date(self.now) + timedelta(days=1))
        second = self.checkout(buyer=self.second_buyer, at=next_day,
            deliveries={self.seller.id: second_address.id})
        self.assertEqual(second.status, 'pending')

    def test_incomplete_address_route_redirects_to_profile_without_creating_order(self):
        self.add()
        self.address.pvz_city = ''
        db.session.commit()
        self.login(self.buyer)
        response = self.client.post('/tariffs-v2/checkout', data={
            'request_key': 'a' * 48, f'delivery_{self.seller.id}': self.address.id})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/profile?section=addresses')
        self.assertEqual(TariffV2Checkout.query.count(), 0)

    def test_review_queue_is_admin_only_and_browser_redirect_never_confirms(self):
        self.add()
        row = self.checkout()
        self.client.get('/tariffs-v2/robokassa/success')
        self.assertEqual(row.status, 'pending')
        self.pay(row, self.now + timedelta(minutes=31))
        self.assertEqual(self.client.get('/main_admin/tariffs-v2/').status_code, 403)
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
            session['main_admin_user'] = 'test-admin'
        response = self.client.get('/main_admin/tariffs-v2/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Срок оплаты истек или корзина изменена', response.get_data(as_text=True))

    def test_seller_shipping_setting_validated_and_existing_order_keeps_snapshot(self):
        self.add()
        row = self.checkout()
        self.login(self.seller)
        path = '/tariffs-v2/seller/shipping-days'
        self.assertEqual(self.client.post(path, data={'shipping_days': '0'}).status_code, 400)
        self.assertEqual(self.client.post(path, data={'shipping_days': '6'}).status_code, 400)
        self.assertEqual(self.client.post(path, data={'shipping_days': '2'}).status_code, 302)
        policy = db.session.get(TariffV2Seller, self.seller.id)
        self.assertEqual(policy.shipping_days, 2)
        self.assertFalse(policy.free_buyer_fee)
        self.pay(row)
        self.assertEqual(row.orders[0].shipping_days, 5)
        self.assertEqual(row.orders[0].shipping_deadline, stored_utc(self.now + timedelta(days=5)))
