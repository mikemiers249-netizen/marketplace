"""Retirement of the successful one-ruble checks and public payment rollout."""
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from app import db
from app.models.products import Product, Category
from app.models.digital import DigitalBook, DigitalPurchase
from app.models.orders import Order, OrderItem, CartItem
from app.models.payments import RobokassaPayment
from app.utils.private_payment_tests import (retire_private_products_and_enable_payments,
    DIGITAL_SKU, PHYSICAL_SKU)
from tests import test_robokassa as base
from tests.test_digital_books_audit import epub_file


class PublicPaymentRolloutTests(unittest.TestCase):
    login = base.RobokassaTests.login
    start = base.RobokassaTests.start
    tearDown = base.RobokassaTests.tearDown

    def setUp(self):
        base.RobokassaTests.setUp(self)
        self.buyer.email = 'ronnie83@mail.ru'
        self.seller.payments_paused = True
        self.app.config['UPLOAD_FOLDER'] = str(Path(self.directory.name) / 'uploads')
        from app.utils.digital_books import storage
        storage().mkdir(parents=True)
        (storage() / 'rollout.epub').write_bytes(epub_file())
        self.physical = Product(name='Physical test', slug='retired-physical',
            article=PHYSICAL_SKU, system_sku=PHYSICAL_SKU, seller=self.seller,
            category=self.receipt_product.category, price=1, stock_quantity=9, status='approved')
        self.book = DigitalBook(seller=self.seller, title='Electronic test', price=1,
            system_sku=DIGITAL_SKU, article=DIGITAL_SKU, file_name='rollout.epub', format='epub',
            published=True, moderation_status='approved',
            category=Category(name='Ebooks', slug='rollout-books', is_digital_book=True))
        db.session.add_all([self.physical, self.book])
        db.session.flush()
        self.digital = self.book.commerce_product
        self.paid = Order(order_number='PAID-TEST', buyer=self.buyer, seller=self.seller,
            total_price=1, status='received')
        db.session.add(OrderItem(order=self.paid, product=self.digital, quantity=1, price_at_order=1))
        db.session.add(RobokassaPayment(order=self.paid, amount=1, is_test=False,
            status='paid', paid_at=datetime.utcnow()))
        self.purchase = DigitalPurchase(buyer_id=self.buyer.id, book=self.book, order=self.paid,
            title=self.book.title, file_name=self.book.file_name, format='epub')
        self.pending = Order(order_number='PENDING-TEST', buyer=self.buyer, seller=self.seller,
            total_price=1, status='awaiting_payment')
        db.session.add(OrderItem(order=self.pending, product=self.physical, quantity=1, price_at_order=1))
        self.unpaid_invoice = RobokassaPayment(order=self.pending, amount=1,
            is_test=False, status='pending')
        db.session.add_all([self.purchase, self.unpaid_invoice,
            CartItem(buyer=self.buyer, product=self.physical, quantity=1)])
        db.session.commit()

    def test_removal_is_committed_before_public_payments_open(self):
        events = []
        def record(message, *args, **kwargs):
            if message.startswith('Test products retired'):
                self.assertTrue(self.seller.payments_paused)
                self.assertEqual(self.physical.status, 'deleted')
                self.assertEqual(self.digital.status, 'deleted')
                self.assertFalse(self.book.published)
                events.append('removed')
            elif message.startswith('Public payments enabled'):
                self.assertFalse(self.seller.payments_paused)
                events.append('opened')
        with patch.object(self.app.logger, 'info', side_effect=record):
            retire_private_products_and_enable_payments()
        self.assertEqual(events, ['removed', 'opened'])
        self.assertEqual(CartItem.query.count(), 0)
        self.assertEqual(self.pending.status, 'payment_expired')
        self.assertEqual(self.unpaid_invoice.status, 'expired')
        self.assertTrue(self.paid.payment_confirmed)
        self.assertEqual(self.paid.status, 'received')
        self.assertEqual(DigitalPurchase.query.count(), 1)
        self.assertEqual(self.client.get(f'/digital/read/{self.purchase.id}').status_code, 200)

    def test_ordinary_buyers_can_pay_and_private_routes_are_closed(self):
        retire_private_products_and_enable_payments()
        self.assertEqual(self.start().status_code, 200)
        self.order.buyer = self.other_buyer
        db.session.commit()
        self.login(self.other_buyer)
        self.assertEqual(self.start().status_code, 200)
        for user in (self.buyer, self.other_buyer):
            self.login(user)
            self.assertEqual(self.client.get('/payments/robokassa/private-tests').status_code, 404)
            for kind in ('physical', 'digital'):
                self.assertEqual(self.client.post(f'/payments/robokassa/private-tests/{kind}/buy').status_code, 404)
            self.assertEqual(self.client.get(f'/product/{self.physical.id}').status_code, 404)
            page = self.client.get(f'/digital/books/{self.book.id}')
            self.assertEqual(page.status_code, 200 if user.id == self.buyer.id else 404)
            if page.status_code == 200:
                self.assertNotIn('id="book-cart-form"', page.get_data(as_text=True))

    def test_redeploy_does_not_recreate_tests_or_override_a_new_sales_pause(self):
        retire_private_products_and_enable_payments()
        self.seller.payments_paused = True
        db.session.commit()
        before = Product.query.count(), Order.query.count(), RobokassaPayment.query.count()
        retire_private_products_and_enable_payments()
        self.assertTrue(self.seller.payments_paused)
        self.assertEqual(before, (Product.query.count(), Order.query.count(), RobokassaPayment.query.count()))
        self.assertEqual(self.physical.status, 'deleted')
        self.assertEqual(self.digital.status, 'deleted')
        self.assertEqual(self.start().status_code, 403)
