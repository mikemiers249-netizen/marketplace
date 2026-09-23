"""Read-only renewal estimates using the first completed-sale timestamp (UTC)."""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import case, func
from app import db
from app.models.orders import Order, OrderItem


def sales_turnover(seller_id, period_start, period_end, category_id=None):
    """Completed sales in [start, end), once per order, never by creation date."""
    if period_end <= period_start:
        return 0.0
    completed_at = case(
        (Order.delivered_at.is_(None), Order.received_at),
        (Order.received_at.is_(None), Order.delivered_at),
        (Order.delivered_at <= Order.received_at, Order.delivered_at),
        else_=Order.received_at,
    )
    if category_id is None:
        query = db.session.query(func.sum(Order.total_price))
    else:
        # Sum matching lines, not the entire order once per matching product.
        query = (db.session.query(func.sum(OrderItem.price_at_order * OrderItem.quantity))
                 .join(Order, Order.id == OrderItem.order_id)
                 .filter(OrderItem.product.has(category_id=category_id)))
    total = query.filter(
        Order.seller_id == seller_id,
        Order.status.in_(('delivered', 'received')),
        completed_at >= period_start,
        completed_at < period_end,
    ).scalar()
    return float(total or 0)


def percentage_amount(turnover, rate):
    return float((Decimal(str(turnover)) * Decimal(str(rate or 0)) / 100)
                 .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def renewal_quote(subscription, now=None):
    """Estimate without advancing a subscription or creating a transaction."""
    now = now or datetime.utcnow()
    row = subscription.row
    start = max(subscription.activated_at,
                subscription.last_billed_at or subscription.activated_at)
    end = min(subscription.expires_at, now)
    category_id = (row.subject_category_id
                   if row.kind == row.KIND_CATEGORY_SALE else None)
    turnover = sales_turnover(subscription.seller_id, start, end, category_id)
    fixed = row.kind == row.KIND_CARDS
    amount = (float(row.price_amount or 0) if fixed
              else percentage_amount(turnover, row.percent_rate))
    return dict(amount=amount, turnover=turnover, period_start=start,
                period_end=max(start, end), as_of=now, fixed=fixed,
                rate=float(row.percent_rate or 0), category_id=category_id)
