"""Isolated catalog eligibility. Never changes legacy publication or stock."""
from app import db
from app.models.products import Product
from app.models.users import Seller
from app.models.tariffs_v2 import TariffV2Audit
from app.models.tariffs_v2_commerce import TariffV2Listing
from app.utils.tariffs_v2 import (
    TariffError, blocked_for_new_sales, quote_line, storage_quote, stored_utc,
)


def publication_error(product, at=None):
    if product.status != 'approved':
        return 'Карточка должна пройти модерацию.'
    if not product.seller or not product.seller.is_active:
        return 'Магазин недоступен.'
    if blocked_for_new_sales(product.seller_id, at):
        return 'Новые продажи заблокированы до оплаты месячного счета.'
    try:
        quote_line(product, 1, at)
        if not storage_quote(product, at)['may_publish']:
            return 'Для публикации требуется предоплата хранения.'
    except TariffError as error:
        return str(error)
    return None


def is_visible(product, at=None):
    listing = db.session.get(TariffV2Listing, product.id)
    return bool(listing and listing.published and publication_error(product, at) is None)


def set_publication(product_id, seller_id, published, at=None):
    if not isinstance(published, bool):
        raise TariffError('Укажите состояние публикации.')
    db.session.query(Seller).filter_by(id=seller_id).with_for_update().one()
    product = Product.query.filter_by(id=product_id, seller_id=seller_id).with_for_update().first()
    if product is None or product.status == 'deleted':
        raise TariffError('Карточка недоступна продавцу.')
    if published:
        error = publication_error(product, at)
        if error:
            raise TariffError(error)
    row = db.session.get(TariffV2Listing, product_id)
    if row is None:
        row = TariffV2Listing(product_id=product_id)
        db.session.add(row)
    row.published = published
    row.changed_at = stored_utc(at)
    db.session.add(TariffV2Audit(actor=f'seller:{seller_id}', action='sandbox_publication',
        subject=str(product_id), details={'published': published}))
    return row
