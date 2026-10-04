import io
import logging
import tempfile
import unittest
from pathlib import Path
from flask import g

from app import create_app, db
from config import TestingConfig
from app.models.users import Buyer, Seller
from app.models.communications import Message, MessageAttachment
from app.models.products import ProductEvent
from app.utils.privacy import analytics_session_id


class MessagePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        class Config(TestingConfig):
            SQLALCHEMY_DATABASE_URI = 'sqlite://'
            SQLALCHEMY_ENGINE_OPTIONS = {}
            UPLOAD_FOLDER = str(root / 'static' / 'uploads')
            LOG_FILE = str(root / 'test.log')
            MAIL_WORKER_ENABLED = False
        self.app = create_app(Config)
        self.app.static_folder = str(root / 'static')
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.buyer = Buyer(login='buyer', email='buyer@example.test')
        self.other = Buyer(login='other', email='other@example.test')
        self.seller = Seller(login='seller', email='seller@example.test',
                             store_name='Store', store_slug='store')
        for user in (self.buyer, self.other, self.seller):
            user.set_password('test-password')
        db.session.add_all([self.buyer, self.other, self.seller])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and handler.baseFilename.startswith(self.directory.name):
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.directory.cleanup()

    def login(self, user=None, admin=False):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session.clear()
            if admin:
                session['main_admin_authenticated'] = True
            elif user:
                session['_user_id'] = user.get_id()
                session['_fresh'] = True

    def upload(self, endpoint='/api/upload-message-file', mime='application/pdf'):
        return self.client.post(endpoint, data={
            'file': (io.BytesIO(b'%PDF-1.4\nprivate'), 'document.pdf', mime)})

    def test_upload_send_read_and_reject_strangers(self):
        self.login(self.buyer)
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        path = response.json['path']
        self.assertTrue((Path(self.app.config['UPLOAD_FOLDER']) / '.private' /
                         'messages' / path.rsplit('/', 1)[1]).exists())
        self.assertFalse((Path(self.app.static_folder) / path).exists())
        url = '/static/' + path
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        response.close()
        self.login(self.seller)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login(self.buyer)
        sent = self.client.post('/messages/send', data={
            'receiver_type': 'seller', 'receiver_id': self.seller.id,
            'file_path': path})
        self.assertEqual(sent.status_code, 200)
        self.login(self.seller)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'%PDF'))
        response.close()
        self.login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        rejected = self.client.post('/messages/send', data={
            'receiver_type': 'seller', 'receiver_id': self.seller.id, 'file_path': path})
        self.assertEqual(rejected.status_code, 403)
        self.login()
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.upload().status_code, 401)

    def test_admin_and_seller_interfaces(self):
        self.login(admin=True)
        response = self.upload('/main_admin/api/upload-message-file')
        self.assertEqual(response.status_code, 200)
        path = response.json['path']
        sent = self.client.post('/main_admin/messages/send', data={
            'partner_type': 'seller', 'partner_id': self.seller.id, 'file_path': path})
        self.assertEqual(sent.status_code, 302)
        self.login(self.seller)
        response = self.client.get('/static/' + path)
        self.assertEqual(response.status_code, 200)
        response.close()
        own_path = self.upload(mime='image/png').json['path']
        self.assertTrue(own_path.endswith('.png'))
        sent = self.client.post('/seller/messages/send', json={
            'receiver_type': 'buyer', 'receiver_id': self.buyer.id, 'image_path': own_path})
        self.assertEqual(sent.status_code, 200)
        self.login(self.buyer)
        response = self.client.get('/static/' + own_path)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_seller_order_chat_with_existing_admin_session(self):
        from app.models.orders import Order
        order = Order(order_number='ROLE-TEST', total_price=0,
                      buyer_id=self.buyer.id, seller_id=self.seller.id)
        db.session.add(order)
        db.session.commit()
        self.login(self.seller)
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        for suffix in ('', '/content', '/new?last_id=0'):
            response = self.client.get(f'/messages/order/{order.id}{suffix}')
            self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.upload('/main_admin/api/upload-message-file').status_code, 200)
        self.login(self.other)
        for suffix in ('', '/content', '/new?last_id=0'):
            self.assertEqual(self.client.get(f'/messages/order/{order.id}{suffix}').status_code, 404)

    def test_permanent_support_contact_and_admin_replies(self):
        from app.blueprints.messages import get_conversations
        for user, role, page, send in (
            (self.buyer, 'buyer', '/messages/buyer', '/messages/send'),
            (self.seller, 'seller', '/seller/messages', '/seller/messages/send'),
        ):
            self.login(user)
            contacts = get_conversations(role, user.id, 'support')
            self.assertEqual(list(contacts), ['admin:0'])
            self.assertEqual(contacts['admin:0']['name'], 'Поддержка')
            self.assertEqual(contacts['admin:0']['unread_count'], 0)
            response = self.client.get(page + '?filter=support')
            self.assertEqual(response.status_code, 200)
            self.assertIn('data-partner-type="admin"', response.text)
            self.assertIn('data-partner-id="0"', response.text)
            self.assertIn('<span class="dialog-name">Поддержка</span>', response.text)
            response = self.client.get(page + '?filter=support&partner_type=admin&partner_id=0')
            self.assertEqual(response.status_code, 200)
            self.assertIn('name="receiver_id" value="0"', response.text)
            self.assertEqual(self.client.get('/messages/admin/0/content').status_code, 200)
            data = {'receiver_type': 'admin', 'receiver_id': 0, 'text': role + ' support request'}
            response = (self.client.post(send, json=data) if role == 'seller'
                        else self.client.post(send, data=data))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['success'])
            self.assertEqual(list(get_conversations(role, user.id, 'support')), ['admin:0'])
            self.login(admin=True)
            tab = 'buyers' if role == 'buyer' else 'sellers'
            response = self.client.get(f'/main_admin/messages?tab={tab}&dialog_id={user.id}')
            self.assertEqual(response.status_code, 200)
            self.assertIn(role + ' support request', response.text)
            response = self.client.post('/main_admin/messages/send', data={
                'partner_type': role, 'partner_id': user.id, 'text': role + ' support reply'})
            self.assertEqual(response.status_code, 302)
            self.login(user)
            contact = get_conversations(role, user.id, 'support')['admin:0']
            self.assertEqual(contact['unread_count'], 1)
            response = self.client.get(page + '?filter=support&partner_type=admin&partner_id=0')
            self.assertIn(role + ' support reply', response.text)
            self.assertEqual(get_conversations(role, user.id, 'support')['admin:0']['unread_count'], 0)

    def test_order_image_send_and_reload(self):
        from app.models.orders import Order
        order = Order(order_number='IMAGE-TEST', total_price=0,
                      buyer_id=self.buyer.id, seller_id=self.seller.id)
        db.session.add(order)
        db.session.commit()
        for user, endpoint, receiver_type, receiver_id in (
            (self.seller, '/seller/messages/send', 'buyer', self.buyer.id),
            (self.buyer, '/messages/send', 'seller', self.seller.id),
        ):
            self.login(user)
            page = self.client.get(f'/messages/order/{order.id}')
            self.assertEqual(page.status_code, 200)
            self.assertIn(f'name="receiver_type" value="{receiver_type}"', page.text)
            path = self.upload(mime='image/png').json['path']
            response = self.client.post(endpoint, data={
                'receiver_type': receiver_type, 'receiver_id': receiver_id,
                'conversation_type': 'order', 'conversation_id': order.id, 'image_path': path})
            self.assertEqual(response.status_code, 200)
            message = db.session.get(Message, response.json['message_id'])
            self.assertEqual(message.conversation_type, 'order')
            self.assertEqual(message.conversation_id, order.id)
            self.assertEqual(message.receiver_type, receiver_type)
            self.assertEqual(message.receiver_id, receiver_id)
            self.assertIn(path, self.client.get(f'/messages/order/{order.id}/content').text)
            self.login(self.buyer if user is self.seller else self.seller)
            response = self.client.get(f'/messages/order/{order.id}/new?last_id=0')
            self.assertIn(path, response.text)
            response = self.client.get('/static/' + path)
            self.assertEqual(response.status_code, 200)
            response.close()

    def test_legacy_migration_keeps_urls_and_is_repeatable(self):
        old_dir = Path(self.app.static_folder) / 'uploads' / 'messages'
        old_dir.mkdir(parents=True)
        (old_dir / 'old.pdf').write_bytes(b'%PDF-old')
        path = 'uploads/messages/old.pdf'
        db.session.add(Message(sender_type='buyer', sender_id=self.buyer.id,
            receiver_type='seller', receiver_id=self.seller.id, file_path=path))
        db.session.commit()
        runner = self.app.test_cli_runner()
        for _ in range(2):
            result = runner.invoke(args=['secure-message-files'])
            self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse((old_dir / 'old.pdf').exists())
        self.login(self.seller)
        response = self.client.get('/static/' + path)
        self.assertEqual(response.data, b'%PDF-old')
        response.close()
        self.login(self.other)
        self.assertEqual(self.client.get('/static/' + path).status_code, 404)

    def test_private_directory_traversal_and_public_assets(self):
        self.login(self.buyer)
        path = self.upload().json['path']
        filename = path.rsplit('/', 1)[1]
        self.assertEqual(self.client.get('/static/uploads/.private/messages/' + filename).status_code, 404)
        self.assertEqual(self.client.get('/static/uploads/messages/../.private/messages/' + filename).status_code, 404)
        self.assertEqual(self.client.post('/messages/send', data={
            'receiver_type': 'seller', 'receiver_id': self.seller.id,
            'file_path': 'uploads/messages/../../config.py'}).status_code, 400)
        asset = Path(self.app.static_folder) / 'uploads' / 'products' / 'photo.png'
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b'public')
        self.login()
        response = self.client.get('/static/uploads/products/photo.png')
        self.assertEqual(response.data, b'public')
        response.close()

    def test_analytics_identifier_is_not_a_session_credential(self):
        with self.app.test_request_context(headers={'Cookie': 'session=credential'}):
            first = analytics_session_id()
            self.assertEqual(len(first), 64)
            self.assertNotEqual(first, 'credential')
            self.assertEqual(first, analytics_session_id())
        with self.app.test_request_context():
            self.assertIsNone(analytics_session_id())


if __name__ == '__main__':
    unittest.main()
