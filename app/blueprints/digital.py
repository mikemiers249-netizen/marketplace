"""Closed electronic-book pilot. Every route and file has a server-side gate."""
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
from app.models.orders import Order
from app.models.digital import DigitalBook, DigitalPurchase
from app.models.payments import RobokassaPayment
from app.utils.digital_books import MAX_FILE, epub_chapters, storage, file_path

bp = Blueprint('digital', __name__, url_prefix='/digital')
PILOT_EMAIL = 'ronnie83@mail.ru'


def is_admin():
    return session.get('main_admin_authenticated') is True


def pilot_allowed():
    return bool(current_app.config.get('DIGITAL_BOOKS_PILOT_ENABLED', True) and (
        is_admin() or (isinstance(current_user, (Buyer, Seller)) and current_user.is_active
                       and (current_user.email or '').strip().casefold() == PILOT_EMAIL)))


def gate(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not pilot_allowed():
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
    rows = DigitalBook.query.filter_by(published=True).order_by(DigitalBook.id.desc()).all()
    return render_template('main/digital_books.html', books=rows, title='Электронные книги')


@bp.route('/manage', methods=['GET', 'POST'])
@gate
def manage():
    manager()
    seller = seller_account()
    if request.method == 'POST':
        upload = request.files.get('book')
        title = request.form.get('title', '').strip()
        author = request.form.get('author', '').strip()
        description = request.form.get('description', '').strip()
        try:
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
            try:
                if extension == 'epub':
                    epub_chapters(path)
                elif not raw.startswith(b'%PDF-'):
                    raise ValueError('Файл не является PDF')
                book = DigitalBook(seller_id=seller.id, title=title, author=author,
                                   description=description, price=price, format=extension,
                                   file_name=name, published=False)
                db.session.add(book)
                db.session.commit()
            except Exception:
                db.session.rollback()
                path.unlink(missing_ok=True)
                raise
            flash('Книга загружена как черновик. Проверьте предпросмотр перед публикацией.', 'success')
            return redirect(url_for('digital.manage'))
        except (ValueError, InvalidOperation) as exc:
            flash(str(exc) if isinstance(exc, ValueError) else 'Проверьте цену книги', 'warning')
    rows = DigitalBook.query.filter_by(seller_id=seller.id).order_by(DigitalBook.id.desc()).all()
    return render_template('main/digital_manage.html', books=rows, title='Электронные товары')


@bp.route('/books/<int:book_id>/publish', methods=['POST'])
@gate
def publish(book_id):
    manager()
    book = db.session.get(DigitalBook, book_id)
    if not book or book.seller_id != seller_account().id:
        abort(404)
    book.published = not book.published
    db.session.commit()
    return redirect(url_for('digital.manage'))


@bp.route('/books/<int:book_id>/buy', methods=['POST'])
@gate
def buy(book_id):
    if not isinstance(current_user, Buyer):
        abort(404)
    book = DigitalBook.query.filter_by(id=book_id, published=True).first_or_404()
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
    order = Order(order_number='D' + secrets.token_hex(9), buyer_id=current_user.id,
                  seller_id=book.seller_id, total_price=float(book.price), delivery_price=0,
                  delivery_address='Электронная книга — доступ в библиотеке', status='awaiting_payment')
    db.session.add(order)
    db.session.flush()
    purchase = DigitalPurchase(buyer_id=current_user.id, book_id=book.id, order_id=order.id,
                               title=book.title, file_name=book.file_name, format=book.format)
    db.session.add(purchase)
    invoice = prepare_invoice(order)
    db.session.commit()
    return redirect(gateway_url(invoice))


@bp.route('/library')
@gate
def library():
    if not isinstance(current_user, Buyer):
        if is_admin():
            return redirect(url_for('digital.manage'))
        abort(404)
    rows = DigitalPurchase.query.filter_by(buyer_id=current_user.id).order_by(DigitalPurchase.id.desc()).all()
    return render_template('main/digital_library.html', purchases=[p for p in rows if purchased(p)],
                           title='Моя библиотека')


def render_reader(book, purchase=None, preview=False):
    chapters = epub_chapters(file_path(book.file_name)) if book.format == 'epub' else []
    index = request.args.get('chapter', purchase.chapter if purchase else 0, type=int)
    index = max(0, min(index, max(0, len(chapters)-1)))
    return render_template('main/digital_reader.html', book=book, purchase=purchase, preview=preview,
                           chapters=chapters, chapter=index, title=book.title)


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
    if not book or book.seller_id != seller_account().id:
        abort(404)
    return render_reader(book, preview=True)


@bp.route('/file/<int:item_id>')
@gate
def content(item_id):
    if request.args.get('preview') == '1':
        manager()
        book = db.session.get(DigitalBook, item_id)
        if not book or book.seller_id != seller_account().id:
            abort(404)
    else:
        book = owned_purchase(item_id)
    if book.format != 'pdf':
        abort(404)
    path = file_path(book.file_name)
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype='application/pdf', as_attachment=False,
                     download_name='book.pdf', conditional=True)


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
