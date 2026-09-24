"""Exercise the stock-only POST without starting unrelated app services."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, abort, flash, redirect, request, url_for, jsonify
from jinja2 import Environment

repo = Path(__file__).resolve().parents[1]
tree = ast.parse((repo / 'app/blueprints/seller.py').read_text(encoding='utf-8'))
handler = next(node for node in tree.body
               if isinstance(node, ast.FunctionDef) and node.name == 'product_edit')
handler.decorator_list = []

class Seller:
    id = 7
    is_authenticated = True

session = Mock()
scope = dict(current_user=Seller(), Seller=Seller, Product=object,
             db=SimpleNamespace(session=session), abort=abort, flash=flash,
             redirect=redirect, request=request, url_for=url_for, jsonify=jsonify)
exec(compile(ast.Module(body=[handler], type_ignores=[]), 'seller.py', 'exec'), scope)
app = Flask(__name__)
app.secret_key = 'test-only'
app.add_url_rule('/products/<int:product_id>/edit', 'seller.product_edit',
                 scope['product_edit'], methods=['POST'])
stock_handler = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == 'product_update_stock')
stock_handler.decorator_list = []
exec(compile(ast.Module(body=[stock_handler], type_ignores=[]), 'seller.py', 'exec'), scope)
app.add_url_rule('/products/<int:product_id>/stock', 'seller.product_update_stock',
                 scope['product_update_stock'], methods=['POST'])
client = app.test_client()

for status in ['approved', 'on_moderation', 'rejected', 'draft']:
    for quantity in ['0', '25', '-1', '1.5', 'bad', '2147483648', None]:
        product = SimpleNamespace(id=1, seller_id=7, stock_quantity=9,
                                  status=status, name='Original', category_id=3)
        session.reset_mock()
        session.get.return_value = product
        data = dict(action='update_stock', name='Do not save', category_id='99')
        if quantity is not None:
            data['stock_quantity'] = quantity
        response = client.post('/products/1/edit', data=data)
        assert response.status_code == 302
        valid = quantity in ['0', '25']
        assert product.stock_quantity == (int(quantity) if valid else 9)
        assert product.status == status
        assert product.name == 'Original' and product.category_id == 3
        assert session.commit.call_count == int(valid)

session.reset_mock()
session.get.return_value = SimpleNamespace(id=2, seller_id=8)
assert client.post('/products/2/edit', data=dict(action='update_stock',
                   stock_quantity='5')).status_code == 404
session.commit.assert_not_called()
for status in ['approved', 'on_moderation', 'rejected']:
    for quantity in [0, 37, -1, 1.5, True, '5', None, 2147483648]:
        product = SimpleNamespace(id=1, seller_id=7, stock_quantity=9, status=status)
        session.reset_mock()
        session.get.return_value = product
        response = client.post('/products/1/stock', json={'stock_quantity': quantity})
        valid = type(quantity) is int and 0 <= quantity <= 2147483647
        assert response.status_code == (200 if valid else 400)
        assert product.stock_quantity == (quantity if valid else 9)
        assert product.status == status
        assert session.commit.call_count == int(valid)
        if valid:
            assert response.json == {'stock_quantity': quantity}
session.reset_mock()
session.get.return_value = SimpleNamespace(id=2, seller_id=8)
assert client.post('/products/2/stock', json={'stock_quantity': 5}).status_code == 404
session.commit.assert_not_called()
scope['current_user'].is_authenticated = False
assert client.post('/products/2/stock', json={'stock_quantity': 5}).status_code == 401
Environment().parse((repo / 'app/templates/seller/products.html').read_text(encoding='utf-8'))
Environment().parse((repo / 'app/templates/seller/product_form.html').read_text(encoding='utf-8'))
print('PASS: stock updates, unchanged fields/statuses, invalid quantities, ownership, template syntax')
