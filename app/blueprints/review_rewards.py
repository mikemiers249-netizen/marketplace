import json
import uuid
from pathlib import Path
from flask import Blueprint, request, redirect, url_for, flash, abort, current_app, session
from flask_login import current_user
from app import db
from app.models.users import Seller, Buyer, Admin
from app.models.products import Product, ProductPhoto, Category
from app.models.communications import Settings
from app.models.review_rewards import ReviewRewardProgram, ReviewGift, ReviewReward
from app.utils.review_rewards import enabled, validate_rules, next_rewards, has_review_bonus

bp = Blueprint('review_rewards', __name__)


def require_seller():
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        abort(403)
    if not enabled():
        abort(403, description='Программа выключена администратором.')


@bp.route('/main_admin/loyalty/reviews/toggle', methods=['POST'])
def toggle():
    if not (session.get('main_admin_authenticated') or
            (current_user.is_authenticated and isinstance(current_user, Admin))):
        abort(403)
    Settings.set('review_rewards_enabled', request.form.get('enabled') == 'on', 'json')
    flash('Программа поощрения за отзывы обновлена.', 'success')
    return redirect(url_for('admin.loyalty'))


@bp.route('/seller/loyalty/reviews/save', methods=['POST'])
def save():
    require_seller()
    try:
        kind, mode = request.form.get('kind'), request.form.get('mode')
        rules = validate_rules(current_user.id, kind, mode, json.loads(request.form.get('rules', '[]')))
    except (ValueError, TypeError) as error:
        flash(str(error), 'error')
        return redirect(url_for('seller.loyalty', tab='reviews') + '#review-program')
    program = db.session.get(ReviewRewardProgram, current_user.id)
    if not program:
        program = ReviewRewardProgram(seller_id=current_user.id)
        db.session.add(program)
    program.kind, program.mode, program.rules = kind, mode, rules
    program.enabled = request.form.get('enabled') == 'on'
    db.session.commit()
    flash('Программа поощрения за отзывы сохранена.', 'success')
    return redirect(url_for('seller.loyalty', tab='reviews') + '#review-program')


@bp.route('/seller/loyalty/reviews/gifts', methods=['POST'])
def create_gift():
    require_seller()
    name = (request.form.get('name') or '').strip()
    file = request.files.get('image')
    if not name or len(name) > 180 or not file or not file.filename:
        flash('Укажите название подарка (до 180 символов) и фотографию.', 'error')
        return redirect(url_for('seller.loyalty', tab='reviews') + '#review-program')
    from PIL import Image, UnidentifiedImageError
    try:
        from io import BytesIO
        raw = file.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError
        with Image.open(BytesIO(raw)) as image:
            image.load()
            image.thumbnail((1000, 1000))
            filename = 'review-gift-' + uuid.uuid4().hex + '.jpg'
            folder = Path(current_app.root_path) / 'static/uploads/products'
            folder.mkdir(parents=True, exist_ok=True)
            image.convert('RGB').save(folder / filename, 'JPEG', quality=88)
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        flash('Загрузите корректную фотографию до 10 МБ.', 'error')
        return redirect(url_for('seller.loyalty', tab='reviews') + '#review-program')
    code = uuid.uuid4().hex
    Seller.query.filter_by(id=current_user.id).with_for_update().one()
    category = Category.query.filter_by(slug=f'review-gifts-{current_user.id}').first()
    if not category:
        category = Category(name='Подарки за отзывы', slug=f'review-gifts-{current_user.id}', is_active=False)
        db.session.add(category)
        db.session.flush()
    product = Product(name=name, slug='gift-' + code, article='GIFT-' + code,
                      category_id=category.id,
                      seller_id=current_user.id, price=0, status='reward_gift',
                      stock_quantity=0, current_discount=0)
    db.session.add(product)
    db.session.flush()
    db.session.add(ProductPhoto(product_id=product.id, path=filename, is_main=True, sort_order=0))
    db.session.add(ReviewGift(seller_id=current_user.id, product_id=product.id))
    db.session.commit()
    flash('Подарок добавлен в галерею.', 'success')
    return redirect(url_for('seller.loyalty', tab='reviews') + '#review-program')


@bp.app_context_processor
def reward_context():
    context = {'review_rewards_enabled': enabled()}
    if request.endpoint == 'seller.loyalty' and isinstance(current_user, Seller):
        gifts = ReviewGift.query.filter_by(seller_id=current_user.id).order_by(ReviewGift.id).all()
        context.update(review_program=db.session.get(ReviewRewardProgram, current_user.id), review_gifts=gifts,
                       review_gift_options=[{'id': g.id, 'name': g.product.name} for g in gifts if g.active])
    if request.endpoint in ('seller.loyalty', 'admin.loyalty'):
        query = ReviewReward.query
        if request.endpoint == 'seller.loyalty':
            query = query.filter_by(seller_id=current_user.id)
        context['reward_page'] = query.order_by(ReviewReward.id.desc()).paginate(
            page=max(1, request.args.get('reward_page', 1, type=int)), per_page=30, error_out=False)
    if isinstance(current_user, Buyer) and current_user.is_authenticated:
        context['has_trophies'] = ReviewReward.query.filter_by(buyer_id=current_user.id, kind='gift').first() is not None
        context['has_review_bonus'] = has_review_bonus(buyer_id=current_user.id)
        if request.endpoint == 'main.profile' and request.args.get('section') == 'trophies':
            context['trophies'] = ReviewReward.query.filter_by(buyer_id=current_user.id, kind='gift').order_by(ReviewReward.id.desc()).all()
        if request.endpoint in ('main.cart', 'main.order_review', 'main.checkout'):
            from app.models.orders import CartItem
            seller_ids = {i.product.seller_id for i in CartItem.query.filter_by(buyer_id=current_user.id).all() if i.product}
            context['cart_review_rewards'] = [r for sid in seller_ids for r in next_rewards(current_user.id, sid).values()]
    return context
