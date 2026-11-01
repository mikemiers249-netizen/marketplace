import unittest

from sqlalchemy import inspect, text

from app import create_app, db
from app.tariffs_v2_schema import prepare_schema, REVISION
from app.tariffs_v2_release import readiness
import test_tariffs_v2_payments as foundation


class ReleasePreparationTests(unittest.TestCase):
    setUp = foundation.StoragePaymentTests.setUp
    tearDown = foundation.StoragePaymentTests.tearDown
    issue = foundation.StoragePaymentTests.issue

    def test_readiness_and_activation_never_expose_keys_or_enable_a_simulator(self):
        self.app.config['TARIFFS_V2_ROBOKASSA_PASSWORD3'] = 'must-not-appear'
        for flag in ('--check', '--activate'):
            result = self.app.test_cli_runner().invoke(args=['tariffs-v2-release', flag])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn('checkout_cutover', result.output)
            self.assertIn('refund_dispatch', result.output)
            self.assertNotIn('must-not-appear', result.output)
            self.assertNotIn('v2-test-two', result.output)
        self.assertTrue(self.app.config['TARIFFS_V2_SANDBOX'])
        self.assertEqual(self.product.status, 'draft')

    def test_release_check_is_available_while_feature_is_disabled(self):
        class Disabled(self.config):
            TARIFFS_V2_ENABLED = False
        app = create_app(Disabled)
        self.assertIn('tariffs-v2-release', app.cli.commands)
        self.assertFalse(any(rule.endpoint.startswith('tariffs_v2.') for rule in app.url_map.iter_rules()))

    def test_prepare_schema_is_additive_and_idempotent_without_live_activation(self):
        self.issue()
        db.session.execute(text('ALTER TABLE tariff_v2_coverage DROP COLUMN is_test'))
        db.session.commit()
        stock = self.product.stock_quantity
        db.session.rollback()
        for _ in range(2):
            result = self.app.test_cli_runner().invoke(args=['tariffs-v2-release', '--prepare'])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(REVISION, result.output)
            self.assertIn('"activation_changed": false', result.output)
        self.assertIn('is_test', {row['name'] for row in inspect(db.engine).get_columns('tariff_v2_coverage')})
        self.assertEqual(self.product.stock_quantity, stock)

    def test_test_financial_rows_are_reported_never_promoted_or_deleted(self):
        self.issue()
        report = readiness()
        block = next(item for item in report['blockers'] if item['code'] == 'test_data')
        self.assertEqual(block['counts']['tariff_v2_storage_invoices'], 1)
        self.assertFalse(report['ready'])
        self.assertFalse(report['activation_changed'])

    def test_empty_database_refused_before_any_v2_table_created(self):
        from sqlalchemy import create_engine
        engine = create_engine('sqlite://')
        try:
            with self.assertRaisesRegex(ValueError, 'базовая схема'):
                prepare_schema(engine)
            self.assertEqual(inspect(engine).get_table_names(), [])
        finally:
            engine.dispose()
