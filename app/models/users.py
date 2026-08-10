"""
Модели пользователей: Покупатели, Продавцы.
"""

from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app import db


class BaseUser(UserMixin):
    """Базовый класс для всех пользователей."""
    
    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    def set_password(self, password):
        """Установка хэша пароля."""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Проверка пароля."""
        return check_password_hash(self.password_hash, password)
    
    def get_id(self):
        """Получение идентификатора для Flask-Login."""
        return f"{self.__class__.__name__}:{self.id}"


class Buyer(BaseUser, db.Model):
    """
    Модель покупателя.
    Таблица: buyers
    """
    
    __tablename__ = 'buyers'
    
    # Профиль покупателя
    name = db.Column(db.String(100), nullable=True)  # Имя (вместо first_name для совместимости с формой)
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    middle_name = db.Column(db.String(100), nullable=True)
    address = db.Column(db.Text, nullable=True)  # Адрес доставки
    bonuses_balance = db.Column(db.Float, default=0.0)
    
    # Связи
    orders = db.relationship('Order', back_populates='buyer', lazy='dynamic')
    favorites = db.relationship('Favorite', back_populates='buyer', lazy='dynamic',
                                foreign_keys='Favorite.buyer_id')
    cart_items = db.relationship('CartItem', back_populates='buyer', lazy='dynamic',
                                 foreign_keys='CartItem.buyer_id')
    reviews = db.relationship('Review', back_populates='buyer', lazy='dynamic')
    bonus_transactions = db.relationship('Bonus', back_populates='buyer', lazy='dynamic')
    delivery_profiles = db.relationship('BuyerDelivery', back_populates='buyer', 
                                         lazy='dynamic')
    
    @property
    def messages_sent(self):
        """Получение отправленных сообщений (через запрос, не relationship)."""
        from app.models.communications import Message
        return Message.query.filter(
            Message.sender_type == 'buyer',
            Message.sender_id == self.id
        )
    
    @property
    def full_name(self):
        """Полное ФИО покупателя."""
        parts = [self.last_name, self.first_name, self.middle_name]
        return ' '.join(filter(None, parts)) or self.login
    
    @property
    def total_orders(self):
        """Общее количество заказов."""
        return self.orders.count()
    
    @property
    def total_spent(self):
        """Общая сумма покупок."""
        from sqlalchemy import func
        from app.models.orders import Order
        result = db.session.query(func.sum(Order.total_price)).filter(
            Order.buyer_id == self.id,
            Order.status.in_(['delivered', 'shipped', 'in_transit'])
        ).scalar()
        return float(result) if result else 0.0
    
    def get_unread_messages_count(self):
        """Получение количества непрочитанных сообщений."""
        from app.models.communications import Message
        return Message.query.filter(
            Message.receiver_type == 'buyer',
            Message.receiver_id == self.id,
            Message.is_read == False
        ).count()
    
    def __repr__(self):
        return f'<Buyer {self.login}>'


class Seller(BaseUser, db.Model):
    """
    Модель продавца.
    Таблица: sellers
    """
    
    __tablename__ = 'sellers'
    
    # Информация о магазине
    store_name = db.Column(db.String(100), nullable=False)
    store_slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    store_description = db.Column(db.Text, nullable=True)
    store_logo = db.Column(db.String(255), nullable=True)
    
    # Рейтинг магазина (кешированное значение)
    rating = db.Column(db.Float, default=0.0)
    reviews_count = db.Column(db.Integer, default=0)

    # Лимит на количество оформленных за сутки заказов.
    # NULL = безлимит. 0 или положительное число = максимум заказов
    # за текущие сутки (UTC). При превышении — товары продавца
    # перестают показываться в каталоге, кроме товаров, уже лежащих
    # в корзине у покупателей.
    daily_orders_limit = db.Column(db.Integer, nullable=True)
    
    # Связи
    products = db.relationship('Product', back_populates='seller', lazy='dynamic',
                               foreign_keys='Product.seller_id')
    orders = db.relationship('Order', back_populates='seller', lazy='dynamic',
                            foreign_keys='Order.seller_id')
    delivery_profiles = db.relationship('SellerDelivery', back_populates='seller',
                                         lazy='dynamic')
    
    @property
    def messages_sent(self):
        """Получение отправленных сообщений (через запрос, не relationship)."""
        from app.models.communications import Message
        return Message.query.filter(
            Message.sender_type == 'seller',
            Message.sender_id == self.id
        )

    @property
    def avatar(self):
        """Алиас аватарки магазина. Хранится в store_logo, но шаблоны
        и публичные страницы (а также чаты) используют короткое имя avatar."""
        return self.store_logo
    
    @property
    def products_count(self):
        """Общее количество товаров."""
        return self.products.count()
    
    @property
    def orders_count(self):
        """Общее количество заказов."""
        return self.orders.count()
    
    @property
    def active_products_count(self):
        """Количество активных товаров."""
        from app.models.products import Product
        return self.products.filter(
            Product.status == 'approved',
            Product.stock_quantity > 0
        ).count()
    
    @property
    def pending_orders_count(self):
        """Количество заказов в обработке."""
        return self.orders.filter(
            Order.status.in_(['processing', 'shipped', 'in_transit'])
        ).count()
    
    def get_unread_messages_count(self):
        """Получение количества непрочитанных сообщений."""
        from app.models.communications import Message
        return Message.query.filter(
            Message.receiver_type == 'seller',
            Message.receiver_id == self.id,
            Message.is_read == False
        ).count()
    
    def calculate_rating(self):
        """Пересчёт среднего рейтинга магазина (только одобренные отзывы)."""
        from sqlalchemy import func
        from app.models.communications import Review

        result = db.session.query(func.avg(Review.rating)).join(
            Product, Review.product_id == Product.id
        ).filter(
            Product.seller_id == self.id,
            Review.status == 'approved'
        ).scalar()

        self.rating = round(float(result), 1) if result else 0.0

        # Обновление количества одобренных отзывов
        self.reviews_count = Review.query.join(
            Product, Review.product_id == Product.id
        ).filter(
            Product.seller_id == self.id,
            Review.status == 'approved'
        ).count()

        db.session.commit()
    
    def __repr__(self):
        return f'<Seller {self.store_name}>'


class Admin(db.Model, UserMixin):
    """
    Модель администратора.
    Примечание: Абсолютный админ хранится в конфиге, не в БД.
    Этот класс для дополнительных админов при необходимости.
    """
    
    __tablename__ = 'admins'
    
    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    full_name = db.Column(db.String(200), nullable=True)
    permissions = db.Column(db.String(500), default='all')  # JSON с правами
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def get_id(self):
        """Получение идентификатора для Flask-Login."""
        return f"Admin:{self.id}"
    
    def has_permission(self, permission):
        """Проверка права доступа."""
        if self.permissions == 'all':
            return True
        try:
            perms = set(self.permissions.split(','))
            return permission in perms
        except:
            return False
    
    def __repr__(self):
        return f'<Admin {self.login}>'


class DeliveryService(db.Model):
    """
    Модель службы доставки.
    """
    
    __tablename__ = 'delivery_services'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    logo_path = db.Column(db.String(255), nullable=True)
    api_module = db.Column(db.String(100), nullable=True)  # Название модуля API
    api_settings = db.Column(db.JSON, nullable=True)  # Настройки API (account, secret и т.д.)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    buyer_deliveries = db.relationship('BuyerDelivery', back_populates='delivery_service',
                                        lazy='dynamic')
    seller_deliveries = db.relationship('SellerDelivery', back_populates='delivery_service',
                                         lazy='dynamic')
    orders = db.relationship('Order', back_populates='delivery_service', lazy='dynamic')
    
    def __repr__(self):
        return f'<DeliveryService {self.name}>'


class BuyerDelivery(db.Model):
    """
    Профиль доставки покупателя.
    """
    
    __tablename__ = 'buyer_deliveries'
    
    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False)
    delivery_service_id = db.Column(db.Integer, db.ForeignKey('delivery_services.id'),
                                     nullable=False)
    address = db.Column(db.String(255), nullable=True)  # Или PVZ код
    pvz_code = db.Column(db.String(50), nullable=True)
    pvz_address = db.Column(db.String(255), nullable=True)
    pvz_city = db.Column(db.String(100), nullable=True)  # Город ПВЗ
    pvz_city_code = db.Column(db.Integer, nullable=True)  # Код города для расчёта доставки
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    buyer = db.relationship('Buyer', back_populates='delivery_profiles')
    delivery_service = db.relationship('DeliveryService', back_populates='buyer_deliveries')
    
    def __repr__(self):
        return f'<BuyerDelivery buyer={self.buyer_id} service={self.delivery_service_id}>'


class SellerDelivery(db.Model):
    """
    Профиль отправки продавца.
    """
    
    __tablename__ = 'seller_deliveries'
    
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    delivery_service_id = db.Column(db.Integer, db.ForeignKey('delivery_services.id'),
                                     nullable=False)
    api_credentials = db.Column(db.JSON, nullable=True)  # Логин/пароль для API
    
    # Адрес отправки (или код ПВЗ для СДЭК)
    ship_from_address = db.Column(db.String(255), nullable=True)
    
    # === CDEK-specific fields ===
    contract_number = db.Column(db.String(50), nullable=True)  # Номер договора СДЭК
    pvz_code = db.Column(db.String(20), nullable=True)  # Код ПВЗ отправки
    pvz_address = db.Column(db.String(500), nullable=True)  # Адрес ПВЗ отправки
    pvz_city = db.Column(db.String(100), nullable=True)  # Город ПВЗ
    pvz_city_code = db.Column(db.Integer, nullable=True)  # Код города ПВЗ для расчёта тарифов
    tariffs = db.Column(db.JSON, nullable=True)  # Выбранные тарифы [1, 2, 3, ...]
    is_test_mode = db.Column(db.Boolean, default=True)  # Тестовый режим
    # =============================
    
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Связи
    seller = db.relationship('Seller', back_populates='delivery_profiles')
    delivery_service = db.relationship('DeliveryService', back_populates='seller_deliveries')
    
    @property
    def is_cdek(self):
        """Проверка, является ли СДЭК."""
        return self.delivery_service and self.delivery_service.code == 'cdek'
    
    @property
    def is_configured(self):
        """Проверка, настроены ли учетные данные."""
        if not self.api_credentials:
            return False
        if self.is_cdek:
            return bool(self.api_credentials.get('account') and self.api_credentials.get('secure'))
        return bool(self.api_credentials.get('login') and self.api_credentials.get('password'))
    
    @property
    def tariffs_list(self):
        """Получение списка тарифов."""
        return self.tariffs or [] if self.tariffs else []
    
    def __repr__(self):
        return f'<SellerDelivery seller={self.seller_id} service={self.delivery_service_id}>'
