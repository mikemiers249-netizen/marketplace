import unittest
from unittest.mock import patch, MagicMock

import test_email as email_fixtures
from app import db
from app.models.communications import Settings, Message, Review
from app.models.email_delivery import EmailOutbox
from app.models.orders import Order
from app.models.products import Product, Category
from app.models.users import Buyer, Seller
from app.utils.admin_notifications import EVENTS, SETTING_KEY
from app.utils.email_service import deliver_batch


class AdminNotificationTests(unittest.TestCase):
    def setUp(self):
        email_fixtures.EmailTests.setUp(self)
        self.category = Category(name='Test', slug='test')
        db.session.add(self.category)
        db.session.commit()
    tearDown = email_fixtures.EmailTests.tearDown

    def enable(self, events=None, enabled=True):
        Settings.set(SETTING_KEY, {'enabled': enabled, 'recipient': 'admin@example.test',
                                  'events': list(EVENTS) if events is None else events}, 'json')

    def admin_rows(self):
        return EmailOutbox.query.filter_by(recipient='admin@example.test').all()

    def test_all_events_and_delivery(self):
        self.enable()
        buyer = Buyer(login='newbuyer', email='newbuyer@example.test', password_hash='test')
        seller = Seller(login='newseller', email='newseller@example.test', password_hash='test',
                        store_name='New Store', store_slug='newstore')
        db.session.add_all([buyer, seller])
        db.session.flush()
        order = Order(order_number='NOTIFY', buyer_id=buyer.id, seller_id=seller.id,
                      total_price=100, status='processing')
        product = Product(name='New product', slug='new-product', article='NEW', price=100,
                          seller_id=seller.id, category_id=self.category.id, status='on_moderation')
        db.session.add_all([order, product])
        db.session.flush()
        db.session.add(Review(product_id=product.id, buyer_id=buyer.id, rating=5))
        for role, user in (('buyer', buyer), ('seller', seller)):
            db.session.add(Message(sender_type=role, sender_id=user.id,
                                   receiver_type='admin', receiver_id=0, text='Test chat text'))
        db.session.commit()
        rows = self.admin_rows()
        self.assertEqual(len(rows), 7)
        self.assertEqual({row.subject for row in rows}, {'Wimli — ' + label for label in EVENTS.values()})
        for row in rows:
            self.assertIn('https://market.example.test/main_admin/auth/login', row.body)
            self.assertIn('Введите логин и пароль', row.body)
        self.assertTrue(any('Test chat text' in row.body for row in rows))
        smtp = MagicMock()
        with patch('app.utils.email_service.smtp_connection') as connection:
            connection.return_value.__enter__.return_value = smtp
            self.assertEqual(deliver_batch(), 7)
        self.assertEqual(smtp.send_message.call_count, 7)

    def test_transitions_filters_and_no_duplicate_on_other_edits(self):
        self.enable(['order_created', 'product_moderation'])
        order = Order(order_number='DRAFT', buyer_id=self.buyer.id, seller_id=self.seller.id,
                      total_price=100, status='pending')
        product = Product(name='Draft product', slug='draft-product', article='DRAFT', price=100,
                          seller_id=self.seller.id, category_id=self.category.id, status='draft')
        db.session.add_all([order, product])
        db.session.commit()
        self.assertEqual(len(self.admin_rows()), 0)
        order.status = 'processing'
        product.status = 'on_moderation'
        db.session.commit()
        self.assertEqual(len(self.admin_rows()), 2)
        product.status = 'on_moderation'
        db.session.commit()
        self.assertEqual(len(self.admin_rows()), 2)
        product.name = 'Updated title'
        order.total_price = 110
        db.session.add(Message(sender_type='buyer', sender_id=self.buyer.id,
                               receiver_type='seller', receiver_id=self.seller.id, text='Private chat'))
        db.session.commit()
        self.assertEqual(len(self.admin_rows()), 2)
        self.enable(enabled=False)
        product.status = 'draft'
        db.session.commit()
        product.status = 'on_moderation'
        db.session.commit()
        self.assertEqual(len(self.admin_rows()), 2)

    def test_rollback_does_not_send_notification(self):
        self.enable()
        db.session.add(Message(sender_type='buyer', sender_id=self.buyer.id,
                               receiver_type='admin', receiver_id=0, image_path='uploads/messages/test.png'))
        db.session.flush()
        self.assertEqual(len(self.admin_rows()), 1)
        db.session.rollback()
        self.assertEqual(len(self.admin_rows()), 0)

    def test_settings_authorization_validation_and_persistence(self):
        page = '/main_admin/settings/notifications'
        self.assertEqual(self.client.get(page).status_code, 302)
        self.assertEqual(self.client.post(page, data={'enabled': 'on', 'recipient': 'stranger@example.test'}).status_code, 302)
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        response = self.client.get(page)
        self.assertEqual(response.status_code, 200)
        for key in EVENTS:
            self.assertIn('name="' + key + '"', response.text)
        self.assertEqual(self.client.post(page, data={'enabled': 'on', 'recipient': 'bad'}).status_code, 400)
        self.assertIsNone(Settings.get(SETTING_KEY))
        response = self.client.post(page, data={'enabled': 'on', 'recipient': 'admin@example.test',
                                               'buyer_message': 'on', 'seller_registered': 'on'})
        self.assertEqual(response.status_code, 302)
        saved = Settings.get(SETTING_KEY)
        self.assertTrue(saved['enabled'])
        self.assertEqual(set(saved['events']), {'buyer_message', 'seller_registered'})
        self.assertEqual(self.client.post(page, data={'recipient': 'admin@example.test'}).status_code, 302)
        self.assertFalse(Settings.get(SETTING_KEY)['enabled'])


if __name__ == '__main__':
    unittest.main()
