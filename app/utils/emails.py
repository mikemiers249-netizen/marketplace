"""Утилиты для отправки email-уведомлений."""

import logging
from flask_mail import Message
from app import db
from app.utils.email_service import enqueue
from app.models.orders import Order, OrderItem
from app.models.users import Seller, Buyer
from app.models.communications import Message as MessageModel

logger = logging.getLogger(__name__)


def create_order_conversation(order):
    """
    Создаёт пустой диалог для заказа между покупателем и продавцом.
    Это позволяет им обсуждать детали заказа в разделе "Заказы".
    
    Создаёт два системных сообщения - одно для покупателя, одно для продавца.
    
    Args:
        order: Объект заказа Order
    
    Returns:
        tuple: (Message для покупателя, Message для продавца) или (None, None) при ошибке
    """
    try:
        from app.models.users import Seller
        seller = db.session.get(Seller, order.seller_id)
        
        # Сообщение для покупателя
        buyer_message = MessageModel(
            sender_type='buyer',
            sender_id=order.buyer_id,
            receiver_type='seller',
            receiver_id=order.seller_id,
            text=f'Заказ #{order.order_number} оформлен. Обсудите детали с продавцом.',
            is_system=True,
            conversation_type='order',
            conversation_id=order.id
        )
        db.session.add(buyer_message)
        db.session.flush()
        
        # Сообщение для продавца (то же самое, но sender - продавец)
        seller_message = MessageModel(
            sender_type='seller',
            sender_id=order.seller_id,
            receiver_type='buyer',
            receiver_id=order.buyer_id,
            text=f'Получен новый заказ #{order.order_number}. Обсудите детали с покупателем.',
            is_system=True,
            conversation_type='order',
            conversation_id=order.id
        )
        db.session.add(seller_message)
        db.session.flush()
        
        logger.info(f"Created order conversation for order {order.id}")
        return buyer_message, seller_message
        
    except Exception as e:
        logger.error(f"Failed to create order conversation: {e}")
        return None, None


def order_notification_details(order):
    with db.session.no_autoflush:
        from datetime import datetime, timezone, timedelta
        created = order.created_at or datetime.utcnow()
        when = (created.replace(tzinfo=timezone.utc) if created and created.tzinfo is None else created)
        when = when.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M:%S МСК') if when else '—'
        buyer = order.buyer or db.session.get(Buyer, order.buyer_id)
        customer = f'{buyer.full_name} (ID {buyer.id})' if buyer else '—'
        contact = f'{buyer.email or "—"}; телефон: {buyer.phone or "—"}' if buyer else '—'
        positions = list(order.items.all())
        positions.extend(i for i in db.session.new if isinstance(i, OrderItem)
                         and (i.order is order or (order.id is not None and i.order_id == order.id)))
        items = '\n'.join(
            f'  - {i.product.name if i.product else "Удалённый товар"}: {i.quantity} шт. × '
            f'{i.price_at_order:.2f} ₽ = {i.total_price:.2f} ₽' for i in positions) or '—'
        if order.digital_purchase:
            purchase = order.digital_purchase
            items = f'  - {purchase.title}: 1 электронная книга = {order.total_price:.2f} ₽'
        delivery = order.delivery_service.name if order.delivery_service else ('Электронная книга — библиотека покупателя' if order.digital_purchase else 'Не указан')
        return (f'Номер заказа: {order.order_number}\nДата и время: {when}\n'
                f'Покупатель: {customer}\nКонтакты: {contact}\n\nПозиции:\n{items}\n\n'
                f'Сумма товаров: {order.total_price:.2f} ₽\nДоставка: {order.delivery_price or 0:.2f} ₽\n'
                f'Бонусы: {order.bonus_used or 0:.2f} ₽\nПромокод: {order.promo_discount or 0:.2f} ₽\n'
                f'Итого: {order.grand_total:.2f} ₽\nСпособ доставки: {delivery}\n'
                f'ПВЗ: {order.pvz_code or "—"}\nАдрес доставки: {order.delivery_address or "—"}\n')


def send_new_order_notification_to_seller(order_id):
    """
    Отправка уведомления продавцу о новом заказе.
    
    Args:
        order_id: ID заказа
    
    Returns:
        True если уведомление отправлено, False в противном случае
    """
    try:
        order = db.session.get(Order, order_id)
        if not order:
            logger.warning(f"Order {order_id} not found for notification")
            return False
        
        seller = db.session.get(Seller, order.seller_id)
        if not seller:
            logger.warning(f"Seller {order.seller_id} not found for order {order_id}")
            return False
        
        if not seller.email:
            logger.warning(f"Seller {seller.id} has no email address")
            return False
        
        subject = f"Wimli — новый заказ #{order.order_number}"
        body = f'Здравствуйте, {seller.store_name}!\n\nНовый заказ.\n\n' + order_notification_details(order)

        if not enqueue(seller.email, subject, body):
            return False
        db.session.commit()
        logger.info(f"Order notification queued for seller {seller.id} (email: {seller.email}) for order {order_id}")
        return True
        
    except Exception as e:
        import traceback
        logger.error(f"Failed to send order notification: {e}")
        logger.error(traceback.format_exc())
        return False


def send_order_status_update_to_seller(order_id, old_status=None):
    """
    Отправка уведомления продавцу об изменении статуса заказа.
    
    Args:
        order_id: ID заказа
        old_status: Предыдущий статус (опционально)
    """
    try:
        order = db.session.get(Order, order_id)
        if not order:
            return False
        
        seller = db.session.get(Seller, order.seller_id)
        if not seller or not seller.email:
            return False
        
        status_texts = {
            'processing': 'в обработке',
            'shipped': 'отправлен',
            'delivered': 'доставлен',
            'received': 'получен покупателем',
            'canceled': 'отменён'
        }
        
        new_status_text = status_texts.get(order.status, order.status)
        
        subject = f"📋 Обновление статуса заказа #{order.order_number}"
        
        body = f"""
Здравствуйте, {seller.store_name}!

Статус заказа #{order.order_number} изменился.

Новый статус: {new_status_text}
Дата: {order.created_at.strftime('%d.%m.%Y %H:%M')}

---
Маркетплейс
"""
        
        msg = Message(
            subject=subject,
            recipients=[seller.email],
            body=body
        )
        
        if not enqueue(seller.email, subject, body):
            return False
        db.session.commit()
        logger.info(f"Status update notification queued for seller {seller.id} for order {order_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send status update notification: {e}")
        return False
