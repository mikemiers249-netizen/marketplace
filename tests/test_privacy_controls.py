import re
import unittest
from datetime import datetime, timedelta

from flask import g
import test_message_privacy as privacy_fixtures
from app import db
from app.blueprints.privacy import document_digest, has_consent, public_contact_allowed
from app.models.privacy import PrivacyConsent, LegalSnapshot, PrivacyRequest
from app.models.communications import Mailing, Message
from app.models.footer import FooterLink
from app.models.products import ProductEvent
from app.models.orders import Order
from app.privacy_cleanup import cleanup


class PrivacyControlsTests(unittest.TestCase):
    def test_cookie_notice_survives_session_reset_without_granting_analytics(self):
        response = self.client.post('/privacy/cookies', data={
            'choice': 'necessary', 'personal_version': document_digest('personal')})
        self.assertEqual(response.status_code, 303)
        self.assertIn('Max-Age=15552000', response.headers.get('Set-Cookie', ''))
        with self.client.session_transaction() as session:
            session.clear()
        self.assertNotIn('id="cookie-notice-title"', self.client.get('/auth/login').text)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='analytics', withdrawn_at=None).count(), 0)
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        self.assertNotIn('id="cookie-notice-title"', self.client.get('/main_admin/settings').text)

    def test_admin_cookie_choice_with_seller_session(self):
        for choice in ('necessary', 'analytics'):
            self.client.delete_cookie('wimli_cookie_notice')
            self.login(self.seller)
            with self.client.session_transaction() as session:
                session['main_admin_authenticated'] = True
            response = self.client.get('/main_admin/settings/notifications')
            self.assertIn('id="cookie-notice-title"', response.text)
            self.assertIn('action="/main_admin/privacy/cookies"', response.text)
            response = self.client.post('/main_admin/privacy/cookies', data={
                'choice': choice, 'personal_version': document_digest('personal'),
                'return_to': '/main_admin/settings/notifications'}, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('id="cookie-notice-title"', response.text)
            self.assertNotIn('id="cookie-notice-title"',
                             self.client.get('/main_admin/settings/notifications').text)
            self.assertEqual(PrivacyConsent.query.filter_by(user_type='seller', purpose='analytics').count(), 0)
        self.assertEqual(PrivacyConsent.query.filter_by(user_type='admin', purpose='analytics', withdrawn_at=None).count(), 1)

    def test_cookie_notice_choice_and_revocation(self):
        self.login()
        response = self.client.get('/privacy-center')
        self.assertIn('id="cookie-notice-title"', response.text)
        data = {'choice': 'necessary', 'personal_version': document_digest('personal'),
                'return_to': '//example.test'}
        response = self.client.post('/privacy/cookies', data=data)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, '/')
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='analytics').count(), 0)
        self.assertNotIn('id="cookie-notice-title"', self.client.get('/privacy-center').text)
        data['choice'] = 'analytics'
        self.assertEqual(self.client.post('/privacy/cookies', data=data).status_code, 303)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='analytics', withdrawn_at=None).count(), 1)
        self.assertNotIn('id="cookie-notice-title"', self.client.get('/privacy-center').text)
        self.choices()
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='analytics', withdrawn_at=None).count(), 0)
        self.assertNotIn('id="cookie-notice-title"', self.client.get('/privacy-center').text)
        data['personal_version'] = 'outdated'
        self.assertEqual(self.client.post('/privacy/cookies', data=data).status_code, 400)

    setUp = privacy_fixtures.MessagePrivacyTests.setUp
    tearDown = privacy_fixtures.MessagePrivacyTests.tearDown
    login = privacy_fixtures.MessagePrivacyTests.login
    upload = privacy_fixtures.MessagePrivacyTests.upload

    def choices(self, **choices):
        return self.client.post('/privacy-center', data={
            'action': 'choices', 'personal_version': document_digest('personal'),
            'distribution_version': document_digest('distribution'), **choices})

    def test_terms_required_separate_from_optional_consents(self):
        data = {'login': 'new', 'email': 'new@example.test', 'phone': '123456789',
            'password': 'test-password', 'password_confirm': 'test-password'}
        self.assertEqual(self.client.post('/auth/signup', data=data).status_code, 400)
        data.update(agree='on', terms_version=document_digest('terms'))
        self.assertEqual(self.client.post('/auth/signup', data=data).status_code, 302)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='terms').count(), 1)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='marketing').count(), 0)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='analytics').count(), 0)
        record = PrivacyConsent.query.filter_by(purpose='terms').one()
        self.assertIn('Правила использования', db.session.get(LegalSnapshot, record.snapshot_id).content)

    def test_separate_choices_revocation_and_subject_isolation(self):
        self.login(self.buyer)
        self.assertEqual(self.choices(analytics='on').status_code, 302)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='analytics', withdrawn_at=None).count(), 1)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='marketing', withdrawn_at=None).count(), 0)
        record = PrivacyConsent.query.filter_by(purpose='analytics').one()
        self.choices()
        self.assertIsNotNone(record.withdrawn_at)
        self.assertEqual(PrivacyConsent.query.filter_by(purpose='analytics', withdrawn_at=None).count(), 0)
        self.login(self.other)
        self.assertNotRegex(self.client.get('/privacy-center').text, r'id="analytics"[^>]*checked')
        self.login()
        self.choices(analytics='on')
        another = self.app.test_client()
        with another.get('/privacy-center') as response:
            self.assertEqual(response.status_code, 200)
            self.assertNotRegex(response.text, r'id="analytics"[^>]*checked')

    def test_public_contact_selection_and_changed_value(self):
        self.seller.phone = '+79990000000'
        db.session.commit()
        self.login(self.seller)
        self.assertFalse(public_contact_allowed(self.seller, 'phone'))
        self.assertEqual(self.choices(public_phone='on', subject_name='Test Seller').status_code, 302)
        self.assertTrue(public_contact_allowed(self.seller, 'phone'))
        self.assertFalse(public_contact_allowed(self.seller, 'email'))
        self.seller.phone = '+79990000001'
        db.session.commit()
        self.assertFalse(public_contact_allowed(self.seller, 'phone'))

    def test_requests_authorization_and_marketing_selection(self):
        self.login(self.buyer)
        exported = self.client.get('/privacy/export')
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json['profile']['id'], self.buyer.id)
        self.assertNotIn('password_hash', exported.text)
        self.assertNotIn('api_credentials', exported.text)
        self.choices(marketing='on')
        mailing = Mailing(target_type='buyers', subject='Offer', text='Test offer')
        db.session.add(mailing)
        db.session.commit()
        mailing.send()
        self.assertEqual(Message.query.filter_by(conversation_type='mailing').count(), 1)
        self.client.post('/privacy-center', data={'action': 'request', 'kind': 'access', 'details': 'My data'})
        ticket = PrivacyRequest.query.one()
        self.assertEqual(ticket.user_id, self.buyer.id)
        self.assertEqual(self.client.get('/main_admin/privacy-requests').status_code, 403)
        self.login(self.other)
        self.assertNotIn('My data', self.client.get('/privacy-center').text)
        self.login(admin=True)
        self.assertEqual(self.client.get('/main_admin/privacy-requests').status_code, 200)

    def test_install_documents_preserves_history_and_admin_edits(self):
        db.session.add(FooterLink(slug='privacy', title='Old', content='Old legal text', is_active=True))
        db.session.commit()
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=['privacy-install'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(LegalSnapshot.query.filter_by(content='Old legal text').count(), 1)
        doc = FooterLink.query.filter_by(slug='privacy').one()
        self.assertIn('263109734513', doc.content)
        doc.content += '<p>Administrator addition</p>'
        db.session.commit()
        self.assertEqual(runner.invoke(args=['privacy-install']).exit_code, 0)
        self.assertIn('Administrator addition', doc.content)
        self.assertEqual(self.client.get('/privacy').status_code, 308)

    def test_csrf_on_upload_and_choices(self):
        self.app.config['WTF_CSRF_ENABLED'] = True
        self.login(self.buyer)
        self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(self.choices(analytics='on').status_code, 400)
        response = self.client.get('/privacy-center')
        token = re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)
        import io
        response = self.client.post('/api/upload-message-file', data={
            'csrf_token': token, 'file': (io.BytesIO(b'%PDF-test'), 'test.pdf', 'application/pdf')})
        self.assertEqual(response.status_code, 200)

    def test_foreign_order_chat_is_denied(self):
        order = Order(order_number='privacy-test', buyer_id=self.buyer.id,
            seller_id=self.seller.id, total_price=10, status='received')
        db.session.add(order)
        db.session.flush()
        db.session.add(Message(sender_type='buyer', sender_id=self.buyer.id,
            receiver_type='seller', receiver_id=self.seller.id, text='Private order text',
            conversation_type='order', conversation_id=order.id))
        db.session.commit()
        self.login(self.other)
        response = self.client.get(f'/messages/order/{order.id}/new')
        self.assertEqual(response.status_code, 404)
        self.assertNotIn('Private order text', response.text)
        self.login(self.buyer)
        self.assertEqual(self.client.get(f'/messages/order/{order.id}/new').status_code, 200)

    def test_retention_preview_and_orders_untouched(self):
        from app.models.products import Product, Category
        category = Category(name='Test', slug='test')
        db.session.add(category)
        db.session.flush()
        product = Product(name='Test product', article='TEST-PRIVACY', slug='test-product', seller_id=self.seller.id,
            category_id=category.id, price=10)
        db.session.add(product)
        db.session.flush()
        db.session.add(ProductEvent(product_id=product.id, seller_id=self.seller.id,
            event_type='view', created_at=datetime.utcnow() - timedelta(days=181)))
        db.session.commit()
        self.assertEqual(cleanup()['analytics_events'], 1)
        self.assertEqual(ProductEvent.query.count(), 1)
        self.assertEqual(cleanup(apply=True)['analytics_events'], 1)
        self.assertEqual(ProductEvent.query.count(), 0)
        self.assertEqual(db.session.get(Product, product.id).name, 'Test product')


if __name__ == '__main__':
    unittest.main()
