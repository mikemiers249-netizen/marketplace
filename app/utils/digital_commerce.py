"""Electronic delivery on top of the existing product, promotion and order engine."""
from datetime import datetime
from flask import has_request_context, request, session
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria
from app import db
from app.models.digital import DigitalBook, DigitalPurchase
from app.models.products import Product


def sync_book(book):
    if not book.category_id and not book.category:
        return None
    product = book.commerce_product
    if product is None:
        import secrets
        product = Product(digital_book=book, slug='digital-' + (book.system_sku or book.article or secrets.token_hex(12)),
                          current_discount=0, max_discount_percent=0, stock_quantity=1)
        db.session.add(product)
    product.name = book.title
    product.description = book.description
    product.price = float(book.price)
    if not book.system_sku:
        book.system_sku = Product.generate_system_sku(book.seller_id or book.seller.id)
    book.article = book.article or book.system_sku
    product.article = book.article
    product.system_sku = book.system_sku
    product.category = book.category
    product.seller_id = book.seller_id or book.seller.id
    from app.models.products import ProductParameter
    saved = ProductParameter.query.filter_by(product_id=product.id).all() if product.id else []
    existing = {p.parameter_id: p for p in saved + [p for p in db.session.new
                if isinstance(p, ProductParameter) and p.product is product]}
    for key, value in (book.parameter_values or {}).items():
        row = existing.pop(int(key), None)
        if row is None:
            row = ProductParameter(product=product, parameter_id=int(key))
            db.session.add(row)
        row.value = value
        row.display_value = str(value)
    for row in existing.values():
        db.session.delete(row)
    product.status = ('deleted' if book.moderation_status == 'deleted' else
                      'approved' if book.published and book.moderation_status == 'approved' else 'draft')
    if product.status == 'approved' and not product.published_at:
        product.published_at = datetime.utcnow()
    return product


@event.listens_for(Session, 'before_flush')
def sync_changed_books(session, flush_context, instances):
    for book in list(session.new) + list(session.dirty):
        if isinstance(book, DigitalBook) and book not in session.deleted:
            sync_book(book)
            # Keep the purchased copy current, independently of catalog visibility.
            # Deleted catalog entries are retained as archives for existing owners.
            if book.id:
                for purchase in session.query(DigitalPurchase).filter_by(book_id=book.id):
                    purchase.file_name, purchase.format = book.file_name, book.format
                    purchase.title = book.title


@event.listens_for(Session, 'do_orm_execute')
def hide_closed_pilot_products(state):
    if not state.is_select or not has_request_context():
        return
    if request.endpoint == 'payments.result':
        # Signed server callbacks have no buyer session; fulfilment needs order products.
        return
    # Do not resolve current_user while its own Buyer/Seller query is loading.
    if not any(mapper.class_ is Product for mapper in state.all_mappers):
        return
    from app.blueprints.digital import pilot_allowed
    if not pilot_allowed():
        state.statement = state.statement.options(with_loader_criteria(
            Product, lambda model: model.digital_book_id.is_(None), include_aliases=True))


def reserve_books(order, items):
    """Snapshot files before payment, without granting library access."""
    for item in items:
        book = item.product.digital_book
        if book:
            db.session.add(DigitalPurchase(buyer_id=order.buyer_id, book_id=book.id,
                order_id=order.id, title=book.title, file_name=book.file_name, format=book.format))


def complete_digital_delivery(order):
    items = order.items.all()
    if order.digital_purchases and (not items or all(item.product and item.product.digital_book_id for item in items)):
        order.status = 'received'
        order.received_at = order.received_at or datetime.utcnow()
        from app.utils.loyalty import accrue_bonuses_for_order
        accrue_bonuses_for_order(order)
