"""
Модели заказов: Заказы, Корзина, Акции, Бонусы.
"""

from datetime import datetime
from decimal import Decimal
from app import db


class CartItem(db.Model):
    """
    Модель товара в корзине покупателя.
    """
    
    __tablename__ = 'cart_items'
    
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), primary_key=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    buyer = db.relationship('Buyer', back_populates='cart_items', foreign_keys=[buyer_id])
    product = db.relationship('Product', back_populates='cart_items', foreign_keys=[product_id])
    
    @property
    def total_price(self):
        """Общая стоимость позиции с учётом скидки."""
        price = self.product.price
        if self.product.current_discount > 0:
            price = price * (1 - self.product.current_discount / 100)
        return round(price * self.quantity, 2)
    
    def update_quantity(self, quantity):
        """Обновление количества товара."""
        if quantity <= 0:
            db.session.delete(self)
        else:
            # Проверка доступного количества
            if quantity > self.product.stock_quantity:
                quantity = self.product.stock_quantity
            self.quantity = quantity
        db.session.commit()
    
    def __repr__(self):
        return f'<CartItem buyer={self.buyer_id} product={self.product_id} qty={self.quantity}>'


class Favorite(db.Model):
    """
    Модель избранного товара.
    """
    
    __tablename__ = 'favorites'
    
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), primary_key=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Связи
    buyer = db.relationship('Buyer', back_populates='favorites', foreign_keys=[buyer_id])
    product = db.relationship('Product', back_populates='favorites', foreign_keys=[product_id])
    
    def __repr__(self):
        return f'<Favorite buyer={self.buyer_id} product={self.product_id}>'


class Order(db.Model):
    """
    Модель заказа.
    """
    
    __tablename__ = 'orders'
    
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    
    # Статус заказа
    # pending = ожидающие оформления (сформированы в форму, но не подтверждены)
    # processing = в обработке (оформленные)
    # assembled = собран
    # shipped = отправлен
    # in_transit = в пути
    # delivered = доставлен (подтверждено продавцом/службой доставки)
    # received = получен (подтверждено покупателем)
    # canceled = отменён
    status = db.Column(db.String(20), default='pending', index=True)
    
    # Временные метки
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    shipped_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)
    received_at = db.Column(db.DateTime, nullable=True)
    canceled_at = db.Column(db.DateTime, nullable=True)
    
    # Финансы
    total_price = db.Column(db.Float, nullable=False)  # Сумма товаров
    delivery_price = db.Column(db.Float, default=0)
    bonus_used = db.Column(db.Float, default=0)  # Использовано бонусов
    bonus_accrued = db.Column(db.Float, default=0)  # Начислено бонусов
    penalty = db.Column(db.Float, default=0)  # Штраф продавцу

    # Промокод. FK nullable, чтобы при удалении кода продавцом запись не ломалась.
    # promo_code_text хранит именно тот код, который был применён (на случай
    # если продавец потом переименует/удалит запись PromoCode).
    # promo_discount — размер скидки в рублях, посчитанный на момент оформления
    # и применённый к total_price (по аналогии с bonus_used).
    promo_code_id = db.Column(
        db.Integer,
        db.ForeignKey('promo_codes.id'),
        nullable=True,
        index=True,
    )
    promo_code_text = db.Column(db.String(32), nullable=True)
    promo_discount = db.Column(db.Float, nullable=True)

    # Доставка
    delivery_address = db.Column(db.String(500), nullable=True)
    delivery_service_id = db.Column(db.Integer, db.ForeignKey('delivery_services.id'), nullable=True)
    pvz_code = db.Column(db.String(50), nullable=True)
    track_number = db.Column(db.String(100), nullable=True)
    
    # Внешние ключи
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    
    # Комментарий покупателя
    customer_note = db.Column(db.Text, nullable=True)
    
    # Связи
    buyer = db.relationship('Buyer', back_populates='orders', foreign_keys=[buyer_id])
    seller = db.relationship('Seller', back_populates='orders', foreign_keys=[seller_id])
    promo_code = db.relationship('PromoCode', foreign_keys=[promo_code_id])
    delivery_service = db.relationship('DeliveryService', back_populates='orders',
                                       foreign_keys=[delivery_service_id])
    items = db.relationship('OrderItem', back_populates='order', lazy='dynamic',
                           cascade='all, delete-orphan')
    bonus_transactions = db.relationship('Bonus', back_populates='order', lazy='dynamic')
    returns = db.relationship('Return', back_populates='order', lazy='dynamic')
    
    @property
    def grand_total(self):
        """Общая сумма к оплате (товары + доставка − бонусы − промокод)."""
        return round(
            self.total_price
            + (self.delivery_price or 0)
            - (self.bonus_used or 0)
            - (self.promo_discount or 0),
            2,
        )

    @property
    def total_amount(self):
        """Общая сумма заказа (товары + доставка)."""
        return round(self.total_price + (self.delivery_price or 0), 2)
    
    @property
    def item_count(self):
        """Количество товаров в заказе."""
        return sum(item.quantity for item in self.items.all())
    
    @property
    def display_status(self):
        """
        Возвращает статус для отображения.
        Маппинг старых статусов на новые для совместимости.
        """
        status_map = {
            'pending': 'pending',
            'paid': 'processing',       # Оплачен → в обработке
            'processing': 'processing',
            'in_assembly': 'processing',  # Старый → в обработке
            'assembled': 'processing',    # Старый → в обработке
            'shipped': 'shipped',
            'in_transit': 'shipped',      # Старый → отправлен
            'delivered': 'delivered',
            'received': 'received',       # Получен покупателем
            'canceled': 'cancelled',
            'cancelled': 'cancelled'
        }
        return status_map.get(self.status, 'pending')
    
    @property
    def status_text(self):
        """
        Возвращает текстовое название статуса для отображения.
        """
        text_map = {
            'pending': 'Ожидает оформления',
            'paid': 'Оплачен',
            'processing': 'В обработке',
            'shipped': 'Отправлен',
            'in_transit': 'В пути',
            'delivered': 'Доставлен',
            'received': 'Получено',
            'canceled': 'Отменён',
            'cancelled': 'Отменён',
            'in_assembly': 'В сборке',
            'assembled': 'Собран',
            'confirmed': 'Подтверждён'
        }
        return text_map.get(self.status, self.status)
    
    @property
    def status_display(self):
        """
        Возвращает текстовое название статуса (алиас для совместимости).
        """
        return self.status_text
    
    @property
    def is_overdue(self):
        """Заказ просрочен."""
        from datetime import timedelta
        from app.models.users import SellerDelivery
        
        if self.status in ['delivered', 'canceled']:
            return False
        
        # Получаем срок выполнения заказа у продавца
        seller_delivery = SellerDelivery.query.filter(
            SellerDelivery.seller_id == self.seller_id,
            SellerDelivery.delivery_service_id == self.delivery_service_id
        ).first()
        
        if seller_delivery and seller_delivery.api_credentials:
            # Берем срок из API или настроек
            deadline_days = 3  # По умолчанию
        else:
            deadline_days = 3
        
        deadline = self.created_at + timedelta(days=deadline_days)
        return datetime.utcnow() > deadline
    
    @property
    def penalty_amount(self):
        """Расчёт суммы штрафа."""
        if self.penalty > 0:
            return self.penalty
        return 0.0
    
    def generate_order_number(self):
        """Генерация уникального номера заказа."""
        import random
        import string
        
        prefix = datetime.utcnow().strftime('%Y%m')
        random_part = ''.join(random.choices(string.digits, k=6))
        self.order_number = f"{prefix}{random_part}"
    
    def assemble(self):
        """Перевод в статус 'Отправлен'."""
        self.status = 'shipped'
        db.session.commit()
    
    def ship(self, track_number=None):
        """Отправка заказа."""
        self.status = 'shipped'
        self.shipped_at = datetime.utcnow()
        if track_number:
            self.track_number = track_number
        db.session.commit()
    
    def deliver(self):
        """Доставка заказа."""
        self.status = 'delivered'
        self.delivered_at = datetime.utcnow()
        db.session.commit()
        
        # Начисление бонусов покупателю
        self._accrue_bonuses()
    
    def mark_received(self):
        """
        Подтверждение получения заказа покупателем.
        Переводит заказ из статуса «Доставлен» в «Получено» (received)
        и начисляет покупателю бонусные баллы по программе лояльности
        (если она включена и селлер подключился к курсу).
        """
        self.status = 'received'
        self.received_at = datetime.utcnow()
        db.session.commit()

        # Начисление бонусов по новой per-seller программе лояльности.
        # Импорт внутри метода, чтобы избежать циклических импортов.
        from app.utils.loyalty import accrue_bonuses_for_order
        try:
            accrue_bonuses_for_order(self)
            db.session.commit()
        except Exception:
            # Начисление бонусов — не критичная операция; сбой не должен
            # откатывать подтверждение получения.
            db.session.rollback()
    
    def cancel(self, reason=None):
        """Отмена заказа."""
        # Запоминаем предыдущий статус, чтобы корректно откатить бонусы
        # по новой программе лояльности (если заказ уже был «получен»).
        was_received = self.status == 'received'

        # Возвращаем товары на склад
        for item in self.items:
            item.product.stock_quantity += item.quantity

        self.status = 'canceled'
        self.canceled_at = datetime.utcnow()
        db.session.commit()

        # Возврат бонусов покупателю (старая логика — общий bonuses_balance)
        self._reverse_bonuses()

        # Списание бонусов с продавца
        self._apply_penalty_to_seller()

        # Откат per-seller начисления, если заказ был получен
        if was_received:
            from app.utils.loyalty import reverse_bonuses_for_order
            try:
                reverse_bonuses_for_order(self)
                db.session.commit()
            except Exception:
                db.session.rollback()
    
    def _accrue_bonuses(self):
        """Начисление бонусов покупателю."""
        if self.bonus_accrued > 0:
            bonus = Bonus(
                buyer_id=self.buyer_id,
                order_id=self.id,
                amount=self.bonus_accrued,
                type='accrued',
                reason=f'Бонусы за заказ {self.order_number}'
            )
            db.session.add(bonus)
            
            buyer = db.session.get(Buyer, self.buyer_id)
            buyer.bonuses_balance += self.bonus_accrued
            db.session.commit()
    
    def _reverse_bonuses(self):
        """Отмена начисленных бонусов."""
        if self.bonus_accrued > 0:
            bonus = Bonus(
                buyer_id=self.buyer_id,
                order_id=self.id,
                amount=self.bonus_accrued,
                type='reversed',
                reason=f'Отмена бонусов за отменённый заказ {self.order_number}'
            )
            db.session.add(bonus)
            
            buyer = db.session.get(Buyer, self.buyer_id)
            buyer.bonuses_balance -= self.bonus_accrued
            db.session.commit()
    
    def _apply_penalty_to_seller(self):
        """Применение штрафа к продавцу."""
        from app.models.users import Seller, Buyer
        
        # Штраф продавцу
        self.penalty = round(self.total_price * 0.1, 2)  # 10% по умолчанию
        
        # Начисление бонусов покупателю
        penalty_bonus = round(self.penalty * 0.5, 2)  # 50% штрафа покупателю
        
        bonus = Bonus(
            buyer_id=self.buyer_id,
            order_id=self.id,
            amount=penalty_bonus,
            type='accrued',
            reason=f'Штрафные бонусы за отменённый заказ {self.order_number}'
        )
        db.session.add(bonus)
        
        buyer = db.session.get(Buyer, self.buyer_id)
        buyer.bonuses_balance += penalty_bonus
        db.session.commit()
    
    def __repr__(self):
        return f'<Order {self.order_number}>'


class OrderItem(db.Model):
    """
    Модель позиции в заказе.
    """
    
    __tablename__ = 'order_items'
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    
    quantity = db.Column(db.Integer, nullable=False)
    price_at_order = db.Column(db.Float, nullable=False)  # Цена на момент заказа
    # Исторически зафиксированная цена до скидки. Может быть None
    # для старых заказов (до миграции) — тогда расшифровка
    # восстанавливается из текущей скидки товара как фолбэк.
    original_price = db.Column(db.Float, nullable=True)
    # Скидка по промо-акции (рубли на позицию, 0 если промо не применялось).
    # Записывается только когда применённая скидка пришла от промо,
    # а не от базовой скидки товара. Правило "не суммируется" — берём
    # максимум из доступных скидок; см. compute_best_discount_for_item.
    promo_discount = db.Column(db.Float, nullable=True)
    
    # Связи
    order = db.relationship('Order', back_populates='items', foreign_keys=[order_id])
    product = db.relationship('Product', back_populates='order_items', foreign_keys=[product_id])
    returns = db.relationship('Return', back_populates='order_item', lazy='dynamic')
    
    @property
    def total_price(self):
        """Общая стоимость позиции (цена после скидки × кол-во)."""
        return round(self.price_at_order * self.quantity, 2)

    @property
    def line_subtotal(self):
        """
        Сумма позиции по «оригинальной» цене (до скидки).
        Используется для расшифровки стоимости заказа.
        """
        orig = self.original_price
        if orig is None:
            # Фолбэк для старых заказов: пробуем восстановить через текущую
            # скидку товара. Это неточно, если продавец менял цену после
            # оформления, но лучше, чем ничего.
            cd = self.product.current_discount if self.product else 0
            if cd and cd > 0 and self.price_at_order > 0:
                orig = round(self.price_at_order / (1 - cd / 100), 2)
            else:
                orig = self.price_at_order
        return round(float(orig) * self.quantity, 2)

    @property
    def line_discount(self):
        """Сумма скидки по позиции (в рублях)."""
        return round(self.line_subtotal - self.total_price, 2)

    @property
    def line_discount_percent(self):
        """Сумма скидки по позиции (в процентах, целое)."""
        sub = self.line_subtotal
        if sub <= 0:
            return 0
        return int(round(self.line_discount / sub * 100))

    @property
    def line_product_discount(self):
        """
        Скидка по базовой скидке товара (current_discount) на позицию в рублях.

        Общая скидка = line_discount. По правилу «не суммируется — берём
        максимум» в заказе зафиксировано либо promo_discount, либо
        current_discount. Поэтому:
          • если promo_discount > 0 — line_product_discount = 0
            (промо победило, базовая скидка товара в этом заказе не применена);
          • иначе line_product_discount = line_discount.
        """
        if self.promo_discount and self.promo_discount > 0:
            return 0.0
        return round(self.line_discount, 2)

    @property
    def line_promo_discount_amount(self):
        """
        Скидка по промо-акции на позицию в рублях (за весь qty).
        Для старых заказов, где promo_discount ещё не заполнялся, —
        возвращает 0.0.
        """
        return round(float(self.promo_discount or 0), 2)

    def __repr__(self):
        return f'<OrderItem order={self.order_id} product={self.product_id} qty={self.quantity}>'


class Promotion(db.Model):
    """
    Модель акции/промо.

    Поддерживает два режима:
      1. Шаблон от админа (scheme in {'second_with_discount','one_plus_one',
         'discount'}) — продавцы могут подключиться через SellerPromotion.
         • second_with_discount — выбор товаров не нужен (акция применяется
           ко всем товарам продавца).
         • one_plus_one — продавец выбирает участвующие товары через
           PromotionProduct (минимум по цене задаётся в Promotion.min_product_price).
         • discount — продавец выбирает участвующие товары через PromotionProduct.
      2. Классическая акция (scheme in {'discount','gift','1+1','2+1','3+1'})
         — товары привязываются через PromotionProduct.
    """

    __tablename__ = 'promotions'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)

    # Схема акции
    # Поддерживаемые значения:
    #   second_with_discount — «Купи один — получи скидку» (каждый второй по минимальной цене)
    #   one_plus_one         — «1+1: купи один — получи второй со скидкой 99%» (по выбранным товарам)
    #   discount             — процентная скидка (с указанием discount_percent)
    #   gift                 — подарок
    #   1+1, 2+1, 3+1        — N+1
    scheme = db.Column(db.String(30), nullable=False)
    discount_percent = db.Column(db.Integer, nullable=True)  # Для scheme='discount' / 'second_with_discount' / 'one_plus_one'

    # Минимальная цена товара, который продавец может добавить в акцию.
    # Используется для схем с выбором товаров (one_plus_one, discount-шаблон).
    # NULL = ограничения нет.
    min_product_price = db.Column(db.Float, nullable=True)

    # Для схемы 'discount' (классическая с привязкой товаров) указывает,
    # что у всех товаров акции одинаковая скидка. Если False — у каждого
    # товара может быть своя (хранится в PromotionProduct.discount_percent).
    apply_same_discount = db.Column(db.Boolean, default=True, nullable=False)

    # Признак шаблона. Шаблон (is_template=True) — это админская акция,
    # которую продавцы подключают к себе. Не-шаблон — старая логика, прямые акции.
    is_template = db.Column(db.Boolean, default=False)

    # Временные рамки
    # Если оба не указаны — акция бессрочная (неограниченный срок действия).
    # Если указан только start_date — действует с этой даты без ограничения по окончанию.
    # Если указан только end_date — действует до этой даты (без нижней границы).
    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)

    # Статус
    status = db.Column(db.String(20), default='draft')  # draft, forming, active, completed

    # Изображения
    icon_path = db.Column(db.String(255), nullable=True)
    banner_path = db.Column(db.String(255), nullable=True)

    # Ограничения
    min_order_amount = db.Column(db.Float, default=0)
    max_discount_amount = db.Column(db.Float, nullable=True)

    # Статистика
    total_sales = db.Column(db.Float, default=0)
    items_sold = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    products = db.relationship('PromotionProduct', back_populates='promotion',
                               lazy='dynamic', cascade='all, delete-orphan')
    seller_links = db.relationship('SellerPromotion', back_populates='promotion',
                                   lazy='dynamic', cascade='all, delete-orphan')

    @property
    def is_active(self):
        """Акция активна. None у дат означает «без ограничения»."""
        now = datetime.utcnow()
        if self.status != 'active':
            return False
        if self.start_date and now < self.start_date:
            return False
        if self.end_date and now > self.end_date:
            return False
        return True

    @property
    def status_text(self):
        text_map = {
            'draft': 'Черновик',
            'forming': 'Формируется',
            'active': 'Активна',
            'completed': 'Завершена',
        }
        return text_map.get(self.status, self.status)

    @property
    def status_display(self):
        return self.status_text

    @property
    def days_left(self):
        """Дней до окончания. None если дата окончания не задана (бессрочно)."""
        if not self.end_date:
            return None
        remaining = self.end_date - datetime.utcnow()
        return max(0, remaining.days)

    # Схемы, в которых товар не выбирается — акция либо
    # идёт «на всю корзину продавца» (second_with_discount), либо применяется
    # ко всем товарам продавца. Идентифицируются явно.
    SELLER_SCOPE_SCHEMES = {'second_with_discount'}

    # Шаблонные схемы, для которых продавец выбирает участвующие товары
    # через PromotionProduct (а не на всю корзину продавца).
    TEMPLATE_WITH_PRODUCTS_SCHEMES = {'one_plus_one', 'two_plus_one', 'three_plus_one'}

    # N+1: сколько товаров нужно купить, чтобы получить следующий со скидкой.
    # one_plus_one    — купи 1, получи 2-й со скидкой (floor(N/2) самых дешёвых).
    # two_plus_one    — купи 2, получи 3-й со скидкой (floor(N/3)).
    # three_plus_one  — купи 3, получи 4-й со скидкой (floor(N/4)).
    N_PLUS_ONE_REQUIRED = {
        'one_plus_one': 1,
        'two_plus_one': 2,
        'three_plus_one': 3,
    }

    @property
    def is_seller_scope(self):
        """True, если акция применяется ко всем товарам продавца, без выбора."""
        return self.scheme in self.SELLER_SCOPE_SCHEMES

    @property
    def is_template_with_products(self):
        """True, если шаблонная акция требует выбора товаров продавцом."""
        return self.scheme in self.TEMPLATE_WITH_PRODUCTS_SCHEMES

    def get_effective_discount_percent(self, seller_id=None):
        """
        Возвращает эффективный процент скидки для продавца.
        Если у продавца есть подключение с override — берётся override.
        Иначе — базовый discount_percent шаблона.
        """
        if seller_id is not None:
            link = SellerPromotion.query.filter_by(
                promotion_id=self.id, seller_id=seller_id
            ).first()
            if link and link.override_discount_percent is not None:
                return link.override_discount_percent
        return self.discount_percent

    def get_seller_apply_same(self, seller_id):
        """Возвращает effective apply_same_discount для подключения продавца."""
        if seller_id is None:
            return self.apply_same_discount
        link = SellerPromotion.query.filter_by(
            promotion_id=self.id, seller_id=seller_id
        ).first()
        if link and link.apply_same_discount is not None:
            return link.apply_same_discount
        return self.apply_same_discount

    def calculate_discount(self, product_price, quantity=1, per_item_percent=None):
        """
        Расчёт скидки для товара (для схем с per-item логикой).

        Args:
            product_price: цена единицы товара
            quantity: количество в корзине
            per_item_percent: индивидуальный процент для этого товара внутри
                              акции (PromotionProduct.discount_percent). Если
                              передан — используется он, иначе берётся общий
                              discount_percent акции (для scheme='discount').
        """
        if not self.is_active:
            return 0, 0

        if self.scheme == 'discount':
            percent = per_item_percent if per_item_percent is not None else (self.discount_percent or 0)
            discount = round(product_price * percent / 100, 2)
            if self.max_discount_amount:
                discount = min(discount, self.max_discount_amount)
            return discount, quantity * discount

        elif self.scheme in ['1+1', '2+1', '3+1']:
            # Legacy N+1: за N товаров 1 бесплатно (со скидкой 99%)
            free_items = quantity // (int(self.scheme[0]) + 1)
            discount = product_price * free_items * 0.99
            return round(discount, 2), discount

        return 0, 0

    def get_product_discount_percent(self, product_id, seller_id=None):
        """
        Возвращает эффективный процент скидки для конкретного товара
        в этой акции (учитывает per-item override из PromotionProduct и
        seller-override из SellerPromotion).
        Возвращает None, если товар не входит в акцию.
        """
        from app.models.orders import PromotionProduct, SellerPromotion
        link = PromotionProduct.query.filter_by(
            promotion_id=self.id, product_id=product_id
        ).first()
        if not link:
            return None
        if self.scheme != 'discount':
            return self.discount_percent

        seller_link = None
        if seller_id is not None:
            seller_link = SellerPromotion.query.filter_by(
                promotion_id=self.id, seller_id=seller_id
            ).first()

        # apply_same_discount: сначала per-seller override, потом шаблонное
        apply_same = self.apply_same_discount
        if seller_link is not None and seller_link.apply_same_discount is not None:
            apply_same = seller_link.apply_same_discount

        if apply_same:
            base = self.discount_percent
            if seller_link is not None and seller_link.override_discount_percent is not None:
                base = seller_link.override_discount_percent
            return base

        # per-item режим: сначала PromotionProduct.discount_percent,
        # затем per-seller override (если per-item не задан), затем общий
        if link.discount_percent is not None:
            return link.discount_percent
        if seller_link is not None and seller_link.override_discount_percent is not None:
            return seller_link.override_discount_percent
        return self.discount_percent

    def calculate_discount_for_product(self, product, quantity=1, seller_id=None):
        """
        Скидка в рублях на указанный товар в данной акции.
        Учитывает apply_same_discount (в т.ч. per-seller) и per-item override.
        """
        if self.scheme != 'discount':
            return self.calculate_discount(product.price, quantity)[1]
        sid = seller_id if seller_id is not None else getattr(product, 'seller_id', None)
        percent = self.get_product_discount_percent(product.id, seller_id=sid)
        if percent is None or percent <= 0:
            return 0.0
        return round(product.price * percent / 100, 2) * quantity

    def calculate_n_plus_one_for_cart(self, cart_items, seller_id):
        """
        Расчёт скидки для шаблонных N+1 (one_plus_one / two_plus_one /
        three_plus_one) на уровне корзины продавца.

        Принцип:
          • Берём только товары, участвующие в акции (есть PromotionProduct
            на эту пару promotion + product).
          • Раскрываем по quantity в плоский список (price, cart_item).
          • Сортируем по цене. floor(N / (required + 1)) самых дешёвых штук
            получают скидку `effective_percent` (по умолчанию 99% — из
            Promotion.discount_percent, или override из SellerPromotion).
            required: 1 для 1+1, 2 для 2+1, 3 для 3+1.
          • Если товаров меньше (required + 1) — скидки нет.
          • Скидки по разным акциям не суммируются — это уже решается на уровне
            get_cart_total / compute_best_discount_for_item.

        Возвращает dict:
            {
                'discount': общая сумма скидки в рублях,
                'discounted_items': список cart_item-ов, на которые дана скидка,
                'discounted_count': сколько штук со скидкой,
            }
        """
        empty = {'discount': 0.0, 'discounted_items': [], 'discounted_count': 0}
        if not self.is_active or self.scheme not in self.N_PLUS_ONE_REQUIRED:
            return empty

        required = self.N_PLUS_ONE_REQUIRED[self.scheme]
        percent = self.get_effective_discount_percent(seller_id) or 0
        if percent <= 0:
            return empty

        # Берём только участвующие в акции товары
        from app.models.orders import PromotionProduct
        in_promo_ids = {
            pp.product_id for pp in
            PromotionProduct.query.filter_by(promotion_id=self.id).all()
        }
        if not in_promo_ids:
            return empty

        seller_items = [
            it for it in cart_items
            if it.product
            and it.product.seller_id == seller_id
            and it.product.id in in_promo_ids
        ]
        if not seller_items:
            return empty

        # Раскрываем в плоский список поштучно
        flat = []
        for it in seller_items:
            for _ in range(it.quantity):
                flat.append((it.product.price, it))

        n_discount = len(flat) // (required + 1)
        if n_discount == 0:
            return empty

        flat.sort(key=lambda t: t[0])
        cheapest = flat[:n_discount]

        per_item_discount = {}
        total_discount = 0.0
        for price, item in cheapest:
            d = round(price * percent / 100, 2)
            per_item_discount[item] = per_item_discount.get(item, 0.0) + d
            total_discount += d

        return {
            'discount': round(total_discount, 2),
            'discounted_items': list(per_item_discount.keys()),
            'discounted_count': n_discount,
        }

    # Обратная совместимость: старое имя метода для существующих вызовов.
    def calculate_one_plus_one_for_cart(self, cart_items, seller_id):
        return self.calculate_n_plus_one_for_cart(cart_items, seller_id)

    def calculate_cart_discount(self, cart_items, seller_id):
        """
        Расчёт скидки на уровне корзины (для схем типа second_with_discount
        и N+1 (one_plus_one / two_plus_one / three_plus_one), где важна
        композиция корзины, а не одна позиция).

        Возвращает dict:
            {
                'discount': общая сумма скидки в рублях,
                'discounted_items': список cart_item-ов, на которые дана скидка,
                'discounted_count': сколько штук со скидкой,
            }
        """
        if not self.is_active:
            return {'discount': 0.0, 'discounted_items': [], 'discounted_count': 0}

        if self.scheme == 'second_with_discount':
            percent = self.get_effective_discount_percent(seller_id)
            if not percent or percent <= 0:
                return {'discount': 0.0, 'discounted_items': [], 'discounted_count': 0}

            # Только товары этого продавца
            seller_items = [it for it in cart_items
                            if it.product and it.product.seller_id == seller_id]
            if len(seller_items) < 2:
                return {'discount': 0.0, 'discounted_items': [], 'discounted_count': 0}

            # Разворачиваем в плоский список (price, cart_item) поштучно
            flat = []
            for it in seller_items:
                for _ in range(it.quantity):
                    flat.append((it.product.price, it))

            # floor(N/2) товаров с минимальной ценой получают скидку
            n_discount = len(flat) // 2
            if n_discount == 0:
                return {'discount': 0.0, 'discounted_items': [], 'discounted_count': 0}

            flat.sort(key=lambda t: t[0])
            cheapest = flat[:n_discount]

            # Суммируем скидку и группируем по cart_item-у
            per_item_discount = {}
            total_discount = 0.0
            for price, item in cheapest:
                d = round(price * percent / 100, 2)
                per_item_discount[item] = per_item_discount.get(item, 0.0) + d
                total_discount += d

            return {
                'discount': round(total_discount, 2),
                'discounted_items': list(per_item_discount.keys()),
                'discounted_count': n_discount,
            }

        if self.scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one'):
            return self.calculate_n_plus_one_for_cart(cart_items, seller_id)

        return {'discount': 0.0, 'discounted_items': [], 'discounted_count': 0}
    
    def activate(self):
        """Активация акции."""
        self.status = 'active'
        db.session.commit()
    
    def pause(self):
        """Приостановка акции."""
        self.status = 'forming'
        db.session.commit()
    
    def complete(self):
        """Завершение акции."""
        self.status = 'completed'
        db.session.commit()
    
    def __repr__(self):
        return f'<Promotion {self.name}>'


class PromotionProduct(db.Model):
    """
    Связь товаров с акциями.
    """
    
    __tablename__ = 'promotion_products'

    promotion_id = db.Column(db.Integer, db.ForeignKey('promotions.id'), primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), primary_key=True)

    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    added_by_admin = db.Column(db.Boolean, default=False)

    # Индивидуальный процент скидки для конкретного товара внутри акции.
    # Используется только если у акции apply_same_discount=False.
    # NULL = использовать общий процент акции (Promotion.discount_percent).
    discount_percent = db.Column(db.Integer, nullable=True)

    # Статистика
    cart_adds = db.Column(db.Integer, default=0)
    sales_count = db.Column(db.Integer, default=0)
    sales_amount = db.Column(db.Float, default=0)
    
    # Связи
    promotion = db.relationship('Promotion', back_populates='products')
    product = db.relationship('Product', back_populates='promotion_items')
    
    def __repr__(self):
        return f'<PromotionProduct promotion={self.promotion_id} product={self.product_id}>'


class SellerPromotion(db.Model):
    """
    Подключение продавца к шаблонной акции.

    Используется для схем, где выбор товаров не нужен
    (например, second_with_discount — «каждый второй со скидкой»).
    Хранит override процента, если админ оставил шаблонный процент пустым.
    """
    __tablename__ = 'seller_promotions'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    promotion_id = db.Column(db.Integer, db.ForeignKey('promotions.id'), nullable=False, index=True)

    # Свой процент скидки. Если NULL — используется шаблонный discount_percent.
    override_discount_percent = db.Column(db.Integer, nullable=True)

    # Применять ли ко всем товарам акции одинаковую скидку (для scheme='discount').
    # True  — единый процент (override_discount_percent или шаблонный).
    # False — у каждого товара своя (хранится в PromotionProduct.discount_percent).
    # NULL  — наследовать значение из Promotion.apply_same_discount.
    apply_same_discount = db.Column(db.Boolean, nullable=True)

    # Активно ли подключение у продавца
    is_active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    promotion = db.relationship('Promotion', back_populates='seller_links')
    seller = db.relationship('Seller', backref=db.backref('promotion_links', lazy='dynamic'))

    __table_args__ = (
        # Один продавец — одна запись на конкретный шаблон
        db.UniqueConstraint('seller_id', 'promotion_id', name='uq_seller_promotion'),
    )

    def __repr__(self):
        return f'<SellerPromotion seller={self.seller_id} promotion={self.promotion_id} active={self.is_active}>'


class Bonus(db.Model):
    """
    Модель бонусных операций.

    Хранит полный журнал начислений/списаний. `seller_id` привязывает
    операцию к конкретному селлеру: в новой модели лояльности у каждого
    покупателя отдельный баланс баллов на каждого селлера (см.
    BuyerBonus в models/loyalty.py). Поле nullable — старые записи и
    штрафные начисления в `Order._apply_penalty_to_seller` остаются
    без привязки к селлеру.
    """

    __tablename__ = 'bonuses'

    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True, index=True)

    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(20), nullable=False)  # accrued, spent, reversed

    reason = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Связи
    buyer = db.relationship('Buyer', back_populates='bonus_transactions', foreign_keys=[buyer_id])
    order = db.relationship('Order', back_populates='bonus_transactions', foreign_keys=[order_id])
    seller = db.relationship('Seller', backref=db.backref('bonus_transactions', lazy='dynamic'))

    def __repr__(self):
        return (
            f'<Bonus buyer={self.buyer_id} seller={self.seller_id} '
            f'amount={self.amount} type={self.type}>'
        )


class Return(db.Model):
    """
    Модель возврата товара.
    """
    
    __tablename__ = 'returns'
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    order_item_id = db.Column(db.Integer, db.ForeignKey('order_items.id'), nullable=True)
    
    reason = db.Column(db.Text, nullable=False)
    photos = db.Column(db.JSON, nullable=True)  # Список путей к фото
    
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    admin_comment = db.Column(db.Text, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)
    
    # Связи
    order = db.relationship('Order', back_populates='returns', foreign_keys=[order_id])
    order_item = db.relationship('OrderItem', back_populates='returns', foreign_keys=[order_item_id])
    
    def approve(self, comment=None):
        """Одобрение возврата."""
        self.status = 'approved'
        self.processed_at = datetime.utcnow()
        if comment:
            self.admin_comment = comment
        
        # Обновление статуса товара в заказе
        if self.order_item:
            self.order_item.status = 'returned'
        
        db.session.commit()
    
    def reject(self, comment=None):
        """Отклонение возврата."""
        self.status = 'rejected'
        self.processed_at = datetime.utcnow()
        self.admin_comment = comment
        db.session.commit()
    
    def __repr__(self):
        return f'<Return order={self.order_id} status={self.status}>'


class Banner(db.Model):
    """
    Модель баннера для главной страницы.
    """
    
    __tablename__ = 'banners'
    
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=True)
    text = db.Column(db.Text, nullable=True)
    image_path = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(500), nullable=True)
    
    # Позиционирование
    position = db.Column(db.String(50), default='main')  # main, sidebar, etc.
    sort_order = db.Column(db.Integer, default=0)
    
    # Активность
    is_active = db.Column(db.Boolean, default=True)
    
    # Временные рамки
    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Banner {self.id}: {self.title}>'
