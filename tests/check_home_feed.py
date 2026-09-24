import ast
from pathlib import Path
repo = Path(__file__).resolve().parents[1]
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from jinja2 import Environment
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
db = SQLAlchemy(app)
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer)
    seller_id = db.Column(db.Integer)
    status = db.Column(db.String)
    stock_quantity = db.Column(db.Integer)

tree = ast.parse((repo / 'app/blueprints/main.py').read_text(encoding='utf-8'))
function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_homepage_product_batch')
scope = dict(db=db, Product=Product, func=func, _visible_seller_ids_with_limit=lambda: {1})
exec(compile(ast.Module(body=[function], type_ignores=[]), 'main.py', 'exec'), scope)
batch = scope[function.name]
with app.app_context():
    db.create_all()
    for total in [0, 1, 4, 5, 6, 9, 10, 14, 15, 19, 20, 21, 24, 25, 63, 101]:
        db.session.query(Product).delete()
        for i in range(total):
            db.session.add(Product(id=i+1, category_id=i % 7, seller_id=1, status='approved', stock_quantity=1))
        db.session.add_all([
            Product(id=1001, category_id=1, seller_id=2, status='approved', stock_quantity=1),
            Product(id=1002, category_id=1, seller_id=1, status='draft', stock_quantity=1),
            Product(id=1003, category_id=1, seller_id=1, status='approved', stock_quantity=0),
        ])
        db.session.commit()
        seen = set()
        while True:
            remaining_categories = {p.category_id for p in Product.query.filter(Product.id <= total, ~Product.id.in_(seen)).all()}
            products, more = batch(seen)
            assert len(products) == min(5, total - len(seen))
            ids = {p.id for p in products}
            assert not ids & seen and all(i <= total for i in ids)
            if products:
                assert len({p.category_id for p in products}) == min(len(products), len(remaining_categories))
            seen |= ids
            assert more == (total - len(seen) > 0)
            if not more:
                break
        assert len(seen) == total
    scope['_visible_seller_ids_with_limit'] = lambda: set()
    assert batch() == ([], False)
for file in ['main/index.html', 'components/home_product_batch.html']:
    Environment().parse((repo / 'app/templates' / file).read_text(encoding='utf-8'))
print('PASS: batch sizes, unique products, category diversity, 0 through 101 products, partial final batches, visibility filters, empty sellers and template syntax')
