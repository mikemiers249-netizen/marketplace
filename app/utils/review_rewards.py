from datetime import datetime
from decimal import Decimal, InvalidOperation
import secrets
from app import db
from app.models.review_rewards import ReviewRewardProgram, ReviewGift, RewardReviewReceipt, ReviewReward
from app.models.communications import Settings
from app.models.users import Buyer
from app.models.orders import OrderItem, Bonus


def enabled():
    value = Settings.get('review_rewards_enabled', False)
    return value is True or str(value).lower() in ('true', '1', 'on')


def validate_rules(seller_id, kind, mode, raw):
    if kind not in ('bonus', 'discount', 'gift') or mode not in ('each', 'grades'):
        raise ValueError('Выберите вид награды и способ начисления.')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError('Заполните от 1 до 100 правил начисления.')
    if mode == 'each' and len(raw) != 1:
        raise ValueError('Для каждого отзыва задайте одно правило.')
    rules, seen = [], set()
    gifts = {g.id for g in ReviewGift.query.filter_by(seller_id=seller_id, active=True).all()}
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('Некорректное правило.')
        try:
            threshold = int(str(row.get('threshold', '')))
        except (TypeError, ValueError):
            raise ValueError('Номер отзыва должен быть целым положительным числом.')
        if not 1 <= threshold <= 1000000 or threshold in seen:
            raise ValueError('Номера отзывов должны быть положительными и не повторяться.')
        seen.add(threshold)
        rule = {'threshold': threshold}
        if kind == 'gift':
            choice = str(row.get('gift', ''))
            if choice == 'random':
                if not gifts:
                    raise ValueError('Сначала добавьте подарок в галерею.')
            elif not choice.isdigit() or int(choice) not in gifts:
                raise ValueError('Выберите доступный подарок из своей галереи.')
            rule['gift'] = choice
        else:
            try:
                amount = Decimal(str(row.get('amount', '')).replace(',', '.'))
                cap = Decimal('100') if kind == 'discount' else Decimal('9999999.99')
                if not amount.is_finite() or not 0 < amount <= cap or amount != amount.quantize(Decimal('.01')):
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValueError('Укажите положительную сумму с точностью до копейки или скидку от 0,01 до 100%.')
            rule['amount'] = str(amount)
        rules.append(rule)
    return sorted(rules, key=lambda r: r['threshold'])


def award_review(review):
    """Caller commits approval and award together. Buyer lock serializes all reward mutations."""
    if not enabled() or review.status != 'approved' or not review.order_id or not review.product:
        return
    order = review.order
    if not order or order.buyer_id != review.buyer_id or order.seller_id != review.product.seller_id:
        return
    if order.status not in ('delivered', 'received'):
        return
    item = OrderItem.query.filter_by(order_id=order.id, product_id=review.product_id).first()
    if not item or item.price_at_order <= 0:
        return
    program = db.session.get(ReviewRewardProgram, order.seller_id)
    if not program or not program.enabled:
        return
    Buyer.query.filter_by(id=review.buyer_id).with_for_update().one()
    if RewardReviewReceipt.query.filter_by(buyer_id=review.buyer_id, order_id=order.id,
                                         product_id=review.product_id).first():
        return
    ordinal = RewardReviewReceipt.query.filter_by(buyer_id=review.buyer_id, seller_id=order.seller_id).count() + 1
    receipt = RewardReviewReceipt(review_id=review.id, buyer_id=review.buyer_id,
                                  seller_id=order.seller_id, order_id=order.id,
                                  product_id=review.product_id, ordinal=ordinal)
    db.session.add(receipt)
    db.session.flush()
    rule = program.rules[0] if program.mode == 'each' else next(
        (r for r in program.rules if r['threshold'] == ordinal), None)
    if not rule:
        return
    reward = ReviewReward(receipt_id=receipt.id, buyer_id=review.buyer_id, seller_id=order.seller_id,
                          kind=program.kind, amount=Decimal(rule.get('amount', '0')))
    if program.kind == 'gift':
        gifts = ReviewGift.query.filter_by(seller_id=order.seller_id, active=True).all()
        gift = secrets.choice(gifts) if rule['gift'] == 'random' and gifts else next(
            (g for g in gifts if str(g.id) == rule['gift']), None)
        if gift is None:
            raise ValueError('Подарок программы недоступен. Обновите программу перед одобрением отзыва.')
        reward.gift = gift
        reward.gift_name = gift.product.name
        reward.gift_image = gift.product.main_photo.path if gift.product.main_photo else None
    elif program.kind == 'bonus':
        from app.utils.loyalty import _add_balance
        _add_balance(review.buyer_id, order.seller_id, float(reward.amount))
        # No order_id: purchase cashback reversal must not reverse review awards.
        db.session.add(Bonus(buyer_id=review.buyer_id, seller_id=order.seller_id,
                             amount=float(reward.amount), type='accrued',
                             reason=f'За одобренный отзыв №{review.id} (отзыв {ordinal})'))
        reward.status = 'credited'
    db.session.add(reward)
    db.session.flush()


def available_rewards(buyer_id, seller_id=None):
    query = ReviewReward.query.filter_by(buyer_id=buyer_id, status='available')
    if seller_id is not None:
        query = query.filter_by(seller_id=seller_id)
    return query.order_by(ReviewReward.id).all()


def next_rewards(buyer_id, seller_id):
    chosen = {}
    for reward in available_rewards(buyer_id, seller_id):
        chosen.setdefault(reward.kind, reward)
    return chosen


def merchandise_total(items, percent=0):
    from app.utils.helpers import compute_item_discount_breakdown
    total = Decimal('0')
    for item in items:
        breakdown = compute_item_discount_breakdown(item, items)
        unit = Decimal(str(round(round(float(item.product.price), 2) -
                                breakdown['total_discount'] / max(1, item.quantity), 2)))
        total += (unit * (1 - Decimal(str(percent)) / 100)).quantize(Decimal('.01')) * item.quantity
    return float(total)


def cart_discount(buyer_id, cart_items):
    grouped = {}
    for item in cart_items:
        if item.product:
            grouped.setdefault(item.product.seller_id, []).append(item)
    result = []
    for sid, items in grouped.items():
        reward = next_rewards(buyer_id, sid).get('discount')
        if reward:
            amount = round(merchandise_total(items) - merchandise_total(items, reward.amount), 2)
            result.append({'promotion_id': None, 'name': f'За отзыв: {reward.seller.store_name} −{reward.amount:g}%',
                           'discount': amount, 'seller_id': sid})
    return result


def apply_order_rewards(order):
    """Consume one oldest gift and discount per store, in the checkout transaction."""
    Buyer.query.filter_by(id=order.buyer_id).with_for_update().one()
    if ReviewReward.query.filter_by(redeemed_order_id=order.id, status='redeemed').first():
        return
    chosen = next_rewards(order.buyer_id, order.seller_id)
    discount = chosen.get('discount')
    if discount:
        items = order.items.all()
        before = sum(Decimal(str(i.price_at_order)) * i.quantity for i in items)
        for item in items:
            old = Decimal(str(item.price_at_order))
            new = (old * (1 - discount.amount / 100)).quantize(Decimal('.01'))
            item.price_at_order = float(new)
            # Existing order summaries group applied discounts as promotion discounts.
            if item.promo_discount:
                item.promo_discount = round(float(item.promo_discount) + float((old - new) * item.quantity), 2)
        after = sum(Decimal(str(i.price_at_order)) * i.quantity for i in items)
        order.total_price = float(after)
        discount.redeemed_amount = before - after
    gift = chosen.get('gift')
    if gift:
        db.session.add(OrderItem(order_id=order.id, product_id=gift.gift.product_id,
                                 quantity=1, price_at_order=0, original_price=0, promo_discount=0))
    for reward in chosen.values():
        reward.status = 'redeemed'
        reward.redeemed_order_id = order.id
        reward.redeemed_at = datetime.utcnow()
    # Do not allow previously selected bonus/promo deductions to exceed the new total.
    order.promo_discount = min(float(order.promo_discount or 0), float(order.total_price))
    from app.utils.loyalty import get_seller_payback_percent
    maximum_bonus = round(min(max(0, float(order.total_price) - float(order.promo_discount or 0)),
                              float(order.total_price) * get_seller_payback_percent(order.seller_id) / 100), 2)
    if float(order.bonus_used or 0) > maximum_bonus:
        from app.utils.loyalty import _add_balance
        refund = round(float(order.bonus_used) - maximum_bonus, 2)
        _add_balance(order.buyer_id, order.seller_id, refund)
        db.session.add(Bonus(buyer_id=order.buyer_id, seller_id=order.seller_id, order_id=order.id,
                             amount=refund, type='reversed', reason='Коррекция списания после скидки за отзыв'))
        order.bonus_used = maximum_bonus
    db.session.flush()


def restore_order_rewards(order):
    Buyer.query.filter_by(id=order.buyer_id).with_for_update().one()
    for reward in ReviewReward.query.filter_by(redeemed_order_id=order.id, status='redeemed').all():
        reward.status = 'available'
        reward.redeemed_order_id = None
        reward.redeemed_at = None
        reward.redeemed_amount = None


def has_review_bonus(buyer_id=None, seller_id=None):
    query = ReviewReward.query.filter_by(kind='bonus')
    if buyer_id is not None:
        query = query.filter_by(buyer_id=buyer_id)
    if seller_id is not None:
        query = query.filter_by(seller_id=seller_id)
    return query.first() is not None
