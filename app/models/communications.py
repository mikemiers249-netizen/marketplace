"""
Модели коммуникаций: Сообщения, Отзывы, Рассылки.
"""

from datetime import datetime
from app import db


class Message(db.Model):
    """
    Модель личного сообщения.
    Поддерживает переписку между покупателями, продавцами и админами.
    """
    
    __tablename__ = 'messages'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Отправитель
    sender_type = db.Column(db.String(20), nullable=False)  # buyer, seller, admin
    sender_id = db.Column(db.Integer, nullable=False)
    
    # Получатель
    receiver_type = db.Column(db.String(20), nullable=False)  # buyer, seller, admin
    receiver_id = db.Column(db.Integer, nullable=False)
    
    # Содержимое
    text = db.Column(db.Text, nullable=True)
    image_path = db.Column(db.String(255), nullable=True)
    file_path = db.Column(db.String(255), nullable=True)  # Для PDF файлов
    
    # Метаданные
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    is_read = db.Column(db.Boolean, default=False)
    is_system = db.Column(db.Boolean, default=False)  # Системное сообщение
    
    # Для группировки в чаты
    conversation_type = db.Column(db.String(20), nullable=True)  # support, order, product
    conversation_id = db.Column(db.Integer, nullable=True)
    
    # Примечание: для доступа к отправителю/получателю используйте свойства sender_model и receiver_model
    # SQLAlchemy не поддерживает динамические foreign_keys для полиморфных связей
    
    @property
    def sender_model(self):
        """Получение модели отправителя."""
        from app.models.users import Buyer, Seller, Admin
        
        if self.sender_type == 'buyer':
            return Buyer.query.get(self.sender_id)
        elif self.sender_type == 'seller':
            return Seller.query.get(self.sender_id)
        elif self.sender_type == 'admin':
            return Admin.query.get(self.sender_id)
        return None
    
    @property
    def receiver_model(self):
        """Получение модели получателя."""
        from app.models.users import Buyer, Seller, Admin
        
        if self.receiver_type == 'buyer':
            return Buyer.query.get(self.receiver_id)
        elif self.receiver_type == 'seller':
            return Seller.query.get(self.receiver_id)
        elif self.receiver_type == 'admin':
            return Admin.query.get(self.receiver_id)
        return None
    
    @property
    def conversation_key(self):
        """Ключ для группировки в чат."""
        return f"{self.sender_type}:{self.sender_id}-{self.receiver_type}:{self.receiver_id}"
    
    @property
    def content(self):
        """Псевдоним для text для совместимости с шаблонами."""
        return self.text
    
    @classmethod
    def get_conversation(cls, user_type, user_id, other_type, other_id):
        """Получение истории переписки."""
        return cls.query.filter(
            ((cls.sender_type == user_type) & (cls.sender_id == user_id) &
             (cls.receiver_type == other_type) & (cls.receiver_id == other_id)) |
            ((cls.sender_type == other_type) & (cls.sender_id == other_id) &
             (cls.receiver_type == user_type) & (cls.receiver_id == user_id))
        ).order_by(cls.timestamp.asc()).all()
    
    @classmethod
    def get_unread_count(cls, receiver_type, receiver_id):
        """Получение количества непрочитанных сообщений."""
        return cls.query.filter(
            cls.receiver_type == receiver_type,
            cls.receiver_id == receiver_id,
            cls.is_read == False
        ).count()
    
    def mark_as_read(self):
        """Отметка как прочитанное."""
        if not self.is_read:
            self.is_read = True
            db.session.commit()
    
    def __repr__(self):
        return f'<Message from={self.sender_type}:{self.sender_id} to={self.receiver_type}:{self.receiver_id}>'


class Review(db.Model):
    """
    Модель отзыва о товаре.
    """
    
    __tablename__ = 'reviews'
    
    id = db.Column(db.Integer, primary_key=True)
    
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False, index=True)
    buyer_id = db.Column(db.Integer, db.ForeignKey('buyers.id'), nullable=False, index=True)
    # Привязка к конкретному заказу. Nullable — для обратной совместимости
    # со старыми отзывами и со старой формой отправки (где order_id не передавался).
    # Если указан — действует правило «один отзыв на (покупатель, товар, заказ)»,
    # то есть на повторную покупку того же товара в новом заказе можно оставить новый отзыв.
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True, index=True)
    
    rating = db.Column(db.Float, nullable=False)  # 1-5 с одним знаком после запятой
    text = db.Column(db.Text, nullable=True)
    
    # Фотографии отзыва
    photos = db.Column(db.JSON, nullable=True)  # Список путей к фото
    
    # Статус
    is_verified = db.Column(db.Boolean, default=False)  # Проверенный отзыв (после покупки)
    is_approved = db.Column(db.Boolean, default=False)  # Одобрено админом (legacy-поле)
    # status: pending = на модерации, approved = одобрен, rejected = отклонён
    status = db.Column(db.String(20), default='pending', index=True)
    moderated_at = db.Column(db.DateTime, nullable=True)
    
    # Ответ продавца
    seller_response = db.Column(db.Text, nullable=True)
    seller_response_approved = db.Column(db.Boolean, default=False)
    seller_response_at = db.Column(db.DateTime, nullable=True)
    
    # Временные метки
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Связи
    product = db.relationship('Product', back_populates='reviews', foreign_keys=[product_id])
    buyer = db.relationship('Buyer', back_populates='reviews', foreign_keys=[buyer_id])
    order = db.relationship('Order', backref=db.backref('reviews', lazy='dynamic'),
                            foreign_keys=[order_id])
    
    @property
    def stars_display(self):
        """Отображение звёздочек."""
        return '★' * int(self.rating) + '☆' * (5 - int(self.rating))
    
    @property
    def has_seller_response(self):
        """Есть ли ответ продавца."""
        return bool(self.seller_response)
    
    def add_seller_response(self, response_text):
        """Добавление ответа продавца."""
        self.seller_response = response_text
        self.seller_response_at = datetime.utcnow()
        db.session.commit()
    
    def approve_response(self):
        """Одобрение ответа продавца."""
        self.seller_response_approved = True
        db.session.commit()
    
    def reject_response(self):
        """Отклонение ответа продавца."""
        self.seller_response = None
        self.seller_response_at = None
        db.session.commit()
    
    @property
    def is_pending(self):
        """Находится ли отзыв на модерации."""
        return self.status == 'pending'
    
    def approve(self):
        """Одобрить отзыв (админ)."""
        self.status = 'approved'
        self.is_approved = True
        self.moderated_at = datetime.utcnow()
        db.session.commit()
    
    def reject(self):
        """Отклонить отзыв (админ) — удаляет запись."""
        db.session.delete(self)
        db.session.commit()
    
    def __repr__(self):
        return f'<Review product={self.product_id} buyer={self.buyer_id} rating={self.rating}>'


class Mailing(db.Model):
    """
    Модель рассылки.
    """
    
    __tablename__ = 'mailings'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Тип рассылки
    target_type = db.Column(db.String(20), nullable=False)  # buyers, sellers, all
    
    # Содержимое
    subject = db.Column(db.String(200), nullable=False)
    text = db.Column(db.Text, nullable=False)
    image_path = db.Column(db.String(255), nullable=True)
    
    # Статус
    status = db.Column(db.String(20), default='draft')  # draft, sending, sent, cancelled
    
    # Статистика
    recipients_count = db.Column(db.Integer, default=0)
    sent_count = db.Column(db.Integer, default=0)
    read_count = db.Column(db.Integer, default=0)
    
    # Планирование
    scheduled_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, nullable=True)  # admin_id
    
    def send(self):
        """Отправка рассылки."""
        from app.models.users import Buyer, Seller
        
        self.status = 'sending'
        db.session.commit()
        
        # Получение списка получателей
        if self.target_type in ['buyers', 'all']:
            recipients = Buyer.query.filter_by(is_active=True).all()
        if self.target_type in ['sellers', 'all']:
            recipients = (recipients if 'recipients' in dir() else []) + \
                         Seller.query.filter_by(is_active=True).all()
        
        self.recipients_count = len(recipients)
        
        # Создание сообщений
        for recipient in recipients:
            msg = Message(
                sender_type='admin',
                sender_id=0,
                receiver_type='buyer' if isinstance(recipient, Buyer) else 'seller',
                receiver_id=recipient.id,
                text=self.text,
                image_path=self.image_path,
                is_system=True,
                conversation_type='mailing',
                conversation_id=self.id
            )
            db.session.add(msg)
        
        self.status = 'sent'
        self.sent_at = datetime.utcnow()
        self.sent_count = self.recipients_count
        db.session.commit()
    
    def __repr__(self):
        return f'<Mailing {self.id}: {self.subject}>'


class Settings(db.Model):
    """
    Модель настроек системы (ключ-значение).
    """
    
    __tablename__ = 'settings'
    
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)  # JSON для гибкости
    value_type = db.Column(db.String(20), default='text')  # text, json, int, float
    
    description = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, nullable=True)
    
    @classmethod
    def get(cls, key, default=None):
        """Получение значения настройки."""
        setting = cls.query.get(key)
        if not setting:
            return default
        
        if setting.value_type == 'json':
            import json
            return json.loads(setting.value)
        elif setting.value_type == 'int':
            return int(setting.value)
        elif setting.value_type == 'float':
            return float(setting.value)
        return setting.value
    
    @classmethod
    def set(cls, key, value, value_type='text', description=None):
        """Установка значения настройки."""
        import json
        
        if value_type == 'json':
            value_str = json.dumps(value)
        else:
            value_str = str(value)
        
        setting = cls.query.get(key)
        if setting:
            setting.value = value_str
            setting.value_type = value_type
        else:
            setting = cls(
                key=key,
                value=value_str,
                value_type=value_type,
                description=description
            )
            db.session.add(setting)
        
        db.session.commit()
        return setting
    
    def __repr__(self):
        return f'<Settings {self.key}>'


class InfoPost(db.Model):
    """
    Новость/пост раздела «Информация» в дашборде админа/продавца.
    Показывается на всю ширину контента; может содержать картинку или видео.

    audience:
        'all'    — виден и админу, и продавцу;
        'admin'  — только админу;
        'seller' — только продавцу.

    media_type: 'image' | 'video' | None.
    """

    __tablename__ = 'info_posts'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=True)  # HTML/Markdown — рендерится как есть, не sanitize (внутренний инструмент)

    media_type = db.Column(db.String(20), nullable=True)  # 'image' / 'video' / None
    media_url = db.Column(db.String(500), nullable=True)   # ссылка или путь в /static/uploads/

    tag = db.Column(db.String(50), nullable=True, index=True)  # категория/метка для фильтрации

    audience = db.Column(db.String(20), default='all', nullable=False, index=True)
    is_published = db.Column(db.Boolean, default=True, nullable=False, index=True)
    sort_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @staticmethod
    def tag_slug(tag):
        """Транслитерация тега → URL-safe slug для фильтра (?tag=...)."""
        if not tag:
            return ''
        import re
        # Явная таблица транслитерации (кириллица → латиница).
        # Словарём, чтобы рассинхрон длин был невозможен.
        cyr_to_lat = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd',
            'е': 'e', 'ё': 'yo', 'ж': 'zh', 'з': 'z', 'и': 'i',
            'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
            'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
            'у': 'u', 'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch',
            'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '',
            'э': 'e', 'ю': 'yu', 'я': 'ya',
        }
        table = {ord(c): l for c, l in cyr_to_lat.items()}
        s = tag.strip().lower().translate(table)
        s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
        return s[:50]

    def __repr__(self):
        return f'<InfoPost {self.id} audience={self.audience} tag={self.tag!r} title={self.title!r}>'


class EduMaterial(db.Model):
    """
    Учебный материал в разделе «Информация → Обучение».

    Отдельная сущность (не InfoPost), потому что у материалов своя семантика:
    длинный HTML-контент «с версткой», обложка-фон, тег для фильтра, аудитория.

    Поля:
        title       — название урока/материала (видно на плитке)
        body        — HTML-контент самого материала (длинная вёрстка)
        cover_url   — ссылка на обложку (необязательна; если нет — дефолтный фон)
        tag         — категория/метка (для фильтра, как у InfoPost)
        audience    — 'all' | 'admin' | 'seller'
        is_published — скрыть/показать (без удаления)
        created_at  — дата создания
    """

    __tablename__ = 'edu_materials'

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=True)  # HTML — рендерится как есть (внутренний инструмент)
    cover_url = db.Column(db.String(500), nullable=True)  # обложка-фон для плитки

    tag = db.Column(db.String(50), nullable=True, index=True)  # для фильтра

    audience = db.Column(db.String(20), default='all', nullable=False, index=True)
    is_published = db.Column(db.Boolean, default=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    @staticmethod
    def tag_slug(tag):
        """Транслитерация тега → URL-safe slug (?tag=...). Делегируем InfoPost,
        чтобы slugи были единообразные во всём разделе «Информация»."""
        return InfoPost.tag_slug(tag)

    @staticmethod
    def all_tags():
        """Список всех непустых тегов (для фильтра)."""
        return [
            t for (t,) in (
                db.session.query(EduMaterial.tag)
                .filter(EduMaterial.tag.isnot(None))
                .filter(EduMaterial.tag != '')
                .distinct()
                .order_by(EduMaterial.tag)
                .all()
            )
        ]

    def __repr__(self):
        return f'<EduMaterial {self.id} audience={self.audience} tag={self.tag!r} title={self.title!r}>'


class RoadmapEvent(db.Model):
    """
    Событие «Траектории развития проекта» в дашбордах админа и продавца.

    Один RoadMapEvent — одна точка на календаре + одна карточка в ленте событий
    (под календарём, отсортированной по прямой дате). При клике на карточку в
    ленте снизу раскрывается полное описание события.

    audience:
        'all'    — виден и админу, и продавцу;
        'admin'  — только админу;
        'seller' — только продавцу.
    """

    __tablename__ = 'roadmap_events'

    id = db.Column(db.Integer, primary_key=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)  # HTML — рендерится как есть (внутренний инструмент)

    # Дата события (календарный день, без времени). Для сортировки в ленте и
    # позиционирования маркера на FullCalendar.
    event_date = db.Column(db.Date, nullable=False, index=True)

    # Категория/тип события — для цвета маркера и группировки в ленте.
    # Один из: 'release' | 'update' | 'event' | 'plan' (см. .category_* ниже).
    category = db.Column(db.String(20), default='event', nullable=False, index=True)

    audience = db.Column(db.String(20), default='all', nullable=False, index=True)
    is_published = db.Column(db.Boolean, default=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Человекочитаемые метки категорий. На фронте используется и для цвета, и
    # для подписи в ленте. Порядок фиксирован — для детерминированной палитры.
    CATEGORIES = (
        ('release',  'Релиз',         '#10b981'),  # зелёный
        ('update',   'Обновление',    '#3b82f6'),  # синий
        ('event',    'Событие',       '#f59e0b'),  # янтарный
        ('plan',     'В плане',       '#8b5cf6'),  # фиолетовый
    )

    @staticmethod
    def category_label(code):
        """Подпись категории на русском. Если код неизвестен — вернёт сам код."""
        for k, label, _color in RoadmapEvent.CATEGORIES:
            if k == code:
                return label
        return code or ''

    @staticmethod
    def category_color(code):
        """Цвет категории (HEX). Если код неизвестен — серый fallback."""
        for k, _label, color in RoadmapEvent.CATEGORIES:
            if k == code:
                return color
        return '#6b7280'

    def to_fullcalendar(self):
        """Сериализация события для FullCalendar (JSON-эндпоинт)."""
        # id отдаём СТРОКОЙ — FullCalendar v6 при загрузке через URL
        # часто теряет числовой id (возвращает undefined в eventClick),
        # и тогда на фронте не работает сопоставление по id. Строка
        # гарантированно проходит через JSON и приходит обратно.
        return {
            'id': str(self.id) if self.id is not None else None,
            'title': self.title,
            'start': self.event_date.isoformat() if self.event_date else None,
            'allDay': True,
            'backgroundColor': self.category_color(self.category),
            'borderColor': self.category_color(self.category),
            'textColor': '#ffffff',
            'extendedProps': {
                'id': self.id,  # числовой id для удобного selectEventById(id)
                'category': self.category,
                'categoryLabel': self.category_label(self.category),
                'audience': self.audience,
            },
        }

    def __repr__(self):
        return f'<RoadmapEvent {self.id} date={self.event_date} category={self.category} title={self.title!r}>'


class TariffBlock(db.Model):
    """
    Блок (заголовок раздела) в таблице тарифов на странице
    «Информация → Тарифы». Внутри одного блока — несколько строк (TariffRow).

    Смысл блока — логически сгруппировать услуги. Например:
      • «Подключение и обслуживание»
      • «Продвижение»
      • «Дополнительно»
    """

    __tablename__ = 'tariff_blocks'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    # Раздел сайта, к которому относится блок:
    # 'sellers' — продавцам, 'buyers' — покупателям, 'all' — всем.
    section = db.Column(db.String(20), default='sellers', nullable=False, index=True)
    sort_order = db.Column(db.Integer, default=0, nullable=False, index=True)
    is_published = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    # Связь со строками. order_by по sort_order + id — порядок создания как tie-breaker.
    rows = db.relationship(
        'TariffRow',
        backref='block',
        cascade='all, delete-orphan',
        order_by='TariffRow.sort_order, TariffRow.id',
        passive_deletes=True,
    )

    SECTIONS = (
        ('sellers', 'Продавцам'),
        ('buyers',  'Покупателям'),
        ('all',     'Всем'),
    )

    @staticmethod
    def section_label(code):
        for k, label in TariffBlock.SECTIONS:
            if k == code:
                return label
        return code or ''

    def __repr__(self):
        return f'<TariffBlock {self.id} section={self.section!r} title={self.title!r}>'


class TariffRow(db.Model):
    """
    Одна строка таблицы тарифов внутри блока (TariffBlock).

    Поля:
        name         — наименование услуги («Размещение товара»)
        description  — описание услуги (кратко, что входит)
        price        — стоимость (строка, чтобы не терять «от 500 ₽» / «договорная»)
        period       — периодичность оплаты («мес.», «разово», «за 1 заказ» и т.п.)
        sort_order   — порядок внутри блока
        is_published — скрыть/показать без удаления

    Новые поля (биллинг-движок):
        kind              — тип правила. От него зависит набор остальных полей
                            и поведение начислений:
                              'cards'          — фикс-тариф «Карточки» (старый режим,
                                                  покупается селлером в магазине);
                              'cards_turnover' — % от оборота всех карточек селлера
                                                  за 30 дней (ежемесячно);
                              'card_sale'      — % с каждой продажи по карточкам
                                                  (списывается per_sale);
                              'category_sale'  — % с каждой продажи в указанной
                                                  категории (subject_category_id).
        is_active         — глобальный тумблер. При True правило применяется ко
                            всем селлерам, у которых нет своей активной подписки.
                            Если у селлера уже есть оплаченная подписка
                            (SellerTariffSubscription с source='self' и
                            is_active_now=True) — новое правило вступит в силу
                            после её истечения.
        percent_rate      — ставка в процентах (0–100). Для kind='cards'
                            игнорируется (там price_amount).
        subject_category_id — категория для kind='category_sale'.
        billing_period    — 'monthly' (раз в 30 дней) | 'per_sale' (по факту
                            сделки). None для kind='cards' (там duration_days).
    """

    __tablename__ = 'tariff_rows'

    # Типы правил
    KIND_CARDS = 'cards'
    KIND_CARDS_TURNOVER = 'cards_turnover'
    KIND_CARD_SALE = 'card_sale'
    KIND_CATEGORY_SALE = 'category_sale'
    KINDS = (
        (KIND_CARDS,          'Карточки (фикс-тариф)'),
        (KIND_CARDS_TURNOVER, 'Процент от оборота карточек'),
        (KIND_CARD_SALE,      'Процент с продажи по карточкам'),
        (KIND_CATEGORY_SALE,  'Процент с продаж по категории'),
    )

    # Периодичность начисления
    BILLING_MONTHLY = 'monthly'
    BILLING_PER_SALE = 'per_sale'
    BILLINGS = (
        (BILLING_MONTHLY, 'Ежемесячно'),
        (BILLING_PER_SALE, 'Сразу (по факту сделки)'),
    )

    id = db.Column(db.Integer, primary_key=True)
    block_id = db.Column(
        db.Integer,
        db.ForeignKey('tariff_blocks.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.String(100), nullable=True)  # строка: «500 ₽», «от 1000», «договорная»
    period = db.Column(db.String(50), nullable=True)   # «мес.», «разово», «за 1 заказ», ...

    sort_order = db.Column(db.Integer, default=0, nullable=False, index=True)
    is_published = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    # Числовые «дублёры» строковых полей price/period — нужны для
    # автоматической покупки тарифа селлером (для SellerTariffSubscription
    # требуются price: float и duration_days: int). Если оба заполнены
    # и строка опубликована — она появляется в «Магазине тарифов» у продавца.
    price_amount = db.Column(db.Float, nullable=True)     # цена в рублях
    duration_days = db.Column(db.Integer, nullable=True)  # срок действия в днях

    # === Новые поля: биллинг-движок ===
    kind = db.Column(
        db.String(20),
        nullable=False,
        default=KIND_CARDS,
        index=True,
    )
    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=False,  # по умолчанию ВЫКЛЮЧЕН — админ сам решает, когда включить
        index=True,
    )
    percent_rate = db.Column(db.Float, nullable=True)  # ставка в %
    subject_category_id = db.Column(
        db.Integer,
        db.ForeignKey('categories.id'),
        nullable=True,
        index=True,
    )
    billing_period = db.Column(
        db.String(20),
        nullable=True,
        index=True,
    )
    # Длительность периода для глобального правила (дни). Используется при
    # «Активации» — селлер нажимает «Зафиксировать» и получает подписку
    # на period_days (по умолчанию 30). Для kind='cards' берётся
    # duration_days, для глобальных — period_days (или 30 как фолбэк).
    period_days = db.Column(db.Integer, nullable=True)

    # Связь на категорию (для kind='category_sale').
    subject_category = db.relationship('Category', foreign_keys=[subject_category_id])

    @property
    def kind_label(self) -> str:
        for code, label in self.KINDS:
            if code == self.kind:
                return label
        return self.kind or ''

    @property
    def billing_period_label(self) -> str:
        for code, label in self.BILLINGS:
            if code == self.billing_period:
                return label
        return self.billing_period or ''

    @property
    def is_purchasable(self) -> bool:
        """Можно ли селлеру «купить» этот тариф в «Магазине тарифов».

        Только для фикс-тарифов (kind='cards'). Глобальные процент-правила
        селлер не покупает — они применяются автоматически.
        """
        if self.kind != self.KIND_CARDS:
            return False
        if not self.is_published:
            return False
        if self.price_amount is None or self.price_amount <= 0:
            return False
        if self.duration_days is None or self.duration_days <= 0:
            return False
        return True

    @property
    def is_global_rule(self) -> bool:
        """Является ли строка глобальным правилом (применяется ко всем селлерам)."""
        return self.kind in (
            self.KIND_CARDS_TURNOVER,
            self.KIND_CARD_SALE,
            self.KIND_CATEGORY_SALE,
        )

    @property
    def effective_period_days(self) -> int:
        """Длительность периода в днях, которую использовать при «активации».

        • kind='cards'         → берём duration_days (если заполнен),
                                  иначе 30.
        • глобальные правила   → берём period_days (если заполнен),
                                  иначе 30.

        Используется при создании SellerTariffSubscription с
        expires_at = now + effective_period_days и
        grace_until = expires_at + 5 дней.
        """
        if self.kind == self.KIND_CARDS:
            return int(self.duration_days) if self.duration_days else 30
        if self.period_days and self.period_days > 0:
            return int(self.period_days)
        return 30

    def compute_billed_amount(self, seller, period_start, period_end) -> float:
        """Сумма к списанию за период [period_start, period_end] для селлера.

        Возвращает 0 для фикс-тарифов (там своя цена price_amount) и для
        правил с billing_period='per_sale' (там списание происходит по
        факту сделки, не раз в период).

        Для kind in {cards_turnover, category_sale} с billing_period='monthly':
            amount = percent_rate × оборот_за_период / 100

        Оборот = сумма total_price доставленных заказов селлера за период.
        Для category_sale дополнительно фильтруем по subject_category_id
        (через OrderItem.product.category_id).
        """
        if not self.is_global_rule:
            return 0.0
        if self.percent_rate is None or self.percent_rate <= 0:
            return 0.0
        if self.billing_period == self.BILLING_PER_SALE:
            # per_sale — списание идёт по факту каждой продажи,
            # здесь его не агрегируем (см. seller.orders hooks).
            return 0.0

        from sqlalchemy import func
        from app.models.orders import Order, OrderItem

        q = (
            db.session.query(func.coalesce(func.sum(Order.total_price), 0.0))
            .filter(Order.seller_id == seller.id)
            .filter(Order.status == 'delivered')
            .filter(Order.created_at >= period_start)
            .filter(Order.created_at < period_end)
        )
        if self.kind == self.KIND_CATEGORY_SALE and self.subject_category_id:
            # Оборот только по товарам указанной категории.
            q = q.join(OrderItem, OrderItem.order_id == Order.id)
            q = q.filter(OrderItem.product.has(category_id=self.subject_category_id))
        turnover = float(q.scalar() or 0.0)
        return round(turnover * float(self.percent_rate) / 100.0, 2)

    def validate(self) -> str | None:
        """Вернёт текст ошибки или None, если строка валидна.

        Используется в admin.create/edit перед сохранением.
        """
        if not self.name:
            return 'Наименование услуги обязательно.'

        if self.kind == self.KIND_CARDS:
            # Старая логика: нужны цена и срок, если планируется продажа.
            # Но price/duration могут быть пустыми (тогда строка только
            # информационная, не показывается в магазине).
            if self.price_amount is not None and self.price_amount < 0:
                return 'Цена для покупки должна быть ≥ 0.'
            if self.duration_days is not None and self.duration_days < 0:
                return 'Срок должен быть ≥ 0.'
            return None

        # Глобальные правила: всегда нужен percent_rate > 0
        if self.percent_rate is None or self.percent_rate <= 0:
            return 'Укажите величину процента (> 0).'
        if self.percent_rate > 100:
            return 'Ставка не может превышать 100%.'
        # billing_period обязателен
        if self.billing_period not in (self.BILLING_MONTHLY, self.BILLING_PER_SALE):
            return 'Выберите периодичность начисления.'
        # Для category_sale — обязательна категория
        if self.kind == self.KIND_CATEGORY_SALE and not self.subject_category_id:
            return 'Для правила по категории выберите категорию.'
        return None

    def __repr__(self):
        return (
            f'<TariffRow {self.id} kind={self.kind!r} '
            f'active={self.is_active} name={self.name!r}>'
        )
