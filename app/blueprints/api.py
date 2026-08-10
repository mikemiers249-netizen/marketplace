"""
Blueprint API для AJAX-запросов.
URL: /api/*
"""

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models.products import Product, Category
from app.models.orders import CartItem, Favorite, Order
from app.models.users import Buyer, Seller, DeliveryService
from app.models.communications import Message
from app.utils.decorators import ajax_login_required, buyer_required, seller_required
from app.utils.helpers import get_cart_total


bp = Blueprint('api', __name__, url_prefix='/api')


# ========== Товары ==========

@bp.route('/products/search')
def products_search():
    """
    Поиск товаров для автодополнения.
    GET /api/products/search?q={query}
    """
    query = request.args.get('q', '').strip()
    limit = request.args.get('limit', 10, type=int)
    
    if len(query) < 2:
        return jsonify({'results': []})
    
    products = Product.query.filter(
        Product.status == 'approved',
        Product.name.ilike(f'%{query}%')
    ).limit(limit).all()
    
    results = [{
        'id': p.id,
        'name': p.name,
        'price': p.price,
        'slug': p.slug,
        'category_id': p.category_id
    } for p in products]
    
    return jsonify({'results': results})


@bp.route('/products/<int:product_id>')
def product_detail(product_id):
    """
    Получение данных товара.
    GET /api/products/{id}
    """
    product = db.session.get(Product, product_id)
    if not product or product.status != 'approved':
        return jsonify({'error': 'Товар не найден'}), 404
    
    # Проверка избранного
    in_favorite = False
    if current_user.is_authenticated and isinstance(current_user, Buyer):
        in_favorite = Favorite.query.filter_by(
            buyer_id=current_user.id,
            product_id=product.id
        ).first() is not None
    
    # Корзина
    in_cart = False
    cart_quantity = 0
    if current_user.is_authenticated and isinstance(current_user, Buyer):
        cart_item = CartItem.query.filter_by(
            buyer_id=current_user.id,
            product_id=product.id
        ).first()
        if cart_item:
            in_cart = True
            cart_quantity = cart_item.quantity
    
    return jsonify({
        'id': product.id,
        'name': product.name,
        'description': product.description,
        'price': product.price,
        'old_price': product.old_price,
        'article': product.article,
        'stock_quantity': product.stock_quantity,
        'category_id': product.category_id,
        'rating': product.average_rating,
        'reviews_count': product.reviews_count,
        'is_new': product.is_new,
        'in_promotion': product.is_in_promotion,
        'in_favorite': in_favorite,
        'in_cart': in_cart,
        'cart_quantity': cart_quantity,
        'photos': [{
            'id': ph.id,
            'path': ph.path,
            'is_main': ph.is_main
        } for ph in product.photos.all()],
        'params': [{
            'id': p.parameter_id,
            'name': p.parameter.name,
            'value': p.value,
            'display_value': p.display_value
        } for p in product.get_all_params()],
        'seller': {
            'id': product.seller.id,
            'store_name': product.seller.store_name,
            'slug': product.seller.store_slug,
            'rating': product.seller.rating
        }
    })


@bp.route('/products/<int:product_id>/similar')
def product_similar(product_id):
    """
    Получение похожих товаров.
    GET /api/products/{id}/similar
    """
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': 'Товар не найден'}), 404
    
    from sqlalchemy import func
    
    similar = Product.query.filter(
        Product.category_id == product.category_id,
        Product.id != product.id,
        Product.status == 'approved',
        Product.stock_quantity > 0
    ).order_by(func.random()).limit(5).all()
    
    return jsonify({
        'products': [{
            'id': p.id,
            'name': p.name,
            'price': p.price,
            'slug': p.slug,
            'rating': p.average_rating,
            'main_photo': p.main_photo.path if p.main_photo else None
        } for p in similar]
    })


# ========== Категории ==========

@bp.route('/categories')
def categories_list():
    """
    Список категорий.
    GET /api/categories
    """
    parent_id = request.args.get('parent_id', type=int)
    
    if parent_id:
        categories = Category.query.filter_by(parent_id=parent_id).all()
    else:
        categories = Category.query.filter_by(parent_id=None).all()
    
    return jsonify({
        'categories': [{
            'id': c.id,
            'name': c.name,
            'slug': c.slug,
            'image': c.image_path,
            'product_count': c.product_count
        } for c in categories]
    })


@bp.route('/categories/tree')
def categories_tree():
    """
    Дерево категорий.
    GET /api/categories/tree
    """
    def get_tree(category):
        return {
            'id': category.id,
            'name': category.name,
            'slug': category.slug,
            'subcategories': [get_tree(sub) for sub in category.subcategories.all()]
        }
    
    categories = Category.query.filter_by(parent_id=None).all()
    
    return jsonify({
        'tree': [get_tree(c) for c in categories]
    })


@bp.route('/categories/<int:category_id>/params')
def category_params(category_id):
    """
    Параметры категории.
    GET /api/categories/{id}/params
    """
    category = db.session.get(Category, category_id)
    if not category:
        return jsonify({'error': 'Категория не найдена'}), 404
    
    params = category.get_all_parameters()
    
    return jsonify({
        'params': [{
            'id': p.id,
            'name': p.name,
            'code': p.code,
            'type': p.type,
            'is_composite': p.is_composite,
            'composite_count': p.composite_count,
            'is_multiple': p.is_multiple,
            'is_input': p.is_input,
            'predefined_values': p.predefined_values
        } for p in params]
    })


# ========== Корзина ==========

@bp.route('/cart')
@login_required
def cart_info():
    """
    Получение данных корзины.
    GET /api/cart
    """
    if not isinstance(current_user, Buyer):
        return jsonify({'error': 'Доступ только для покупателей'}), 403
    
    cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()
    
    items = [{
        'product_id': item.product_id,
        'name': item.product.name,
        'price': item.product.price,
        'quantity': item.quantity,
        'total': item.total_price,
        'main_photo': item.product.main_photo.path if item.product.main_photo else None
    } for item in cart_items]
    
    cart_total = get_cart_total(current_user.id)
    
    return jsonify({
        'items': items,
        'count': len(cart_items),
        'subtotal': cart_total['subtotal'],
        'discount': cart_total['discount'],
        'total': cart_total['total']
    })


@bp.route('/cart/add', methods=['POST'])
@ajax_login_required
@buyer_required
def cart_add():
    """
    Добавление товара в корзину.
    POST /api/cart/add
    """
    product_id = request.form.get('product_id', type=int)
    quantity = request.form.get('quantity', 1, type=int)
    
    if not product_id:
        return jsonify({'error': 'Не указан товар'}), 400
    
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': 'Товар не найден'}), 404
    
    if product.stock_quantity < quantity:
        return jsonify({
            'error': 'Недостаточно товара',
            'available': product.stock_quantity
        }), 400
    
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if cart_item:
        new_quantity = min(cart_item.quantity + quantity, product.stock_quantity)
        cart_item.quantity = new_quantity
    else:
        cart_item = CartItem(
            buyer_id=current_user.id,
            product_id=product_id,
            quantity=quantity
        )
        db.session.add(cart_item)
    
    db.session.commit()
    
    # Получаем общее количество товаров в корзине (сумма всех quantity)
    cart_count = db.session.query(db.func.sum(CartItem.quantity)).filter(
        CartItem.buyer_id == current_user.id
    ).scalar() or 0
    
    # Получаем количество этого конкретного товара
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    return jsonify({
        'success': True,
        'cart_count': cart_count,
        'in_cart': True,
        'cart_quantity': cart_item.quantity if cart_item else 0,
        'message': 'Товар добавлен в корзину'
    })


@bp.route('/cart/update', methods=['POST'])
@ajax_login_required
@buyer_required
def cart_update():
    """
    Обновление количества товара.
    POST /api/cart/update
    """
    product_id = request.form.get('product_id', type=int)
    quantity = request.form.get('quantity', type=int)
    
    if not product_id or quantity is None:
        return jsonify({'error': 'Неверные параметры'}), 400
    
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if not cart_item:
        return jsonify({'error': 'Товар не найден в корзине'}), 404
    
    product = db.session.get(Product, product_id)
    
    if quantity <= 0:
        db.session.delete(cart_item)
        message = 'Товар удалён'
    elif quantity > product.stock_quantity:
        cart_item.quantity = product.stock_quantity
        message = f'Количество ограничено {product.stock_quantity}'
    else:
        cart_item.quantity = quantity
        message = 'Количество обновлено'
    
    db.session.commit()
    
    # Получаем общее количество товаров в корзине (сумма всех quantity)
    cart_count = db.session.query(db.func.sum(CartItem.quantity)).filter(
        CartItem.buyer_id == current_user.id
    ).scalar() or 0
    
    cart_total = get_cart_total(current_user.id)
    
    # Получаем актуальное количество этого товара
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    return jsonify({
        'success': True,
        'cart_count': cart_count,
        'in_cart': cart_item is not None,
        'cart_quantity': cart_item.quantity if cart_item else 0,
        'item_total': cart_item.total_price if cart_item and quantity > 0 else 0,
        'subtotal': cart_total['subtotal'],
        'total': cart_total['total'],
        'message': message
    })


@bp.route('/cart/remove', methods=['POST'])
@ajax_login_required
@buyer_required
def cart_remove():
    """
    Удаление товара из корзины.
    POST /api/cart/remove
    """
    product_id = request.form.get('product_id', type=int)
    
    if not product_id:
        return jsonify({'error': 'Не указан товар'}), 400
    
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if cart_item:
        db.session.delete(cart_item)
        db.session.commit()
    
    # Получаем общее количество товаров в корзине (сумма всех quantity)
    cart_count = db.session.query(db.func.sum(CartItem.quantity)).filter(
        CartItem.buyer_id == current_user.id
    ).scalar() or 0
    
    cart_total = get_cart_total(current_user.id)
    
    return jsonify({
        'success': True,
        'cart_count': cart_count,
        'in_cart': False,
        'cart_quantity': 0,
        'subtotal': cart_total['subtotal'],
        'total': cart_total['total'],
        'message': 'Товар удалён из корзины'
    })


# ========== Избранное ==========

@bp.route('/favorite')
@login_required
def favorite_list():
    """
    Список избранных товаров.
    GET /api/favorite
    """
    if not isinstance(current_user, Buyer):
        return jsonify({'error': 'Доступ только для покупателей'}), 403
    
    favorites = Favorite.query.filter_by(buyer_id=current_user.id).all()
    
    return jsonify({
        'products': [{
            'id': f.product_id,
            'name': f.product.name,
            'price': f.product.price,
            'slug': f.product.slug,
            'main_photo': f.product.main_photo.path if f.product.main_photo else None
        } for f in favorites]
    })


@bp.route('/favorite/toggle', methods=['POST'])
@ajax_login_required
@buyer_required
def favorite_toggle():
    """
    Переключение избранного.
    POST /api/favorite/toggle
    """
    product_id = request.form.get('product_id', type=int)
    
    if not product_id:
        return jsonify({'error': 'Не указан товар'}), 400
    
    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': 'Товар не найден'}), 404
    
    favorite = Favorite.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if favorite:
        db.session.delete(favorite)
        message = 'Товар удалён из избранного'
        in_favorite = False
    else:
        favorite = Favorite(
            buyer_id=current_user.id,
            product_id=product_id
        )
        db.session.add(favorite)
        message = 'Товар добавлен в избранное'
        in_favorite = True
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'in_favorite': in_favorite,
        'message': message
    })


# ========== Сообщения ==========

@bp.route('/messages/conversations')
@login_required
def messages_conversations():
    """
    Список диалогов.
    GET /api/messages/conversations
    """
    if isinstance(current_user, Buyer):
        user_type = 'buyer'
    elif isinstance(current_user, Seller):
        user_type = 'seller'
    else:
        return jsonify({'error': 'Нет доступа'}), 403
    
    messages = Message.query.filter(
        or_(
            and_(Message.sender_type == user_type, Message.sender_id == current_user.id),
            and_(Message.receiver_type == user_type, Message.receiver_id == current_user.id)
        )
    ).distinct().all()
    
    conversations = {}
    for msg in messages:
        if msg.sender_id == current_user.id and msg.sender_type == user_type:
            key = f"{msg.receiver_type}:{msg.receiver_id}"
        elif msg.receiver_id == current_user.id and msg.receiver_type == user_type:
            key = f"{msg.sender_type}:{msg.sender_id}"
        else:
            continue
        
        if key not in conversations:
            conversations[key] = {
                'partner_type': msg.receiver_type if msg.sender_id == current_user.id else msg.sender_type,
                'partner_id': msg.receiver_id if msg.sender_id == current_user.id else msg.sender_id,
                'last_message': {
                    'text': msg.text,
                    'timestamp': msg.timestamp.isoformat()
                },
                'unread': 0
            }
        else:
            if msg.timestamp > datetime.fromisoformat(conversations[key]['last_message']['timestamp']):
                conversations[key]['last_message'] = {
                    'text': msg.text,
                    'timestamp': msg.timestamp.isoformat()
                }
        
        if not msg.is_read and msg.receiver_id == current_user.id:
            conversations[key]['unread'] += 1
    
    return jsonify({'conversations': conversations})


@bp.route('/messages/chat/<partner_type>/<int:partner_id>')
@login_required
def messages_chat(partner_type, partner_id):
    """
    История переписки.
    GET /api/messages/chat/{type}/{id}
    """
    if isinstance(current_user, Buyer):
        user_type = 'buyer'
    elif isinstance(current_user, Seller):
        user_type = 'seller'
    else:
        return jsonify({'error': 'Нет доступа'}), 403
    
    messages = Message.get_conversation(
        user_type, current_user.id,
        partner_type, partner_id
    )
    
    # Помечаем как прочитанные
    for msg in messages:
        if msg.receiver_id == current_user.id:
            msg.mark_as_read()
    
    return jsonify({
        'messages': [{
            'id': m.id,
            'sender_type': m.sender_type,
            'sender_id': m.sender_id,
            'text': m.text,
            'image': m.image_path,
            'timestamp': m.timestamp.isoformat(),
            'is_read': m.is_read
        } for m in messages]
    })


@bp.route('/messages/send', methods=['POST'])
@ajax_login_required
def messages_send():
    """
    Отправка сообщения.
    POST /api/messages/send
    """
    receiver_type = request.form.get('receiver_type')
    receiver_id = request.form.get('receiver_id', type=int)
    text = request.form.get('text')
    
    if not all([receiver_type, receiver_id, text]):
        return jsonify({'error': 'Заполните все поля'}), 400
    
    # Определение типа отправителя
    if isinstance(current_user, Buyer):
        sender_type = 'buyer'
    elif isinstance(current_user, Seller):
        sender_type = 'seller'
    else:
        return jsonify({'error': 'Нет доступа'}), 403
    
    msg = Message(
        sender_type=sender_type,
        sender_id=current_user.id,
        receiver_type=receiver_type,
        receiver_id=receiver_id,
        text=text
    )
    db.session.add(msg)
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message_id': msg.id,
        'timestamp': msg.timestamp.isoformat()
    })


# ========== Уведомления ==========

@bp.route('/notifications/count')
@login_required
def notifications_count():
    """
    Количество непрочитанных уведомлений.
    GET /api/notifications/count
    """
    if isinstance(current_user, Buyer):
        user_type = 'buyer'
    elif isinstance(current_user, Seller):
        user_type = 'seller'
    else:
        return jsonify({'error': 'Нет доступа'}), 403
    
    unread_messages = Message.query.filter(
        Message.receiver_type == user_type,
        Message.receiver_id == current_user.id,
        Message.is_read == False
    ).count()
    
    return jsonify({
        'unread_messages': unread_messages,
        'total': unread_messages
    })


# ========== Продавцы ==========

@bp.route('/seller/<seller_slug>/info')
def seller_info(seller_slug):
    """
    Информация о магазине продавца.
    GET /api/seller/{slug}/info
    """
    from app.models.users import Seller
    
    seller = Seller.query.filter_by(store_slug=seller_slug).first()
    if not seller:
        return jsonify({'error': 'Магазин не найден'}), 404
    
    products_count = Product.query.filter(
        Product.seller_id == seller.id,
        Product.status == 'approved',
        Product.stock_quantity > 0
    ).count()
    
    return jsonify({
        'id': seller.id,
        'store_name': seller.store_name,
        'slug': seller.store_slug,
        'logo': seller.store_logo,
        'rating': seller.rating,
        'reviews_count': seller.reviews_count,
        'products_count': products_count,
        'created_at': seller.created_at.isoformat()
    })


# Импорт datetime для использования в messages_chat
from datetime import datetime


# ========== CDEK API ==========

@bp.route('/cdek/validate', methods=['POST'])
@login_required
def cdek_validate():
    """
    Валидация учетных данных CDEK.
    POST /api/cdek/validate
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    data = request.get_json() or {}
    
    account = data.get('account')
    secure = data.get('secure')
    test_mode = data.get('test_mode', True)
    
    if not account or not secure:
        return jsonify({
            'success': False,
            'message': 'Укажите Account и Secure'
        }), 400
    
    try:
        from app.integrations.cdek import validate_credentials
        
        result = validate_credentials(account, secure, test_mode)
        return jsonify(result)
    
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Ошибка: {str(e)}'
        }), 500


@bp.route('/cdek/pvz')
def cdek_pvz_list():
    """
    Получение списка ПВЗ СДЭК.
    GET /api/cdek/pvz?city=Москва
    
    Всегда использует API credentials Vibli (id=1) для получения реальных данных.
    """
    city = request.args.get('city', '')
    region = request.args.get('region', '')
    
    try:
        from app.integrations.cdek import get_cdek_pvz_list, get_cdek_pvz_list_public, get_cdek_client, CDEKError
        
        pvz_list = []
        use_seller_creds = False
        
        # Всегда используем CDEK credentials Vibli (id=1)
        from app.models.users import SellerDelivery
        cdek_service = db.session.get(DeliveryService, 1)  # delivery_service id=1 это CDEK
        
        if cdek_service and cdek_service.code == 'cdek':
            # Получаем профиль доставки Vibli (seller_id=1)
            seller_delivery = SellerDelivery.query.filter_by(
                seller_id=1,  # Vibli
                delivery_service_id=cdek_service.id
            ).first()
            
            if seller_delivery:
                try:
                    client = get_cdek_client(seller_delivery)
                    pvz_list = client.get_pvz_list(country_code="RU")
                    use_seller_creds = True
                except (CDEKError, Exception) as e:
                    # Логируем ошибку и используем fallback
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(f"CDEK API failed for Vibli: {e}")
        
        # Если не получили данные через API - используем fallback
        if not pvz_list:
            pvz_list = get_cdek_pvz_list_public()
        
        # Фильтрация по городу если указан
        if city:
            city_lower = city.lower()
            pvz_list = [p for p in pvz_list if 
                city_lower in (p.get('location', {}) or {}).get('city', '').lower() or
                city_lower in p.get('city', '').lower()]
        
        # Форматирование для фронтенда с координатами
        # Поддерживаем разные форматы данных (из API и из fallback)
        result = []
        for p in pvz_list[:50]:
            # Получаем location (может быть None или отсутствовать)
            location = p.get('location') or {}
            
            # Координаты могут быть в корне или в location
            latitude = p.get('latitude') or location.get('latitude')
            longitude = p.get('longitude') or location.get('longitude')
            
            # Город может быть в корне или в location
            city_name = p.get('city') or location.get('city', '')
            address = p.get('address') or location.get('address', '')
            address_full = p.get('address_full') or location.get('address_full', address)
            city_code = p.get('city_code') or location.get('city_code')
            
            # Пропускаем ПВЗ без координат (нельзя отобразить на карте)
            if not latitude or not longitude:
                continue
            
            result.append({
                'code': p.get('code'),
                'name': p.get('name'),
                'city': city_name,
                'address': address,
                'address_full': address_full,
                'type': p.get('type'),
                'work_time': p.get('work_time'),
                'weight_max': p.get('weight_max'),
                'latitude': latitude,
                'longitude': longitude,
                'city_code': city_code,
            })
        
        return jsonify({'success': True, 'pvz': result})
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/cdek/calculate', methods=['POST'])
@login_required
def cdek_calculate():
    """
    Расчёт стоимости доставки CDEK.
    POST /api/cdek/calculate
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    data = request.get_json() or {}
    
    from_city = data.get('from_city')
    to_city = data.get('to_city')
    weight = data.get('weight', 1000)
    tariff_code = data.get('tariff_code')
    
    if not from_city or not to_city:
        return jsonify({'error': 'Укажите города отправки и доставки'}), 400
    
    try:
        from app.integrations.cdek import get_cdek_client
        
        client = get_cdek_client()
        
        result = client.calculate(
            from_location={'code': from_city},
            to_location={'code': to_city},
            weight=weight,
            tariff_code=tariff_code
        )
        
        return jsonify({
            'success': True,
            'result': result
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/cdek/tariffs', methods=['POST'])
@login_required
def cdek_get_tariffs():
    """
    Получение списка доступных тарифов CDEK.
    POST /api/cdek/tariffs
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    data = request.get_json() or {}
    
    from_city_code = data.get('from_city_code')  # Код города отправки (из ПВЗ)
    to_city_code = data.get('to_city_code')      # Код города доставки
    weight = data.get('weight', 1000)             # Вес в граммах
    length = data.get('length', 10)               # Длина в см
    width = data.get('width', 10)                 # Ширина в см
    height = data.get('height', 10)               # Высота в см
    
    if not from_city_code:
        return jsonify({'error': 'Укажите код города отправки'}), 400
    
    # Если город доставки не указан, используем тестовый (Москва=44 или СПб=137)
    # для демонстрации доступных тарифов
    if not to_city_code:
        # Используем Москву по умолчанию, если отправка не из Москвы
        to_city_code = 137 if str(from_city_code) == '44' else 44
    
    try:
        from app.integrations.cdek import get_cdek_client, CDEKClient
        
        client = get_cdek_client()
        
        # Используем tarifflist для получения всех доступных тарифов
        results = client.calculate(
            from_location={'code': int(from_city_code)},
            to_location={'code': int(to_city_code)},
            weight=weight,
            length=length,
            width=width,
            height=height
        )
        
        # Форматируем результат для отображения
        tariffs = []
        for item in results:
            tariff = item.get('tariff', {})
            tariffs.append({
                'code': tariff.get('code'),
                'name': tariff.get('name'),
                'price': item.get('price'),
                'period_min': tariff.get('period_min'),
                'period_max': tariff.get('period_max'),
                'delivery_mode': tariff.get('delivery_mode')
            })
        
        return jsonify({
            'success': True,
            'tariffs': tariffs
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
