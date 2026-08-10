"""
Модели товаров: Категории, Товары, Параметры.
"""

from datetime import datetime
from sqlalchemy import event
from app import db


class Category(db.Model):
    """
    Модель категории товаров.
    Поддерживает древовидную структуру через parent_id.
    """
    
    __tablename__ = 'categories'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    image_path = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Иерархия категорий
    parent_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True)
    
    # Связи
    parent = db.relationship('Category', remote_side=[id], backref='subcategories')
    products = db.relationship('Product', back_populates='category', lazy='dynamic',
                              foreign_keys='Product.category_id')
    parameters = db.relationship('CategoryParameter', back_populates='category',
                                 lazy='dynamic', cascade='all, delete-orphan')
    
    @property
    def all_parents(self):
        """Получение всех родительских категорий."""
        parents = []
        current = self.parent
        while current:
            parents.append(current)
            current = current.parent
        return parents[::-1]  # От корня к текущей
    
    @property
    def full_path(self):
        """Полный путь категории для хлебных крошек."""
        return '/'.join([c.name for c in self.all_parents] + [self.name])
    
    @property
    def product_count(self):
        """Количество товаров в категории (включая подкатегории)."""
        from sqlalchemy import func
        
        # Подкатегории
        subcategory_ids = [c.id for c in self.subcategories]
        query_ids = [self.id] + subcategory_ids
        
        return Product.query.filter(
            Product.category_id.in_(query_ids),
            Product.status == 'approved',
            Product.stock_quantity > 0
        ).count()
    
    def get_all_parameters(self):
        """
        Получение всех параметров категории и её предков.
        Рекурсивный сбор параметров для товаров.
        """
        parameters = []
        
        # Параметры всех родительских категорий (включая их унаследованные)
        for parent in self.all_parents:
            # Получаем ВСЕ параметры родителя (свои + унаследованные)
            parent_params = CategoryParameter.query.filter_by(
                category_id=parent.id
            ).all()
            parameters.extend([cp.parameter for cp in parent_params])
        
        # Параметры текущей категории (свои собственные)
        category_params = CategoryParameter.query.filter_by(
            category_id=self.id
        ).all()
        parameters.extend([cp.parameter for cp in category_params])
        
        # Удаляем дубликаты по id
        seen = set()
        unique_params = []
        for p in parameters:
            if p and p.id not in seen:
                seen.add(p.id)
                unique_params.append(p)
        
        return unique_params
    
    def __repr__(self):
        return f'<Category {self.name}>'


class Parameter(db.Model):
    """
    Модель параметра товара.
    Поддерживает различные типы: числовой, строковый, текстовый, картинка.
    """
    
    __tablename__ = 'parameters'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    type = db.Column(db.String(20), nullable=False)  # numeric, string, text, image
    description = db.Column(db.Text, nullable=True)
    
    # Характеристики параметра
    is_composite = db.Column(db.Boolean, default=False)  # Составной (например, размеры)
    composite_count = db.Column(db.Integer, nullable=True)  # 2-4 поля
    is_multiple = db.Column(db.Boolean, default=False)  # Несколько значений
    is_input = db.Column(db.Boolean, default=True)  # Вводимый или предустановленный
    is_required = db.Column(db.Boolean, default=False)
    
    # Для предустановленных значений
    predefined_values = db.Column(db.JSON, nullable=True)  # Список значений
    
    # Изображение параметра
    image_path = db.Column(db.String(255), nullable=True)
    
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    
    # Связи
    product_parameters = db.relationship('ProductParameter', back_populates='parameter',
                                          lazy='dynamic')
    categories = db.relationship('CategoryParameter', back_populates='parameter',
                                 lazy='dynamic')
    
    def validate_value(self, value):
        """
        Валидация значения параметра.
        Возвращает (is_valid, error_message).
        """
        if self.is_composite:
            if not isinstance(value, list):
                return False, "Ожидался список значений"
            if len(value) != self.composite_count:
                return False, f"Ожидалось {self.composite_count} значений"
        
        if self.is_multiple and not isinstance(value, list):
            return False, "Ожидался список значений"
        
        if not self.is_input and self.predefined_values:
            if isinstance(value, list):
                valid = all(v in self.predefined_values for v in value)
            else:
                valid = value in self.predefined_values
            if not valid:
                return False, f"Значение должно быть одним из: {self.predefined_values}"
        
        return True, None
    
    def __repr__(self):
        return f'<Parameter {self.name}>'


class CategoryParameter(db.Model):
    """
    Связь категории с параметрами.
    Позволяет наследовать параметры от родительских категорий.
    """
    
    __tablename__ = 'category_parameters'
    
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False)
    parameter_id = db.Column(db.Integer, db.ForeignKey('parameters.id'), nullable=False)
    
    is_inherited = db.Column(db.Boolean, default=False)  # Наследуется от родителя
    default_value = db.Column(db.JSON, nullable=True)  # Значение по умолчанию
    sort_order = db.Column(db.Integer, default=0)
    is_required = db.Column(db.Boolean, default=False)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    category = db.relationship('Category', back_populates='parameters')
    parameter = db.relationship('Parameter', back_populates='categories')
    
    def __repr__(self):
        return f'<CategoryParameter category={self.category_id} parameter={self.parameter_id}>'


class Product(db.Model):
    """
    Модель товара.
    """
    
    __tablename__ = 'products'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), unique=True, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    
    # Ценообразование
    price = db.Column(db.Float, nullable=False)
    max_discount_percent = db.Column(db.Integer, default=0)
    current_discount = db.Column(db.Integer, default=0)
    
    # Артикул
    article = db.Column(db.String(50), unique=True, nullable=False, index=True)
    
    # Остатки
    stock_quantity = db.Column(db.Integer, default=0)
    low_stock_threshold = db.Column(db.Integer, default=5)
    
    # Доставка
    weight = db.Column(db.Float, nullable=True)  # В кг
    volume = db.Column(db.Float, nullable=True)  # В м³
    
    # Общая карточка (группировка товаров) - устаревшее поле, сохранено для совместимости
    common_card = db.Column(db.String(100), nullable=True, index=True)

    # Связь с карточкой товара (новая система)
    product_card_id = db.Column(db.Integer, db.ForeignKey('product_cards.id'), nullable=True, index=True)

    # Статус модерации
    status = db.Column(db.String(20), default='draft')  # draft, on_moderation, approved, rejected
    
    # Временные метки
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime, nullable=True)
    moderated_at = db.Column(db.DateTime, nullable=True)

    # Статистика товара (для аналитики продавца)
    views_count = db.Column(db.Integer, default=0, nullable=False)  # Просмотры карточки
    cart_adds_count = db.Column(db.Integer, default=0, nullable=False)  # Добавления в корзину
    
    # Внешние ключи
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False, index=True)
    
    # Связи
    seller = db.relationship('Seller', back_populates='products', foreign_keys=[seller_id])
    category = db.relationship('Category', back_populates='products', foreign_keys=[category_id])
    product_card = db.relationship('ProductCard', back_populates='products', foreign_keys=[product_card_id])
    
    photos = db.relationship('ProductPhoto', back_populates='product', lazy='dynamic',
                            cascade='all, delete-orphan', order_by='ProductPhoto.sort_order')
    parameters = db.relationship('ProductParameter', back_populates='product',
                                lazy='dynamic', cascade='all, delete-orphan')
    reviews = db.relationship('Review', back_populates='product', lazy='dynamic')
    favorites = db.relationship('Favorite', back_populates='product', lazy='dynamic')
    cart_items = db.relationship('CartItem', back_populates='product', lazy='dynamic')
    order_items = db.relationship('OrderItem', back_populates='product', lazy='dynamic')
    promotion_items = db.relationship('PromotionProduct', back_populates='product',
                                      lazy='dynamic', viewonly=True)
    moderation_remarks = db.relationship('ModerationRemark', back_populates='product',
                                        lazy='dynamic', cascade='all, delete-orphan')
    
    @property
    def main_photo(self):
        """Основное фото товара."""
        return self.photos.filter_by(is_main=True).first() or self.photos.first()
    
    @property
    def old_price(self):
        """Цена до скидки (без вычета current_discount). При наличии скидки
        возвращает базовую цену товара; саму скидочную цену см. в
        compute_product_promotion_info().final_price."""
        if self.current_discount and self.current_discount > 0:
            return round(float(self.price or 0), 2)
        return None

    @property
    def discounted_price(self):
        """Цена товара с учётом current_discount (price − скидка)."""
        cd = int(self.current_discount or 0)
        if cd <= 0:
            return round(float(self.price or 0), 2)
        if cd >= 100:
            return 0.0
        return round(float(self.price or 0) * (1 - cd / 100), 2)
    
    @property
    def is_new(self):
        """Товар считается новым, если опубликован менее недели назад."""
        if self.published_at:
            from datetime import timedelta
            return (datetime.utcnow() - self.published_at) < timedelta(days=7)
        return False
    
    @property
    def is_in_promotion(self):
        """Товар участвует в классической акции (через PromotionProduct)."""
        from app.models.orders import Promotion, PromotionProduct
        return Promotion.query.join(PromotionProduct).filter(
            PromotionProduct.product_id == self.id,
            Promotion.status == 'active',
            Promotion.start_date <= datetime.utcnow(),
            Promotion.end_date >= datetime.utcnow()
        ).first() is not None

    def is_in_seller_promotion(self):
        """
        Товар участвует в seller-уровневой акции (second_with_discount и подобных).
        Зависит от того, подключил ли продавец этого товара активный шаблон.
        """
        from app.models.orders import Promotion, SellerPromotion
        now = datetime.utcnow()
        return Promotion.query.join(SellerPromotion).filter(
            SellerPromotion.seller_id == self.seller_id,
            SellerPromotion.is_active == True,
            Promotion.id == SellerPromotion.promotion_id,
            Promotion.status == 'active',
            Promotion.scheme == 'second_with_discount',
            (Promotion.start_date.is_(None)) | (Promotion.start_date <= now),
            (Promotion.end_date.is_(None)) | (Promotion.end_date >= now),
        ).first() is not None

    @property
    def has_any_active_promotion(self):
        """Товар участвует хоть в какой-то активной акции (классической или seller-уровневой)."""
        if self.is_in_promotion:
            return True
        try:
            return self.is_in_seller_promotion()
        except Exception:
            return False
    
    @property
    def average_rating(self):
        """Средний рейтинг товара (только одобренные отзывы)."""
        from sqlalchemy import func
        from app.models.communications import Review
        result = db.session.query(func.avg(Review.rating)).filter(
            Review.product_id == self.id,
            Review.status == 'approved'
        ).scalar()
        return round(float(result), 1) if result else 0.0
    
    @property
    def reviews_count(self):
        """Количество одобренных отзывов."""
        from app.models.communications import Review
        return self.reviews.filter(Review.status == 'approved').count()
    
    @property
    def is_favorite(self, buyer_id):
        """Проверка в избранном."""
        from app.models.users import Favorite
        return Favorite.query.filter_by(buyer_id=buyer_id, product_id=self.id).first() is not None
    
    def get_all_params(self):
        """Получение всех параметров товара (включая категорийные)."""
        # Параметры категории
        category_params = self.category.get_all_parameters()
        
        # Параметры товара
        product_params = {p.parameter_id: p for p in self.parameters.all()}
        
        # Объединяем
        result = []
        for param in category_params:
            if param.id in product_params:
                result.append(product_params[param.id])
        
        # Добавляем параметры товара, которых нет в категории
        existing_ids = {p.parameter_id for p in result}
        for param in product_params.values():
            if param.parameter_id not in existing_ids:
                result.append(param)
        
        return result
    
    def submit_for_moderation(self):
        """Отправка на модерацию."""
        self.status = 'on_moderation'
        db.session.commit()
    
    def approve(self):
        """Одобрение товара."""
        self.status = 'approved'
        self.moderated_at = datetime.utcnow()
        if not self.published_at:
            self.published_at = datetime.utcnow()
        db.session.commit()
    
    def reject(self, remark_text):
        """Отклонение товара с замечанием."""
        self.status = 'rejected'
        self.moderated_at = datetime.utcnow()
        
        # Добавление замечания
        remark = ModerationRemark(product_id=self.id, text=remark_text)
        db.session.add(remark)
        db.session.commit()
    
    def generate_slug(self):
        """Генерация URL-слага."""
        from slugify import slugify
        base_slug = slugify(self.name)
        self.slug = f"{base_slug}-{self.id}"
    
    def __repr__(self):
        return f'<Product {self.name}>'


class ProductPhoto(db.Model):
    """
    Модель фотографии товара.
    """
    
    __tablename__ = 'product_photos'
    
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    path = db.Column(db.String(255), nullable=False)
    is_main = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=0)
    alt_text = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    product = db.relationship('Product', back_populates='photos')
    
    def __repr__(self):
        return f'<ProductPhoto {self.id} for product={self.product_id}>'


class ProductParameter(db.Model):
    """
    Модель значения параметра товара.
    Поддерживает составные и множественные значения через JSON.
    """
    
    __tablename__ = 'product_parameters'
    
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    parameter_id = db.Column(db.Integer, db.ForeignKey('parameters.id'), nullable=False)
    
    # JSON для составных и множественных значений
    value = db.Column(db.JSON, nullable=False)
    
    # Для отображения в каталоге
    display_value = db.Column(db.String(255), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Связи
    product = db.relationship('Product', back_populates='parameters')
    parameter = db.relationship('Parameter', back_populates='product_parameters')
    
    def set_value(self, value):
        """Установка значения с преобразованием в JSON."""
        if self.parameter.is_composite or self.parameter.is_multiple:
            if not isinstance(value, list):
                value = [value]
        self.value = value
        self.display_value = self._format_value()
    
    def _format_value(self):
        """Форматирование значения для отображения."""
        if isinstance(self.value, list):
            if self.parameter.is_composite:
                # Составной параметр (например, размеры)
                return ' × '.join(str(v) for v in self.value)
            else:
                # Множественные значения
                return ', '.join(str(v) for v in self.value)
        return str(self.value)
    
    def __repr__(self):
        return f'<ProductParameter product={self.product_id} param={self.parameter_id}>'


class ModerationRemark(db.Model):
    """
    Замечания по модерации товара.
    """
    
    __tablename__ = 'moderation_remarks'
    
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    product = db.relationship('Product', back_populates='moderation_remarks')
    
    def __repr__(self):
        return f'<ModerationRemark for product={self.product_id}>'


class ProductEvent(db.Model):
    """
    Событие по товару: просмотр карточки или добавление в корзину.
    Источник истины для конверсии по периоду. Накопительные
    `Product.views_count` / `Product.cart_adds_count` остаются как
    кэш для отображения в UI и обновляются триггерно.
    """

    __tablename__ = 'product_events'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'),
                           nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'),
                          nullable=False, index=True)
    # Nullable: гость без логина тоже может открыть карточку
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'),
                         nullable=True, index=True)
    # 'view' | 'add_to_cart'
    event_type = db.Column(db.String(20), nullable=False, index=True)
    # Опционально — для последующей антифрод-аналитики
    session_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)

    # Связи
    product = db.relationship('Product', backref=db.backref(
        'events', lazy='dynamic', cascade='all, delete-orphan'))
    seller = db.relationship('Seller', backref=db.backref(
        'product_events', lazy='dynamic'))
    buyer = db.relationship('Buyer', backref=db.backref(
        'product_events', lazy='dynamic'))

    __table_args__ = (
        db.Index('ix_product_events_seller_type_time',
                 'seller_id', 'event_type', 'created_at'),
    )

    def __repr__(self):
        return (f'<ProductEvent product={self.product_id} '
                f'seller={self.seller_id} type={self.event_type}>')


class ProductCard(db.Model):
    """
    Карточка товара для группировки товаров одной категории по общему параметру.
    Позволяет объединять товары с разными значениями параметра (например, разные цвета/размеры).
    """
    
    __tablename__ = 'product_cards'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    
    # Владелец карточки (продавец)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    
    # Категория товаров в карточке
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False, index=True)
    
    # Параметр для группировки (например, "Цвет" или "Размер")
    grouping_parameter_id = db.Column(db.Integer, db.ForeignKey('parameters.id'), nullable=False)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Связи
    seller = db.relationship('Seller', backref=db.backref('product_cards', lazy='dynamic'))
    category = db.relationship('Category', backref=db.backref('product_cards', lazy='dynamic'))
    grouping_parameter = db.relationship('Parameter', backref=db.backref('product_cards', lazy='dynamic'))
    products = db.relationship('Product', back_populates='product_card', lazy='dynamic')
    
    @property
    def products_count(self):
        """Количество товаров в карточке."""
        return self.products.count()
    
    @property
    def main_product(self):
        """Основной товар карточки (первый одобренный)."""
        return self.products.filter_by(status='approved').first()
    
    def get_grouping_values(self):
        """
        Получение всех значений параметра группировки для товаров в карточке.
        Возвращает список словарей {product_id, value, display_value}.
        """
        values = []
        for product in self.products.filter_by(status='approved').all():
            param_value = ProductParameter.query.filter_by(
                product_id=product.id,
                parameter_id=self.grouping_parameter_id
            ).first()
            if param_value:
                values.append({
                    'product_id': product.id,
                    'value': param_value.value,
                    'display_value': param_value.display_value or str(param_value.value)
                })
        return values
    
    def __repr__(self):
        return f'<ProductCard {self.name} (seller={self.seller_id})>'
