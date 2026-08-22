"""
Blueprint авторизации.
Обработка регистрации, входа и выхода пользователей.
"""

import logging
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, flash, request, session, abort
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash
from app import db, csrf
from app.models.users import Buyer, Seller
from app.utils.helpers import slugify
from app.utils.decorators import login_required_with_message

# Настройка логирования
logger = logging.getLogger(__name__)

bp = Blueprint('auth', __name__, url_prefix='/auth')


# Декоратор для проверки входа продавца
def seller_login_required(f):
    """
    Декоратор, требующий авторизации как продавец.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, Seller):
            if not current_user.is_authenticated:
                flash('Пожалуйста, войдите как продавец.', 'warning')
            else:
                flash('Этот раздел доступен только продавцам.', 'error')
            return redirect(url_for('auth.seller_login'))
        return f(*args, **kwargs)
    return decorated


@bp.route('/login', methods=['GET', 'POST'])
@csrf.exempt
def login():
    """
    Страница входа для покупателей.
    URL: /auth/login
    """
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    if request.method == 'POST':
        login_field = request.form.get('login')
        password = request.form.get('password')
        remember = 'remember' in request.form
        
        # Поиск покупателя
        buyer = Buyer.query.filter(
            (Buyer.login == login_field) | (Buyer.email == login_field)
        ).first()
        
        if buyer and buyer.check_password(password):
            if not buyer.is_active:
                flash('Ваш аккаунт заблокирован.', 'error')
            else:
                login_user(buyer, remember=remember)
                buyer.last_login = db.func.now()
                db.session.commit()
                
                next_page = request.args.get('next')
                if not next_page or not next_page.startswith('/'):
                    next_page = url_for('main.index')
                return redirect(next_page)
        else:
            flash('Неверный логин или пароль.', 'error')
    
    return render_template('auth/login.html', title='Вход')


@bp.route('/seller/login', methods=['GET', 'POST'])
@csrf.exempt
def seller_login():
    """
    Страница входа для продавцов.
    URL: seller.domain/auth/login
    """
    logger.info("=== SELLER LOGIN START ===")
    logger.info(f"current_user.is_authenticated: {current_user.is_authenticated}")
    
    if current_user.is_authenticated and isinstance(current_user, Seller):
        logger.info("Already logged in as seller, redirecting to dashboard")
        return redirect(url_for('seller.dashboard'))
    
    if request.method == 'POST':
        login_field = request.form.get('login')
        password = request.form.get('password')
        remember = 'remember' in request.form
        
        logger.info(f"POST request - login_field: {login_field}, remember: {remember}")
        
        # Поиск продавца
        seller = Seller.query.filter(
            (Seller.login == login_field) | (Seller.email == login_field)
        ).first()
        
        logger.info(f"Seller found: {seller}")
        if seller:
            logger.info(f"Seller id: {seller.id}, login: {seller.login}, is_active: {seller.is_active}")
        
        if seller and seller.check_password(password):
            logger.info("Password check passed")
            if not seller.is_active:
                logger.warning("Seller account is not active")
                flash('Ваш магазин заблокирован.', 'error')
                return render_template('auth/seller_login.html', title='Вход для продавцов')
            else:
                logger.info("Logging in seller...")
                login_user(seller, remember=remember)
                if hasattr(seller, 'last_login'):
                    seller.last_login = datetime.utcnow()
                db.session.commit()
                
                next_page = request.args.get('next')
                if not next_page or not next_page.startswith('/'):
                    next_page = url_for('seller.dashboard')
                logger.info(f"Redirecting to: {next_page}")
                return redirect(next_page)
        else:
            logger.warning("Invalid login or password")
            flash('Неверный логин или пароль.', 'error')
    
    logger.info("Rendering seller_login.html")
    return render_template('auth/seller_login.html', title='Вход для продавцов')


@bp.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """
    Страница входа для администраторов.
    URL: /main_admin/auth/login
    """
    from flask import current_app
    from werkzeug.security import check_password_hash
    
    # Проверка сессионного админа
    if current_user.is_authenticated:
        from app.models.users import Admin
        if isinstance(current_user, Admin):
            return redirect(url_for('admin.dashboard'))
    
    if request.method == 'POST':
        login_field = request.form.get('login')
        password = request.form.get('password')
        
        # Проверка абсолютного админа из конфига
        main_admin_login = current_app.config.get('MAIN_ADMIN_LOGIN')
        main_admin_hash = current_app.config.get('MAIN_ADMIN_PASSWORD_HASH')
        
        if (login_field == main_admin_login and 
            main_admin_hash and 
            check_password_hash(main_admin_hash, password)):
            
            session['main_admin_authenticated'] = True
            session['main_admin_user'] = login_field
            login_user_dummy('admin')
            
            next_page = request.args.get('next')
            if not next_page:
                next_page = url_for('admin.dashboard')
            return redirect(next_page)
        
        # Проверка админа из БД
        from app.models.users import Admin
        admin = Admin.query.filter(
            (Admin.login == login_field) | (Admin.email == login_field)
        ).first()
        
        if admin and admin.check_password(password):
            login_user(admin)
            admin.last_login = db.func.now()
            db.session.commit()
            
            next_page = request.args.get('next')
            if not next_page:
                next_page = url_for('admin.dashboard')
            return redirect(next_page)
        
        flash('Неверный логин или пароль.', 'error')
    
    return render_template('auth/admin_login.html', title='Вход для администратора')


@bp.route('/signup', methods=['GET', 'POST'])
def signup():
    """
    Страница регистрации покупателей.
    URL: /auth/signup
    """
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    if request.method == 'POST':
        login = request.form.get('login')
        email = request.form.get('email')
        password = request.form.get('password')
        password_confirm = request.form.get('password_confirm')
        phone = request.form.get('phone')
        
        # Валидация
        if password != password_confirm:
            flash('Пароли не совпадают.', 'error')
            return render_template('auth/signup.html')
        
        if len(password) < 8:
            flash('Пароль должен содержать минимум 8 символов.', 'error')
            return render_template('auth/signup.html')

        if not (phone or '').strip():
            flash('Укажите номер телефона.', 'error')
            return render_template('auth/signup.html')

        # Проверка уникальности
        if Buyer.query.filter_by(login=login).first():
            flash('Этот логин уже занят.', 'error')
            return render_template('auth/signup.html')

        if Buyer.query.filter_by(email=email).first():
            flash('Этот email уже зарегистрирован.', 'error')
            return render_template('auth/signup.html')
        
        # Создание покупателя
        buyer = Buyer(
            login=login,
            email=email,
            phone=phone,
            bonuses_balance=0
        )
        buyer.set_password(password)
        
        db.session.add(buyer)
        db.session.commit()
        
        flash('Регистрация успешна! Теперь вы можете войти.', 'success')
        return redirect(url_for('auth.login'))
    
    return render_template('auth/signup.html', title='Регистрация')


@bp.route('/seller/signup', methods=['GET', 'POST'])
def seller_signup():
    """
    Страница регистрации продавцов.
    URL: seller.domain/auth/signup
    """
    if current_user.is_authenticated and isinstance(current_user, Seller):
        return redirect(url_for('seller.dashboard'))
    
    if request.method == 'POST':
        login = request.form.get('login')
        email = request.form.get('email')
        password = request.form.get('password')
        password_confirm = request.form.get('password_confirm')
        store_name = request.form.get('store_name')
        phone = request.form.get('phone')
        
        # Валидация
        if password != password_confirm:
            flash('Пароли не совпадают.', 'error')
            return render_template('auth/seller_signup.html')
        
        if len(password) < 8:
            flash('Пароль должен содержать минимум 8 символов.', 'error')
            return render_template('auth/seller_signup.html')
        
        if not store_name or len(store_name) < 3:
            flash('Название магазина должно содержать минимум 3 символа.', 'error')
            return render_template('auth/seller_signup.html')
        
        # Проверка уникальности
        if Seller.query.filter_by(login=login).first():
            flash('Этот логин уже занят.', 'error')
            return render_template('auth/seller_signup.html')
        
        if Seller.query.filter_by(email=email).first():
            flash('Этот email уже зарегистрирован.', 'error')
            return render_template('auth/seller_signup.html')
        
        store_slug = slugify(store_name)
        # Уникализация slug
        base_slug = store_slug
        counter = 1
        while Seller.query.filter_by(store_slug=store_slug).first():
            store_slug = f"{base_slug}-{counter}"
            counter += 1
        
        # Создание продавца
        seller = Seller(
            login=login,
            email=email,
            phone=phone,
            store_name=store_name,
            store_slug=store_slug,
            rating=0,
            reviews_count=0
        )
        seller.set_password(password)
        
        db.session.add(seller)
        db.session.commit()
        
        flash('Регистрация успешна! Теперь вы можете войти в панель продавца.', 'success')
        return redirect(url_for('auth.seller_login'))
    
    return render_template('auth/seller_signup.html', title='Регистрация продавца')


@bp.route('/logout')
@login_required
def logout():
    """
    Выход из системы.
    URL: /auth/logout
    """
    logout_user()
    session.clear()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('main.index'))


@bp.route('/seller/logout')
@seller_login_required
def seller_logout():
    """
    Выход из системы продавца.
    URL: seller.domain/auth/logout
    """
    logout_user()
    session.clear()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('main.index'))


@bp.route('/check-login')
def check_login():
    """
    AJAX проверка доступности логина.
    """
    login = request.args.get('login', '')
    user_type = request.args.get('type', 'buyer')
    
    if user_type == 'seller':
        exists = Seller.query.filter_by(login=login).first()
    else:
        exists = Buyer.query.filter_by(login=login).first()
    
    return {'available': not exists}


@bp.route('/check-email')
def check_email():
    """
    AJAX проверка доступности email.
    """
    email = request.args.get('email', '')
    user_type = request.args.get('type', 'buyer')
    
    if user_type == 'seller':
        exists = Seller.query.filter_by(email=email).first()
    else:
        exists = Buyer.query.filter_by(email=email).first()
    
    return {'available': not exists}


def login_user_dummy(user_type):
    """
    Создание фиктивного пользователя для сессии абсолютного админа.
    """
    class DummyUser:
        def __init__(self, user_type):
            self.id = 0
            self.user_type = user_type
            self.is_authenticated = True
            self.is_active = True
            self.is_anonymous = False
            
        def get_id(self):
            return f"{self.user_type.capitalize()}:0"
    
    session['user'] = DummyUser(user_type)


# Импорт login_user для использования в seller_login
from flask_login import login_user


@bp.route('/change_password', methods=['POST'])
@login_required
def change_password():
    """
    Смена пароля текущего пользователя.
    URL: /auth/change_password
    """
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    if not all([current_password, new_password, confirm_password]):
        flash('Заполните все поля.', 'error')
        return redirect(url_for('seller.settings'))
    
    if new_password != confirm_password:
        flash('Новые пароли не совпадают.', 'error')
        return redirect(url_for('seller.settings'))
    
    if len(new_password) < 6:
        flash('Пароль должен быть не менее 6 символов.', 'error')
        return redirect(url_for('seller.settings'))
    
    # Проверяем текущий пароль
    if not current_user.check_password(current_password):
        flash('Неверный текущий пароль.', 'error')
        return redirect(url_for('seller.settings'))
    
    # Устанавливаем новый пароль
    current_user.set_password(new_password)
    db.session.commit()
    
    flash('Пароль успешно изменён.', 'success')
    return redirect(url_for('seller.settings'))
