"""
Промокоды (скидки для покупателей).

Каждый промокод принадлежит одному продавцу (`seller_id`) и задаёт:
  - размер скидки (фиксированная сумма в рублях ИЛИ процент от чека);
  - минимальную сумму заказа, от которой промокод действует
    (если None — работает для любого заказа);
  - получателя:
      * `recipient_type = 'public'`   — без конкретного получателя, код может
        применить любой, у кого он есть;
      * `recipient_type = 'personal'` — выдаётся конкретному покупателю
        (`buyer_id` обязателен);
  - многоразовость:
      * `usage_type = 'single'`   — одноразовый, получатель может применить
        ровно один раз;
      * `usage_type = 'multiple'` — многоразовый, `max_uses` раз суммарно.
  - срок действия:
      * `validity_type = 'forever'` — бессрочный (`valid_until` и
        `valid_days` игнорируются);
      * `validity_type = 'days'`    — действует указанное число дней с
        момента создания (`valid_until` = created_at + valid_days);
      * `validity_type = 'until'`   — действует до конкретной даты
        (`valid_until` обязателен).

`used_count` инкрементируется при погашении. Промокод считается
действительным, если:
  - `is_active = True`;
  - `used_count < max_uses` (для single — 1, для multiple — max_uses);
  - `valid_until` либо NULL, либо в будущем.
"""

from datetime import datetime, timedelta
from app import db


class PromoCode(db.Model):
    __tablename__ = 'promo_codes'

    id = db.Column(db.Integer, primary_key=True)

    # Сам код — уникален в рамках одного продавца.
    code = db.Column(db.String(32), nullable=False, index=True)

    # Владелец-продавец.
    seller_id = db.Column(
        db.Integer,
        db.ForeignKey('sellers.id'),
        nullable=False,
        index=True,
    )

    # Скидка.
    discount_type = db.Column(db.String(16), nullable=False)  # 'rub' | 'percent'
    discount_value = db.Column(db.Float, nullable=False)

    # Минимальная сумма заказа для применения. NULL = любая.
    min_order_amount = db.Column(db.Float, nullable=True)

    # Получатель.
    recipient_type = db.Column(db.String(16), nullable=False)  # 'public' | 'personal'
    buyer_id = db.Column(
        db.Integer,
        db.ForeignKey('buyers.id'),
        nullable=True,
        index=True,
    )

    # Многоразовость.
    usage_type = db.Column(db.String(16), nullable=False)  # 'single' | 'multiple'
    max_uses = db.Column(db.Integer, nullable=False, default=1)
    used_count = db.Column(db.Integer, nullable=False, default=0)

    # Срок действия.
    validity_type = db.Column(db.String(16), nullable=False)  # 'forever' | 'days' | 'until'
    valid_days = db.Column(db.Integer, nullable=True)
    valid_until = db.Column(db.DateTime, nullable=True)

    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    # Связи.
    seller = db.relationship('Seller', backref=db.backref('promo_codes', lazy='dynamic'))
    buyer = db.relationship('Buyer', backref=db.backref('promo_codes', lazy='dynamic'))

    __table_args__ = (
        # Код уникален в рамках одного продавца. У разных продавцов
        # может быть одинаковый код (например, "SALE10").
        db.UniqueConstraint('seller_id', 'code', name='uq_promo_seller_code'),
    )

    # ------------------------------------------------------------------ #
    # Статус «действителен ли сейчас»
    # ------------------------------------------------------------------ #
    @property
    def is_valid_now(self) -> bool:
        """Промокод рабочий: активен, лимит не исчерпан, срок не истёк."""
        if not self.is_active:
            return False
        if self.used_count >= self.max_uses:
            return False
        if self.valid_until is not None and self.valid_until < datetime.utcnow():
            return False
        return True

    @property
    def status_label(self) -> str:
        """Короткая подпись статуса для UI."""
        if not self.is_active:
            return 'Отключён'
        if self.used_count >= self.max_uses:
            return 'Лимит исчерпан'
        if self.valid_until is not None and self.valid_until < datetime.utcnow():
            return 'Истёк'
        return 'Активен'

    @property
    def recipient_label(self) -> str:
        """'Для всех' или логин покупателя."""
        if self.recipient_type == 'public':
            return 'Для всех'
        if self.buyer is not None:
            # У модели Buyer поле называется `login` (из BaseUser),
            # `username` отсутствует — обращаемся через getattr,
            # чтобы и для других моделей не падать.
            return (
                getattr(self.buyer, 'login', None)
                or getattr(self.buyer, 'username', None)
                or f'#{self.buyer_id}'
            )
        return f'Покупатель #{self.buyer_id}'

    @property
    def discount_label(self) -> str:
        if self.discount_type == 'percent':
            return f'{self.discount_value:g}%'
        return f'{self.discount_value:g} ₽'

    @property
    def validity_label(self) -> str:
        if self.validity_type == 'forever':
            return 'Бессрочный'
        if self.valid_until is None:
            return '—'
        return self.valid_until.strftime('%d.%m.%Y')

    @property
    def usage_label(self) -> str:
        """Одноразовый / многоразовый с прогрессом."""
        if self.usage_type == 'single':
            return 'Одноразовый'
        return f'{self.used_count}/{self.max_uses}'

    def __repr__(self) -> str:
        return f'<PromoCode {self.code} seller={self.seller_id} {self.discount_label}>'
