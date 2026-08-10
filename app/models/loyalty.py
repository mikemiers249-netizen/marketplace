"""
Модели программы лояльности.

Логика:
  • Админ создаёт шаблоны курсов начисления (LoyaltyRate) — например,
    «1 балл за 1 ₽», «2 балла за 1 ₽», «5 баллов за 1 ₽». Курс хранится
    как `points_per_ruble` — сколько баллов начисляется за каждый потраченный
    рубль.
  • Селлер подключает к своему магазину ровно один курс (SellerLoyalty)
    и задаёт `payback_percent` — какой процент стоимости товара покупатель
    может оплатить бонусными баллами. Селлер может в любой момент
    переключить курс или изменить процент.
  • При получении заказа покупателем (Order.status = 'received') покупателю
    начисляются бонусные баллы от селлера этого заказа. Баланс per-seller
    хранится в BuyerBonus (одна запись на пару buyer_id + seller_id).
  • Все операции (accrued / spent / reversed) пишутся в журнал Bonus
    (расширенный колонкой seller_id), чтобы была полная история.
  • Глобальный тумблер «программа лояльности для продавцов» хранится в
    Settings['loyalty_enabled'].
"""

from datetime import datetime
from app import db


class LoyaltyRate(db.Model):
    """
    Шаблон курса начисления, доступный продавцам.

    Курс задаётся в виде `points_per_ruble` — сколько баллов начисляется
    покупателю за каждый рубль стоимости товара (после скидок). Например,
    points_per_ruble=2 → за 100 ₽ покупателю капает 200 баллов.

    Админ делает курс видимым/невидимым для селлеров через `is_active`,
    не удаляя его (это сохраняет историю начислений по старым заказам).
    """

    __tablename__ = 'loyalty_rates'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    points_per_ruble = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    sort_order = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    seller_links = db.relationship(
        'SellerLoyalty',
        back_populates='rate',
        lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<LoyaltyRate {self.id}: {self.title} ({self.points_per_ruble} баллов/₽)>'


class SellerLoyalty(db.Model):
    """
    Подключение селлера к курсу начисления и его собственный % списания.

    У одного селлера может быть не более одной активной записи (UNIQUE по
    seller_id). Если `rate_id` = NULL — селлер пока не подключился.

    `payback_percent` — максимальный процент стоимости товаров, который
    покупатель может оплатить бонусными баллами данного селлера. Задаётся
    самим селлером (per-seller). 0..100.
    """

    __tablename__ = 'seller_loyalties'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(
        db.Integer,
        db.ForeignKey('sellers.id'),
        nullable=False,
        unique=True,
        index=True,
    )
    rate_id = db.Column(
        db.Integer,
        db.ForeignKey('loyalty_rates.id'),
        nullable=True,
    )
    # 0..100. Сколько процентов стоимости покупатель может оплатить баллами.
    payback_percent = db.Column(db.Integer, default=50, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    seller = db.relationship('Seller', backref=db.backref('loyalty', uselist=False))
    rate = db.relationship('LoyaltyRate', back_populates='seller_links')

    @property
    def is_configured(self):
        """Селлер выбрал курс и задал % списания — и программа включена у него."""
        return self.rate_id is not None and self.payback_percent is not None

    def __repr__(self):
        return (
            f'<SellerLoyalty seller={self.seller_id} '
            f'rate={self.rate_id} payback={self.payback_percent}%>'
        )


class BuyerBonus(db.Model):
    """
    Кеш-баланс бонусных баллов покупателя у конкретного селлера.

    Одна запись на пару (buyer_id, seller_id). Обновляется атомарно при
    начислении/списании; основной источник правды — журнал Bonus
    (bonus_transactions).

    `balance` может быть отрицательным только в результате гонок, в
    нормальном режиме >= 0. Проверка и списание — в BonusService.
    """

    __tablename__ = 'buyer_bonuses'

    id = db.Column(db.Integer, primary_key=True)
    buyer_id = db.Column(
        db.Integer,
        db.ForeignKey('buyers.id'),
        nullable=False,
        index=True,
    )
    seller_id = db.Column(
        db.Integer,
        db.ForeignKey('sellers.id'),
        nullable=False,
        index=True,
    )
    balance = db.Column(db.Float, default=0.0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    buyer = db.relationship('Buyer', backref=db.backref('bonus_balances', lazy='dynamic'))
    seller = db.relationship('Seller', backref=db.backref('buyer_bonuses', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('buyer_id', 'seller_id', name='uq_buyer_seller_bonus'),
    )

    def __repr__(self):
        return f'<BuyerBonus buyer={self.buyer_id} seller={self.seller_id} balance={self.balance}>'


__all__ = ['LoyaltyRate', 'SellerLoyalty', 'BuyerBonus']
