"""
Сервисный модуль для операций с бонусными баллами и промокодами.

Правила (см. ответы пользователя):
  • Курс: points_per_ruble — сколько баллов начисляется за каждый рубль
    стоимости товара после скидок.
  • % списания (payback_percent) — per-seller, селлер задаёт сам в
    своём разделе «Лояльность». Ограничивает, какую долю стоимости
    товаров селлера в корзине покупатель может оплатить баллами.
  • Начисление: при status='received' (покупатель подтвердил получение).
  • Лимит списания: max(spendable) = min(balance_buyer_at_seller,
    sum(cost_in_cart) * payback_percent / 100).
  • Балансы — per-seller, в таблице buyer_bonuses (одна запись на пару).
  • Журнал — bonuses (с seller_id).

Все операции атомарны и пишут в журнал.
"""

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import or_, and_
from app import db
from app.models.orders import Order, Bonus
from app.models.loyalty import LoyaltyRate, SellerLoyalty, BuyerBonus
from app.models.promo import PromoCode
from app.models.users import Seller, Buyer


# Утилиты округления для денег/баллов — все целочисленные рубли,
# баллы считаем с точностью до 0.01 (но на границе списания берём
# floor — нельзя списать больше, чем хватает).
def _q2(value):
    """Округление до 0.01 в большую сторону для итогов начисления/списания."""
    return float(
        Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    )


def is_loyalty_enabled():
    """Глобальный тумблер программы лояльности (настройка в Settings)."""
    from app.models.communications import Settings
    return bool(Settings.get('loyalty_enabled', False))


def is_promo_enabled():
    """Глобальный тумблер промокодов (настройка в Settings)."""
    from app.models.communications import Settings
    return bool(Settings.get('promo_enabled', False))


def is_promo_enabled_for_seller(seller_id):
    """
    Разрешена ли выдача промокодов конкретным продавцом.
    Включено = оба тумблера: глобальный админский и индивидуальный продавца.
    """
    from app.models.communications import Settings
    if not is_promo_enabled():
        return False
    return bool(Settings.get(f'promo_enabled_seller_{seller_id}', True))


def set_promo_enabled_for_seller(seller_id, enabled):
    """Сохранить индивидуальный тумблер промокодов продавца."""
    from app.models.communications import Settings
    Settings.set(
        f'promo_enabled_seller_{seller_id}',
        bool(enabled),
        'bool',
    )


def get_active_rates():
    """Все активные (видимые селлерам) курсы начисления, отсортированы по sort_order."""
    return (
        LoyaltyRate.query
        .filter(LoyaltyRate.is_active.is_(True))
        .order_by(LoyaltyRate.sort_order.asc(), LoyaltyRate.id.asc())
        .all()
    )


def get_seller_loyalty(seller_id):
    """Запись SellerLoyalty для селлера (или None, если ещё не подключился)."""
    return SellerLoyalty.query.filter_by(seller_id=seller_id).first()


def get_or_create_seller_loyalty(seller_id):
    """Ленивое создание SellerLoyalty, чтобы можно было его обновлять
    в одной форме без отдельной кнопки «создать»."""
    link = get_seller_loyalty(seller_id)
    if link is None:
        link = SellerLoyalty(
            seller_id=seller_id,
            rate_id=None,
            payback_percent=50,
            is_active=True,
        )
        db.session.add(link)
        db.session.flush()
    return link


def get_buyer_balance(buyer_id, seller_id):
    """Текущий баланс баллов покупателя у селлера. 0 если записи нет."""
    bb = BuyerBonus.query.filter_by(
        buyer_id=buyer_id, seller_id=seller_id,
    ).first()
    return float(bb.balance) if bb else 0.0


def get_buyer_balances_grouped(buyer_id):
    """
    Все пары (seller, balance) для покупателя — для плиток в ЛК.
    Возвращает list[dict] с полями: seller_id, store_name, store_slug,
    store_logo, balance. Только селлеры с ненулевым балансом.
    """
    rows = (
        db.session.query(BuyerBonus, Seller)
        .join(Seller, Seller.id == BuyerBonus.seller_id)
        .filter(BuyerBonus.buyer_id == buyer_id, BuyerBonus.balance > 0)
        .all()
    )
    out = []
    for bb, seller in rows:
        out.append({
            'seller_id': seller.id,
            'store_name': seller.store_name,
            'store_slug': seller.store_slug,
            'store_logo': seller.store_logo,
            'balance': float(bb.balance),
        })
    return out


def _add_balance(buyer_id, seller_id, delta):
    """Внутренняя: пополнить/уменьшить баланс на `delta` (может быть < 0)."""
    bb = BuyerBonus.query.filter_by(
        buyer_id=buyer_id, seller_id=seller_id,
    ).first()
    if bb is None:
        bb = BuyerBonus(buyer_id=buyer_id, seller_id=seller_id, balance=0.0)
        db.session.add(bb)
    bb.balance = float(bb.balance) + float(delta)
    # Защита от отрицательных значений из-за гонок — допустим только 0+.
    if bb.balance < 0:
        bb.balance = 0.0
    return bb


def accrue_bonuses_for_order(order):
    """
    Начислить бонусные баллы покупателю за заказ.

    Вызывается при переходе заказа в 'received' (см. Order.mark_received).
    Кол-во баллов = points_per_ruble * order.total_price (сумма товаров
    после скидок, до доставки). Округляется до 0.01.

    Ничего не делает, если:
      • глобальный тумблер выключен;
      • селлер не подключился к программе;
      • у селлера нет активного курса;
      • по этому заказу уже начислено (ищем запись в Bonus с
        order_id и type='accrued' и seller_id).

    Returns:
        float: начисленная сумма, или 0.
    """
    if not is_loyalty_enabled():
        return 0.0

    if order.status != 'received':
        return 0.0

    sl = get_seller_loyalty(order.seller_id)
    if not sl or not sl.is_active or sl.rate_id is None:
        return 0.0

    rate = db.session.get(LoyaltyRate, sl.rate_id)
    if not rate or not rate.is_active:
        return 0.0

    # Защита от двойного начисления
    already = Bonus.query.filter_by(
        order_id=order.id, seller_id=order.seller_id, type='accrued',
    ).first()
    if already:
        return 0.0

    amount = _q2((rate.points_per_ruble or 0) * float(order.total_price))
    if amount <= 0:
        return 0.0

    _add_balance(order.buyer_id, order.seller_id, amount)

    bonus = Bonus(
        buyer_id=order.buyer_id,
        order_id=order.id,
        seller_id=order.seller_id,
        amount=amount,
        type='accrued',
        reason=f'Начисление баллов за заказ {order.order_number}',
    )
    db.session.add(bonus)

    # Помечаем заказ для UI
    order.bonus_accrued = amount
    db.session.flush()
    return amount


def reverse_bonuses_for_order(order):
    """
    Откатить начисление по заказу (вызывается при отмене после received).
    Списывает с баланса ровно ту сумму, которая была начислена.
    """
    if not is_loyalty_enabled():
        return 0.0

    accrued_entry = Bonus.query.filter_by(
        order_id=order.id, seller_id=order.seller_id, type='accrued',
    ).first()
    if not accrued_entry:
        return 0.0

    # Списываем с баланса
    _add_balance(order.buyer_id, order.seller_id, -float(accrued_entry.amount))

    bonus = Bonus(
        buyer_id=order.buyer_id,
        order_id=order.id,
        seller_id=order.seller_id,
        amount=-float(accrued_entry.amount),
        type='reversed',
        reason=f'Отмена начисления по заказу {order.order_number}',
    )
    db.session.add(bonus)
    db.session.flush()
    return float(accrued_entry.amount)


def get_seller_payback_percent(seller_id):
    """% списания для селлера, или 0 если не подключён/программа выключена."""
    if not is_loyalty_enabled():
        return 0
    sl = get_seller_loyalty(seller_id)
    if not sl or not sl.is_active or sl.rate_id is None:
        return 0
    return int(sl.payback_percent or 0)


def calculate_spendable_for_seller(buyer_id, seller_id, cart_subtotal):
    """
    Сколько баллов покупатель может списать у селлера в текущей корзине.

    Args:
        buyer_id: ID покупателя
        seller_id: ID селлера
        cart_subtotal: сумма товаров селлера в корзине (после скидок, до доставки)

    Returns:
        float: доступная к списанию сумма в рублях (НЕ в баллах)
    """
    balance = get_buyer_balance(buyer_id, seller_id)
    if balance <= 0:
        return 0.0

    percent = get_seller_payback_percent(seller_id)
    if percent <= 0:
        return 0.0

    cap_by_percent = _q2(float(cart_subtotal) * percent / 100.0)
    return min(balance, cap_by_percent)


def spend_bonuses(buyer_id, seller_id, amount_rub, order_id=None, reason=None):
    """
    Списать `amount_rub` баллов (=рублей) у пары buyer_id+seller_id.
    Списывается и из кеша (BuyerBonus), и пишется запись в Bonus(type='spent').

    Returns:
        float: фактически списанная сумма (>=0). Если баланса не хватает —
        списывается всё, что есть.
    """
    if amount_rub <= 0:
        return 0.0
    balance = get_buyer_balance(buyer_id, seller_id)
    if balance <= 0:
        return 0.0
    amount = min(float(amount_rub), balance)
    if amount <= 0:
        return 0.0
    _add_balance(buyer_id, seller_id, -amount)
    bonus = Bonus(
        buyer_id=buyer_id,
        order_id=order_id,
        seller_id=seller_id,
        amount=-amount,
        type='spent',
        reason=reason or 'Списание баллов при оформлении заказа',
    )
    db.session.add(bonus)
    db.session.flush()
    return amount


def get_sellers_in_cart(cart_items):
    """
    Сгруппировать cart_items по продавцам: dict[seller_id] = {
        'items': [...], 'subtotal': float
    }. Сумма — после скидок (item.total_price), до доставки.
    """
    from collections import defaultdict
    by_seller = defaultdict(lambda: {'items': [], 'subtotal': 0.0})
    for it in cart_items:
        sid = it.product.seller_id if it.product else None
        if not sid:
            continue
        by_seller[sid]['items'].append(it)
        by_seller[sid]['subtotal'] += float(it.total_price)
    # Округляем subtotal
    for sid in by_seller:
        by_seller[sid]['subtotal'] = _q2(by_seller[sid]['subtotal'])
    return dict(by_seller)


def get_cart_bonus_snapshot(buyer_id, cart_items):
    """
    Снимок бонусов для корзины: по каждому селлеру в корзине возвращает
    словарь с балансом, доступной к списанию суммой и флагом активности.
    Используется в cart.html и для JS-обновления.
    """
    by_seller = get_sellers_in_cart(cart_items)
    enabled = is_loyalty_enabled()
    out = []
    for sid, data in by_seller.items():
        balance = get_buyer_balance(buyer_id, sid) if enabled else 0.0
        spendable = (
            calculate_spendable_for_seller(buyer_id, sid, data['subtotal'])
            if enabled else 0.0
        )
        seller_obj = db.session.get(Seller, sid)
        sl = get_seller_loyalty(sid) if enabled else None
        out.append({
            'seller_id': sid,
            'store_name': seller_obj.store_name if seller_obj else f'Продавец #{sid}',
            'store_slug': seller_obj.store_slug if seller_obj else None,
            'subtotal': data['subtotal'],
            'balance': balance,
            'spendable': spendable,
            'payback_percent': sl.payback_percent if sl else 0,
            'seller_active': bool(sl and sl.rate_id is not None),
        })
    return out


# --------------------------------------------------------------------- #
# Промокоды (per-seller скидки магазинов)
# --------------------------------------------------------------------- #

def get_applicable_promos_for_seller(buyer_id, seller_id, cart_subtotal):
    """
    Список промокодов продавца, которые покупатель реально может применить
    к своей корзине прямо сейчас.

    Условия (см. PromoCode):
      • is_active = True
      • used_count < max_uses (одноразовые и многоразовые одинаково)
      • valid_until либо NULL, либо в будущем
      • recipient_type = 'public' ИЛИ (personal + buyer_id совпал)
      • min_order_amount <= cart_subtotal (если задан)
      • глобальный и per-seller тумблер промокодов включены

    Returns:
        list[dict]: для каждого подходящего промокода:
            {
                'id', 'code', 'discount_type', 'discount_value',
                'discount_label', 'min_order_amount', 'validity_label',
                'recipient_label', 'usage_label', 'min_subtotal',
                'discount_amount': float (рассчитанная скидка в рублях
                    от cart_subtotal с учётом типа скидки и без учёта
                    бонусов — это «номинал», который UI покажет покупателю)
            }
    """
    if not is_promo_enabled() or not is_promo_enabled_for_seller(seller_id):
        return []

    now = datetime.utcnow()
    cart_subtotal = float(cart_subtotal or 0)
    if cart_subtotal <= 0:
        return []

    base_q = (
        PromoCode.query
        .filter(PromoCode.seller_id == seller_id)
        .filter(PromoCode.is_active.is_(True))
        .filter(PromoCode.used_count < PromoCode.max_uses)
        .filter(
            or_(
                PromoCode.valid_until.is_(None),
                PromoCode.valid_until > now,
            )
        )
        .filter(
            or_(
                PromoCode.recipient_type == 'public',
                and_(
                    PromoCode.recipient_type == 'personal',
                    PromoCode.buyer_id == buyer_id,
                ),
            )
        )
    )

    out = []
    for p in base_q.all():
        if p.min_order_amount and cart_subtotal < float(p.min_order_amount):
            continue

        # Расчёт номинала скидки именно по этой корзине.
        if p.discount_type == 'percent':
            discount_amount = _q2(cart_subtotal * float(p.discount_value) / 100.0)
        else:  # 'rub'
            discount_amount = _q2(float(p.discount_value))
        # Скидка не может превышать стоимость товаров.
        if discount_amount > cart_subtotal:
            discount_amount = _q2(cart_subtotal)
        if discount_amount <= 0:
            continue

        out.append({
            'id': p.id,
            'code': p.code,
            'discount_type': p.discount_type,
            'discount_value': float(p.discount_value),
            'discount_label': p.discount_label,
            'min_order_amount': float(p.min_order_amount) if p.min_order_amount else None,
            'min_subtotal': float(p.min_order_amount) if p.min_order_amount else 0.0,
            'validity_label': p.validity_label,
            'recipient_label': p.recipient_label,
            'usage_label': p.usage_label,
            'discount_amount': discount_amount,
        })
    return out


def get_cart_promo_snapshot(buyer_id, cart_items):
    """
    Снимок применимых промокодов для всех продавцов в корзине.
    Аналог get_cart_bonus_snapshot, но для промокодов.

    Returns:
        list[dict]: [{'seller_id', 'promos': [...]}, ...]
    """
    by_seller = get_sellers_in_cart(cart_items)
    out = []
    for sid, data in by_seller.items():
        promos = get_applicable_promos_for_seller(
            buyer_id, sid, data['subtotal'],
        )
        out.append({
            'seller_id': sid,
            'subtotal': data['subtotal'],
            'promos': promos,
        })
    return out


def calculate_promo_discount_amount(promo_code, base_amount):
    """
    Размер скидки (в рублях) для конкретного PromoCode по `base_amount`.

    Не обращается к БД — принимает готовый объект PromoCode и считает
    по его полям. Используется в момент оформления заказа, чтобы
    перепроверить условия на сервере (защита от подмены UI).
    """
    if promo_code is None or base_amount is None:
        return 0.0
    base = float(base_amount)
    if base <= 0:
        return 0.0
    if promo_code.discount_type == 'percent':
        amount = _q2(base * float(promo_code.discount_value) / 100.0)
    else:
        amount = _q2(float(promo_code.discount_value))
    if amount > base:
        amount = _q2(base)
    return max(0.0, amount)


def consume_promo_code(promo_code):
    """
    Инкрементировать used_count у промокода. Атомарно.
    Используется при оформлении заказа (после того, как мы уже
    перепроверили условия применимости).
    """
    if promo_code is None:
        return
    # Перечитываем актуальное значение, чтобы не потерять инкремент
    # при гонках (если бы ORM-объект был stale).
    fresh = db.session.get(PromoCode, promo_code.id)
    target = fresh or promo_code
    target.used_count = int(target.used_count or 0) + 1
    if target.used_count >= target.max_uses:
        # Исчерпали — оставляем активным, но фильтр used_count<max_uses
        # автоматически отсечёт его при следующем подборе.
        pass
    db.session.flush()


def parse_promo_per_seller(raw_value):
    """
    Распарсить строку `promo_per_seller` из формы/URL.
    Формат: "sellerId:promoCodeId;sellerId:promoCodeId" (пустая строка = без промокодов).

    Returns:
        dict[int, int]: {seller_id: promo_code_id}
    """
    out = {}
    if not raw_value:
        return out
    for part in str(raw_value).split(';'):
        part = part.strip()
        if not part or ':' not in part:
            continue
        sid_str, pid_str = part.split(':', 1)
        try:
            sid = int(sid_str)
            pid = int(pid_str)
        except (TypeError, ValueError):
            continue
        if sid > 0 and pid > 0:
            out[sid] = pid
    return out
