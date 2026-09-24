from decimal import Decimal, InvalidOperation


def parse_minimum_order_amount(raw):
    try:
        amount = Decimal((raw or '').strip().replace(',', '.') or '0')
        if not amount.is_finite() or not 0 <= amount <= Decimal('9999999999.99'):
            raise ValueError
        if amount != amount.quantize(Decimal('0.01')):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError('Укажите неотрицательную сумму в рублях, не более двух знаков после запятой.')
    return amount or None


def minimum_order_error(cart_items):
    """Check each store's discounted merchandise subtotal before order discounts."""
    from app.utils.helpers import compute_best_discount_for_item
    groups = {}
    sellers = {}
    for item in cart_items:
        if not item.product or not item.product.seller:
            continue
        seller = item.product.seller
        sellers[seller.id] = seller
        groups.setdefault(seller.id, []).append(item)
    errors = []
    for seller_id, items in groups.items():
        seller = sellers[seller_id]
        minimum = seller.minimum_order_amount or Decimal('0')
        if not minimum:
            continue
        subtotal = Decimal('0')
        for item in items:
            discount = compute_best_discount_for_item(item, items)
            unit_price = round(round(float(item.product.price), 2) - discount / max(1, item.quantity), 2)
            subtotal += Decimal(str(unit_price)) * item.quantity
        subtotal = subtotal.quantize(Decimal('0.01'))
        if subtotal < minimum:
            errors.append(
                f'Минимальная сумма заказа в магазине «{seller.store_name}» — {minimum:.2f} ₽. '
                f'Добавьте товаров ещё на {minimum - subtotal:.2f} ₽ (без доставки).'
            )
    return ' '.join(errors) or None
