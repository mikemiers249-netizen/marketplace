"""Frozen v2 receipt lines. The working legacy receipt calculator stays intact."""
from decimal import Decimal
import json

from app import db
from app.models.users import Seller
from app.models.tariffs_v2_operations import TariffV2FiscalReceipt
from app.utils.tariffs_v2 import TariffError, money, ZERO, is_owner_seller


def capture_checkout_receipt(checkout):
    if not checkout.gateway_id or checkout.online_amount <= ZERO:
        return None
    previous = db.session.get(TariffV2FiscalReceipt, checkout.gateway_id)
    if previous:
        return previous
    items, mapping, fee_lines = [], {}, []
    platform_fee = ZERO
    for order in checkout.orders:
        owner = is_owner_seller(db.session.get(Seller, order.seller_id))
        for line in order.lines:
            snapshot = line.snapshot
            online = money(snapshot['online_amount'])
            if online == ZERO:
                continue
            if not owner:
                if online != money(snapshot['fee']) or money(snapshot['cod_amount']) != money(snapshot['goods_amount']):
                    raise TariffError('Стоимость товара стороннего селлера нельзя принимать как доход площадки.')
                platform_fee += online
                fee_lines.append(line.id)
                continue
            if online != money(snapshot['goods_amount']) or money(snapshot['fee']) != ZERO or money(snapshot['cod_amount']) != ZERO:
                raise TariffError('Проверьте расчет полной оплаты собственного магазина.')
            digital = bool(line.digital_book_id)
            quantity = line.quantity
            item = {'name': snapshot['product_name'],
                'quantity': quantity, 'sum': float(online), 'tax': 'none',
                'payment_method': 'full_payment' if digital else 'full_prepayment',
                'payment_object': 'intellectual_activity' if digital else 'commodity'}
            item['name'] = ' '.join(item['name'].split())[:128]
            cents = int(online * 100)
            if cents % quantity == 0:
                item['cost'] = (cents // quantity) / 100
            items.append(item)
            mapping[str(line.id)] = item
    if platform_fee > ZERO:
        item = {'name': 'Услуга доступа к площадке', 'quantity': 1,
            'sum': float(platform_fee), 'cost': float(platform_fee), 'tax': 'none',
            'payment_method': 'full_payment', 'payment_object': 'service'}
        items.append(item)
        for line_id in fee_lines:
            mapping[str(line_id)] = item
    if sum((Decimal(str(item['sum'])) for item in items), ZERO) != checkout.online_amount:
        raise TariffError('Номенклатура не совпадает с суммой тестового платежа.')
    row = TariffV2FiscalReceipt(gateway_id=checkout.gateway_id, amount=checkout.online_amount,
        receipt_json=json.dumps({'items': items}, ensure_ascii=False, separators=(',', ':')),
        line_items=mapping, is_test=checkout.is_test)
    db.session.add(row)
    return row


def refund_receipt_items(refund):
    checkout = refund.line.order.checkout
    receipt = db.session.get(TariffV2FiscalReceipt, checkout.gateway_id)
    if not receipt or receipt.is_test != checkout.is_test or receipt.amount != checkout.online_amount:
        raise TariffError('Не найден сохраненный состав исходного платежа.')
    original = receipt.line_items.get(str(refund.line_id))
    if not original or refund.amount > money(original['sum']):
        raise TariffError('Возврат не соответствует сохраненной позиции чека.')
    if original['payment_object'] == 'service':
        if refund.amount > refund.line.fee or money(refund.line.snapshot['online_amount']) != refund.line.fee:
            raise TariffError('Можно вернуть только сбор площадки, без стоимости товара селлера.')
        quantity, cost = 1, refund.amount
    else:
        cost = money(original.get('cost')) if original.get('cost') is not None else ZERO
        if not cost or refund.amount % cost != 0:
            raise TariffError('Сумму возврата нельзя представить сохраненной ценой единицы. Нужна фискальная сверка.')
        quantity = int(refund.amount / cost)
        if quantity > original['quantity']:
            raise TariffError('Количество возврата превышает исходный чек.')
    return [{'Name': original['name'], 'Quantity': quantity, 'Cost': str(cost),
        'Tax': original['tax'], 'PaymentMethod': original['payment_method'], 'PaymentObject': original['payment_object']}]
