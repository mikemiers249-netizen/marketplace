import base64
from dataclasses import replace
from decimal import Decimal
import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock

from app.utils.tariffs_v2_provider import (
    Credentials, RobokassaClient, ProviderError, RefundOutcomeUnknown, RefundRejected,
    OPERATION_URL, REFUND_URL, REFUND_STATE_URL, transport,
)

IDENTIFIER = '68cd7fa6-1338-4745-ba5c-28d16cbcdb3d'


class ProviderProtocolTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials('fake-shop', 'fake-second', 'fake-third', test_mode=False)
        self.send = Mock()
        self.client = RobokassaClient(self.credentials, self.send)
        self.fields = {'Shp_flow': 'tariffs_v2_checkout', 'Shp_token': 'a' * 48}
        self.items = [{'Name': 'Услуга доступа', 'Quantity': 1, 'Cost': '300.00',
                       'Tax': 'none', 'PaymentMethod': 'full_payment', 'PaymentObject': 'service'}]

    def xml(self, amount='300.00', state='100', token=None):
        return (f'<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/">'
            f'<Result><Code>0</Code></Result><State><Code>{state}</Code></State>'
            f'<Info><OutSum>{amount}</OutSum><OpKey>safe-operation_1</OpKey></Info>'
            f'<UserFields><Field><Name>Shp_flow</Name><Value>tariffs_v2_checkout</Value></Field>'
            f'<Field><Name>Shp_token</Name><Value>{token or "a" * 48}</Value></Field></UserFields>'
            '</OperationStateResponse>').encode()

    def test_operation_is_bound_to_exact_invoice_amount_and_scope(self):
        self.send.return_value = self.xml()
        self.assertEqual(self.client.operation(12, '300', self.fields), 'safe-operation_1')
        method, url = self.send.call_args.args
        self.assertEqual(method, 'GET')
        self.assertTrue(url.startswith(OPERATION_URL + '?'))
        expected = hashlib.sha256(b'fake-shop:12:fake-second').hexdigest()
        self.assertIn(expected, url)
        self.assertNotIn('fake-second', url)
        for data in (self.xml(amount='301'), self.xml(state='50'), self.xml(token='b' * 48), b'invalid xml'):
            self.send.return_value = data
            with self.assertRaises(ProviderError):
                self.client.operation(12, '300', self.fields)

    def test_test_payment_can_never_call_live_operation_or_refund(self):
        client = RobokassaClient(replace(self.credentials, test_mode=True), self.send)
        with self.assertRaises(ProviderError):
            client.operation(12, 300, self.fields)
        with self.assertRaises(ProviderError):
            client.create_refund('operation', 300, self.items)
        self.send.assert_not_called()

    def test_refund_token_has_exact_decimal_amount_receipt_and_password3_signature(self):
        token = self.client.refund_token('operation', '300.00', self.items)
        header, payload, signed = token.decode().split('.')
        decoded = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)), parse_float=Decimal)
        self.assertEqual(decoded['RefundSum'], Decimal('300.00'))
        self.assertEqual(decoded['InvoiceItems'][0]['Cost'], Decimal('300.00'))
        signature = base64.urlsafe_b64encode(hmac.new(b'fake-third', (header + '.' + payload).encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
        self.assertEqual(signature, signed)
        self.assertNotIn('fake-third', repr(self.credentials))

    def test_receipt_unit_cost_is_multiplied_by_quantity(self):
        items = [dict(self.items[0], Quantity=3, Cost='100.00')]
        self.client.refund_token('operation', 300, items)
        with self.assertRaises(ProviderError):
            self.client.refund_token('operation', 100, items)

    def test_missing_password3_receipt_or_wrong_total_prevents_any_request(self):
        client = RobokassaClient(replace(self.credentials, password3=''), self.send)
        with self.assertRaises(ProviderError):
            client.create_refund('operation', 300, self.items)
        with self.assertRaises(ProviderError):
            self.client.create_refund('operation', 300, [])
        with self.assertRaises(ProviderError):
            self.client.create_refund('operation', 301, self.items)
        self.send.assert_not_called()

    def test_creation_returns_request_id_but_is_not_confirmation(self):
        self.send.return_value = json.dumps({'success': True, 'requestId': IDENTIFIER}).encode()
        self.assertEqual(self.client.create_refund('operation', 300, self.items), IDENTIFIER)
        self.assertEqual(self.send.call_args.args[:2], ('POST', REFUND_URL))
        self.assertEqual(self.send.call_count, 1)

    def test_timeout_or_bad_response_requires_reconciliation_without_retry(self):
        for bad in (TimeoutError('SECRET must not leak'), b'bad json', b'{"success":true,"requestId":"bad"}'):
            self.send.reset_mock()
            self.send.side_effect = bad if isinstance(bad, Exception) else None
            self.send.return_value = bad
            with self.assertRaises(RefundOutcomeUnknown) as caught:
                self.client.create_refund('operation', 300, self.items)
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(self.send.call_count, 1)

    def test_explicit_rejection_is_distinguished_from_unknown_outcome(self):
        self.send.return_value = b'{"success":false,"requestId":null}'
        with self.assertRaises(RefundRejected):
            self.client.create_refund('operation', 300, self.items)

    def test_only_exact_status_amount_and_request_id_can_be_consumed(self):
        for state in ('processing', 'finished', 'canceled'):
            self.send.return_value = json.dumps({'requestId': IDENTIFIER, 'amount': 300, 'label': state}).encode()
            self.assertEqual(self.client.refund_state(IDENTIFIER, 300), state)
        self.assertTrue(self.send.call_args.args[1].startswith(REFUND_STATE_URL))
        self.send.return_value = json.dumps({'requestId': IDENTIFIER, 'amount': 301, 'label': 'finished'}).encode()
        with self.assertRaises(ProviderError):
            self.client.refund_state(IDENTIFIER, 300)

    def test_duplicate_json_fields_and_xml_entities_rejected(self):
        self.send.return_value = b'{"success":true,"success":false}'
        with self.assertRaises(RefundOutcomeUnknown):
            self.client.create_refund('operation', 300, self.items)
        self.send.return_value = b'<!DOCTYPE x [<!ENTITY y "z">]><x>&y;</x>'
        with self.assertRaises(ProviderError):
            self.client.operation(12, 300, self.fields)

    def test_transport_rejects_untrusted_destinations_before_network(self):
        with self.assertRaises(ProviderError):
            transport('POST', 'https://example.test/steal-secrets', b'secret')
