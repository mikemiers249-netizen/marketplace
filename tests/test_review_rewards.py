import io
import json
import logging
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app import create_app, db
from config import TestingConfig
from app.models.users import Buyer, Seller, DeliveryService
from app.models.products import Product, Category
from app.models.communications import Review, Settings
from app.models.orders import Order, OrderItem, CartItem, Bonus
from app.models.loyalty import BuyerBonus
from app.models.review_rewards import ReviewRewardProgram, ReviewReward, RewardReviewReceipt, ReviewGift
from app.utils.review_rewards import validate_rules, apply_order_rewards, restore_order_rewards, enabled
from app.utils.helpers import get_cart_total


class ReviewRewardsTests(unittest.TestCase):
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
        self.buyer = Buyer(login='buyer', email='buyer@example.test', password_hash='test')
        self.seller = Seller(login='seller', email='seller@example.test', password_hash='test',
                             store_name='Store', store_slug='store')
        self.category = Category(name='Goods', slug='goods')
        self.product = Product(name='Goods', slug='goods', article='A', seller=self.seller,
                               category=self.category, price=1000, stock_quantity=100,
                               status='approved', current_discount=0)
        db.session.add_all([self.buyer, self.seller, self.category, self.product])
        db.session.commit()
        Settings.set('review_rewards_enabled', True, 'bool')
        self.program = ReviewRewardProgram(seller_id=self.seller.id, enabled=True,
                                           kind='bonus', mode='each', rules=[{'threshold': 1, 'amount': '50'}])
        db.session.add(self.program)
        db.session.commit()
        self.client = self.app.test_client()
        self.serial = 0

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and self.directory.name in handler.baseFilename:
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.directory.cleanup()

    def login(self, user):
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = user.get_id()
            session['_fresh'] = True
        # Flask-Login caches in g; this fixture keeps an outer app context.
        from flask import g
        g.pop('_login_user', None)

    def order(self, status='received'):
        self.serial += 1
        order = Order(order_number=f'T{self.serial}', buyer=self.buyer, seller=self.seller,
                      total_price=1000, status=status)
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(order_id=order.id, product_id=self.product.id, quantity=1,
                                 price_at_order=1000, original_price=1000))
        db.session.commit()
        return order

    def review(self, status='received'):
        order = self.order(status)
        review = Review(product=self.product, buyer=self.buyer, order=order,
                        rating=1, text='Honest review', status='pending', is_verified=True)
        db.session.add(review)
        db.session.commit()
        return review

    def gift(self):
        product = Product(name='Gift', slug='gift', article='G', seller=self.seller,
                           category=self.category, price=0, stock_quantity=0, status='reward_gift')
        db.session.add(product)
        db.session.flush()
        gift = ReviewGift(seller_id=self.seller.id, product_id=product.id)
        db.session.add(gift)
        db.session.commit()
        return gift

    def test_bonus_approval_idempotency_and_purchase_validation(self):
        review = self.review()
        self.assertEqual(ReviewReward.query.count(), 0)
        review.approve()
        review.approve()
        self.assertEqual(ReviewReward.query.count(), 1)
        self.assertEqual(BuyerBonus.query.one().balance, 50)
        self.assertEqual(Bonus.query.filter_by(type='accrued').count(), 1)
        # Deleting/reposting a review for the same purchased product cannot earn twice.
        order = review.order
        db.session.delete(review)
        db.session.commit()
        replacement = Review(product=self.product, buyer=self.buyer, order=order, rating=5, status='pending')
        db.session.add(replacement)
        db.session.commit()
        replacement.approve()
        self.assertEqual(ReviewReward.query.count(), 1)
        self.review('processing').approve()
        self.assertEqual(ReviewReward.query.count(), 1)
        Settings.set('review_rewards_enabled', False, 'bool')
        self.review().approve()
        self.assertEqual(ReviewReward.query.count(), 1)

    def test_grades_and_validation(self):
        self.program.mode = 'grades'
        self.program.rules = [{'threshold': 1, 'amount': '10'}, {'threshold': 3, 'amount': '30'}]
        db.session.commit()
        for _ in range(4):
            self.review().approve()
        self.assertEqual(RewardReviewReceipt.query.count(), 4)
        self.assertEqual(ReviewReward.query.count(), 2)
        self.assertEqual(BuyerBonus.query.one().balance, 40)
        for rules in [[{'threshold': 0, 'amount': 2}], [{'threshold': 1, 'amount': 'NaN'}],
                      [{'threshold': 1, 'amount': 101}], [{'threshold': 1, 'amount': 1}] * 2]:
            with self.assertRaises(ValueError):
                validate_rules(self.seller.id, 'discount', 'grades', rules)

    def test_one_discount_and_gift_per_order_rollback_and_cancellation(self):
        self.program.kind, self.program.rules = 'discount', [{'threshold': 1, 'amount': '20'}]
        db.session.commit()
        self.review().approve()
        self.review().approve()
        gift = self.gift()
        self.program.kind, self.program.rules = 'gift', [{'threshold': 1, 'gift': str(gift.id)}]
        db.session.commit()
        self.review().approve()
        self.review().approve()
        order = self.order('processing')
        apply_order_rewards(order)
        self.assertEqual(order.total_price, 800)
        self.assertEqual(order.items.filter_by(price_at_order=0).count(), 1)
        self.assertEqual(ReviewReward.query.filter_by(status='available').count(), 2)
        db.session.rollback()
        self.assertEqual(ReviewReward.query.filter_by(status='available').count(), 4)
        apply_order_rewards(order)
        db.session.commit()
        apply_order_rewards(order)
        self.assertEqual(order.total_price, 800)
        self.assertEqual(order.items.filter_by(price_at_order=0).count(), 1)
        restore_order_rewards(order)
        db.session.commit()
        self.assertEqual(ReviewReward.query.filter_by(status='available').count(), 4)
        self.assertEqual(ReviewReward.query.filter_by(status='redeemed').count(), 0)

    def test_checkout_and_trophies(self):
        gift = self.gift()
        self.program.kind, self.program.rules = 'gift', [{'threshold': 1, 'gift': 'random'}]
        db.session.commit()
        self.review().approve()
        reward = ReviewReward.query.one()
        reward.gift_image = 'test.jpg'
        self.program.kind, self.program.rules = 'discount', [{'threshold': 1, 'amount': '10'}]
        db.session.commit()
        self.review().approve()
        db.session.add(CartItem(buyer=self.buyer, product=self.product, quantity=1))
        db.session.commit()
        self.login(self.buyer)
        with self.app.test_request_context():
            self.assertEqual(get_cart_total(self.buyer.id)['total'], 900)
        response = self.client.get('/profile?section=trophies')
        self.assertEqual(response.status_code, 200)
        self.assertIn('grayscale(1)', response.get_data(as_text=True))
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}), \
             patch('app.blueprints.main.send_new_order_notification_to_seller'):
            response = self.client.get('/cart')
            self.assertEqual(response.status_code, 200)
            self.assertIn('Gift', response.get_data(as_text=True))
            html = response.get_data(as_text=True)
            gift_row = html.split('class="cart-items-list"', 1)[1].split('class="cart-summary"', 1)[0]
            self.assertIn('data-gift-reward-id=', gift_row)
            self.assertIn('0 ₽', gift_row)
            self.assertNotIn('Применяются автоматически', html)
            response = self.client.get('/order-review')
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            gift_row = html.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
            self.assertIn('data-gift-reward-id=', gift_row)
            self.assertIn('0 ₽', gift_row)
            self.assertNotIn('Ваши награды за отзывы', html)
            response = self.client.post('/order-review/submit')
        self.assertEqual(response.status_code, 200, response.data)
        order = Order.query.filter_by(status='processing').one()
        self.assertEqual(order.total_price, 900)
        self.assertEqual(order.items.filter_by(product_id=gift.product_id).one().price_at_order, 0)
        response = self.client.get('/profile?section=trophies')
        self.assertNotIn('grayscale(1)', response.get_data(as_text=True))

    def test_review_bonus_spending_with_discount_without_purchase_cashback(self):
        self.program.rules = [{'threshold': 1, 'amount': '1000'}]
        db.session.commit()
        self.review().approve()
        self.program.kind, self.program.rules = 'discount', [{'threshold': 1, 'amount': '20'}]
        self.product.current_discount = 10
        db.session.commit()
        self.review().approve()
        db.session.add(CartItem(buyer=self.buyer, product=self.product, quantity=1))
        db.session.commit()
        self.login(self.buyer)
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}), \
             patch('app.blueprints.main.send_new_order_notification_to_seller'):
            page = self.client.get('/order-review?bonus=1:1000')
            self.assertEqual(page.status_code, 200, page.data)
            response = self.client.post('/order-review/submit', data={'bonus_per_seller': '1:1000'})
        self.assertEqual(response.status_code, 200, response.data)
        order = Order.query.filter_by(status='processing').one()
        self.assertEqual(order.total_price, 720)
        self.assertEqual(order.bonus_used, 360)
        self.assertEqual(order.grand_total, 360)
        self.assertEqual(BuyerBonus.query.one().balance, 640)

    def test_legacy_checkout_and_cross_store_isolation(self):
        db.session.add(DeliveryService(id=1, name='Test delivery', code='test'))
        self.program.kind, self.program.rules = 'discount', [{'threshold': 1, 'amount': '20'}]
        db.session.commit()
        self.review().approve()
        another = Seller(login='other', email='other@example.test', password_hash='test',
                          store_name='Other', store_slug='other')
        db.session.add(another)
        db.session.flush()
        other_order = Order(order_number='OTHER', buyer=self.buyer, seller=another,
                            total_price=1000, status='processing')
        db.session.add(other_order)
        db.session.commit()
        apply_order_rewards(other_order)
        self.assertEqual(ReviewReward.query.filter_by(status='available').count(), 1)
        db.session.add(CartItem(buyer=self.buyer, product=self.product, quantity=1))
        db.session.commit()
        self.login(self.buyer)
        with patch('app.blueprints.main._visible_seller_ids', return_value={self.seller.id}), \
             patch('app.blueprints.main.send_new_order_notification_to_seller'):
            response = self.client.get('/checkout?delivery=1:1:PVZ:0')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(ReviewReward.query.filter_by(status='available').count(), 1)
            response = self.client.post('/order/create', data={'delivery_info':'[{"seller_id":1,"cost":0}]'})
        self.assertEqual(response.status_code, 200, response.data)
        order = Order.query.filter_by(seller_id=self.seller.id, status='processing').one()
        self.assertEqual(order.total_price, 800)
        self.assertEqual(ReviewReward.query.one().redeemed_order_id, order.id)

    def test_seller_settings_toggle_and_gallery(self):
        self.login(self.seller)
        response = self.client.get('/seller/loyalty')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('review-program-form', response.get_data(as_text=True))
        response = self.client.post('/seller/loyalty/reviews/save', data={
            'kind': 'discount', 'mode': 'grades', 'enabled': 'on',
            'rules': json.dumps([{'threshold': 5, 'amount': 15}, {'threshold': 1, 'amount': 10}])})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.program.rules[0]['threshold'], 1)
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (10, 10), 'red').save(image, 'PNG')
        image.seek(0)
        # Images are written only into a temporary app root during this test.
        with patch.object(self.app, 'root_path', self.directory.name):
            response = self.client.post('/seller/loyalty/reviews/gifts', data={
                'name': 'Test gift', 'image': (image, 'gift.png')}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReviewGift.query.count(), 1)
        self.assertEqual(ReviewGift.query.one().product.status, 'reward_gift')
        with self.assertRaises(ValueError):
            validate_rules(999, 'gift', 'each', [{'threshold': 1, 'gift': ReviewGift.query.one().id}])
        self.login(self.buyer)
        self.assertEqual(self.client.post('/seller/loyalty/reviews/save').status_code, 403)
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        response = self.client.get('/main_admin/loyalty')
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/main_admin/loyalty/reviews/toggle')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(enabled())


if __name__ == '__main__':
    unittest.main()
