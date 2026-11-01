"""Deletion rehearsal for generated replicas ONLY. No legacy file paths are used.

The executor owns commits: durable 'purging' precedes file deletion, so a crash
can be retried without treating a partially removed replica as available.
"""
import hashlib
import re
import secrets
import stat
from pathlib import Path

from flask import current_app
from sqlalchemy import or_

from app import db
from app.models.products import Product
from app.models.users import Seller
from app.models.tariffs_v2 import TariffV2Audit
from app.models.tariffs_v2_lifecycle import TariffV2Replica, TariffV2ReplicaStore
from app.utils.tariffs_v2 import TariffError, stored_utc

MARKER = '.wimli-tariffs-v2-replicas'


def sandbox():
    from app.utils.tariffs_v2_finance import require_simulator
    require_simulator()


def no_links(path):
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise TariffError('Ссылки и точки перенаправления запрещены в хранилище копий.')


def configured_root():
    sandbox()
    value = current_app.config.get('TARIFFS_V2_REPLICA_ROOT')
    if not value or not Path(value).is_absolute():
        raise TariffError('Задайте отдельный абсолютный TARIFFS_V2_REPLICA_ROOT.')
    root = Path(value)
    no_links(root)
    root = root.resolve()
    if root.parent == root:
        raise TariffError('Корень диска нельзя использовать для тестовых копий.')
    for value in (current_app.config.get('UPLOAD_FOLDER'), current_app.static_folder,
                  current_app.config.get('DIGITAL_UPLOAD_FOLDER')):
        if value:
            protected = Path(value).resolve()
            if root == protected or root in protected.parents or protected in root.parents:
                raise TariffError('Хранилище копий должно быть отдельно от рабочих загрузок и static.')
    return root


def regular(path):
    no_links(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TariffError('Разрешены только обычные файлы без жестких ссылок.')


def marker_text(store):
    return 'wimli-tariffs-v2-replicas:' + store.token


def store_root():
    root = configured_root()
    store = db.session.get(TariffV2ReplicaStore, 1)
    if not store or store.root != str(root):
        raise TariffError('Хранилище копий не зарегистрировано для этой базы.')
    marker = root / MARKER
    if not marker.exists():
        raise TariffError('Маркер хранилища копий отсутствует.')
    regular(marker)
    if marker.read_text(encoding='utf-8') != marker_text(store):
        raise TariffError('Маркер хранилища копий не соответствует базе.')
    return root


def initialize_store():
    root = configured_root()
    if db.session.get(TariffV2ReplicaStore, 1):
        return store_root()
    if root.exists() and any(root.iterdir()):
        raise TariffError('Новое хранилище копий должно быть пустым.')
    root.mkdir(parents=True, exist_ok=True)
    store = TariffV2ReplicaStore(id=1, root=str(root), token=secrets.token_hex(24))
    with (root / MARKER).open('x', encoding='utf-8') as stream:
        stream.write(marker_text(store))
    db.session.add(store)
    return root


def create_replica(product_id, payloads, at=None):
    """Write independent byte copies; source upload paths are never registered."""
    root = store_root()
    if not payloads or len(payloads) > 20 or any(not isinstance(value, bytes) for value in payloads):
        raise TariffError('Передайте от 1 до 20 файлов как независимые байтовые копии.')
    if sum(len(value) for value in payloads) > 20 * 1024 * 1024:
        raise TariffError('Для репетиции размер копий ограничен 20 МБ.')
    product = db.session.get(Product, product_id)
    if not product or product.status == 'deleted':
        raise TariffError('Карточка не найдена.')
    db.session.query(Seller).filter_by(id=product.seller_id).with_for_update().one()
    db.session.refresh(product, with_for_update=True)
    if TariffV2Replica.query.filter_by(active_product_id=product.id).first():
        raise TariffError('У карточки уже есть активная тестовая копия.')
    directory = secrets.token_hex(24)
    target = root / directory
    target.mkdir()
    files = []
    for payload in payloads:
        name = secrets.token_hex(24) + '.bin'
        with (target / name).open('xb') as stream:
            stream.write(payload)
        files.append(dict(name=name, size=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
    row = TariffV2Replica(product_id=product.id, active_product_id=product.id,
        seller_id=product.seller_id, directory=directory, files=files,
        status='active', created_at=stored_utc(at))
    db.session.add(row)
    db.session.flush()
    return row


def replica_is_purged(product_id):
    rows = TariffV2Replica.query.filter_by(product_id=product_id).all()
    return bool(rows and not any(row.status == 'active' for row in rows))


def validate_files(root, row, resuming=False):
    if not re.fullmatch('[0-9a-f]{48}', row.directory):
        raise TariffError('Некорректный каталог копии.')
    directory = root / row.directory
    no_links(directory)
    if not directory.is_dir():
        raise TariffError('Каталог копии отсутствует.')
    names = [item.get('name', '') for item in row.files]
    if len(names) != len(set(names)) or any(not re.fullmatch(r'[0-9a-f]{48}\.bin', name) for name in names):
        raise TariffError('Некорректный реестр файлов копии.')
    if set(path.name for path in directory.iterdir()) - set(names):
        raise TariffError('В каталоге копии обнаружены незарегистрированные файлы.')
    paths = []
    for item in row.files:
        path = directory / item['name']
        if not path.exists() and not path.is_symlink():
            if resuming:
                continue
            raise TariffError('Зарегистрированный файл копии отсутствует.')
        regular(path)
        if path.stat().st_size != item['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise TariffError('Содержимое копии изменилось после регистрации.')
        paths.append(path)
    return paths


def purge_replica(replica_id, at=None):
    """Own transaction; durable start -> registered files only -> durable finish."""
    from app.models.tariffs_v2_commerce import TariffV2Listing, TariffV2CheckoutLine, TariffV2SandboxOrder, TariffV2Checkout
    from app.models.tariffs_v2_finance import TariffV2Claim, TariffV2CheckoutRefund
    from app.utils.tariffs_v2_storage_lifecycle import storage_expiry_plan
    root = store_root()
    row = db.session.get(TariffV2Replica, replica_id)
    if not row:
        raise TariffError('Тестовая копия не найдена.')
    db.session.query(Seller).filter_by(id=row.seller_id).with_for_update().one()
    product = Product.query.filter_by(id=row.product_id, seller_id=row.seller_id).with_for_update().first()
    db.session.refresh(row, with_for_update=True)
    if row.status == 'purged':
        return row
    paths = validate_files(root, row, resuming=row.status == 'purging')
    if row.status == 'active':
        if not product or storage_expiry_plan(product, at)['status'] != 'expired':
            raise TariffError('Срок хранения не истек или оплата восстановлена.')
        obligations = TariffV2CheckoutLine.query.join(TariffV2SandboxOrder).join(TariffV2Checkout).filter(
            TariffV2CheckoutLine.product_id == product.id,
            or_((TariffV2Checkout.status == 'pending') & (TariffV2Checkout.expires_at > stored_utc(at)),
                (TariffV2Checkout.status == 'complete') & TariffV2SandboxOrder.status.in_(('ready', 'shipped')))).first()
        claim = TariffV2Claim.query.join(TariffV2SandboxOrder, TariffV2Claim.order_id == TariffV2SandboxOrder.id).join(
            TariffV2CheckoutLine).filter(TariffV2CheckoutLine.product_id == product.id,
                TariffV2Claim.active_order_id.isnot(None)).first()
        refund = TariffV2CheckoutRefund.query.join(TariffV2CheckoutLine).filter(
            TariffV2CheckoutLine.product_id == product.id, TariffV2CheckoutRefund.status == 'pending').first()
        if obligations or claim or refund:
            raise TariffError('Сначала необходимо завершить связанные тестовые заказы.')
        row.status = 'purging'
        row.purge_started_at = stored_utc(at)
        db.session.add(TariffV2Audit(actor='system:replica_cleanup', action='replica_purge_started',
            subject=str(row.id), details={'product_id': product.id, 'replica_only': True}))
        db.session.commit()
    # Revalidate after the durable boundary. No recursive unlink or arbitrary
    # path deletion is allowed. Empty container directory is deliberately kept.
    root = store_root()
    paths = validate_files(root, row, resuming=True)
    for path in paths:
        regular(path)
        path.unlink()
    row.status = 'purged'
    row.active_product_id = None
    row.purged_at = stored_utc(at)
    listing = db.session.get(TariffV2Listing, row.product_id)
    if listing:
        listing.published = False
    db.session.add(TariffV2Audit(actor='system:replica_cleanup', action='replica_purge_completed',
        subject=str(row.id), details={'product_id': row.product_id, 'replica_only': True, 'files': len(row.files)}))
    db.session.commit()
    return row
