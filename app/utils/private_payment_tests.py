"""Two owner-requested one-ruble fixtures, restricted to one buyer account."""
from flask import abort, current_app, request, session
from flask_login import current_user
from app import db

BUYER_EMAIL = 'ronnie83@mail.ru'
DIGITAL_SKU = 'WML-PRIVATE-PAYMENT-DIGITAL-1R'
PHYSICAL_SKU = 'WML-PRIVATE-PAYMENT-PHYSICAL-1R'
PRIVATE_SKUS = (DIGITAL_SKU, PHYSICAL_SKU)


def allowed_buyer(user=None):
    from app.models.users import Buyer
    user = current_user if user is None else user
    return bool(isinstance(user, Buyer) and user.is_active
                and (user.email or '').strip().casefold() == BUYER_EMAIL)


def require_buyer():
    if not allowed_buyer():
        abort(404)


def private_view_allowed():
    from app.models.users import Seller
    return bool(allowed_buyer() or session.get('main_admin_authenticated') is True or
        (isinstance(current_user, Seller) and request.endpoint and
         (request.endpoint.startswith('seller.') or request.endpoint.startswith('digital.'))))


def private_order_allowed(order):
    # The one-ruble fixtures are retired; no personal payment exception remains.
    return False


def retire_private_products_and_enable_payments():
    """Owner-requested one-time rollout: commit test removal BEFORE enabling sales."""
    from app.models.users import Buyer, Seller
    from app.models.products import Product
    from app.models.digital import DigitalBook
    from app.models.orders import Order, OrderItem, CartItem
    from app.models.communications import Settings
    from app.blueprints.payments import release_reservation
    marker_key = 'private_payment_tests_retired_and_sales_open_20261103'
    seller_email = (current_app.config.get('ROBOKASSA_SELLER_EMAIL') or '').strip().lower()
    seller = Seller.query.filter(db.func.lower(Seller.email) == seller_email).with_for_update().first() if seller_email else None
    if not seller:
        raise ValueError('Cannot reopen payments: configured seller not found.')
    marker = db.session.get(Settings, marker_key)
    if marker and marker.value == 'done':
        current_app.logger.info('Test retirement already completed; configured store payment pause=%s (preserved).', seller.payments_paused)
        db.session.commit()
        return
    products = Product.query.filter(Product.system_sku.in_(PRIVATE_SKUS)).order_by(Product.id).all()
    if any(product.seller_id != seller.id for product in products):
        raise ValueError('Private test product belongs to an unexpected seller.')
    product_ids = [product.id for product in products]
    pending = Order.query.join(OrderItem).filter(OrderItem.product_id.in_(product_ids),
        Order.status == 'awaiting_payment').order_by(Order.buyer_id, Order.id).all() if product_ids else []
    for order in pending:
        Buyer.query.filter_by(id=order.buyer_id).with_for_update().one()
        order = Order.query.filter_by(id=order.id).populate_existing().with_for_update().one()
        release_reservation(order)
    for book in DigitalBook.query.filter(DigitalBook.system_sku.in_(PRIVATE_SKUS)).all():
        if book.seller_id != seller.id:
            raise ValueError('Private test book belongs to an unexpected seller.')
        book.published = False
        book.moderation_status = 'deleted'
    for product in products:
        product.status = 'deleted'
        product.stock_quantity = 0
    if product_ids:
        CartItem.query.filter(CartItem.product_id.in_(product_ids)).delete(synchronize_session=False)
    if marker is None:
        marker = Settings(key=marker_key, value_type='text',
            description='One-time owner request: retire private test products, then resume public payments.')
        db.session.add(marker)
    marker.value = 'removed'
    db.session.commit()
    current_app.logger.info('Test products retired and committed: products=%s; paid orders and library copies preserved.', product_ids)
    # Separate transaction enforces the user's requested sequence. A durable
    # marker prevents a future deployment from undoing a new manual sales pause.
    seller = Seller.query.filter_by(id=seller.id).populate_existing().with_for_update().one()
    marker = Settings.query.filter_by(key=marker_key).populate_existing().with_for_update().one()
    if marker.value != 'done':
        seller.payments_paused = False
        marker.value = 'done'
        db.session.commit()
        current_app.logger.info('Public payments enabled for all buyers: seller=%s payments_paused=False; private tests retired first.', seller.id)
    else:
        db.session.commit()
