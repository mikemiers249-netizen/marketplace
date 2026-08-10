"""
Модели тарифов магазина (покупаемых селлерами).

Архитектура (источник правды — /main_admin/info/tariffs):

  • TariffRow (из app/models/communications.py) — каталог тарифов, который
    админ создаёт и редактирует на странице «Информация → Тарифы». Если
    у строки заполнены числовые поля `price_amount` и `duration_days`,
    она становится покупаемой: появляется в «Магазине тарифов» у селлера
    и доступна для оплаты.

  • SellerTariffSubscription — факт подключения конкретного тарифа
    конкретным селлером. Содержит признак оплаты (`is_paid`), даты
    активации/истечения, статус (`active`/`paused`/`disabled`).
    Ссылается на `TariffRow` через `row_id` (раньше называлось plan_id).

  • TariffTransaction — расчёт (транзакция) между селлером и площадкой
    за покупку тарифа. Нужна для вкладки «Расчёты».

Все цены и сроки действия считаем по UTC `datetime.utcnow()`, как и
остальные модели в проекте.
"""

from datetime import datetime, timedelta
from app import db


class SellerTariffSubscription(db.Model):
    """
    Подписка селлера на конкретный тариф (TariffRow).

    Создаётся, когда селлер «покупает» тариф (в текущей итерации — только
    отображение; в будущем появится реальная оплата). Несколько подписок
    на один тариф допускаются (например, селлер продлил тариф после
    истечения — получит две записи).

    Статусы:
        active   — тариф действует, оплачен, всё ок;
        paused   — действие приостановлено (по запросу админа/селлера);
        disabled — отключён (аннулирован, дальше не действует).

    «Купленным» считается `is_paid = True`. «Действующим в данный момент»
    дополнительно требует `status = 'active'` и `expires_at > now()`.
    """

    __tablename__ = 'seller_tariff_subscriptions'

    STATUS_ACTIVE = 'active'
    STATUS_PAUSED = 'paused'
    STATUS_DISABLED = 'disabled'
    STATUSES = (
        (STATUS_ACTIVE, 'Активен'),
        (STATUS_PAUSED, 'Приостановлен'),
        (STATUS_DISABLED, 'Отключён'),
    )

    # Источник подписки:
    #   self        — селлер сам купил тариф в магазине (фикс-тариф kind='cards').
    #                Биллится по правилам строки (price_amount + duration_days).
    #   global_auto — подписка создана автоматически при включении глобального
    #                правила (kind in cards_turnover/card_sale/category_sale).
    #                Стоимость считается динамически (начисления, не покупка).
    #                Если у селлера появится 'self' подписка — global_auto
    #                не отменяется, но при начислении приоритет у 'self'.
    SOURCE_SELF = 'self'
    SOURCE_GLOBAL_AUTO = 'global_auto'
    SOURCES = (
        (SOURCE_SELF, 'Куплен селлером'),
        (SOURCE_GLOBAL_AUTO, 'Глобальное правило'),
    )

    id = db.Column(db.Integer, primary_key=True)

    seller_id = db.Column(
        db.Integer,
        db.ForeignKey('sellers.id'),
        nullable=False,
        index=True,
    )
    row_id = db.Column(
        db.Integer,
        db.ForeignKey('tariff_rows.id'),
        nullable=False,
        index=True,
    )

    source = db.Column(
        db.String(20),
        nullable=False,
        default=SOURCE_SELF,
        index=True,
    )

    is_paid = db.Column(db.Boolean, nullable=False, default=False, index=True)
    status = db.Column(
        db.String(20),
        nullable=False,
        default=STATUS_ACTIVE,
        index=True,
    )

    activated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)

    paused_at = db.Column(db.DateTime, nullable=True)
    disabled_at = db.Column(db.DateTime, nullable=True)

    # Грейс-период: после expires_at даётся ещё 5 дней, чтобы оплатить
    # тариф. В эти дни подписка считается «истекающей», но магазин ещё
    # работает. После grace_until — переходит в состояние 'locked'
    # (см. tariff_state). NULL = грейса нет (бессрочная подписка или
    # не активированная — глобальное правило).
    grace_until = db.Column(db.DateTime, nullable=True)
    # Последняя дата списания по правилу (для percent-тарифов с
    # billing_period='monthly'). Используется биллинг-движком.
    last_billed_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    # Связи
    seller = db.relationship(
        'Seller',
        backref=db.backref('tariff_subscriptions', lazy='dynamic'),
    )
    row = db.relationship(
        'TariffRow',
        backref=db.backref(
            'subscriptions',
            lazy='dynamic',
            # cascade='all' достаточно: при db.session.delete(row) ORM
            # сначала сгенерирует DELETE для всех sub'ов, потом для
            # самого row. Без 'delete-orphan' нет промежуточного
            # UPDATE SET row_id=NULL, который на nullable=False FK
            # падает с IntegrityError. passive_deletes убран, чтобы
            # SQLAlchemy не полагался на ON DELETE CASCADE в БД
            # (для SQLite PRAGMA foreign_keys по умолчанию выключена).
            cascade='all',
        ),
    )
    transactions = db.relationship(
        'TariffTransaction',
        back_populates='subscription',
        lazy='dynamic',
    )

    # Алиас для старого кода и шаблонов: раньше был self.plan, теперь — row,
    # но в шаблонах удобно писать sub.plan.name / sub.plan.price.
    @property
    def plan(self):
        return self.row

    @property
    def is_purchased(self) -> bool:
        """Оплачен ли тариф (для отображения в «Клиентах»)."""
        return bool(self.is_paid)

    @property
    def is_active_now(self) -> bool:
        """Оплачен, не приостановлен, не отключён, срок не истёк."""
        if not self.is_paid:
            return False
        if self.status != self.STATUS_ACTIVE:
            return False
        if self.expires_at is None or self.expires_at <= datetime.utcnow():
            return False
        return True

    @property
    def is_in_grace(self) -> bool:
        """Срок оплаты истёк, но грейс-период ещё не закончился.

        В эти дни подписка формально неактивна (is_active_now=False), но
        магазин продавца ещё работает в прежнем режиме. Селлер должен
        оплатить тариф до grace_until, иначе магазин блокируется.
        """
        if self.is_paid is False:
            return False
        if self.expires_at is None:
            return False
        now = datetime.utcnow()
        if self.expires_at > now:
            return False
        if self.grace_until is None or self.grace_until <= now:
            return False
        return True

    @property
    def is_locked(self) -> bool:
        """Подписка оплачена, но и обычный срок, и грейс истекли.

        В этом состоянии селлер теряет доступ к функционалу продаж/товаров,
        пока не оплатит (см. tariff_state='locked' и @require_active_tariff).
        """
        if not self.is_paid:
            return False
        if self.expires_at is None:
            return False
        now = datetime.utcnow()
        if self.expires_at > now:
            return False
        if self.grace_until is not None and self.grace_until > now:
            return False
        return True

    @property
    def days_to_grace_end(self) -> int:
        """Сколько целых дней осталось до конца грейс-периода. 0 если истёк."""
        if self.grace_until is None:
            return 0
        delta = self.grace_until - datetime.utcnow()
        if delta.total_seconds() <= 0:
            return 0
        return max(int(delta.total_seconds() // 86400), 0)

    @property
    def days_to_expire(self) -> int:
        """Сколько целых дней осталось до конца оплаченного периода. 0 если истёк."""
        if self.expires_at is None:
            return 0
        delta = self.expires_at - datetime.utcnow()
        if delta.total_seconds() <= 0:
            return 0
        return max(int(delta.total_seconds() // 86400), 0)

    def recompute_grace(self, grace_days: int = 5) -> None:
        """Пересчитать grace_until относительно expires_at.

        Вызывается при создании и продлении подписки. grace_days=5 — по
        бизнес-правилу (5 дней на оплату после истечения срока).
        """
        if self.expires_at is None:
            self.grace_until = None
            return
        self.grace_until = self.expires_at + timedelta(days=grace_days)

    @property
    def status_label(self) -> str:
        for code, label in self.STATUSES:
            if code == self.status:
                return label
        return self.status or ''

    def pause(self) -> None:
        """Приостановить действие тарифа."""
        if self.status == self.STATUS_DISABLED:
            return
        self.status = self.STATUS_PAUSED
        self.paused_at = datetime.utcnow()

    def resume(self) -> None:
        """Возобновить приостановленный тариф (только если не истёк и оплачен)."""
        if self.status != self.STATUS_PAUSED:
            return
        if not self.is_paid:
            return
        if self.expires_at is not None and self.expires_at <= datetime.utcnow():
            return
        self.status = self.STATUS_ACTIVE
        self.paused_at = None

    def disable(self) -> None:
        """Отключить тариф (аннулировать)."""
        self.status = self.STATUS_DISABLED
        self.disabled_at = datetime.utcnow()

    def __repr__(self) -> str:
        return (
            f'<SellerTariffSubscription id={self.id} seller={self.seller_id} '
            f'row={self.row_id} paid={self.is_paid} status={self.status!r}>'
        )


class TariffTransaction(db.Model):
    """
    Транзакция (расчёт) за покупку тарифа.

    Фиксируется в момент «оплаты». Используется во вкладке «Расчёты» —
    по сути журнал платежей селлеров за тарифы.
    """

    __tablename__ = 'tariff_transactions'

    id = db.Column(db.Integer, primary_key=True)

    seller_id = db.Column(
        db.Integer,
        db.ForeignKey('sellers.id'),
        nullable=False,
        index=True,
    )
    row_id = db.Column(
        db.Integer,
        db.ForeignKey('tariff_rows.id'),
        nullable=False,
        index=True,
    )
    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey('seller_tariff_subscriptions.id'),
        nullable=True,
        index=True,
    )

    amount = db.Column(db.Float, nullable=False, default=0.0)
    paid_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
    )

    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Связи
    seller = db.relationship(
        'Seller',
        backref=db.backref('tariff_transactions', lazy='dynamic'),
    )
    row = db.relationship(
        'TariffRow',
        backref=db.backref(
            'transactions',
            lazy='dynamic',
            cascade='all, delete-orphan',
            passive_deletes=True,
        ),
    )
    # Алиас для совместимости с шаблонами, которые пишут tx.plan.name
    @property
    def plan(self):
        return self.row
    subscription = db.relationship(
        'SellerTariffSubscription',
        back_populates='transactions',
    )

    def __repr__(self) -> str:
        return (
            f'<TariffTransaction id={self.id} seller={self.seller_id} '
            f'row={self.row_id} amount={self.amount}>'
        )


__all__ = ['SellerTariffSubscription', 'TariffTransaction']
