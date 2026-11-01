import os
import stat
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from app import db
from app.models.tariffs_v2_lifecycle import TariffV2Replica
from app.models.tariffs_v2_commerce import TariffV2Listing
from app.utils.tariffs_v2 import TariffError, storage_quote, record_storage_payment
from app.utils.tariffs_v2_commerce import is_visible
from app.utils.tariffs_v2_payments import issue_storage_invoice
from app.utils.tariffs_v2_replicas import (
    MARKER, initialize_store, create_replica, purge_replica, store_root,
)
import test_tariffs_v2_lifecycle as foundation


class ReplicaTests(unittest.TestCase):
    setUpBase = foundation.LifecycleTests.setUp
    tearDown = foundation.LifecycleTests.tearDown
    setup_renewal = foundation.LifecycleTests.setup_renewal
    add = foundation.LifecycleTests.add
    checkout = foundation.LifecycleTests.checkout
    callback = foundation.LifecycleTests.callback
    pay = foundation.LifecycleTests.pay
    ready = foundation.LifecycleTests.ready

    def setUp(self):
        self.setUpBase()
        self.root = Path(self.tmp.name) / 'independent-replicas'
        self.app.config['TARIFFS_V2_REPLICA_ROOT'] = str(self.root)
        initialize_store()
        db.session.commit()
        self.source = Path(self.tmp.name) / 'original-upload.bin'
        self.source.write_bytes(b'original photo must survive')

    def replica(self, payloads=None):
        row = create_replica(self.product.id, payloads or [self.source.read_bytes()], self.now)
        db.session.commit()
        return row

    def expired_at(self):
        return self.setup_renewal() + timedelta(days=4)

    def test_expired_replica_deleted_but_source_product_history_and_stock_survive(self):
        at = self.expired_at()
        row = self.replica()
        copied = self.root / row.directory / row.files[0]['name']
        self.assertEqual(copied.read_bytes(), self.source.read_bytes())
        purge_replica(row.id, at)
        self.assertFalse(copied.exists())
        self.assertEqual(self.source.read_bytes(), b'original photo must survive')
        self.assertEqual(self.product.status, 'approved')
        self.assertEqual(self.product.stock_quantity, 10)
        self.assertEqual(row.status, 'purged')
        self.assertFalse(db.session.get(TariffV2Listing, self.product.id).published)
        self.assertFalse(is_visible(self.product, at))
        self.assertFalse(storage_quote(self.product, at)['may_publish'])
        self.assertEqual(purge_replica(row.id, at).status, 'purged')
        with self.assertRaises(TariffError):
            issue_storage_invoice(self.product.id, self.seller.id, at)

    def test_grace_and_late_payment_prevent_purge(self):
        start = self.setup_renewal()
        row = self.replica()
        with self.assertRaises(TariffError):
            purge_replica(row.id, start)
        db.session.rollback()
        at = start + timedelta(days=4)
        record_storage_payment(self.product, storage_quote(self.product, at)['due'], 'late-paid', at)
        db.session.commit()
        with self.assertRaises(TariffError):
            purge_replica(row.id, at)
        self.assertEqual(row.status, 'active')
        self.assertTrue((self.root / row.directory / row.files[0]['name']).exists())

    def test_active_orders_postpone_replica_cleanup(self):
        self.ready()
        at = self.expired_at()
        row = self.replica()
        with self.assertRaisesRegex(TariffError, 'заказы'):
            purge_replica(row.id, at)
        self.assertEqual(row.status, 'active')

    def test_recovery_after_one_file_deleted_and_process_stops(self):
        at = self.expired_at()
        row = self.replica([b'first', b'second'])
        original_unlink = Path.unlink
        calls = []
        def fail_second(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('simulated interruption')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_second):
            with self.assertRaises(OSError):
                purge_replica(row.id, at)
        db.session.rollback()
        db.session.expire_all()
        self.assertEqual(row.status, 'purging')
        self.assertFalse(storage_quote(self.product, at)['may_publish'])
        purge_replica(row.id, at)
        self.assertEqual(row.status, 'purged')
        self.assertEqual(list((self.root / row.directory).iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_unknown_file_or_changed_contents_block_all_deletion(self):
        at = self.expired_at()
        row = self.replica([b'first', b'second'])
        directory = self.root / row.directory
        unrelated = directory / 'unregistered.txt'
        unrelated.write_bytes(b'keep')
        with self.assertRaises(TariffError):
            purge_replica(row.id, at)
        self.assertEqual(len(list(directory.iterdir())), 3)
        unrelated.unlink()
        second = directory / row.files[1]['name']
        second.write_bytes(b'changed')
        with self.assertRaises(TariffError):
            purge_replica(row.id, at)
        self.assertTrue((directory / row.files[0]['name']).exists())
        self.assertEqual(row.status, 'active')

    def test_tampered_relative_and_absolute_manifest_paths_cannot_escape(self):
        at = self.expired_at()
        row = self.replica()
        for name in ('../original-upload.bin', str(self.source)):
            row.files = [dict(name=name, size=1, sha256='')]
            db.session.commit()
            with self.assertRaises(TariffError):
                purge_replica(row.id, at)
            db.session.rollback()
        self.assertEqual(self.source.read_bytes(), b'original photo must survive')

    def test_hardlinked_replica_is_rejected_without_touching_original(self):
        at = self.expired_at()
        row = self.replica()
        copied = self.root / row.directory / row.files[0]['name']
        copied.unlink()
        os.link(self.source, copied)
        with self.assertRaises(TariffError):
            purge_replica(row.id, at)
        self.assertTrue(self.source.exists())
        self.assertTrue(copied.exists())

    def test_symlinked_file_is_rejected(self):
        at = self.expired_at()
        row = self.replica()
        copied = self.root / row.directory / row.files[0]['name']
        copied.unlink()
        try:
            copied.symlink_to(self.source)
        except OSError:
            self.skipTest('Host does not permit creation of symbolic links.')
        with self.assertRaises(TariffError):
            purge_replica(row.id, at)
        self.assertTrue(self.source.exists())

    def test_windows_reparse_attribute_blocks_directory_before_deletion(self):
        at = self.expired_at()
        row = self.replica()
        directory = self.root / row.directory
        real_lstat = Path.lstat
        def reparse_info(path, *args, **kwargs):
            if path == directory:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400, st_nlink=1)
            return real_lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', reparse_info):
            with self.assertRaises(TariffError):
                purge_replica(row.id, at)
        self.assertTrue((directory / row.files[0]['name']).exists())

    def test_nonempty_unregistered_store_cannot_be_adopted(self):
        from app.models.tariffs_v2_lifecycle import TariffV2ReplicaStore
        db.session.delete(db.session.get(TariffV2ReplicaStore, 1))
        db.session.commit()
        with self.assertRaises(TariffError):
            initialize_store()
        self.assertTrue((self.root / MARKER).exists())

    def test_missing_or_foreign_marker_and_upload_root_rejected(self):
        marker = self.root / MARKER
        saved = marker.read_text(encoding='utf-8')
        marker.write_text('foreign database', encoding='utf-8')
        with self.assertRaises(TariffError):
            store_root()
        marker.write_text(saved, encoding='utf-8')
        self.app.config['TARIFFS_V2_REPLICA_ROOT'] = self.app.config['UPLOAD_FOLDER']
        with self.assertRaises(TariffError):
            store_root()
        self.app.config['TARIFFS_V2_REPLICA_ROOT'] = str(self.root)
        self.app.config['TARIFFS_V2_SANDBOX'] = False
        with self.assertRaises(TariffError):
            store_root()

    def test_cli_preview_never_deletes_and_new_copy_can_be_created_after_purge(self):
        at = self.expired_at()
        row = self.replica()
        result = self.app.test_cli_runner().invoke(args=['tariffs-v2-replica-purge', '--replica-id', str(row.id)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('Preview only', result.output)
        self.assertTrue((self.root / row.directory / row.files[0]['name']).exists())
        purge_replica(row.id, at)
        replacement = self.replica()
        self.assertNotEqual(row.id, replacement.id)
        self.assertEqual(replacement.status, 'active')
        self.assertTrue(storage_quote(self.product, at)['due'] > 0)
