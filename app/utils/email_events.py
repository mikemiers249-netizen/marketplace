"""Enqueue order status changes in the same transaction as the order."""
from flask import has_app_context
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from app.models.orders import Order
from app.utils.email_service import configured, enqueue


@event.listens_for(Session, 'before_flush')
def queue_order_status(session, flush_context, instances):
    if not has_app_context() or not configured():
        return
    labels = {'pending': 'Ожидает оплаты', 'paid': 'Оплачен', 'processing': 'В обработке',
              'assembled': 'Собран', 'shipped': 'Отправлен', 'in_transit': 'В пути', 'delivered': 'Доставлен',
              'received': 'Получен покупателем', 'canceled': 'Отменён', 'cancelled': 'Отменён'}
    for order in list(session.dirty):
        if not isinstance(order, Order) or not inspect(order).attrs.status.history.has_changes():
            continue
        subject = f'Wimli — заказ №{order.order_number}: {labels.get(order.status, order.status)}'
        body = f'Статус заказа №{order.order_number}: {labels.get(order.status, order.status)}.\nПодробности доступны в личном кабинете.\n\nWimli'
        for user in (order.buyer, order.seller):
            if user and user.is_active and user.email:
                enqueue(user.email, subject, body)
