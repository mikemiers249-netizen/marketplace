"""Read-only isolated catalog; no production checkout links or cart writes."""
from flask import Blueprint, abort, current_app, render_template, request

from app.models.products import Product
from app.models.tariffs_v2_commerce import TariffV2Listing
from app.utils.tariffs_v2 import quote_line, utc
from app.utils.tariffs_v2_commerce import is_visible

bp = Blueprint('tariffs_v2_commerce', __name__, url_prefix='/tariffs-v2')


@bp.before_request
def sandbox_only():
    if not (current_app.config.get('TARIFFS_V2_ENABLED') and current_app.config.get('TARIFFS_V2_SANDBOX')):
        abort(404)


@bp.get('/catalog')
def catalog():
    page = request.args.get('page', 1, type=int)
    if page < 1:
        abort(404)
    products = Product.query.join(TariffV2Listing, TariffV2Listing.product_id == Product.id).filter(
        TariffV2Listing.published.is_(True), Product.status == 'approved').order_by(Product.id).paginate(
            page=page, per_page=24, error_out=False)
    instant = utc()
    rows = [dict(product=product, quote=quote_line(product, 1, instant))
            for product in products.items if is_visible(product, instant)]
    return render_template('main/tariffs_v2_catalog.html', rows=rows, pagination=products)
