import unittest

import test_email as email_fixtures
from app.models.communications import Settings


class SellerLinksSettingTests(unittest.TestCase):
    setUp = email_fixtures.EmailTests.setUp
    tearDown = email_fixtures.EmailTests.tearDown

    def assert_links(self, visible):
        for page, label in (('/auth/login', 'Войти как продавец'),
                            ('/auth/signup', 'Стать продавцом')):
            response = self.client.get(page)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(label in response.text, visible)

    def test_default_and_saved_toggle(self):
        self.assert_links(True)
        with self.client.session_transaction() as session:
            session['main_admin_authenticated'] = True
        for checked in (False, True):
            data = {'seller_links_enabled': 'on'} if checked else {}
            response = self.client.post('/main_admin/settings', data=data)
            self.assertEqual(response.status_code, 302)
            self.assertIs(Settings.get('seller_links_enabled'), checked)
            self.assert_links(checked)

    def test_unauthorized_cannot_change_setting(self):
        self.client.post('/main_admin/settings', data={}, environ_overrides={'REMOTE_ADDR': '203.0.113.10'})
        self.assertIsNone(Settings.get('seller_links_enabled'))
        self.assert_links(True)
