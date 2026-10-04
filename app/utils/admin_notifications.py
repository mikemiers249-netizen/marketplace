"""Administrator event emails queued atomically with platform changes."""
import re

from flask import current_app, has_app_context
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.models.communications import Message, Review, Settings
from app.models.orders import Order
from app.models.products import Product
from app.models.users import Buyer, Seller
from app.utils.email_service import configured, enqueue

SETTING_KEY = 'admin_email_notifications'
EVENTS = {
    'buyer_registered': 'Новые зарегистрированные покупатели',
    'seller_registered': 'Новые зарегистрированные продавцы',
    'order_created': 'Новые оформленные заказы на платформе',
    'review_moderation': 'Новые отзывы, требующие модерации',
    'product_moderation': 'Товары, отправленные на модерацию',
    'buyer_message': 'Сообщения в поддержку от покупателей',
    'seller_message': 'Сообщения в поддержку от продавцов',
}


def notification_settings():
    return Settings.get(SETTING_KEY, {}) or {}


def valid_email(value):
    return len(value) <= 254 and re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+', value) is not None


def entering(session, row, state, new, default=None):
    if row.status != state and not (new and row.status is None and default == state):
        return False
    if new:
        return True
    history = inspect(row).attrs.status.history
    if not history.has_changes():
        return False
    model = type(row)
    previous = (history.deleted[0] if history.deleted else
                session.connection().execute(select(model.status).where(model.id == row.id)).scalar())
    return previous != state


@event.listens_for(Session, 'before_flush')
def queue_admin_notifications(session, flush_context, instances):
    if not has_app_context() or not configured():
        return
    preferences = notification_settings()
    recipient = preferences.get('recipient', '')
    if not preferences.get('enabled') or not valid_email(recipient):
        return
    selected = preferences.get('events', [])
    new_rows = list(session.new)
    for row in new_rows + list(session.dirty):
        new = row in new_rows
        kind = text = None
        if new and isinstance(row, Buyer):
            kind, text = 'buyer_registered', f'Зарегистрирован новый покупатель: {row.login}.'
        elif new and isinstance(row, Seller):
            kind, text = 'seller_registered', f'Зарегистрирован новый продавец: {row.login}. Магазин: {row.store_name or "не указан"}.'
        elif isinstance(row, Order):
            history = inspect(row).attrs.status.history
            previous = None
            if not new and history.has_changes():
                previous = (history.deleted[0] if history.deleted else
                            session.connection().execute(select(Order.status).where(Order.id == row.id)).scalar())
            if (new and row.status not in (None, 'pending', 'canceled', 'cancelled')) or (
                    not new and history.has_changes() and previous == 'pending'
                    and row.status not in ('pending', 'canceled', 'cancelled')):
                kind, text = 'order_created', f'На платформе оформлен новый заказ №{row.order_number}.'
        elif isinstance(row, Review) and entering(session, row, 'pending', new, default='pending'):
            kind, text = 'review_moderation', 'Поступил новый отзыв, требующий модерации.'
        elif isinstance(row, Product) and entering(session, row, 'on_moderation', new):
            kind, text = 'product_moderation', f'Товар «{row.name}» отправлен на модерацию.'
        elif (new and isinstance(row, Message) and row.receiver_type == 'admin'
              and row.receiver_id == 0 and not row.is_system
              and row.sender_type in ('buyer', 'seller')):
            kind = row.sender_type + '_message'
            sender = 'покупателя' if row.sender_type == 'buyer' else 'продавца'
            text = f'Получено новое сообщение в поддержку от {sender} #{row.sender_id}.'
            if row.text:
                text += '\n\n' + row.text[:2000]
            if row.image_path or row.file_path:
                text += '\nК сообщению приложено вложение. Оно доступно в админке.'
        if kind in selected:
            base = current_app.config['PUBLIC_BASE_URL'].rstrip('/')
            login = base + '/main_admin/auth/login'
            enqueue(recipient, 'Wimli — ' + EVENTS[kind],
                    text + '\n\nВойти в админку:\n' + login +
                    '\nВведите логин и пароль администратора.\n\nWimli')
