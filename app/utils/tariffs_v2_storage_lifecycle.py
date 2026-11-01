"""Non-destructive three-calendar-day storage expiry planning.

This module never deletes Product, photos, books, files, orders or payments.
"""
from datetime import timedelta

from sqlalchemy import or_

from app import db
from app.models.products import Product
from app.models.users import Seller
from app.models.tariffs_v2 import TariffV2Audit, TariffV2Coverage, TariffV2Seller
from app.models.tariffs_v2_lifecycle import TariffV2StorageExpiry
from app.utils.tariffs_v2 import TariffError, moscow_date, midnight_utc, stored_utc, storage_quote, utc


def storage_expiry_plan(product, at=None):
    """Read-only projection; month renewal is anchored to Moscow month start.

    Mid-month additional payment after existing coverage gets its first observed
    grace date. We cannot infer an unrecorded legacy category change retroactively.
    """
    quote = storage_quote(product, at)
    today = moscow_date(at)
    current = db.session.get(TariffV2StorageExpiry, product.id)
    if product.status == 'deleted' or quote['due'] == 0:
        return dict(product_id=product.id, due=quote['due'], status='resolved', unpaid_since=None, expires_at=None)
    if current and current.status in ('grace', 'expired'):
        since = current.unpaid_since
        # A confirmed payment since the last check can end the old episode even
        # when a later category change creates another shortfall.
        paid_since = TariffV2Coverage.query.filter(TariffV2Coverage.product_id == product.id,
            TariffV2Coverage.paid_at > current.checked_at).first()
        if paid_since:
            since = today
    else:
        since = max(today.replace(day=1), moscow_date(product.created_at))
        if quote['credit'] > 0:
            since = today
        # Assignments/policies are audited in v2, so don't backdate a newly
        # introduced tariff to before it existed.
        changes = TariffV2Audit.query.filter(or_(
            (TariffV2Audit.action == 'assign_category') & (TariffV2Audit.subject == str(product.category_id)),
            (TariffV2Audit.action == 'seller_policy') & (TariffV2Audit.subject == str(product.seller_id))
        ), TariffV2Audit.created_at <= stored_utc(at)).order_by(TariffV2Audit.created_at.desc()).first()
        if changes:
            since = max(since, moscow_date(changes.created_at))
        policy = db.session.get(TariffV2Seller, product.seller_id)
        if policy and policy.free_storage and policy.benefits_until and utc(policy.benefits_until) <= utc(at):
            since = max(since, moscow_date(policy.benefits_until))
    expires = stored_utc(midnight_utc(since + timedelta(days=3)))
    return dict(product_id=product.id, due=quote['due'], unpaid_since=since,
        expires_at=expires, status='expired' if stored_utc(at) >= expires else 'grace')


def check_storage_expiry(at=None):
    """Record an auditable dry-run queue in the local sandbox, no deletion."""
    from app.utils.tariffs_v2_finance import require_simulator
    require_simulator()
    result = {'grace': 0, 'expired': 0, 'resolved': 0, 'errors': 0}
    products = Product.query.filter(Product.status != 'deleted').order_by(Product.seller_id, Product.id).all()
    for product in products:
        db.session.query(Seller).filter_by(id=product.seller_id).with_for_update().one()
        db.session.refresh(product, with_for_update=True)
        try:
            plan = storage_expiry_plan(product, at)
        except TariffError:
            # Missing/inactive tariff is an admin configuration problem, never
            # a reason to schedule destructive cleanup of a seller's files.
            result['errors'] += 1
            continue
        row = db.session.get(TariffV2StorageExpiry, product.id)
        result[plan['status']] += 1
        if plan['status'] == 'resolved' and row is None:
            continue
        previous = row.status if row else None
        if row is None:
            row = TariffV2StorageExpiry(product_id=product.id, seller_id=product.seller_id)
            db.session.add(row)
        if plan['status'] != 'resolved':
            row.unpaid_since = plan['unpaid_since']
            row.expires_at = plan['expires_at']
        row.seller_id = product.seller_id
        row.status = plan['status']
        row.checked_at = stored_utc(at)
        if previous != row.status:
            db.session.add(TariffV2Audit(actor='system:sandbox_storage', action='storage_expiry_plan_changed',
                subject=str(product.id), details={'from': previous, 'to': row.status, 'deletion_executed': False}))
    return result
