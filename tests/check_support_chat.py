"""Verify a buyer can open support before any messages exist."""
import logging
import tempfile
from pathlib import Path

from app import create_app, db
from config import TestingConfig
from app.models.users import Buyer
from app.models.communications import Message
from app.blueprints.messages import get_conversations

with tempfile.TemporaryDirectory() as directory:
    class Config(TestingConfig):
        SQLALCHEMY_DATABASE_URI = 'sqlite://'
        SQLALCHEMY_ENGINE_OPTIONS = {}
        MAIL_WORKER_ENABLED = False
        MAIL_PASSWORD = None
        LOG_FILE = str(Path(directory) / 'test.log')
    app = create_app(Config)
    try:
        with app.app_context():
            db.create_all()
            buyer = Buyer(login='buyer', email='buyer@example.test', password_hash='test')
            db.session.add(buyer)
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as session:
                session['_user_id'] = buyer.get_id()
                session['_fresh'] = True
            support = get_conversations('buyer', buyer.id, 'support')
            assert list(support) == ['admin:0']
            assert support['admin:0']['unread_count'] == 0
            assert not get_conversations('buyer', buyer.id, 'stores')
            assert Message.query.count() == 0
            response = client.get('/profile?section=messages&filter=support')
            assert response.status_code == 200
            html = response.get_data(as_text=True)
            assert 'data-partner-type="admin"' in html and 'data-partner-id="0"' in html
            response = client.get('/profile?section=messages&filter=support&partner_type=admin&partner_id=0')
            assert response.status_code == 200
            assert 'name="receiver_id" value="0"' in response.get_data(as_text=True)
            response = client.post('/messages/send', data={
                'receiver_type': 'admin', 'receiver_id': '0', 'text': 'Local test support request'
            })
            assert response.status_code == 200 and response.json['success']
            assert Message.query.one().receiver_id == 0
            assert f'buyer:{buyer.id}' in get_conversations('admin', 0, 'buyers')
            support = get_conversations('buyer', buyer.id, 'support')
            assert list(support) == ['admin:0']
            assert support['admin:0']['last_message'] == 'Local test support request'
            assert get_conversations('buyer', buyer.id + 1, 'support')['admin:0']['last_message'] == ''
            assert client.get('/main_admin/api/messages/unread-count').status_code == 401
            with client.session_transaction() as session:
                session['main_admin_authenticated'] = True
            assert client.get('/main_admin/api/messages/unread-count').json['unread_messages'] == 1
            seller_message = Message(sender_type='seller', sender_id=42,
                                     receiver_type='admin', receiver_id=0,
                                     text='Local seller request', is_read=False)
            db.session.add(seller_message)
            db.session.commit()
            response = client.get('/main_admin/api/messages/unread-count')
            assert response.json['unread_messages'] == 2
            assert response.headers['Cache-Control'] == 'no-store'
            Message.query.filter_by(receiver_type='admin', receiver_id=0).update({'is_read': True})
            db.session.commit()
            assert client.get('/main_admin/api/messages/unread-count').json['unread_messages'] == 0
            db.session.remove()
            db.drop_all()
    finally:
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and directory in handler.baseFilename:
                handler.close()
                logging.getLogger().removeHandler(handler)
print('PASS: permanent support contact, empty chat, first message delivery, no duplicates or cross-buyer history')
