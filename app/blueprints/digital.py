"""Public book storefront with private purchases and seller-scoped management."""
import math
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_login import current_user
from app import db
from app.models.users import Buyer, Seller
from app.models.orders import Order, OrderItem, CartItem
from app.models.digital import DigitalBook, DigitalPurchase
from app.models.products import Category, Product
from app.utils.digital_catalog import category_parameters, read_parameters
from app.utils.book_covers import save_cover
from app.utils.book_pages import page_count, page_image
from app.utils.book_text import reading_sections
from app.models.payments import RobokassaPayment
from app.utils.digital_books import MAX_FILE, epub_chapters, storage, file_path

bp = Blueprint('digital', __name__, url_prefix='/digital')
PILOT_EMAIL = 'ronnie83@mail.ru'
TEST_ACCESS_NOTE = 'Тестовый доступ к электронной книге без оплаты'


def test_buyer():
    return bool(pilot_allowed() and isinstance(current_user, Buyer)
                and (current_user.email or '').strip().casefold() == PILOT_EMAIL)


def is_admin():
    return session.get('main_admin_authenticated') is True


def pilot_allowed():
    return bool(current_app.config.get('DIGITAL_BOOKS_PILOT_ENABLED', True) and (
        is_admin() or not current_user.is_authenticated
        or (isinstance(current_user, Buyer) and current_user.is_active)
        or (isinstance(current_user, Seller) and current_user.is_active
                       and (current_user.email or '').strip().casefold() == PILOT_EMAIL)))


def gate(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not pilot_allowed():
            abort(404)
        if not current_user.is_authenticated and not is_admin():
            if request.method != 'GET' or request.endpoint not in (
                    'digital.books', 'digital.detail', 'digital.cover'):
                abort(404)
        return func(*args, **kwargs)
    return wrapped


def manager():
    if not (is_admin() or isinstance(current_user, Seller)):
        abort(404)


def seller_account():
    if isinstance(current_user, Seller) and not is_admin():
        return current_user
    seller = Seller.query.filter(db.func.lower(Seller.email) == PILOT_EMAIL,
                                 Seller.is_active.is_(True)).first()
    if not seller:
        abort(404)
    return seller


def purchased(purchase):
    if purchase and purchase.order:
        order = purchase.order
        if (order.status == 'payment_demo' and order.customer_note == TEST_ACCESS_NOTE
                and order.total_price == 0 and order.delivery_price == 0
                and order.buyer_id == purchase.buyer_id and order.buyer.is_active
                and (order.buyer.email or '').strip().casefold() == PILOT_EMAIL):
            return True
    return bool(purchase and purchase.order.payment_confirmed and purchase.order.status
                not in ('awaiting_payment', 'payment_expired', 'payment_demo', 'canceled', 'cancelled'))


def owned_purchase(purchase_id):
    purchase = db.session.get(DigitalPurchase, purchase_id)
    if (not purchase or not isinstance(current_user, Buyer)
            or purchase.buyer_id != current_user.id or not purchased(purchase)):
        abort(404)
    return purchase


@bp.app_context_processor
def context():
    return {'digital_pilot_allowed': pilot_allowed,
            'digital_test_buyer': test_buyer,
            'digital_buyer': lambda: pilot_allowed() and isinstance(current_user, Buyer),
            'digital_manager': lambda: pilot_allowed() and (is_admin() or isinstance(current_user, Seller))}


@bp.after_request
def protect(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.route('/books')
@gate
def books():
    category_id = request.args.get('category_id', type=int)
    categories = [c for c in Category.query.filter_by(is_active=True).order_by(Category.name)
                  if c.digital_book_category and all(p.is_active for p in c.all_parents)]
    query = DigitalBook.query.filter_by(published=True, moderation_status='approved')
    if category_id:
        selected = next((c for c in categories if c.id == category_id), None)
        if not selected:
            abort(404)
        ids = [c.id for c in categories if c.id == category_id or any(p.id == category_id for p in c.all_parents)]
        query = query.filter(DigitalBook.category_id.in_(ids))
    rows = query.order_by(DigitalBook.id.desc()).all()
    return render_template('main/digital_books.html', books=rows, title='Электронные книги',
                           categories=categories, selected_category_id=category_id)


@bp.route('/manage', methods=['GET', 'POST'])
@gate
def manage():
    manager()
    seller = seller_account()
    category_id = request.values.get('category_id', type=int)
    category = db.session.get(Category, category_id) if category_id else None
    if request.method == 'POST':
        upload = request.files.get('book')
        title = request.form.get('title', '').strip()
        author = request.form.get('author', '').strip()
        description = request.form.get('description', '').strip()
        try:
            if not category or not category.is_active or not category.digital_book_category or any(not p.is_active for p in category.all_parents):
                raise ValueError('Выберите категорию электронной книги')
            parameter_values = read_parameters(category, request.form)
            system_sku = Product.generate_system_sku(seller.id)
            article = request.form.get('article', '').strip()
            if not article or article == f'WML-{seller.id}-XXXXXX':
                article = system_sku
            if len(article) > 50 or Product.query.filter_by(article=article).first() or DigitalBook.query.filter_by(article=article).first():
                raise ValueError('Артикул уже существует или длиннее 50 символов')
            price = Decimal(request.form.get('price', ''))
            if not price.is_finite() or not 0 < price <= 1000000 or price != price.quantize(Decimal('.01')):
                raise ValueError('Укажите цену с точностью до копеек')
            if not title or len(title) > 200 or len(author) > 200 or len(description) > 10000:
                raise ValueError('Проверьте название, автора и описание')
            extension = (upload.filename or '').rsplit('.', 1)[-1].lower() if upload else ''
            if extension not in ('epub', 'pdf'):
                raise ValueError('Загрузите книгу EPUB или PDF')
            raw = upload.stream.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE or not raw:
                raise ValueError('Размер файла должен быть от 1 байта до 15 МБ')
            directory = storage()
            directory.mkdir(parents=True, exist_ok=True)
            name = secrets.token_hex(24) + '.' + extension
            path = file_path(name)
            path.write_bytes(raw)
            cover_name = None
            try:
                if extension == 'epub':
                    epub_chapters(path)
                elif not raw.startswith(b'%PDF-'):
                    raise ValueError('Файл не является PDF')
                else:
                    from types import SimpleNamespace
                    page_count(SimpleNamespace(format='pdf', file_name=name))
                cover_name = save_cover(request.files)
                book = DigitalBook(seller_id=seller.id, title=title, author=author,
                                   description=description, price=price, format=extension,
                                   file_name=name, cover_name=cover_name, published=False, moderation_status='pending', category=category,
                                   article=article, system_sku=system_sku, parameter_values=parameter_values)
                db.session.add(book)
                db.session.commit()
            except Exception:
                db.session.rollback()
                path.unlink(missing_ok=True)
                if cover_name:
                    file_path(cover_name).unlink(missing_ok=True)
                raise
            flash('Книга загружена и отправлена администратору на модерацию.', 'success')
            return redirect(url_for('digital.manage'))
        except (ValueError, InvalidOperation) as exc:
            flash(str(exc) if isinstance(exc, ValueError) else 'Проверьте цену книги', 'warning')
    current_status = request.args.get('status')
    if current_status not in ('active', 'inactive', 'pending', 'rejected'):
        current_status = None
    query = DigitalBook.query.filter_by(seller_id=seller.id).filter(DigitalBook.moderation_status != 'deleted')
    if current_status in ('pending', 'rejected'):
        query = query.filter_by(moderation_status=current_status)
    elif current_status:
        query = query.filter_by(published=current_status == 'active')
    pagination = query.order_by(DigitalBook.id.desc()).paginate(
        page=request.args.get('page', 1, type=int), per_page=20, error_out=False)
    return render_template('main/digital_manage.html', books=pagination.items, title='Электронные товары',
                           pagination=pagination, current_status=current_status,
                           category=category, parameter_settings=category_parameters(category) if category else [],
                           seller_account=seller, creating=request.method == 'POST')


@bp.route('/new', methods=['GET', 'POST'])
@gate
def new():
    manager()
    if request.method == 'POST':
        return manage()
    category = db.session.get(Category, request.args.get('category_id', type=int))
    if not category or not category.is_active or not category.digital_book_category or any(not p.is_active for p in category.all_parents):
        abort(404)
    return render_template('main/digital_manage.html', books=[], category=category,
        parameter_settings=category_parameters(category), seller_account=seller_account(),
        creating=True, title='Добавить электронную книгу')


@bp.route('/choose')
@gate
def choose():
    manager()
    categories = [c for c in Category.query.filter_by(is_active=True).order_by(Category.name)
                  if c.digital_book_category and all(p.is_active for p in c.all_parents)]
    return render_template('seller/product_category.html', categories=categories,
                           digital_only=True, title='Добавить электронную книгу')


@bp.route('/books/<int:book_id>/publish', methods=['POST'])
@gate
def publish(book_id):
    book = managed_book(book_id)
    if book.published:
        book.published = False
    elif book.moderation_status == 'approved':
        book.published = True
    else:
        book.moderation_status = 'pending'
        book.moderation_comment = ''
        book.published = False
        flash('Книга отправлена администратору на модерацию.', 'success')
    db.session.commit()
    return redirect(url_for('digital.manage'))


def accessible_book(book_id):
    book = db.session.get(DigitalBook, book_id)
    if not book:
        abort(404)
    own = is_admin() or (isinstance(current_user, Seller) and book.seller_id == current_user.id)
    if (not book.published or book.moderation_status != 'approved') and not own:
        purchases = DigitalPurchase.query.filter_by(book_id=book.id, buyer_id=current_user.id).all() if isinstance(current_user, Buyer) else []
        if not any(purchased(p) for p in purchases):
            abort(404)
    return book


@bp.route('/books/<int:book_id>')
@gate
def detail(book_id):
    book = accessible_book(book_id)
    in_cart = bool(isinstance(current_user, Buyer) and book.commerce_product and
                   CartItem.query.filter_by(buyer_id=current_user.id, product_id=book.commerce_product.id).first())
    return render_template('main/digital_product.html', book=book, title=book.title, in_cart=in_cart)


@bp.route('/books/<int:book_id>/cover', methods=['GET', 'POST'])
@gate
def cover(book_id):
    if request.method == 'POST':
        book = managed_book(book_id)
        name = None
        old_name = book.cover_name
        try:
            name = save_cover(request.files)
            if not name:
                raise ValueError('Выберите изображение обложки')
            book.cover_name = name
            book.published = False
            book.moderation_status = 'pending'
            book.moderation_comment = ''
            db.session.commit()
        except ValueError as exc:
            flash(str(exc), 'warning')
        except Exception:
            db.session.rollback()
            if name:
                file_path(name).unlink(missing_ok=True)
            raise
        else:
            if old_name:
                file_path(old_name).unlink(missing_ok=True)
            flash('Обложка сохранена.', 'success')
        return redirect(url_for('digital.manage'))
    book = accessible_book(book_id)
    if not book.cover_name or not file_path(book.cover_name).is_file():
        abort(404)
    return send_file(file_path(book.cover_name), mimetype='image/jpeg', conditional=True)


@bp.route('/books/<int:book_id>/test-access', methods=['POST'])
@gate
def test_access(book_id):
    if not test_buyer():
        abort(404)
    book = DigitalBook.query.filter_by(id=book_id, published=True, moderation_status='approved').first_or_404()
    # Serialize repeated clicks for this buyer; never convert an unpaid order.
    Buyer.query.filter_by(id=current_user.id).with_for_update().one()
    for purchase in DigitalPurchase.query.filter_by(buyer_id=current_user.id, book_id=book.id):
        if purchased(purchase):
            return redirect(url_for('digital.read', purchase_id=purchase.id))
    order = Order(order_number='T' + secrets.token_hex(9), buyer_id=current_user.id,
                  seller_id=book.seller_id, total_price=0, delivery_price=0,
                  status='payment_demo', customer_note=TEST_ACCESS_NOTE)
    db.session.add(order)
    db.session.flush()
    purchase = DigitalPurchase(buyer_id=current_user.id, book_id=book.id, order_id=order.id,
                               title=book.title, file_name=book.file_name, format=book.format)
    db.session.add(purchase)
    db.session.commit()
    return redirect(url_for('digital.read', purchase_id=purchase.id))


@bp.route('/books/<int:book_id>/buy', methods=['POST'])
@gate
def buy(book_id):
    if not isinstance(current_user, Buyer):
        abort(404)
    book = DigitalBook.query.filter_by(id=book_id, published=True, moderation_status='approved').first_or_404()
    Buyer.query.filter_by(id=current_user.id).with_for_update().one()
    previous = DigitalPurchase.query.filter_by(buyer_id=current_user.id, book_id=book.id).all()
    if any(purchased(p) for p in previous):
        return redirect(url_for('digital.library'))
    from app.blueprints.payments import (configured, prepare_invoice, gateway_url,
                                         release_reservation, RESERVATION_TIME)
    if not configured():
        flash('Оплата временно недоступна. Попробуйте позже.', 'warning')
        return redirect(url_for('digital.books'))
    for purchase in previous:
        invoice = RobokassaPayment.query.filter_by(order_id=purchase.order_id,
            is_test=current_app.config['ROBOKASSA_TEST_MODE'], status='pending').first()
        if (invoice and purchase.order.status == 'awaiting_payment'
                and purchase.order.created_at >= datetime.utcnow() - RESERVATION_TIME):
            return redirect(gateway_url(invoice))
        if purchase.order.status == 'awaiting_payment':
            release_reservation(purchase.order)
    from app.utils.digital_commerce import sync_book
    from app.utils.helpers import compute_item_discount_breakdown
    from types import SimpleNamespace
    product = sync_book(book)
    item = SimpleNamespace(product=product, quantity=1) if product else None
    discount = compute_item_discount_breakdown(item, [item]) if item else None
    total = round(float(book.price) - (discount['total_discount'] if discount else 0), 2)
    order = Order(order_number='D' + secrets.token_hex(9), buyer_id=current_user.id,
                  seller_id=book.seller_id, total_price=total, delivery_price=0,
                  delivery_address='Электронная книга — доступ в библиотеке', status='awaiting_payment')
    db.session.add(order)
    db.session.flush()
    if product:
        db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1,
            price_at_order=total, original_price=float(book.price), promo_discount=discount['promo_discount']))
    purchase = DigitalPurchase(buyer_id=current_user.id, book_id=book.id, order_id=order.id,
                               title=book.title, file_name=book.file_name, format=book.format)
    db.session.add(purchase)
    invoice = prepare_invoice(order)
    db.session.commit()
    return redirect(gateway_url(invoice))


@bp.route('/books/<int:book_id>/cart', methods=['POST'])
@gate
def add_to_cart(book_id):
    if not isinstance(current_user, Buyer):
        abort(404)
    ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    Buyer.query.filter_by(id=current_user.id).with_for_update().one()
    book = DigitalBook.query.filter_by(id=book_id, published=True, moderation_status='approved').first_or_404()
    from app.utils.digital_commerce import sync_book
    product = sync_book(book)
    if not product:
        abort(404)
    from app.blueprints.main import _resolve_tariff_state
    if _resolve_tariff_state(book.seller)['state'] in ('locked', 'none'):
        if ajax:
            return jsonify(error='Магазин временно не принимает новые заказы.'), 409
        flash('Магазин временно не принимает новые заказы.', 'warning')
        return redirect(url_for('digital.detail', book_id=book.id))
    if any(purchased(p) for p in DigitalPurchase.query.filter_by(buyer_id=current_user.id, book_id=book.id)):
        if ajax:
            return jsonify(error='Книга уже доступна в вашей библиотеке.'), 409
        return redirect(url_for('digital.library'))
    db.session.flush()
    item = CartItem.query.filter_by(buyer_id=current_user.id, product_id=product.id).first()
    if not item:
        db.session.add(CartItem(buyer_id=current_user.id, product_id=product.id, quantity=1))
    db.session.commit()
    if ajax:
        count = db.session.query(db.func.sum(CartItem.quantity)).filter(CartItem.buyer_id == current_user.id).scalar() or 0
        return jsonify(success=True, in_cart=True, cart_count=count)
    return redirect(url_for('digital.detail', book_id=book.id))


@bp.route('/library')
@gate
def library():
    if not isinstance(current_user, Buyer):
        if is_admin():
            return redirect(url_for('digital.manage'))
        abort(404)
    rows = DigitalPurchase.query.filter_by(buyer_id=current_user.id).order_by(DigitalPurchase.id.desc()).all()
    status = request.args.get('status', '')
    if status not in ('unread', 'reading', 'finished'):
        status = ''
    items = []
    for purchase in rows:
        if not purchased(purchase):
            continue
        try:
            total = page_count(purchase)
        except (ValueError, OSError, RuntimeError):
            total = 0
        started = bool(purchase.chapter or purchase.position)
        pages = min(total, max(0, purchase.chapter + 1)) if started else 0
        finished = bool(total and pages >= total and purchase.position >= 1)
        state = 'finished' if finished else ('reading' if started else 'unread')
        if not status or status == state:
            items.append(dict(purchase=purchase, total=total, pages=pages,
                              percent=(100 if finished else min(99, int((purchase.chapter + purchase.position) * 100 / total))) if total and started else 0,
                              started=started))
    return render_template('main/profile.html', library_items=items, library_status=status,
                           section='library', user=current_user, title='Моя библиотека')


def render_reader(book, purchase=None, preview=False):
    try:
        sections = reading_sections(book)
        error = None
    except (ValueError, OSError, RuntimeError) as exc:
        sections = []
        error = str(exc) if isinstance(exc, ValueError) else 'Не удалось открыть текст книги.'
    count = len(sections)
    index = max(0, min(purchase.chapter if purchase else 0, max(0, count - 1)))
    return render_template('main/digital_reader.html', book=book, purchase=purchase,
                           reader_author=(purchase.book.author if purchase and purchase.book else getattr(book, 'author', '')),
                           preview=preview, chapter=index, count=count, sections=sections,
                           reader_error=error, title=book.title)


@bp.route('/read/<int:purchase_id>')
@gate
def read(purchase_id):
    purchase = owned_purchase(purchase_id)
    return render_reader(purchase, purchase)


@bp.route('/preview/<int:book_id>')
@gate
def preview(book_id):
    manager()
    book = db.session.get(DigitalBook, book_id)
    if not book or (not is_admin() and book.seller_id != seller_account().id):
        abort(404)
    return render_reader(book, preview=True)


@bp.route('/file/<int:item_id>')
@gate
def content(item_id):
    # Old source-file links must not expose even paid documents.
    abort(404)


@bp.route('/page/<int:item_id>/<int:index>')
@gate
def reader_page(item_id, index):
    if request.args.get('preview') == '1':
        manager()
        book = db.session.get(DigitalBook, item_id)
        if not book or (not is_admin() and book.seller_id != seller_account().id):
            abort(404)
        mark = 'WIMLI / PREVIEW'
    else:
        book = owned_purchase(item_id)
        mark = f'WIMLI / purchase {book.id} / reader {book.buyer_id}'
    try:
        data = page_image(book, index, mark)
    except IndexError:
        abort(404)
    except (ValueError, OSError, RuntimeError):
        abort(422)
    response = send_file(data, mimetype='image/jpeg', conditional=False)
    response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'self'"
    return response


@bp.route('/illustration/<int:item_id>/<int:source>/<int:block>')
@gate
def illustration(item_id, source, block):
    if request.args.get('preview') == '1':
        book = managed_book(item_id)
    else:
        book = owned_purchase(item_id)
    from app.utils.book_text import illustration_image
    try:
        data, mimetype = illustration_image(book, source, block)
    except (IndexError, KeyError):
        abort(404)
    except (ValueError, OSError, RuntimeError):
        abort(422)
    response = send_file(data, mimetype=mimetype, conditional=False)
    response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'none'; sandbox"
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.route('/progress/<int:purchase_id>', methods=['POST'])
@gate
def progress(purchase_id):
    purchase = owned_purchase(purchase_id)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        abort(400)
    chapter, position = data.get('chapter'), data.get('position')
    if (not isinstance(chapter, int) or not 0 <= chapter <= 100000
            or not isinstance(position, (int, float)) or not math.isfinite(position) or not 0 <= position <= 1):
        abort(400)
    purchase.chapter, purchase.position = chapter, position
    purchase.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(success=True)


def managed_book(book_id):
    manager()
    book = db.session.get(DigitalBook, book_id)
    if not book or book.moderation_status == 'deleted' or (not is_admin() and book.seller_id != seller_account().id):
        abort(404)
    return book


@bp.route('/books/<int:book_id>/delete', methods=['POST'])
@gate
def delete(book_id):
    book = managed_book(book_id)
    book.published = False
    book.moderation_status = 'deleted'
    db.session.commit()
    flash('Товар удалён. Купленные книги остаются доступны покупателям.', 'success')
    return redirect(url_for('digital.manage'))


def valid_price(raw):
    try:
        price = Decimal(raw)
        if not price.is_finite() or not 0 < price <= 1000000 or price != price.quantize(Decimal('.01')):
            raise ValueError()
        return price
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Укажите цену от 0,01 до 1 000 000 с точностью до копеек')


def resubmit(book):
    book.published = False
    book.moderation_status = 'pending'
    book.moderation_comment = ''


@bp.route('/books/<int:book_id>/price', methods=['POST'])
@gate
def price(book_id):
    book = managed_book(book_id)
    try:
        value = valid_price(request.form.get('price'))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if book.price != value:
        book.price = value
        resubmit(book)
        db.session.commit()
    return jsonify(success=True, price=str(book.price), status=book.moderation_status)


@bp.route('/books/<int:book_id>/edit', methods=['GET', 'POST'])
@gate
def edit(book_id):
    book = managed_book(book_id)
    categories = [c for c in Category.query.filter_by(is_active=True).all()
                  if c.digital_book_category and all(p.is_active for p in c.all_parents)]
    category = db.session.get(Category, request.values.get('category_id', book.category_id, type=int))
    if category is None and request.method == 'GET' and categories:
        category = categories[0]
    if request.method == 'POST':
        new_file = new_cover = None
        try:
            if category not in categories:
                raise ValueError('Выберите категорию электронной книги')
            values = read_parameters(category, request.form)
            title = request.form.get('title', '').strip()
            author = request.form.get('author', '').strip()
            description = request.form.get('description', '').strip()
            if not title or len(title) > 200 or len(author) > 200 or len(description) > 10000:
                raise ValueError('Проверьте название, автора и описание')
            value = valid_price(request.form.get('price'))
            article = request.form.get('article', '').strip() or book.system_sku
            if not article or len(article) > 50 or Product.query.filter(Product.article == article, db.or_(Product.digital_book_id.is_(None), Product.digital_book_id != book.id)).first() or DigitalBook.query.filter(DigitalBook.article == article, DigitalBook.id != book.id).first():
                raise ValueError('Артикул уже используется или некорректен')
            upload = request.files.get('book')
            extension = book.format
            if upload and upload.filename:
                extension = upload.filename.rsplit('.', 1)[-1].lower()
                raw = upload.stream.read(MAX_FILE + 1)
                if extension not in ('pdf', 'epub') or not raw or len(raw) > MAX_FILE:
                    raise ValueError('Загрузите EPUB или PDF до 15 МБ')
                new_file = secrets.token_hex(24) + '.' + extension
                file_path(new_file).write_bytes(raw)
                if extension == 'epub':
                    epub_chapters(file_path(new_file))
                else:
                    from types import SimpleNamespace
                    page_count(SimpleNamespace(format='pdf', file_name=new_file))
            new_cover = save_cover(request.files)
            book.title, book.author, book.description = title, author, description
            book.price, book.article, book.category = value, article, category
            book.parameter_values = values
            if new_file:
                book.file_name, book.format = new_file, extension
            if new_cover:
                book.cover_name = new_cover
            resubmit(book)
            db.session.commit()
            flash('Изменения сохранены. Книга отправлена на модерацию.', 'success')
            return redirect(url_for('digital.manage'))
        except (ValueError, InvalidOperation) as exc:
            db.session.rollback()
            for name in (new_file, new_cover):
                if name:
                    file_path(name).unlink(missing_ok=True)
            flash(str(exc), 'warning')
        except Exception:
            db.session.rollback()
            for name in (new_file, new_cover):
                if name:
                    file_path(name).unlink(missing_ok=True)
            raise
    settings = category_parameters(category) if category else []
    for setting in settings:
        setting['default'] = book.parameter_values.get(str(setting['parameter'].id), setting['default'])
    return render_template('main/digital_edit.html', book=book, categories=categories,
                           category=category, parameter_settings=settings, seller_account=book.seller)


@bp.route('/moderation/<int:book_id>', methods=['GET', 'POST'])
@gate
def moderate(book_id):
    if not is_admin():
        abort(404)
    book = db.get_or_404(DigitalBook, book_id)
    if request.method == 'POST':
        action = request.form.get('action')
        if action not in ('approve', 'reject'):
            abort(400)
        comment = request.form.get('comment', '').strip()[:2000]
        if action == 'reject' and not comment:
            flash('Укажите причину отклонения.', 'warning')
        else:
            book.moderation_status = 'approved' if action == 'approve' else 'rejected'
            book.published = action == 'approve'
            book.moderation_comment = comment
            db.session.commit()
            return redirect(url_for('admin.products_moderation'))
    return render_template('admin/digital_moderate.html', book=book)
