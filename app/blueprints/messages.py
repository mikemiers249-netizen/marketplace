"""
Унифицированный модуль сообщений для всех типов пользователей.
Поддерживает админов, продавцов и покупателей.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import current_user
from sqlalchemy import or_, and_
from datetime import datetime, timezone

from app import db, csrf
from app.models.communications import Message
from app.models.users import Buyer, Seller, Admin
from flask_login import login_required
from app.utils.decorators import admin_required

bp = Blueprint('messages', __name__)


def get_user_type():
    """
    Определение типа текущего пользователя.
    Returns: 'admin', 'seller', 'buyer' или None
    """
    if not current_user.is_authenticated:
        return None
    
    if isinstance(current_user, Admin):
        return 'admin'
    elif isinstance(current_user, Seller):
        return 'seller'
    elif isinstance(current_user, Buyer):
        return 'buyer'
    return None


def get_user_id():
    """
    Получение ID текущего пользователя.
    Для админа возвращает 0.
    """
    user_type = get_user_type()
    if user_type == 'admin':
        return 0
    return current_user.id if current_user.is_authenticated else None


def get_conversations(user_type, user_id, filter_type=None):
    """
    Получение всех диалогов пользователя.
    
    Args:
        user_type: тип пользователя ('admin', 'seller', 'buyer')
        user_id: ID пользователя (0 для админа)
        filter_type: фильтр для диалогов ('support', 'buyers', 'stores', 'orders' для buyers/sellers)
    
    Returns:
        dict: словарь диалогов
    """
    # Обработка фильтра 'orders' - показываем заказы
    if filter_type == 'orders':
        from app.models.orders import Order
        
        conversations = {}
        
        if user_type == 'buyer':
            # Получаем заказы покупателя
            orders = Order.query.filter_by(buyer_id=user_id).order_by(Order.created_at.desc()).all()
        elif user_type == 'seller':
            # Получаем заказы продавца
            orders = Order.query.filter_by(seller_id=user_id).order_by(Order.created_at.desc()).all()
        else:
            return {}  # У админа нет фильтра заказов
        
        for order in orders:
            partner_id = order.seller_id if user_type == 'buyer' else order.buyer_id
            partner_type = 'seller' if user_type == 'buyer' else 'buyer'
            key = f"order:{order.id}"
            
            partner_name = get_partner_name(partner_type, partner_id)
            
            # Проверяем, есть ли сообщения в этом диалоге
            order_messages = Message.query.filter(
                Message.conversation_type == 'order',
                Message.conversation_id == order.id
            ).order_by(Message.timestamp.desc()).first()
            
            # Считаем непрочитанные сообщения
            unread = Message.query.filter(
                Message.conversation_type == 'order',
                Message.conversation_id == order.id,
                Message.receiver_type == user_type,
                Message.receiver_id == user_id,
                Message.is_read == False
            ).count()
            
            # Имя диалога - номер заказа
            order_key = f"Заказ {order.order_number}"
            
            conversations[key] = {
                'id': f"order_{order.id}",
                'name': order_key,
                'partner_type': partner_type,
                'partner_id': partner_id,
                'order_id': order.id,
                'order_number': order.order_number,
                'last_message': order_messages.text[:50] if order_messages and order_messages.text else 'Обсуждение деталей заказа',
                'last_message_full': order_messages,
                'unread_count': unread,
                'avatar': _get_partner_avatar(partner_type, partner_id),
                'is_order': True
            }
        
        return conversations
    
    # Определяем типы собеседников в зависимости от роли
    if user_type == 'admin':
        # Админ может общаться с покупателями и продавцами
        partner_types = ['buyer', 'seller']
    elif user_type == 'seller':
        # Продавец может общаться с покупателями и поддержкой (admin)
        partner_types = ['buyer', 'admin']
    else:  # buyer
        # Покупатель может общаться с продавцами и поддержкой (admin)
        partner_types = ['seller', 'admin']
    
    # Фильтрация по типу
    if filter_type == 'support':
        partner_types = ['admin']
    elif filter_type == 'buyers' and user_type == 'seller':
        partner_types = ['buyer']
    elif filter_type == 'stores':
        partner_types = ['seller']
    elif filter_type == 'buyers' and user_type == 'admin':
        partner_types = ['buyer']
    elif filter_type == 'sellers' and user_type == 'admin':
        partner_types = ['seller']
    
    # Получаем все сообщения
    all_messages = Message.query.filter(
        or_(
            and_(Message.sender_type == user_type, Message.sender_id == user_id),
            and_(Message.receiver_type == user_type, Message.receiver_id == user_id)
        )
    ).order_by(Message.timestamp.desc()).all()
    
    # Группируем по собеседникам
    conversations = {}
    
    for msg in all_messages:
        # Пропускаем сообщения из order-диалогов - они отображаются в фильтре "Заказы"
        if msg.conversation_type == 'order':
            continue
        
        # Определяем собеседника
        if msg.sender_type == user_type and msg.sender_id == user_id:
            # Текущий пользователь отправил -> собеседник это получатель
            partner_type = msg.receiver_type
            partner_id = msg.receiver_id
        elif msg.receiver_type == user_type and msg.receiver_id == user_id:
            # Текущий пользователь получил -> собеседник это отправитель
            partner_type = msg.sender_type
            partner_id = msg.sender_id
        else:
            continue
        
        # Нормализуем типы
        if partner_type == 'sellers':
            partner_type = 'seller'
        elif partner_type == 'buyers':
            partner_type = 'buyer'
        
        # Применяем фильтр
        if filter_type:
            if user_type == 'buyer':
                if filter_type == 'support' and partner_type != 'admin':
                    continue
                elif filter_type == 'stores' and partner_type != 'seller':
                    continue
            elif user_type == 'seller':
                if filter_type == 'support' and partner_type != 'admin':
                    continue
                elif filter_type == 'buyers' and partner_type != 'buyer':
                    continue
            elif user_type == 'admin':
                if filter_type == 'buyers' and partner_type != 'buyer':
                    continue
                elif filter_type == 'sellers' and partner_type != 'seller':
                    continue
        
        key = f"{partner_type}:{partner_id}"
        
        if key not in conversations:
            # Получаем имя собеседника
            partner_name = get_partner_name(partner_type, partner_id)
            
            # Считаем непрочитанные
            unread = Message.query.filter(
                Message.sender_type == partner_type,
                Message.sender_id == partner_id,
                Message.receiver_type == user_type,
                Message.receiver_id == user_id,
                Message.is_read == False
            ).count()
            
            conversations[key] = {
                'id': partner_id,
                'name': partner_name,
                'partner_type': partner_type,
                'partner_id': partner_id,
                'last_message': msg.text[:50] if msg.text else '',
                'last_message_full': msg,
                'unread_count': unread,
                'avatar': _get_partner_avatar(partner_type, partner_id)
            }
        else:
            # Обновляем last_message если текущее сообщение новее
            # Используем timezone-aware datetime для корректного сравнения
            stored_timestamp = conversations[key].get('_timestamp')
            current_timestamp = msg.timestamp
            
            # Приводим к timezone-aware если нужно
            if stored_timestamp and stored_timestamp.tzinfo is None:
                stored_timestamp = stored_timestamp.replace(tzinfo=timezone.utc)
            if current_timestamp and current_timestamp.tzinfo is None:
                current_timestamp = current_timestamp.replace(tzinfo=timezone.utc)
            
            if current_timestamp > (stored_timestamp or datetime.min.replace(tzinfo=timezone.utc)):
                conversations[key]['last_message'] = msg.text[:50] if msg.text else ''
                conversations[key]['last_message_full'] = msg
                conversations[key]['_timestamp'] = msg.timestamp
    
    return conversations


def get_partner_name(partner_type, partner_id):
    """
    Получение имени собеседника по типу и ID.
    """
    if partner_type == 'buyer':
        partner = Buyer.query.get(partner_id)
        return partner.full_name if partner else f'Покупатель #{partner_id}'
    elif partner_type == 'seller':
        partner = Seller.query.get(partner_id)
        return partner.store_name if partner else f'Магазин #{partner_id}'
    elif partner_type == 'admin':
        return 'Служба поддержки'
    return 'Неизвестный'


def _get_partner_avatar(partner_type, partner_id):
    """
    Получение относительного пути к аватарке собеседника (или None).
    Для магазинов это store_logo, для покупателей — пока None
    (аватарки покупателей не реализованы).
    """
    if partner_type == 'seller':
        partner = Seller.query.get(partner_id)
        return partner.store_logo if partner else None
    if partner_type == 'buyer':
        # У покупателей сейчас нет аватарки, расширим при необходимости
        return None
    return None


def get_partner_avatar(partner_type, partner_id):
    """Публичный алиас для _get_partner_avatar (используется в шаблонах)."""
    return _get_partner_avatar(partner_type, partner_id)


def get_filter_options(user_type):
    """
    Получение доступных фильтров для пользователя.
    
    Returns:
        dict: {ключ: название}
    """
    if user_type == 'buyer':
        return {
            'support': 'Поддержка',
            'stores': 'Магазины',
            'orders': 'Заказы'
        }
    elif user_type == 'seller':
        return {
            'support': 'Поддержка',
            'buyers': 'Покупатели',
            'orders': 'Заказы'
        }
    elif user_type == 'admin':
        return {
            'buyers': 'Покупатели',
            'sellers': 'Продавцы'
        }
    return {}


def mark_messages_read(user_type, user_id, partner_type, partner_id):
    """
    Отметка всех сообщений от партнера как прочитанных.
    """
    messages = Message.query.filter(
        Message.sender_type == partner_type,
        Message.sender_id == partner_id,
        Message.receiver_type == user_type,
        Message.receiver_id == user_id,
        Message.is_read == False
    ).all()
    
    for msg in messages:
        msg.is_read = True
    
    db.session.commit()


def send_message(sender_type, sender_id, receiver_type, receiver_id, text, image_path=None, file_path=None, conversation_type=None, conversation_id=None):
    """
    Отправка сообщения.
    
    Args:
        sender_type: тип отправителя
        sender_id: ID отправителя
        receiver_type: тип получателя
        receiver_id: ID получателя
        text: текст сообщения
        image_path: путь к изображению (опционально)
        file_path: путь к PDF файлу (опционально)
        conversation_type: тип диалога ('order' для заказов)
        conversation_id: ID диалога (ID заказа для заказов)
    
    Returns:
        Message: созданное сообщение или None при ошибке
    """
    if not text or not text.strip():
        if not image_path and not file_path:
            return None
    
    message = Message(
        sender_type=sender_type,
        sender_id=sender_id,
        receiver_type=receiver_type,
        receiver_id=receiver_id,
        text=text.strip() if text else None,
        image_path=image_path,
        file_path=file_path,
        conversation_type=conversation_type,
        conversation_id=conversation_id
    )
    
    db.session.add(message)
    db.session.commit()
    
    return message


@bp.route('/messages')
def index():
    """
    Главная страница сообщений.
    Перенаправляет на унифицированную страницу сообщений в зависимости от роли.
    """
    user_type = get_user_type()
    
    if not user_type:
        return redirect(url_for('auth.login'))
    
    # Используем унифицированные маршруты
    if user_type == 'admin':
        return redirect(url_for('messages.admin_messages'))
    elif user_type == 'seller':
        return redirect(url_for('messages.seller_messages'))
    else:
        return redirect(url_for('messages.buyer_messages'))


@bp.route('/messages/')
def messages_index():
    """
    Главная страница сообщений - перенаправляет на соответствующую страницу в зависимости от роли.
    """
    user_type = get_user_type()
    
    if not user_type:
        return redirect(url_for('auth.login'))
    
    if user_type == 'admin':
        return redirect(url_for('messages.admin_messages'))
    elif user_type == 'seller':
        return redirect(url_for('messages.seller_messages'))
    else:
        return redirect(url_for('messages.buyer_messages'))


@bp.route('/messages/<partner_type>/<int:partner_id>')
def chat(partner_type, partner_id):
    """
    Просмотр чата с конкретным собеседником.
    """
    user_type = get_user_type()
    user_id = get_user_id()
    
    if not user_type:
        return redirect(url_for('auth.login'))
    
    # Проверка доступа
    if user_type == 'seller' and not isinstance(current_user, Seller):
        return redirect(url_for('auth.seller_login'))
    elif user_type == 'buyer' and not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    # Проверка: если это order-диалог
    if partner_type == 'order':
        order = None
        try:
            from app.models.orders import Order
            order = Order.query.get(partner_id)
        except:
            pass
        
        if order:
            actual_partner_type = 'seller' if user_type == 'buyer' else 'buyer'
            actual_partner_id = order.seller_id if user_type == 'buyer' else order.buyer_id
            
            # Получаем сообщения для этого заказа
            messages = Message.query.filter(
                Message.conversation_type == 'order',
                Message.conversation_id == partner_id
            ).order_by(Message.timestamp.asc()).all()
            
            # Отмечаем как прочитанные
            mark_messages_read(user_type, user_id, actual_partner_type, actual_partner_id)
            
            # Имя собеседника
            partner_name = get_partner_name(actual_partner_type, actual_partner_id)
            order_key = f"Заказ {order.order_number}"
            
            # Определяем фильтры
            if user_type == 'buyer':
                filter_options = {'support': 'Поддержка', 'stores': 'Магазины', 'orders': 'Заказы'}
                filter_type = 'orders'
                base_template = 'base.html'
                site_title = 'Покупатель'
            elif user_type == 'seller':
                filter_options = {'support': 'Поддержка', 'buyers': 'Покупатели', 'orders': 'Заказы'}
                filter_type = 'orders'
                base_template = 'seller/layout.html'
                site_title = 'Панель продавца'
            else:
                filter_options = {}
                filter_type = None
                base_template = 'admin/layout.html'
                site_title = 'Админ-панель'
            
            # Получаем диалоги
            conversations = get_conversations(user_type, user_id, filter_type)
            
            # Подсчет непрочитанных
            unread_support = sum(1 for k, c in get_conversations(user_type, user_id, 'support').items() if c['unread_count'] > 0)
            unread_stores = sum(1 for k, c in get_conversations(user_type, user_id, 'stores').items() if c['unread_count'] > 0)
            unread_buyers = sum(1 for k, c in get_conversations(user_type, user_id, 'buyers').items() if c['unread_count'] > 0)
            unread_sellers = sum(1 for k, c in get_conversations(user_type, user_id, 'sellers').items() if c['unread_count'] > 0)
            
            # Текущий диалог (order-тип)
            current_partner = {
                'partner_type': 'order',
                'partner_id': partner_id,
                'name': order_key,
                'order_id': order.id,
                'order_number': order.order_number
            }
            
            return render_template('messages/user_messages.html',
                                 title='Чат - ' + order_key,
                                 messages=messages,
                                 conversations=conversations,
                                 current_partner=current_partner,
                                 partner_type='order',
                                 partner_id=partner_id,
                                 partner_name=order_key,
                                 user_type=user_type,
                                 base_template=base_template,
                                 site_title=site_title,
                                 filter_options=filter_options,
                                 current_filter=filter_type,
                                 unread_support=unread_support,
                                 unread_stores=unread_stores,
                                 unread_buyers=unread_buyers,
                                 unread_sellers=unread_sellers,
                                 is_order_chat=True)
    
    # Обычный чат (не order)
    # Получение сообщений
    messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
    
    # Отмечаем как прочитанные
    mark_messages_read(user_type, user_id, partner_type, partner_id)
    
    # Имя собеседника
    partner_name = get_partner_name(partner_type, partner_id)
    
    # Получаем фильтр из URL параметра
    url_filter = request.args.get('filter')

    # Определяем фильтры для диалогов
    if user_type == 'buyer':
        filter_options = {'support': 'Поддержка', 'stores': 'Магазины', 'orders': 'Заказы'}
        filter_type = url_filter if url_filter in filter_options else 'stores'
        base_template = 'base.html'
        site_title = 'Покупатель'
    elif user_type == 'seller':
        filter_options = {'support': 'Поддержка', 'buyers': 'Покупатели', 'orders': 'Заказы'}
        filter_type = url_filter if url_filter in filter_options else 'buyers'
        base_template = 'seller/layout.html'
        site_title = 'Панель продавца'
    elif user_type == 'admin':
        filter_options = {'buyers': 'Покупатели', 'sellers': 'Продавцы'}
        filter_type = url_filter if url_filter in filter_options else 'buyers'
        base_template = 'admin/layout.html'
        site_title = 'Админ-панель'
    else:
        filter_options = {}
        filter_type = None
        base_template = 'base.html'
        site_title = 'Покупатель'
    
    # Получаем диалоги
    conversations = get_conversations(user_type, user_id, filter_type)
    
    # Подсчет непрочитанных
    unread_support = sum(1 for k, c in get_conversations(user_type, user_id, 'support').items() if c['unread_count'] > 0)
    unread_stores = sum(1 for k, c in get_conversations(user_type, user_id, 'stores').items() if c['unread_count'] > 0)
    unread_buyers = sum(1 for k, c in get_conversations(user_type, user_id, 'buyers').items() if c['unread_count'] > 0)
    unread_sellers = sum(1 for k, c in get_conversations(user_type, user_id, 'sellers').items() if c['unread_count'] > 0)
    
    # Текущий диалог
    current_partner = {
        'partner_type': partner_type,
        'partner_id': partner_id,
        'name': partner_name
    }
    
    return render_template('messages/user_messages.html',
                         title='Чат',
                         messages=messages,
                         conversations=conversations,
                         current_partner=current_partner,
                         partner_type=partner_type,
                         partner_id=partner_id,
                         partner_name=partner_name,
                         user_type=user_type,
                         base_template=base_template,
                         site_title=site_title,
                         filter_options=filter_options,
                         current_filter=filter_type,
                         unread_support=unread_support,
                         unread_stores=unread_stores,
                         unread_buyers=unread_buyers,
                         unread_sellers=unread_sellers)


@bp.route('/messages/<partner_type>/<int:partner_id>/content')
def chat_content(partner_type, partner_id):
    """
    AJAX загрузка контента чата.
    """
    from flask import render_template_string
    import logging
    import os
    from datetime import datetime
    
    logger = logging.getLogger(__name__)
    log_path = os.path.join(os.path.dirname(__file__), '..', '..', 'logs', 'debug.log')
    
    def debug_log(msg):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        log_line = f"[{timestamp}] [CHAT_CONTENT] {msg}\n"
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(log_line)
        except:
            pass
        print(log_line.strip())
    
    debug_log("=== chat_content() called ===")
    debug_log(f"partner_type: {partner_type}, partner_id: {partner_id}")
    
    user_type = get_user_type()
    user_id = get_user_id()
    debug_log(f"user_type: {user_type}, user_id: {user_id}")
    
    if not user_type:
        debug_log("ERROR: Not authenticated")
        return '<div class="error">Требуется авторизация</div>', 401
    
    # Проверка доступа
    if user_type == 'seller' and not isinstance(current_user, Seller):
        debug_log("ERROR: Not a seller")
        return '<div class="error">Требуется авторизация продавца</div>', 401
    elif user_type == 'buyer' and not isinstance(current_user, Buyer):
        debug_log("ERROR: Not a buyer")
        return '<div class="error">Требуется авторизация покупателя</div>', 401
    
    # Проверка: если это order-диалог
    is_order_chat = False
    actual_partner_type = partner_type
    actual_partner_id = partner_id
    order_info = None
    
    if partner_type == 'order':
        from app.models.orders import Order
        order = Order.query.get(partner_id)
        if order:
            is_order_chat = True
            # Определяем реального партнёра
            actual_partner_type = 'seller' if user_type == 'buyer' else 'buyer'
            actual_partner_id = order.seller_id if user_type == 'buyer' else order.buyer_id
            
            # Получаем сообщения для этого заказа через conversation_type
            messages = Message.query.filter(
                Message.conversation_type == 'order',
                Message.conversation_id == partner_id
            ).order_by(Message.timestamp.asc()).all()
            
            # Отмечаем как прочитанные
            mark_messages_read(user_type, user_id, actual_partner_type, actual_partner_id)
            
            # Имя диалога
            partner_name = f"Заказ {order.order_number}"
            
            order_info = {
                'order_id': order.id,
                'order_number': order.order_number
            }
            
            debug_log(f"Order chat loaded: {partner_name}, messages count: {len(messages)}")
            
            return render_template('messages/_chat_content.html',
                                 messages=messages,
                                 partner_type=actual_partner_type,
                                 partner_id=actual_partner_id,
                                 partner_name=partner_name,
                                 user_type=user_type,
                                 is_order_chat=True,
                                 order_info=order_info)
    
    # Обычный чат (не order)
    # Получение сообщений
    messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
    
    # Отмечаем как прочитанные
    mark_messages_read(user_type, user_id, partner_type, partner_id)
    
    # Имя собеседника
    partner_name = get_partner_name(partner_type, partner_id)
    
    return render_template('messages/_chat_content.html',
                         messages=messages,
                         partner_type=partner_type,
                         partner_id=partner_id,
                         partner_name=partner_name,
                         user_type=user_type,
                         is_order_chat=False)


@bp.route('/messages/<partner_type>/<int:partner_id>/new')
def new_messages(partner_type, partner_id):
    """
    AJAX получение новых сообщений (для polling).
    """
    user_type = get_user_type()
    user_id = get_user_id()
    
    if not user_type:
        return jsonify({'error': 'Unauthorized'}), 401
    
    last_id = request.args.get('last_id', 0, type=int)
    
    # Если это order-диалог, получаем сообщения через conversation_type
    if partner_type == 'order':
        from app.models.orders import Order
        order = Order.query.get(partner_id)
        if order:
            # Определяем реального партнёра
            actual_partner_type = 'seller' if user_type == 'buyer' else 'buyer'
            actual_partner_id = order.seller_id if user_type == 'buyer' else order.buyer_id
            
            # Получаем сообщения для этого заказа
            messages = Message.query.filter(
                Message.conversation_type == 'order',
                Message.conversation_id == partner_id,
                Message.id > last_id
            ).order_by(Message.timestamp.asc()).all()
            
            # Форматируем сообщения
            messages_data = []
            for msg in messages:
                messages_data.append({
                    'id': msg.id,
                    'text': msg.text,
                    'image_path': msg.image_path,
                    'file_path': msg.file_path,
                    'timestamp': msg.timestamp.isoformat() if msg.timestamp else None,
                    'is_outgoing': msg.sender_type == user_type and msg.sender_id == user_id
                })
            
            return jsonify({
                'success': True,
                'messages': messages_data
            })
        return jsonify({'success': True, 'messages': []})
    
    # Обычный чат (не order)
    # Получаем новые сообщения (и входящие и исходящие)
    messages = Message.query.filter(
        or_(
            and_(
                Message.sender_type == partner_type,
                Message.sender_id == partner_id,
                Message.receiver_type == user_type,
                Message.receiver_id == user_id
            ),
            and_(
                Message.sender_type == user_type,
                Message.sender_id == user_id,
                Message.receiver_type == partner_type,
                Message.receiver_id == partner_id
            )
        ),
        Message.id > last_id
    ).order_by(Message.timestamp.asc()).all()
    
    # Форматируем сообщения
    messages_data = []
    for msg in messages:
        messages_data.append({
            'id': msg.id,
            'text': msg.text,
            'image_path': msg.image_path,
            'file_path': msg.file_path,
            'timestamp': msg.timestamp.isoformat() if msg.timestamp else None,
            'is_outgoing': msg.sender_type == user_type and msg.sender_id == user_id
        })
    
    return jsonify({
        'success': True,
        'messages': messages_data
    })


@bp.route('/messages/send', methods=['POST'])
@csrf.exempt
def send():
    """
    Отправка сообщения (общий endpoint).
    Поддерживает отправку с изображениями и PDF файлами.
    """
    user_type = get_user_type()
    user_id = get_user_id()
    
    if not user_type:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Получаем данные из формы (multipart/form-data)
    receiver_type = request.form.get('receiver_type')
    receiver_id = request.form.get('receiver_id', type=int)
    text = request.form.get('text', '').strip()
    image_path = request.form.get('image_path')
    file_path = request.form.get('file_path')
    conversation_type = request.form.get('conversation_type')
    conversation_id = request.form.get('conversation_id', type=int)
    
    # Проверяем, что есть текст или вложение
    if not text and not image_path and not file_path:
        return jsonify({'error': 'Добавьте текст сообщения или вложение'}), 400
    
    # Проверяем обязательные данные получателя
    if not receiver_type or receiver_id is None:
        return jsonify({'error': 'Не указан получатель'}), 400
    
    # Для order чатов определяем реального получателя
    if conversation_type == 'order' and conversation_id:
        from app.models.orders import Order
        order = Order.query.get(conversation_id)
        if order:
            # Определяем получателя в зависимости от текущего пользователя
            if user_type == 'seller':
                receiver_type = 'buyer'
                receiver_id = order.buyer_id
            elif user_type == 'buyer':
                receiver_type = 'seller'
                receiver_id = order.seller_id
    
    # Отправляем сообщение
    message = send_message(user_type, user_id, receiver_type, receiver_id, text, image_path, file_path, conversation_type, conversation_id)
    
    if message:
        return jsonify({'success': True, 'message_id': message.id})
    else:
        return jsonify({'error': 'Ошибка отправки сообщения'}), 500


@bp.route('/api/upload-message-file', methods=['POST'])
@csrf.exempt
def upload_message_file():
    """
    Загрузка файла (изображения или PDF) для сообщения.
    """
    import logging
    import os
    from datetime import datetime
    
    logger = logging.getLogger(__name__)
    log_path = os.path.join(os.path.dirname(__file__), '..', '..', 'logs', 'debug.log')
    
    def debug_log(msg):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        log_line = f"[{timestamp}] [UPLOAD] {msg}\n"
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(log_line)
        except:
            pass
        print(log_line.strip())
    
    debug_log("=== upload_message_file() called ===")
    
    user_type = get_user_type()
    user_id = get_user_id()
    debug_log(f"user_type: {user_type}, user_id: {user_id}")
    debug_log(f"request.files: {list(request.files.keys()) if request.files else 'EMPTY'}")
    debug_log(f"request.headers: {dict(request.headers)}")
    
    if not user_type:
        debug_log("ERROR: Unauthorized request")
        logger.warning("[UPLOAD] Unauthorized request")
        return jsonify({'error': 'Unauthorized'}), 401
    
    if 'file' not in request.files:
        debug_log("ERROR: No file in request.files")
        logger.warning("[UPLOAD] No file in request")
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    debug_log(f"File received: {file.filename}, content_type: {file.content_type}")
    logger.info(f"[UPLOAD] File received: {file.filename}, content_type: {file.content_type}")
    
    if file.filename == '':
        debug_log("ERROR: Empty filename")
        logger.warning("[UPLOAD] Empty filename")
        return jsonify({'error': 'No file selected'}), 400
    
    # Проверяем тип файла
    allowed_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf']
    content_type = file.content_type
    debug_log(f"Content type check: {content_type} in {allowed_types} = {content_type in allowed_types}")
    
    if content_type not in allowed_types:
        debug_log(f"ERROR: Invalid file type: {content_type}")
        return jsonify({'error': 'Invalid file type. Allowed: images (JPEG, PNG, GIF, WebP) and PDF'}), 400
    
    # Проверяем размер (10 MB max)
    file.seek(0, 2)  # Seek to end
    size = file.tell()
    file.seek(0)  # Reset position
    debug_log(f"File size: {size} bytes")
    
    if size > 10 * 1024 * 1024:
        debug_log(f"ERROR: File too large: {size}")
        return jsonify({'error': 'File too large. Maximum size: 10 MB'}), 400
    
    # Генерируем уникальное имя файла
    import secrets
    from datetime import datetime
    
    ext = os.path.splitext(file.filename)[1].lower()
    if ext == '.jpeg':
        ext = '.jpg'
    
    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(8)}{ext}"
    debug_log(f"Generated filename: {filename}")
    
    # Создаем папку для загрузок если её нет
    upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'messages')
    debug_log(f"Upload folder: {upload_folder}")
    os.makedirs(upload_folder, exist_ok=True)
    
    filepath = os.path.join(upload_folder, filename)
    debug_log(f"Saving to: {filepath}")
    file.save(filepath)
    
    # Проверяем что файл сохранился
    if os.path.exists(filepath):
        actual_size = os.path.getsize(filepath)
        debug_log(f"File saved successfully, actual size: {actual_size}")
    else:
        debug_log("ERROR: File was NOT saved!")
    
    # Относительный путь для хранения в БД
    relative_path = f"uploads/messages/{filename}"
    
    # Определяем тип файла
    is_image = content_type.startswith('image/')
    
    debug_log(f"SUCCESS - saved to: {filepath}, relative_path: {relative_path}, is_image: {is_image}")
    logger.info(f"[UPLOAD] SUCCESS - saved to: {filepath}, relative_path: {relative_path}, is_image: {is_image}")
    
    return jsonify({
        'success': True,
        'path': relative_path,
        'filename': file.filename,
        'is_image': is_image,
        'is_pdf': content_type == 'application/pdf',
        'size': size
    })


@bp.route('/api/conversations')
def api_conversations():
    """
    API для получения списка диалогов.
    """
    user_type = get_user_type()
    user_id = get_user_id()
    
    if not user_type:
        return jsonify({'error': 'Unauthorized'}), 401
    
    filter_type = request.args.get('filter', type=str)
    
    conversations = get_conversations(user_type, user_id, filter_type)
    
    # Форматируем для JSON
    conv_list = []
    for key, conv in conversations.items():
        conv_list.append({
            'id': conv['id'],
            'name': conv['name'],
            'partner_type': conv['partner_type'],
            'partner_id': conv['partner_id'],
            'last_message': conv['last_message'],
            'unread_count': conv['unread_count']
        })
    
    return jsonify({
        'success': True,
        'conversations': conv_list,
        'filter_options': get_filter_options(user_type)
    })


@bp.route('/api/search-users')
def search_users():
    """
    API для поиска пользователей.
    Для админа - поиск покупателей и продавцов.
    """
    user_type = get_user_type()
    
    if user_type != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    query = request.args.get('query', '').strip()
    search_type = request.args.get('type', 'buyers')  # buyers или sellers
    
    if not query or len(query) < 2:
        return jsonify({'users': []})
    
    limit = request.args.get('limit', 10, type=int)
    users = []
    
    if search_type == 'sellers':
        from sqlalchemy import or_
        sellers = Seller.query.filter(
            or_(
                Seller.email.ilike(f'%{query}%'),
                Seller.login.ilike(f'%{query}%'),
                Seller.store_name.ilike(f'%{query}%'),
                Seller.phone.ilike(f'%{query}%')
            ),
            Seller.is_active == True
        ).limit(limit).all()
        
        for seller in sellers:
            users.append({
                'id': seller.id,
                'type': 'seller',
                'name': seller.store_name,
                'email': seller.email,
                'login': seller.login,
                'phone': seller.phone or ''
            })
    else:
        from sqlalchemy import or_
        buyers = Buyer.query.filter(
            or_(
                Buyer.email.ilike(f'%{query}%'),
                Buyer.login.ilike(f'%{query}%'),
                Buyer.first_name.ilike(f'%{query}%'),
                Buyer.last_name.ilike(f'%{query}%'),
                Buyer.phone.ilike(f'%{query}%')
            ),
            Buyer.is_active == True
        ).limit(limit).all()
        
        for buyer in buyers:
            users.append({
                'id': buyer.id,
                'type': 'buyer',
                'name': buyer.full_name,
                'email': buyer.email,
                'login': buyer.login,
                'phone': buyer.phone or ''
            })
    
    return jsonify({'users': users})


# ============================================================================
# Специфические представления для каждой роли
# ============================================================================

@bp.route('/messages/buyer')
@login_required
def buyer_messages():
    """
    Сообщения для покупателя.
    URL: /messages/buyer
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    user_type = 'buyer'
    user_id = current_user.id
    
    # Получаем фильтр (support или stores)
    filter_type = request.args.get('filter', 'stores')
    partner_type = request.args.get('partner_type', type=str)
    partner_id = request.args.get('partner_id', type=int)
    
    # Получаем диалоги с учетом фильтра
    conversations = get_conversations(user_type, user_id, filter_type)
    
    # Определяем фильтры для покупателя
    filter_options = {
        'support': 'Поддержка',
        'stores': 'Магазины',
        'orders': 'Заказы'
    }
    
    # Текущий диалог
    current_partner = None
    messages = []
    is_order_chat = False
    
    if partner_type and partner_id is not None:
        # Проверяем, если это order-диалог
        if partner_type == 'order':
            from app.models.orders import Order
            order = Order.query.get(partner_id)
            if order:
                is_order_chat = True
                # Получаем сообщения через conversation_type='order'
                messages = Message.query.filter(
                    Message.conversation_type == 'order',
                    Message.conversation_id == partner_id
                ).order_by(Message.timestamp.asc()).all()
                
                # Отмечаем как прочитанные
                actual_partner_type = 'seller'
                actual_partner_id = order.seller_id
                mark_messages_read(user_type, user_id, actual_partner_type, actual_partner_id)
                
                # Имя диалога
                order_key = f"Заказ {order.order_number}"
                current_partner = {
                    'partner_type': 'order',
                    'partner_id': partner_id,
                    'name': order_key,
                    'order_id': order.id,
                    'order_number': order.order_number,
                    # Для отправки сообщений - реальный получатель
                    'receiver_type': 'seller',
                    'receiver_id': actual_partner_id
                }
        else:
            # Обычный диалог
            messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
            mark_messages_read(user_type, user_id, partner_type, partner_id)
            partner_name = get_partner_name(partner_type, partner_id)
            current_partner = {
                'partner_type': partner_type,
                'partner_id': partner_id,
                'name': partner_name,
                'receiver_type': partner_type,
                'receiver_id': partner_id
            }
    
    # Подсчет непрочитанных для каждого фильтра
    unread_support = sum(1 for k, c in get_conversations(user_type, user_id, 'support').items() if c['unread_count'] > 0)
    unread_stores = sum(1 for k, c in get_conversations(user_type, user_id, 'stores').items() if c['unread_count'] > 0)
    unread_orders = sum(1 for k, c in get_conversations(user_type, user_id, 'orders').items() if c['unread_count'] > 0)
    
    return render_template('messages/user_messages.html',
                         title='Сообщения',
                         conversations=conversations,
                         messages=messages,
                         current_partner=current_partner,
                         filter_options=filter_options,
                         current_filter=filter_type,
                         user_type=user_type,
                         base_template='base.html',
                         site_title='Покупатель',
                         unread_support=unread_support,
                         unread_stores=unread_stores,
                         unread_orders=unread_orders,
                         is_order_chat=is_order_chat)


@bp.route('/messages/seller')
def seller_messages():
    """
    Сообщения для продавца.
    URL: seller.domain/messages (поддомен)
    """
    import logging
    import os
    from datetime import datetime
    
    logger = logging.getLogger(__name__)
    log_path = os.path.join(os.path.dirname(__file__), '..', '..', 'logs', 'debug.log')
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    
    def debug_log(msg):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        log_line = f"[{timestamp}] [SELLER_MESSAGES] {msg}\n"
        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(log_line)
        except:
            pass
        print(log_line.strip())
    
    debug_log("=== seller_messages() called ===")
    debug_log(f"current_user: {current_user}")
    debug_log(f"is_authenticated: {current_user.is_authenticated if current_user else 'N/A'}")
    if current_user.is_authenticated:
        debug_log(f"isinstance Seller: {isinstance(current_user, Seller)}")
    
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        debug_log("REDIRECT: Not authenticated or not a seller")
        return redirect(url_for('auth.seller_login'))
    
    user_type = 'seller'
    user_id = current_user.id
    debug_log(f"user_type: {user_type}, user_id: {user_id}")
    
    # Получаем фильтр (support или buyers)
    filter_type = request.args.get('filter', 'buyers')
    partner_type = request.args.get('partner_type', type=str)
    partner_id = request.args.get('partner_id', type=int)
    
    # Получаем диалоги с учетом фильтра
    conversations = get_conversations(user_type, user_id, filter_type)
    
    # Определяем фильтры для продавца
    filter_options = {
        'support': 'Поддержка',
        'buyers': 'Покупатели',
        'orders': 'Заказы'
    }
    
    # Текущий диалог
    current_partner = None
    messages = []
    is_order_chat = False
    
    if partner_type and partner_id is not None:
        # Проверяем, если это order-диалог
        if partner_type == 'order':
            from app.models.orders import Order
            order = Order.query.get(partner_id)
            if order:
                is_order_chat = True
                # Получаем сообщения через conversation_type='order'
                messages = Message.query.filter(
                    Message.conversation_type == 'order',
                    Message.conversation_id == partner_id
                ).order_by(Message.timestamp.asc()).all()
                
                # Отмечаем как прочитанные
                actual_partner_type = 'buyer'
                actual_partner_id = order.buyer_id
                mark_messages_read(user_type, user_id, actual_partner_type, actual_partner_id)
                
                # Имя диалога
                order_key = f"Заказ {order.order_number}"
                current_partner = {
                    'partner_type': 'order',
                    'partner_id': partner_id,
                    'name': order_key,
                    'order_id': order.id,
                    'order_number': order.order_number,
                    # Для отправки сообщений - реальный получатель
                    'receiver_type': 'buyer',
                    'receiver_id': actual_partner_id
                }
        else:
            # Обычный диалог
            messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
            mark_messages_read(user_type, user_id, partner_type, partner_id)
            partner_name = get_partner_name(partner_type, partner_id)
            current_partner = {
                'partner_type': partner_type,
                'partner_id': partner_id,
                'name': partner_name,
                'receiver_type': partner_type,
                'receiver_id': partner_id
            }
    
    # Подсчет непрочитанных
    unread_support = sum(1 for k, c in get_conversations(user_type, user_id, 'support').items() if c['unread_count'] > 0)
    unread_buyers = sum(1 for k, c in get_conversations(user_type, user_id, 'buyers').items() if c['unread_count'] > 0)
    unread_orders = sum(1 for k, c in get_conversations(user_type, user_id, 'orders').items() if c['unread_count'] > 0)
    
    return render_template('messages/user_messages.html',
                         title='Сообщения',
                         conversations=conversations,
                         messages=messages,
                         current_partner=current_partner,
                         filter_options=filter_options,
                         current_filter=filter_type,
                         user_type=user_type,
                         base_template='seller/layout.html',
                         site_title='Панель продавца',
                         unread_support=unread_support,
                         unread_buyers=unread_buyers,
                         unread_orders=unread_orders,
                         is_order_chat=is_order_chat)


@bp.route('/messages/admin')
@admin_required
def admin_messages():
    """
    Сообщения для админа.
    URL: /main_admin/messages
    """
    user_type = 'admin'
    user_id = 0
    
    # Получаем фильтр (buyers или sellers)
    filter_type = request.args.get('filter', 'buyers')
    partner_type = request.args.get('partner_type', type=str)
    partner_id = request.args.get('partner_id', type=int)
    
    # Получаем диалоги с учетом фильтра
    conversations = get_conversations(user_type, user_id, filter_type)
    
    # Определяем фильтры для админа
    filter_options = {
        'buyers': 'Покупатели',
        'sellers': 'Продавцы'
    }
    
    # Текущий диалог
    current_partner = None
    messages = []
    
    if partner_type and partner_id is not None:
        messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
        mark_messages_read(user_type, user_id, partner_type, partner_id)
        partner_name = get_partner_name(partner_type, partner_id)
        current_partner = {
            'partner_type': partner_type,
            'partner_id': partner_id,
            'name': partner_name
        }
    
    # Подсчет непрочитанных
    unread_buyers = sum(1 for k, c in get_conversations(user_type, user_id, 'buyers').items() if c['unread_count'] > 0)
    unread_sellers = sum(1 for k, c in get_conversations(user_type, user_id, 'sellers').items() if c['unread_count'] > 0)
    
    return render_template('messages/user_messages.html',
                         title='Сообщения',
                         conversations=conversations,
                         messages=messages,
                         current_partner=current_partner,
                         filter_options=filter_options,
                         current_filter=filter_type,
                         user_type=user_type,
                         base_template='admin/layout.html',
                         site_title='Админ-панель',
                         unread_buyers=unread_buyers,
                         unread_sellers=unread_sellers)
