"""
Декораторы для контроля доступа.
"""

from functools import wraps
from urllib.parse import urlparse
from flask import abort, redirect, url_for, flash, request, current_app
from flask_login import current_user


def login_required_with_message(message=None):
    """
    Декоратор, требующий авторизации.
    Опционально можно задать сообщение для редиректа.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash(message or 'Пожалуйста, войдите для доступа к этой странице.', 'warning')
                # Сохраняем URL для редиректа после входа
                next_page = request.url
                parsed = urlparse(next_page)
                if parsed.path != url_for('auth.login'):
                    return redirect(url_for('auth.login', next=next_page))
                return redirect(url_for('auth.login'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def buyer_required(f):
    """
    Декоратор, требующий авторизации как покупатель.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Пожалуйста, войдите как покупатель.', 'warning')
            return redirect(url_for('auth.login', next=request.url))
        
        if not isinstance(current_user, current_app.model_globals['Buyer']):
            flash('Этот раздел доступен только покупателям.', 'error')
            if hasattr(current_user, 'store_name'):
                # Продавец - редирект в панель продавца
                return redirect(url_for('seller.dashboard'))
            # Админ - редирект в админку
            return redirect(url_for('admin.dashboard'))
        
        return f(*args, **kwargs)
    return decorated_function


def seller_required(f):
    """
    Декоратор, требующий авторизации как продавец.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Пожалуйста, войдите как продавец.', 'warning')
            return redirect(url_for('auth_seller.seller_login', next=request.url))
        
        if not isinstance(current_user, current_app.model_globals['Seller']):
            flash('Этот раздел доступен только продавцам.', 'error')
            if isinstance(current_user, current_app.model_globals['Buyer']):
                # Покупатель - редирект на главную
                return redirect(url_for('main.index'))
            # Админ - редирект в админку
            return redirect(url_for('admin.dashboard'))
        
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """
    Декоратор, требующий авторизации как администратор.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from app.models.users import Admin
        
        # Проверка абсолютного админа из конфига
        if (request.path.startswith('/main_admin') or 
            request.endpoint.startswith('admin.')):
            
            # Проверка сессионного админа
            if not current_user.is_authenticated:
                flash('Пожалуйста, войдите как администратор.', 'warning')
                return redirect(url_for('auth.admin_login', next=request.url))
            
            if not isinstance(current_user, Admin):
                flash('Доступ запрещён. Требуются права администратора.', 'error')
                if isinstance(current_user, current_app.model_globals['Seller']):
                    return redirect(url_for('seller.dashboard'))
                return redirect(url_for('main.index'))
        
        return f(*args, **kwargs)
    return decorated_function


def main_admin_required(f):
    """
    Декоратор для абсолютного админа (из конфига).
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Проверка авторизации абсолютного админа
        if not session.get('main_admin_authenticated'):
            # Проверка по IP или другим признакам
            if request.remote_addr != '127.0.0.1' and request.remote_addr != '::1':
                flash('Доступ запрещён.', 'error')
                return redirect(url_for('main.index'))
        
        return f(*args, **kwargs)
    return decorated_function


def seller_owns_product(f):
    """
    Декоратор, проверяющий что продавец владеет товаром.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from app import db
        from app.models.products import Product
        
        product_id = kwargs.get('product_id')
        if not product_id:
            abort(404)
        
        product = db.session.get(Product, product_id)
        if not product:
            abort(404)
        
        if not isinstance(current_user, current_app.model_globals['Seller']):
            abort(403)
        
        if product.seller_id != current_user.id:
            flash('У вас нет прав на редактирование этого товара.', 'error')
            return redirect(url_for('seller.products'))
        
        kwargs['product'] = product
        return f(*args, **kwargs)
    return decorated_function


def seller_owns_order(f):
    """
    Декоратор, проверяющий что продавец владеет заказом.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from app import db
        from app.models.orders import Order
        
        order_id = kwargs.get('order_id')
        if not order_id:
            abort(404)
        
        order = db.session.get(Order, order_id)
        if not order:
            abort(404)
        
        if not isinstance(current_user, current_app.model_globals['Seller']):
            abort(403)
        
        if order.seller_id != current_user.id:
            flash('У вас нет прав на этот заказ.', 'error')
            return redirect(url_for('seller.orders'))
        
        kwargs['order'] = order
        return f(*args, **kwargs)
    return decorated_function


def ajax_login_required(f):
    """
    Декоратор для AJAX-запросов, требующих авторизации.
    Возвращает JSON с ошибкой вместо редиректа.
    """
    from flask import jsonify
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Требуется авторизация', 'code': 'auth_required'}), 401
        return f(*args, **kwargs)
    return decorated_function


def ajax_buyer_required(f):
    """
    Декоратор для AJAX-запросов, требующих авторизации как покупатель.
    Возвращает JSON с ошибкой вместо редиректа.
    """
    from flask import jsonify
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Требуется авторизация', 'code': 'auth_required'}), 401
        
        if not isinstance(current_user, current_app.model_globals['Buyer']):
            return jsonify({'error': 'Этот раздел доступен только покупателям', 'code': 'buyer_required'}), 403
        
        return f(*args, **kwargs)
    return decorated_function


def rate_limit(limit=100, per=3600, key_prefix='rate_limit_'):
    """
    Декоратор для ограничения частоты запросов.
    Использует кэш для хранения счётчиков.
    """
    from flask import request, jsonify
    from app import cache
    
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Ключ ограничения
            if current_user.is_authenticated:
                key = f"{key_prefix}{current_user.id}"
            else:
                key = f"{key_prefix}{request.remote_addr}"
            
            # Получение текущего счётчика
            current = cache.get(key) or 0
            
            if current >= limit:
                return jsonify({'error': 'Слишком много запросов', 'retry_after': per}), 429
            
            # Увеличение счётчика
            cache.set(key, current + 1, timeout=per)
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# Импорт session для проверки main_admin
from flask import session
