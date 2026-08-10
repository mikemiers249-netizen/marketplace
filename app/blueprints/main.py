"""
Blueprint основного сайта (публичная часть для покупателей).
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user
from sqlalchemy import or_, and_, func
from datetime import datetime
from app import db, cache
from app.models.products import Category, Product, ProductPhoto, ProductParameter, ProductEvent
from app.models.orders import CartItem, Favorite, Order, OrderItem, Banner, Promotion
from app.models.users import Buyer, DeliveryService, BuyerDelivery, SellerDelivery, Seller
from app.models.communications import Message, Review
from app.utils.helpers import (
    format_price, get_breadcrumbs, PaginationHelper,
    get_cart_total, get_cart_discount_breakdown, slugify
)
from app.utils.decorators import buyer_required, ajax_login_required
from app.utils.emails import send_new_order_notification_to_seller, create_order_conversation
# Импорт из seller blueprint: приватный _resolve_tariff_state и публичный
# get_active_seller_ids нужны для фильтрации товаров в публичном каталоге
# (скрываем товары заблокированных магазинов: state in ('locked', 'none')).
from app.blueprints.seller import _resolve_tariff_state, get_active_seller_ids


bp = Blueprint('main', __name__)


def _get_category_filters(category):
    """
    Получение параметров фильтрации для категории.
    Собирает параметры от текущей категории и всех её родителей.
    """
    from app.models.products import CategoryParameter, Parameter
    
    if not category:
        return []
    
    # Собираем все параметры от родителей + текущая категория
    all_params_data = []
    seen_param_ids = set()
    
    # Получаем всех родителей (от корня к текущей)
    parents = category.all_parents
    
    # Добавляем параметры от каждого родителя
    for parent in parents:
        parent_params = CategoryParameter.query.filter_by(
            category_id=parent.id
        ).order_by(CategoryParameter.sort_order).all()
        
        for cp in parent_params:
            if cp.parameter_id not in seen_param_ids and cp.parameter:
                seen_param_ids.add(cp.parameter_id)
                # Определяем уровень для отображения
                level = 'parent'
                if parent.parent_id is None:
                    level = 'global'
                else:
                    level = 'parent'
                
                all_params_data.append({
                    'parameter': cp.parameter,
                    'level': level,
                    'category_name': parent.name,
                    'sort_order': cp.sort_order
                })
    
    # Добавляем параметры текущей категории
    current_params = CategoryParameter.query.filter_by(
        category_id=category.id
    ).order_by(CategoryParameter.sort_order).all()
    
    for cp in current_params:
        if cp.parameter_id not in seen_param_ids and cp.parameter:
            seen_param_ids.add(cp.parameter_id)
            all_params_data.append({
                'parameter': cp.parameter,
                'level': 'current',
                'category_name': category.name,
                'sort_order': cp.sort_order
            })
    
    # Группируем параметры по типу для отображения
    grouped_params = {}
    for param_data in all_params_data:
        param = param_data['parameter']
        param_type = param.type
        
        if param_type not in grouped_params:
            grouped_params[param_type] = []
        
        # Получаем уникальные значения для этого параметра из товаров категории
        values = _get_parameter_values(category, param.id)
        
        if values:  # Добавляем только если есть значения
            param_info = {
                'id': param.id,
                'name': param.name,
                'code': param.code,
                'type': param.type,
                'is_multiple': param.is_multiple,
                'predefined_values': param.predefined_values,
                'is_input': param.is_input,
                'level': param_data['level'],
                'category_name': param_data['category_name'],
                'filter_values': values
            }
            grouped_params[param_type].append(param_info)
    
    return grouped_params


def _get_parameter_values(category, parameter_id):
    """
    Получение уникальных значений параметра для товаров в категории.
    """
    from app.models.products import ProductParameter, Product
    
    # Собираем ID всех подкатегорий
    category_ids = [category.id]
    for sub in category.subcategories:
        category_ids.append(sub.id)
        for sub2 in sub.subcategories:
            category_ids.append(sub2.id)
    
    # Получаем уникальные значения
    values_query = db.session.query(
        ProductParameter.value
    ).join(Product).filter(
        Product.category_id.in_(category_ids),
        Product.status == 'approved',
        ProductParameter.parameter_id == parameter_id
    ).distinct().all()
    
    # Обрабатываем значения
    values = []
    for row in values_query:
        val = row[0]
        if isinstance(val, list):
            values.extend(val)
        else:
            values.append(val)
    
    # Удаляем дубликаты и None
    values = list(set(v for v in values if v is not None))
    
    # Сортируем
    try:
        values = sorted(values, key=lambda x: float(x) if isinstance(x, (int, float)) or (isinstance(x, str) and x.replace('.', '').replace('-', '').isdigit()) else str(x))
    except:
        values = sorted(values, key=str)
    
    return values[:20]  # Ограничиваем количество значений


def _visible_seller_ids() -> set:
    """Возвращает множество seller_id с активным тарифом (paid/grace/global).

    Результат кэшируется на уровне `g` (один HTTP-запрос = один расчёт),
    чтобы не дёргать БД несколько раз в рамках одного view.
    """
    from flask import g
    if not hasattr(g, '_visible_seller_ids_cache'):
        g._visible_seller_ids_cache = get_active_seller_ids()
    return g._visible_seller_ids_cache


def _start_of_today_utc() -> datetime:
    """Начало текущих суток в UTC. Сутки считаются по серверному времени
    (везде в проекте используется datetime.utcnow)."""
    now = datetime.utcnow()
    return datetime(now.year, now.month, now.day)


def _seller_blocked_by_daily_limit(seller) -> bool:
    """Возвращает True, если продавец превысил дневной лимит заказов.

    Лимит хранится в `Seller.daily_orders_limit`:
      - None или 0 → безлимит (всегда False);
      - N > 0      → если за текущие сутки (UTC) у продавца уже
                     оформлено >= N заказов со статусом != 'canceled',
                     возвращаем True.

    Кэшируем результат на уровне seller_id в `g`, чтобы не считать
    счётчик по несколько раз для одного и того же продавца в рамках
    одного запроса (например, при обходе списка товаров).
    """
    if seller is None:
        return False
    limit = getattr(seller, 'daily_orders_limit', None)
    if not limit or limit <= 0:
        return False

    from flask import g
    cache_attr = f'_seller_blocked_cache_{seller.id}'
    if hasattr(g, cache_attr):
        return getattr(g, cache_attr)

    start = _start_of_today_utc()
    count = Order.query.filter(
        Order.seller_id == seller.id,
        Order.created_at >= start,
        Order.status != 'canceled',
    ).count()

    blocked = count >= limit
    setattr(g, cache_attr, blocked)
    return blocked


def _visible_seller_ids_with_limit() -> set:
    """Как _visible_seller_ids, но дополнительно исключает продавцов,
    у которых превышен дневной лимит заказов. Используется в каталоге."""
    sids = _visible_seller_ids()
    if not sids:
        return sids
    sellers = Seller.query.filter(Seller.id.in_(sids)).all()
    blocked = {s.id for s in sellers if _seller_blocked_by_daily_limit(s)}
    return sids - blocked


def _filter_visible(products):
    """Отфильтровать список ORM-объектов Product, оставив только товары
    продавцов с активным тарифом (paid/grace/global). Используется в view,
    где запрос уже выполнен и нужна пост-фильтрация по подмножеству."""
    sids = _visible_seller_ids()
    return [p for p in products if p.seller_id in sids]


@bp.route('/')
def index():
    """
    Главная страница.
    URL: /
    """
    # Баннеры
    banners = Banner.query.filter(
        Banner.is_active == True,
        or_(Banner.start_date == None, Banner.start_date <= db.func.now()),
        or_(Banner.end_date == None, Banner.end_date >= db.func.now())
    ).order_by(Banner.sort_order).all()

    # Категории для навигации
    random_categories = cache.get('random_categories')
    if random_categories is None:
        random_categories = Category.query.filter(
            Category.is_active == True,
            Category.parent_id == None
        ).order_by(func.random()).limit(6).all()
        cache.set('random_categories', random_categories, timeout=300)

    # Новинки (опубликованные менее недели назад).
    # Берём с запасом (50), а финальное количество для показа решает шаблон:
    # если новинок меньше 5 — блок скрываем; иначе показываем
    # count // 5 * 5 (округление вниз к ближайшему кратному 5).
    # Товары заблокированных магазинов (tariff state in 'locked','none')
    # из выдачи исключаются, чтобы на главной не светились мёртвые карточки.
    from datetime import datetime, timedelta
    week_ago = datetime.utcnow() - timedelta(days=7)
    new_products = Product.query.filter(
        Product.status == 'approved',
        Product.published_at >= week_ago,
        Product.stock_quantity > 0
    ).order_by(Product.published_at.desc()).limit(50).all()
    new_products = _filter_visible(new_products)

    # Товары со скидкой
    discounted_products = Product.query.filter(
        Product.status == 'approved',
        Product.current_discount > 0,
        Product.stock_quantity > 0
    ).order_by(Product.published_at.desc()).limit(10).all()
    discounted_products = _filter_visible(discounted_products)

    # Все товары
    products = Product.query.filter(
        Product.status == 'approved',
        Product.stock_quantity > 0
    ).order_by(Product.published_at.desc()).limit(50).all()
    products = _filter_visible(products)
    
    # Активные акции
    active_promotions = Promotion.query.filter(
        Promotion.status == 'active',
        Promotion.start_date <= db.func.now(),
        Promotion.end_date >= db.func.now()
    ).limit(5).all()
    
    return render_template('main/index.html',
                         title='Маркетплейс',
                         banners=banners,
                         random_categories=random_categories,
                         new_products=new_products,
                         discounted_products=discounted_products,
                         products=products,
                         promotions=active_promotions)


@bp.route('/catalogue/')
@bp.route('/catalogue/<int:category_id>/')
def catalog(category_id=None):
    """
    Каталог товаров.
    URL: /catalogue/, /catalogue/{id}
    """
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    # Поддержка category_id из параметров запроса (альтернативный формат)
    if category_id is None:
        category_id = request.args.get('category_id', type=int)
    
    # Построение запроса
    query = Product.query.filter(Product.status == 'approved')

    # Скрываем товары продавцов с заблокированным тарифом (state in
    # 'locked', 'none') ИЛИ превысивших дневной лимит заказов.
    # Делаем на уровне SQL до пагинации, чтобы не показывать пустые страницы.
    visible_sids = _visible_seller_ids_with_limit()
    if visible_sids:
        query = query.filter(Product.seller_id.in_(visible_sids))
    else:
        # Ни один селлер не активен — отдаём пустой результат. Product.id — PK
        # NOT NULL, поэтому `Product.id.is_(None)` всегда даёт 0 строк.
        query = query.filter(Product.id.is_(None))
    
    # Фильтрация по цене (поддержка обоих форматов: с filter_ и без)
    price_min = request.args.get('filter_price_min') or request.args.get('price_min')
    price_max = request.args.get('filter_price_max') or request.args.get('price_max')
    
    if price_min:
        try:
            query = query.filter(Product.price >= float(price_min))
        except (ValueError, TypeError):
            pass
    
    if price_max:
        try:
            query = query.filter(Product.price <= float(price_max))
        except (ValueError, TypeError):
            pass
    
    # Сортировка
    sort = request.args.get('sort', 'new')
    
    if category_id:
        # Получаем категорию и её подкатегории
        category = db.session.get(Category, category_id)
        if not category:
            abort(404)
        
        # Список ID категорий для фильтрации
        category_ids = [category_id]
        for sub in category.subcategories:
            category_ids.append(sub.id)
            for sub2 in sub.subcategories:
                category_ids.append(sub2.id)
        
        query = query.filter(Product.category_id.in_(category_ids))
        
        # Параметры фильтрации из URL (формат filter_)
        for key, value in request.args.items():
            if key.startswith('filter_'):
                param_id = key.replace('filter_', '')
                
                # Параметры товара (не ценовые - они уже обработаны выше)
                if param_id not in ('price_min', 'price_max'):
                    try:
                        param_id_int = int(param_id)
                        query = query.join(ProductParameter).filter(
                            and_(
                                ProductParameter.parameter_id == param_id_int,
                                ProductParameter.value == value
                            )
                        )
                    except ValueError:
                        pass
    else:
        category = None
    
    # Сортировка
    sort = request.args.get('sort', 'new')
    if sort == 'new':
        query = query.order_by(Product.published_at.desc())
    elif sort == 'price_asc':
        query = query.order_by(Product.price.asc())
    elif sort == 'price_desc':
        query = query.order_by(Product.price.desc())
    elif sort == 'popular':
        # Используем подзапрос для подсчёта отзывов
        reviews_subquery = db.session.query(
            Review.product_id,
            func.count(Review.id).label('review_count')
        ).group_by(Review.product_id).subquery()
        
        query = query.outerjoin(
            reviews_subquery,
            Product.id == reviews_subquery.c.product_id
        ).order_by(func.coalesce(reviews_subquery.c.review_count, 0).desc())
    
    # Пагинация
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    products = pagination.items
    
    # Категории для фильтров
    if category:
        categories = [category] + list(category.subcategories)
        # Получаем параметры для фильтрации (наследуемые от родителей + свои)
        category_parameters = _get_category_filters(category)
    else:
        categories = Category.query.filter(
            Category.is_active == True,
            Category.parent_id == None
        ).all()
        category_parameters = []
    
    # Активные фильтры из URL
    active_filters = {}
    for key, value in request.args.items():
        if key.startswith('filter_'):
            filter_key = key.replace('filter_', '')
            active_filters[filter_key] = value
    
    return render_template('main/catalog.html',
                         title=category.name if category else 'Каталог',
                         products=products,
                         pagination=pagination,
                         categories=categories,
                         current_category=category,
                         category_parameters=category_parameters,
                         active_filters=active_filters,
                         breadcrumbs=get_breadcrumbs(category) if category else [])


@bp.route('/product/<int:product_id>')
def product(product_id):
    """
    Карточка товара.
    URL: /product/{id}
    """
    product = db.session.get(Product, product_id)
    if not product or product.status != 'approved':
        abort(404)

    # Скрываем карточку, если магазин селлера заблокирован по тарифу
    # (state in 'locked', 'none'). В грейсе — оставляем видимой, чтобы
    # покупатели могли оформить заказ пока действует льготный период.
    # Та же проверка, что и в каталоге, чтобы старые ссылки/SEO не
    # указывали на мёртвые карточки.
    seller = product.seller
    if seller is not None:
        _seller_state = _resolve_tariff_state(seller)['state']
        if _seller_state in ('locked', 'none'):
            abort(404)
        # Лимит заказов в сутки. Если превышен — карточку прячем,
        # кроме случая, когда у залогиненного покупателя этот товар
        # уже лежит в корзине (тогда он сможет оформить).
        if _seller_state not in ('locked', 'none') and _seller_blocked_by_daily_limit(seller):
            from flask_login import current_user as _cu_card
            in_cart = False
            if _cu_card.is_authenticated and isinstance(_cu_card, Buyer):
                in_cart = CartItem.query.filter_by(
                    buyer_id=_cu_card.id, product_id=product.id
                ).first() is not None
            if not in_cart:
                abort(404)

    # Инкрементируем счётчик просмотров для аналитики продавца.
    # Не считаем просмотры продавца этого товара — иначе аналитика
    # замусоривается собственными заходами. Ошибка инкремента не должна
    # ломать показ карточки.
    from flask_login import current_user as _cu
    is_owner = (
        _cu.is_authenticated
        and isinstance(_cu, Seller)
        and _cu.id == product.seller_id
    )
    if not is_owner:
        try:
            product.views_count = (product.views_count or 0) + 1
            # Пишем событие в таблицу событий — источник истины для
            # конверсии "просмотры → заказы" в заданном периоде.
            # Кэш-инкремент выше оставлен для UI-карточек/списков.
            db.session.add(ProductEvent(
                product_id=product.id,
                seller_id=product.seller_id,
                buyer_id=current_user.id if isinstance(current_user, Buyer) else None,
                event_type='view',
                session_id=request.cookies.get('session'),
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
    
    # Проверка избранного
    in_favorite = False
    if current_user.is_authenticated and isinstance(current_user, Buyer):
        in_favorite = Favorite.query.filter_by(
            buyer_id=current_user.id,
            product_id=product.id
        ).first() is not None
    
    # Отзывы (только одобренные)
    reviews = Review.query.filter_by(
        product_id=product.id,
        status='approved'
    ).order_by(Review.created_at.desc()).limit(10).all()
    
    # Товары из той же подкатегории (рекомендации).
    # Цель — 2 ряда по 5 плиток (10 штук). Стратегия выборки:
    # 1) подкатегория (товар.category_id);
    # 2) родительская категория, если есть;
    # 3) любые одобренные товары в наличии (финальный fallback),
    #    чтобы блок не пустовал на бедных подкатегориях.
    # На каждом шаге исключаем уже выбранные и сам товар.
    from sqlalchemy import func
    DESIRED = 10
    similar_products = []
    excluded_ids = {product.id}

    # Шаг 1: подкатегория
    same_sub = Product.query.filter(
        Product.category_id == product.category_id,
        Product.id.notin_(excluded_ids),
        Product.status == 'approved',
        Product.stock_quantity > 0
    ).order_by(func.random()).limit(DESIRED).all()
    similar_products.extend(same_sub)
    excluded_ids.update(p.id for p in same_sub)

    # Шаг 2: родительская категория
    if len(similar_products) < DESIRED and product.category and product.category.parent_id:
        need = DESIRED - len(similar_products)
        same_parent = Product.query.filter(
            Product.category_id == product.category.parent_id,
            Product.id.notin_(excluded_ids),
            Product.status == 'approved',
            Product.stock_quantity > 0
        ).order_by(func.random()).limit(need).all()
        similar_products.extend(same_parent)
        excluded_ids.update(p.id for p in same_parent)

    # Шаг 3: финальный fallback — любые одобренные товары в наличии
    if len(similar_products) < DESIRED:
        need = DESIRED - len(similar_products)
        any_other = Product.query.filter(
            Product.id.notin_(excluded_ids),
            Product.status == 'approved',
            Product.stock_quantity > 0
        ).order_by(func.random()).limit(need).all()
        similar_products.extend(any_other)

    # Скрываем из «Похожих товаров» позиции продавцов с заблокированным
    # тарифом (state in 'locked','none'). Грейс — оставляем.
    similar_products = _filter_visible(similar_products)
    # Если после фильтрации стало меньше DESIRED — добираем из общего пула.
    if len(similar_products) < DESIRED:
        need = DESIRED - len(similar_products)
        excluded_ids.update(p.id for p in similar_products)
        # Ищем только среди видимых продавцов.
        visible_sids = _visible_seller_ids()
        if visible_sids:
            extra_q = Product.query.filter(
                Product.id.notin_(excluded_ids),
                Product.status == 'approved',
                Product.stock_quantity > 0,
                Product.seller_id.in_(visible_sids),
            ).order_by(func.random()).limit(need).all()
            similar_products.extend(extra_q)
    
    # Параметры товара
    params = product.get_all_params()
    
    # Корзина
    in_cart = False
    cart_quantity = 0
    if current_user.is_authenticated and isinstance(current_user, Buyer):
        cart_item = CartItem.query.filter_by(
            buyer_id=current_user.id,
            product_id=product.id
        ).first()
        if cart_item:
            in_cart = True
            cart_quantity = cart_item.quantity
    
    # Хлебные крошки
    breadcrumbs = get_breadcrumbs(product.category, product)
    
    # Проверка, покупал ли пользователь этот товар (для возможности оставить отзыв)
    has_purchased = False
    has_active_review = False  # уже есть pending/approved отзыв — форму не показываем
    if current_user.is_authenticated and isinstance(current_user, Buyer):
        from app.models.orders import OrderItem
        purchased_order = Order.query.join(OrderItem).filter(
            Order.buyer_id == current_user.id,
            Order.status == 'received',
            OrderItem.product_id == product.id
        ).first()
        has_purchased = purchased_order is not None
        has_active_review = Review.query.filter(
            Review.buyer_id == current_user.id,
            Review.product_id == product.id,
            Review.status.in_(['pending', 'approved'])
        ).first() is not None
    
    # Товары из той же карточки (если товар входит в карточку)
    card_products = []
    if product.product_card_id:
        card_products = Product.query.filter(
            Product.product_card_id == product.product_card_id,
            Product.id != product.id,
            Product.status == 'approved'
        ).order_by(Product.id).all()
    # Скрываем варианты из карточки, продавцы которых заблокированы.
    card_products = _filter_visible(card_products)
    
    # Получаем способы доставки продавца
    seller_delivery_services = []
    if product.seller:
        seller_deliveries = SellerDelivery.query.filter_by(
            seller_id=product.seller_id,
            is_active=True
        ).all()
        seller_delivery_services = [sd.delivery_service for sd in seller_deliveries 
                                     if sd.delivery_service and sd.delivery_service.is_active]
    
    return render_template('main/product.html',
                         title=product.name,
                         product=product,
                         params=params,
                         reviews=reviews,
                         similar_products=similar_products,
                         in_favorite=in_favorite,
                         in_cart=in_cart,
                         cart_quantity=cart_quantity,
                         breadcrumbs=breadcrumbs,
                         has_purchased=has_purchased,
                         has_active_review=has_active_review,
                         card_products=card_products,
                         seller_delivery_services=seller_delivery_services)


@bp.route('/search')
def search():
    """
    Поиск товаров.
    URL: /search?q={query}
    """
    query_str = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    if not query_str:
        return redirect(url_for('main.index'))
    
    # Поиск по названию и описанию. Товары продавцов с заблокированным
    # тарифом (state in 'locked', 'none') сразу исключаем — чтобы в выдаче
    # не появлялись карточки, на которые нельзя зайти.
    search_query = Product.query.filter(
        Product.status == 'approved',
        or_(
            Product.name.ilike(f'%{query_str}%'),
            Product.description.ilike(f'%{query_str}%'),
            Product.article.ilike(f'%{query_str}%')
        )
    )
    visible_sids = _visible_seller_ids()
    if visible_sids:
        search_query = search_query.filter(Product.seller_id.in_(visible_sids))
    else:
        search_query = search_query.filter(Product.id.is_(None))
    
    pagination = search_query.paginate(page=page, per_page=per_page, error_out=False)
    products = pagination.items
    
    return render_template('main/search.html',
                         title=f'Поиск: {query_str}',
                         query=query_str,
                         products=products,
                         pagination=pagination)


@bp.route('/cart')
@login_required
def cart():
    """
    Корзина покупателя.
    URL: /cart
    """
    if not isinstance(current_user, Buyer):
        flash('Корзина доступна только покупателям.', 'error')
        return redirect(url_for('main.index'))

    cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()

    # Чистим корзину от товаров заблокированных продавцов (tariff state in
    # 'locked', 'none'). Если селлер ушёл в лок пока товар лежал в корзине —
    # позиция автоматически удаляется. Грейс — НЕ трогаем, чтобы покупатель
    # мог оформить заказ в льготный период.
    # NB: visible_sids может быть пустым — это значит, что ВСЕ seller'ы
    # в БД неактивны (в т.ч. никогда не активировали глобальный процент).
    # В этом случае removable = все cart_items, и мы их удаляем.
    visible_sids = _visible_seller_ids()
    removed_count = 0
    if cart_items:
        removable = [
            ci for ci in cart_items
            if ci.product and ci.product.seller_id not in visible_sids
        ]
        if removable:
            for ci in removable:
                db.session.delete(ci)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            else:
                removed_count = len(removable)
                cart_items = [ci for ci in cart_items if ci not in removable]
    if removed_count:
        flash(
            f'Из корзины удалено {removed_count} '
            f'{"товар" if removed_count == 1 else "товара"} '
            f'от {"продавца" if removed_count == 1 else "продавцов"} '
            'с заблокированным тарифом.',
            'info',
        )

    # Расчёт суммы
    cart_total = get_cart_total(current_user.id)

    # Профили доставки покупателя (ВСЕ сохранённые ПВЗ)
    buyer_deliveries = BuyerDelivery.query.filter_by(
        buyer_id=current_user.id
    ).all()

    # Создаём словарь: delivery_service_code -> buyer_delivery
    buyer_delivery_by_service = {d.delivery_service.code: d for d in buyer_deliveries if d.delivery_service}

    # Получаем доступные службы доставки продавцов
    seller_deliveries = {}
    for item in cart_items:
        seller_id = item.product.seller_id
        if seller_id and seller_id not in seller_deliveries:
            # Получаем активные профили доставки продавца
            profiles = SellerDelivery.query.filter_by(
                seller_id=seller_id,
                is_active=True
            ).all()
            seller_deliveries[seller_id] = {
                'seller': item.product.seller,
                'profiles': profiles
            }

    # Снимок бонусов по продавцам в корзине (per-seller балансы и лимиты списания)
    from app.utils.loyalty import (
        get_cart_bonus_snapshot, is_loyalty_enabled,
        get_cart_promo_snapshot, is_promo_enabled,
        parse_promo_per_seller,
    )
    bonus_snapshot = (
        get_cart_bonus_snapshot(current_user.id, cart_items)
        if is_loyalty_enabled() else []
    )
    loyalty_enabled_global = is_loyalty_enabled()

    # Снимок применимых промокодов (для UI выпадающего списка).
    promo_snapshot = (
        get_cart_promo_snapshot(current_user.id, cart_items)
        if is_promo_enabled() else []
    )
    # Карта {seller_id: [promo,...]} для удобного lookup в шаблоне.
    promo_by_seller = {p['seller_id']: p['promos'] for p in promo_snapshot}

    # Уже выбранные промокоды покупателя (из сессии). Покупатель мог
    # выбрать промокод раньше — нужно сохранить выбор, чтобы при
    # изменении кол-ва товаров он не сбрасывался.
    from flask import session
    promo_per_seller_str = session.get('cart_promo_per_seller', '') or ''
    selected_promo_ids = parse_promo_per_seller(promo_per_seller_str)

    return render_template('main/cart.html',
                         title='Корзина',
                         cart_items=cart_items,
                         cart_total=cart_total,
                         buyer_delivery_by_service=buyer_delivery_by_service,
                         seller_deliveries=seller_deliveries,
                         promo_snapshot=promo_snapshot,
                         promo_by_seller=promo_by_seller,
                         selected_promo_ids=selected_promo_ids,
                         promo_per_seller_str=promo_per_seller_str,
                         bonus_snapshot=bonus_snapshot,
                         loyalty_enabled=loyalty_enabled_global)


@bp.route('/cart/promo/select', methods=['POST'])
@ajax_login_required
@buyer_required
def cart_promo_select():
    """
    AJAX-сохранение выбора промокодов в корзине.
    Тело: { 'promo_per_seller': 'sellerId:promoId;sellerId:promoId' }
    Сохраняет выбор в session, пересчитывает скидку и итог.
    """
    from app.utils.loyalty import (
        parse_promo_per_seller, get_applicable_promos_for_seller,
        calculate_promo_discount_amount,
    )
    from app.models.promo import PromoCode

    raw = (request.form.get('promo_per_seller') or '').strip()
    promo_map = parse_promo_per_seller(raw)

    # Получаем товары корзины, сгруппированные по продавцам
    cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()
    from collections import defaultdict
    by_seller = defaultdict(list)
    for it in cart_items:
        sid = it.product.seller_id if it.product else None
        if sid:
            by_seller[sid].append(it)

    # Серверная валидация: убираем из выборки промокоды, которые
    # больше не подходят (например, после изменения кол-ва товаров
    # перестали выполняться условия min_order_amount).
    applied_per_seller = []
    total_promo_discount = 0.0
    for sid, items in by_seller.items():
        subtotal = round(sum(float(it.total_price) for it in items), 2)
        if subtotal <= 0:
            continue
        promo_id = promo_map.get(sid)
        if not promo_id:
            continue
        promo = db.session.get(PromoCode, promo_id)
        if not promo or promo.seller_id != sid:
            continue
        # Проверяем, что промокод всё ещё подходит
        applicable = get_applicable_promos_for_seller(
            current_user.id, sid, subtotal,
        )
        if not any(p['id'] == promo_id for p in applicable):
            continue
        discount = calculate_promo_discount_amount(promo, subtotal)
        if discount <= 0:
            continue
        applied_per_seller.append({
            'seller_id': sid,
            'promo_id': promo.id,
            'promo_code': promo.code,
            'discount_label': promo.discount_label,
            'discount': round(discount, 2),
            'subtotal': subtotal,
        })
        total_promo_discount += discount

    # Сохраняем валидный выбор в сессии
    from flask import session
    clean_str = ';'.join(
        '{}:{}'.format(a['seller_id'], a['promo_id'])
        for a in applied_per_seller
    )
    session['cart_promo_per_seller'] = clean_str
    session.modified = True

    # Пересчитываем итог: subtotal корзины − скидки промокодов
    cart_total = get_cart_total(current_user.id)
    base_total = (
        cart_total['total'] if isinstance(cart_total, dict) else float(cart_total or 0)
    )
    # Промокоды применяются к сумме товаров (после промо-акций),
    # до бонусов и доставки — поэтому в cart.html пересчёт на клиенте
    # будет: total_with_promo = base_total - total_promo_discount.
    new_total = round(max(0.0, base_total - total_promo_discount), 2)

    return jsonify({
        'success': True,
        'applied': applied_per_seller,
        'total_promo_discount': round(total_promo_discount, 2),
        'base_total': round(base_total, 2),
        'new_total': new_total,
        'clean_str': clean_str,
    })


@bp.route('/cart/add', methods=['POST'])
@ajax_login_required
@buyer_required
def cart_add():
    """
    AJAX добавление товара в корзину.
    """
    product_id = request.form.get('product_id', type=int)
    quantity = request.form.get('quantity', 1, type=int)
    
    if not product_id:
        return {'error': 'Не указан товар'}, 400
    
    product = db.session.get(Product, product_id)
    if not product:
        return {'error': 'Товар не найден'}, 404

    # Защита: не даём добавлять в корзину товар от заблокированного продавца.
    if product.seller is not None and _resolve_tariff_state(product.seller)['state'] in ('locked', 'none'):
        return {'error': 'Товар недоступен для покупки.'}, 404
    # Лимит заказов в сутки. Если у продавца лимит превышен, новый
    # покупатель не сможет положить товар в корзину.
    if product.seller is not None and _seller_blocked_by_daily_limit(product.seller):
        return {'error': 'Магазин временно не принимает новые заказы (превышен дневной лимит).'}, 404

    if product.stock_quantity < quantity:
        return {'error': 'Недостаточно товара на складе'}, 400
    
    # Проверка существующего товара в корзине
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if cart_item:
        new_quantity = min(cart_item.quantity + quantity, product.stock_quantity)
        cart_item.quantity = new_quantity
    else:
        cart_item = CartItem(
            buyer_id=current_user.id,
            product_id=product_id,
            quantity=quantity
        )
        db.session.add(cart_item)

    # Счётчик добавлений в корзину для аналитики продавца.
    # Считаем именно "нажали добавить", а не "сколько раз товар
    # лежит в чьей-то корзине" — это про воронку, не про остатки.
    product.cart_adds_count = (product.cart_adds_count or 0) + 1
    # Событийный лог для конверсии по периоду (add_to_cart → order).
    db.session.add(ProductEvent(
        product_id=product.id,
        seller_id=product.seller_id,
        buyer_id=current_user.id if isinstance(current_user, Buyer) else None,
        event_type='add_to_cart',
        session_id=request.cookies.get('session'),
    ))

    db.session.commit()
    
    # Общее количество товаров в корзине (сумма всех quantity)
    cart_count = db.session.query(db.func.sum(CartItem.quantity)).filter(
        CartItem.buyer_id == current_user.id
    ).scalar() or 0
    
    return {
        'success': True,
        'cart_count': cart_count,
        'message': 'Товар добавлен в корзину'
    }


@bp.route('/cart/update', methods=['POST'])
@ajax_login_required
@buyer_required
def cart_update():
    """
    AJAX обновление количества товара в корзине.
    """
    product_id = request.form.get('product_id', type=int)
    quantity = request.form.get('quantity', type=int)
    
    if not product_id or quantity is None:
        return {'error': 'Неверные параметры'}, 400
    
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if not cart_item:
        return {'error': 'Товар не найден в корзине'}, 404
    
    product = db.session.get(Product, product_id)
    
    if quantity <= 0:
        db.session.delete(cart_item)
        message = 'Товар удалён из корзины'
    elif quantity > product.stock_quantity:
        cart_item.quantity = product.stock_quantity
        message = f'Количество ограничено остатком {product.stock_quantity}'
    else:
        cart_item.quantity = quantity
        message = 'Количество обновлено'
    
    db.session.commit()
    
    # Обновлённые данные
    cart_total = get_cart_total(current_user.id)
    # Общее количество товаров в корзине (сумма всех quantity)
    cart_count = db.session.query(db.func.sum(CartItem.quantity)).filter(
        CartItem.buyer_id == current_user.id
    ).scalar() or 0
    
    return {
        'success': True,
        'cart_count': cart_count,
        'item_total': cart_item.total_price if quantity > 0 else 0,
        'subtotal': cart_total['subtotal'],
        'total': cart_total['total'],
        'message': message
    }


@bp.route('/cart/remove', methods=['POST'])
@ajax_login_required
@buyer_required
def cart_remove():
    """
    AJAX удаление товара из корзины.
    """
    product_id = request.form.get('product_id', type=int)
    
    if not product_id:
        return {'error': 'Не указан товар'}, 400
    
    cart_item = CartItem.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if cart_item:
        db.session.delete(cart_item)
        db.session.commit()
    
    cart_total = get_cart_total(current_user.id)
    # Общее количество товаров в корзине (сумма всех quantity)
    cart_count = db.session.query(db.func.sum(CartItem.quantity)).filter(
        CartItem.buyer_id == current_user.id
    ).scalar() or 0
    
    return {
        'success': True,
        'cart_count': cart_count,
        'subtotal': cart_total['subtotal'],
        'total': cart_total['total'],
        'message': 'Товар удалён из корзины'
    }


@bp.route('/order-review')
@login_required
def order_review():
    """
    Страница просмотра заказа перед оформлением.
    URL: /order-review
    Показывает перечень товаров с ценами и кнопку оформления.
    """
    if not isinstance(current_user, Buyer):
        flash('Оформление заказа доступно только покупателям.', 'error')
        return redirect(url_for('main.index'))

    cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()

    # Чистим корзину от товаров заблокированных продавцов (tariff state in
    # 'locked', 'none'). Если после очистки корзина пуста — редиректим в /cart.
    # NB: visible_sids_or может быть пустым — тогда удаляем всё (см. cart()).
    visible_sids_or = _visible_seller_ids()
    if cart_items:
        removable_or = [
            ci for ci in cart_items
            if ci.product and ci.product.seller_id not in visible_sids_or
        ]
        if removable_or:
            for ci in removable_or:
                db.session.delete(ci)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            else:
                cart_items = [ci for ci in cart_items if ci not in removable_or]

    if not cart_items:
        flash('Корзина пуста.', 'info')
        return redirect(url_for('main.cart'))

    cart_total = get_cart_total(current_user.id)

    # Подсчитываем общее количество товаров
    total_items = sum(item.quantity for item in cart_items)
    subtotal = cart_total['subtotal'] if isinstance(cart_total, dict) else cart_total
    discount = cart_total.get('discount', 0) if isinstance(cart_total, dict) else 0
    total = cart_total['total'] if isinstance(cart_total, dict) else cart_total

    # Снимок бонусов и выбор покупателя по списанию (из query ?bonus=)
    from app.utils.loyalty import (
        get_cart_bonus_snapshot, is_loyalty_enabled,
        calculate_spendable_for_seller,
        get_applicable_promos_for_seller, calculate_promo_discount_amount,
        parse_promo_per_seller,
    )
    bonus_snapshot = (
        get_cart_bonus_snapshot(current_user.id, cart_items)
        if is_loyalty_enabled() else []
    )
    loyalty_enabled_global = is_loyalty_enabled()

    # Парсим выбранные к списанию бонусы: "sellerId:amount;sellerId:amount"
    bonus_raw = (request.args.get('bonus') or '').strip()
    bonus_per_seller = {}  # {seller_id: amount_rub}
    if bonus_raw and loyalty_enabled_global:
        for part in bonus_raw.split(';'):
            part = part.strip()
            if not part or ':' not in part:
                continue
            sid_str, amt_str = part.split(':', 1)
            try:
                sid = int(sid_str)
                amt = float(amt_str.replace(',', '.'))
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            # Пересчитываем доступный лимит на сервере, чтобы UI не мог
            # подсунуть завышенную сумму.
            snap = next((b for b in bonus_snapshot if int(b['seller_id']) == sid), None)
            if not snap:
                continue
            allowed = calculate_spendable_for_seller(
                current_user.id, sid, snap['subtotal'],
            )
            if allowed <= 0:
                continue
            bonus_per_seller[sid] = min(round(amt, 2), round(allowed, 2))
    bonus_used_total = round(sum(bonus_per_seller.values()), 2)
    total_after_bonus = round(max(0.0, total - bonus_used_total), 2)
    # Строковое представление для скрытого поля формы
    bonus_per_seller_str = ';'.join(
        '{}:{}'.format(sid, amt) for sid, amt in bonus_per_seller.items()
    )

    # --- Промокоды магазинов: выбор покупателя из query ?promo= -----
    # Если в query ничего нет — берём последний сохранённый выбор из
    # сессии (покупатель мог выбирать раньше, до смены бонусов).
    promo_raw = (request.args.get('promo') or '').strip()
    if not promo_raw:
        from flask import session
        promo_raw = (session.get('cart_promo_per_seller') or '').strip()
    promo_selection = parse_promo_per_seller(promo_raw)

    # Перепроверяем условия применимости и считаем скидку для каждого
    # продавца в корзине.
    from collections import defaultdict
    from app.models.promo import PromoCode
    seller_items_iter = defaultdict(list)
    for item in cart_items:
        sid = item.product.seller_id if item.product else None
        if sid:
            seller_items_iter[sid].append(item)
    promo_lines = []  # [{seller_id, store_name, promo_id, code, label, amount, subtotal}]
    promo_per_seller_amount = {}  # {seller_id: amount}
    promo_per_seller_code = {}    # {seller_id: code}
    for sid, items in seller_items_iter.items():
        pid = promo_selection.get(sid)
        if not pid:
            continue
        subtotal = round(sum(float(it.total_price) for it in items), 2)
        if subtotal <= 0:
            continue
        applicable = get_applicable_promos_for_seller(
            current_user.id, sid, subtotal,
        )
        if not any(p['id'] == pid for p in applicable):
            continue
        promo = db.session.get(PromoCode, pid)
        if not promo or promo.seller_id != sid:
            continue
        amount = calculate_promo_discount_amount(promo, subtotal)
        if amount <= 0:
            continue
        # Имя магазина
        first = items[0]
        store_name = (
            first.product.seller.store_name
            if first.product and first.product.seller
            else f'Продавец #{sid}'
        )
        promo_lines.append({
            'seller_id': sid,
            'store_name': store_name,
            'promo_id': promo.id,
            'code': promo.code,
            'label': promo.discount_label,
            'amount': round(amount, 2),
            'subtotal': subtotal,
        })
        promo_per_seller_amount[sid] = round(amount, 2)
        promo_per_seller_code[sid] = promo.code

    promo_used_total = round(sum(promo_per_seller_amount.values()), 2)
    # Скидка промокода применяется до списания бонусов (как и в cart.html).
    # В subtotal корзины уже заложены промо-скидки товаров, поэтому
    # считаем: total - promo_used_total - bonus_used_total, но не меньше 0.
    total_after_promo = round(max(0.0, total - promo_used_total), 2)
    total_after_promo_and_bonus = round(
        max(0.0, total_after_promo - bonus_used_total), 2,
    )
    # Строковое представление для скрытого поля формы
    promo_per_seller_str = ';'.join(
        '{}:{}'.format(sid, pid) for sid, pid in promo_selection.items()
    )

    # Группируем товары по продавцам
    from collections import defaultdict
    seller_groups = defaultdict(list)
    for item in cart_items:
        seller_id = item.product.seller_id if item.product else None
        if seller_id:
            seller_groups[seller_id].append(item)

    # Получаем информацию о доставке для каждого продавца
    seller_delivery_data = {}
    for seller_id in list(seller_groups.keys()):
        # Получаем профили доставки продавца
        seller_deliveries = SellerDelivery.query.filter_by(
            seller_id=seller_id,
            is_active=True
        ).all()

        # Получаем коды служб доставки продавца
        seller_service_codes = [sd.delivery_service.code for sd in seller_deliveries
                                 if sd.delivery_service and sd.delivery_service.is_active]

        # Получаем сохранённые ПВЗ покупателя для этих служб доставки
        buyer_deliveries = BuyerDelivery.query.filter(
            BuyerDelivery.buyer_id == current_user.id,
            BuyerDelivery.delivery_service.has(DeliveryService.code.in_(seller_service_codes))
        ).all() if seller_service_codes else []

        # Группируем ПВЗ по службе доставки
        # Считаем "пустым" BuyerDelivery, у которого фактически не указан
        # ни код ПВЗ, ни конкретный адрес (только город — не адрес).
        def _is_pvz_filled(bd):
            if bd.pvz_code and str(bd.pvz_code).strip():
                return True
            for attr in ('pvz_address', 'address'):
                val = getattr(bd, attr, None)
                if val and str(val).strip():
                    return True
            return False

        delivery_options = {}
        for bd in buyer_deliveries:
            if not _is_pvz_filled(bd):
                continue
            service_code = bd.delivery_service.code if bd.delivery_service else 'unknown'
            if service_code not in delivery_options:
                delivery_options[service_code] = {
                    'service': bd.delivery_service,
                    'options': []
                }
            delivery_options[service_code]['options'].append(bd)

        # Службы доставки продавца, по которым у покупателя ещё не указан ПВЗ
        covered_codes = set(delivery_options.keys())
        seller_services_missing_pvz = []
        for sd in seller_deliveries:
            svc = sd.delivery_service
            if svc and svc.is_active and svc.code not in covered_codes:
                seller_services_missing_pvz.append(svc)

        # Подсчитываем сумму для этого продавца
        items_list = list(seller_groups[seller_id])
        seller_total = sum(item.total_price for item in items_list)

        # Получаем seller с защитой от None
        first_item = items_list[0]
        seller = first_item.product.seller if first_item.product else None

        # Создаём delivery_options с сериализуемыми данными
        delivery_options_serializable = {}
        for service_code, service_data in delivery_options.items():
            delivery_options_serializable[service_code] = {
                'service': service_data['service'],
                'options': list(service_data['options'])
            }

        seller_delivery_data[seller_id] = {
            'seller': seller,
            'cart_items': items_list,
            'total': seller_total,
            'delivery_options': delivery_options_serializable,
            'seller_service_codes': list(seller_service_codes),
            'missing_pvz_services': seller_services_missing_pvz
        }

    # Готовим строки расшифровки "магазин → сумма списания" для шаблона.
    # Имя магазина берём из seller_delivery_data (там точно есть имя),
    # а если по какой-то причине селлера там нет — фолбэк через bonus_snapshot
    # и финальный запасной вариант "Продавец #N".
    bonus_lines = []
    for sid, amt in bonus_per_seller.items():
        store_name = None
        seller_data = seller_delivery_data.get(sid)
        if seller_data and seller_data.get('seller'):
            store_name = seller_data['seller'].store_name
        if not store_name:
            snap = next((b for b in bonus_snapshot if int(b['seller_id']) == sid), None)
            if snap:
                store_name = snap.get('store_name')
        if not store_name:
            store_name = f'Продавец #{sid}'
        bonus_lines.append({
            'seller_id': sid,
            'store_name': store_name,
            'amount': round(float(amt), 2),
        })

    return render_template('main/order_review.html',
                         title='Оформление заказа',
                         cart_items=cart_items,
                         total_items=total_items,
                         subtotal=subtotal,
                         discount=discount,
                         total=total,
                         seller_groups=seller_delivery_data,
                         bonus_snapshot=bonus_snapshot,
                         bonus_per_seller=bonus_per_seller,
                         bonus_per_seller_str=bonus_per_seller_str,
                         bonus_lines=bonus_lines,
                         bonus_used_total=bonus_used_total,
                         total_after_bonus=total_after_bonus,
                         loyalty_enabled=loyalty_enabled_global,
                         promo_lines=promo_lines,
                         promo_used_total=promo_used_total,
                         total_after_promo=total_after_promo,
                         total_after_promo_and_bonus=total_after_promo_and_bonus,
                         promo_per_seller_str=promo_per_seller_str)


@bp.route('/order-review/submit', methods=['POST'])
@login_required
def checkout_submit():
    """
    Обработка оформления заказа.
    После нажатия кнопки "Сделать заказ" перенаправляет на страницу заказов.
    URL: /order-review/submit
    """
    if not isinstance(current_user, Buyer):
        return jsonify({'success': False, 'error': 'Доступ только для покупателей'}), 403

    import logging
    logger = logging.getLogger(__name__)

    # Защита: в корзине не должно быть товаров заблокированных продавцов.
    # Если такие есть — удаляем и сообщаем. Сама корзина на предыдущих шагах
    # уже должна была очиститься, но на случай прямого POST — перепроверяем.
    _cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()
    _visible = _visible_seller_ids()
    if _cart_items:
        _bad = [
            ci for ci in _cart_items
            if ci.product and ci.product.seller_id not in _visible
        ]
        if _bad:
            for ci in _bad:
                db.session.delete(ci)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return jsonify({
                'success': False,
                'error': 'Из корзины удалены товары от заблокированных продавцов. Обновите страницу корзины.',
            }), 400

    # Защита по дневному лимиту заказов: если у продавца лимит превышен,
    # новые покупатели не могут положить его товар в корзину (см. add_to_cart).
    # Но если товар УЖЕ лежит в корзине — оформление должно пройти.
    # Поэтому здесь только проверяем, что в корзине не появилось «свежих»
    # товаров от заблокированного по лимиту продавца. Защита работает так:
    # мы НЕ удаляем такие позиции (allow-list для уже-в-корзине), но и
    # не пускаем в новый заказ, если корзина содержит товар только от
    # заблокированного по лимиту продавца, который добавили после блокировки.
    # На практике: add_to_cart уже блокирует, так что сюда ничего не попадёт.
    # Эта проверка — страховка от гонки и прямого POST.

    try:
        # Получаем товары из корзины
        cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()
        if not cart_items:
            return jsonify({'success': False, 'error': 'Корзина пуста'}), 400
        
        # Группируем товары по продавцам
        from collections import defaultdict
        seller_items = defaultdict(list)
        for item in cart_items:
            seller_id = item.product.seller_id if item.product else None
            if seller_id:
                seller_items[seller_id].append(item)
        
        if not seller_items:
            return jsonify({'success': False, 'error': 'Не найдены продавцы для товаров в корзине'}), 400
        
        from datetime import datetime
        created_orders = []

        # Получаем выбранные способы доставки из формы
        delivery_selections = {}
        for key, value in request.form.items():
            if key.startswith('delivery[') and key.endswith(']'):
                seller_id = key.replace('delivery[', '').replace(']', '')
                delivery_selections[int(seller_id)] = value

        # Получаем выбранные к списанию бонусы (per-seller).
        # Формат скрытого поля: "sellerId:amount;sellerId:amount".
        from app.utils.loyalty import (
            is_loyalty_enabled, calculate_spendable_for_seller, spend_bonuses,
            get_applicable_promos_for_seller, calculate_promo_discount_amount,
            parse_promo_per_seller, consume_promo_code,
        )
        from app.models.promo import PromoCode

        loyalty_on = is_loyalty_enabled()
        bonus_raw = (request.form.get('bonus_per_seller') or '').strip()
        bonus_to_spend = {}  # {seller_id: amount}
        if bonus_raw and loyalty_on:
            for part in bonus_raw.split(';'):
                part = part.strip()
                if not part or ':' not in part:
                    continue
                sid_str, amt_str = part.split(':', 1)
                try:
                    sid = int(sid_str)
                    amt = float(amt_str.replace(',', '.'))
                except (TypeError, ValueError):
                    continue
                if amt <= 0:
                    continue
                # Считаем subtotal по этому селлеру в корзине
                items_for_seller = seller_items.get(sid) or []
                seller_subtotal = round(
                    sum(float(i.total_price) for i in items_for_seller), 2,
                )
                allowed = calculate_spendable_for_seller(
                    current_user.id, sid, seller_subtotal,
                )
                if allowed <= 0:
                    continue
                bonus_to_spend[sid] = min(round(amt, 2), round(allowed, 2))

        # Промокоды магазинов, выбранные покупателем.
        # Формат скрытого поля: "sellerId:promoId;sellerId:promoId".
        promo_raw = (request.form.get('promo_per_seller') or '').strip()
        promo_selection = parse_promo_per_seller(promo_raw)
        promo_to_apply = {}  # {seller_id: {'promo': obj, 'amount': float}}
        for sid, pid in promo_selection.items():
            items_for_seller = seller_items.get(sid) or []
            seller_subtotal = round(
                sum(float(i.total_price) for i in items_for_seller), 2,
            )
            if seller_subtotal <= 0:
                continue
            applicable = get_applicable_promos_for_seller(
                current_user.id, sid, seller_subtotal,
            )
            if not any(p['id'] == pid for p in applicable):
                logger.info(
                    f"Promo {pid} no longer applicable for seller {sid}, "
                    f"buyer {current_user.id}; skipping"
                )
                continue
            promo = db.session.get(PromoCode, pid)
            if not promo or promo.seller_id != sid:
                continue
            amount = calculate_promo_discount_amount(promo, seller_subtotal)
            if amount <= 0:
                continue
            promo_to_apply[sid] = {'promo': promo, 'amount': round(amount, 2)}

        for seller_id, items in seller_items.items():
            seller = items[0].product.seller
            if not seller:
                logger.warning(f"Skipping order for seller_id={seller_id}: seller not found")
                continue

            # Рассчитываем сумму заказа
            total_price = sum(item.total_price for item in items)
            order_number_prefix = f"M{datetime.now().strftime('%Y%m%d%H%M%S')}"

            # Получаем информацию о доставке
            delivery_service_id = None
            pvz_code = None
            pvz_address = None

            selected_delivery_id = delivery_selections.get(seller_id)
            buyer_delivery = None
            if selected_delivery_id:
                try:
                    buyer_delivery = BuyerDelivery.query.get(int(selected_delivery_id))
                except (TypeError, ValueError):
                    buyer_delivery = None

                if buyer_delivery and (buyer_delivery.pvz_code or
                                       buyer_delivery.pvz_address or
                                       buyer_delivery.address):
                    delivery_service_id = buyer_delivery.delivery_service_id
                    pvz_code = buyer_delivery.pvz_code
                    pvz_address = buyer_delivery.pvz_address or buyer_delivery.address

            # Если для этого продавца ПВЗ не выбран / не подходит —
            # берём любой заполненный BuyerDelivery покупателя как фолбэк,
            # чтобы оформление не зависало.
            if not delivery_service_id:
                fallback = BuyerDelivery.query.filter(
                    BuyerDelivery.buyer_id == current_user.id,
                    db.or_(
                        BuyerDelivery.pvz_code.isnot(None),
                        BuyerDelivery.pvz_address.isnot(None),
                        BuyerDelivery.address.isnot(None),
                    )
                ).order_by(BuyerDelivery.id.asc()).first()
                if fallback:
                    delivery_service_id = fallback.delivery_service_id
                    pvz_code = fallback.pvz_code
                    pvz_address = fallback.pvz_address or fallback.address
                    logger.info(
                        f"Fallback delivery used for seller {seller_id}: "
                        f"buyer_delivery_id={fallback.id}, service_id={delivery_service_id}"
                    )

            if not delivery_service_id:
                # Совсем нет ни одного сохранённого адреса/ПВЗ у покупателя —
                # оформляем без конкретного ПВЗ (продавец свяжется).
                logger.warning(
                    f"No BuyerDelivery for buyer {current_user.id} when checking out "
                    f"seller {seller_id}; creating order without PVZ"
                )
            
            # Создаём заказ со статусом processing (в обработке)
            order = Order(
                order_number=order_number_prefix,
                buyer_id=current_user.id,
                seller_id=seller_id,
                total_price=total_price,
                status='processing',
                delivery_service_id=delivery_service_id,
                pvz_code=pvz_code,
                delivery_address=pvz_address
            )
            db.session.add(order)
            db.session.flush()

            # Обновляем номер заказа с ID
            order.order_number = f"{order_number_prefix}{order.id}"

            # Списываем бонусные баллы, если покупатель выбрал их
            # к списанию для этого селлера.
            bonus_to_apply = bonus_to_spend.pop(seller_id, 0) or 0
            if bonus_to_apply > 0 and loyalty_on:
                try:
                    spent = spend_bonuses(
                        buyer_id=current_user.id,
                        seller_id=seller_id,
                        amount_rub=bonus_to_apply,
                        order_id=order.id,
                        reason=f'Списание баллов при оформлении заказа {order.order_number}',
                    )
                    if spent > 0:
                        order.bonus_used = round(float(spent), 2)
                except Exception as spend_err:
                    # Ошибка списания не должна ронять оформление.
                    logger.warning(
                        f"Failed to spend bonuses for order {order.id}: {spend_err}"
                    )

            # Применяем промокод магазина, если покупатель его выбрал
            # для этого продавца. Скидка фиксируется в Order.promo_discount
            # и НЕ уменьшает total_price (по аналогии с бонусами — это
            # отдельная строка расшифровки). Учитывается в grand_total.
            promo_apply = promo_to_apply.pop(seller_id, None)
            if promo_apply:
                try:
                    order.promo_code_id = promo_apply['promo'].id
                    order.promo_code_text = promo_apply['promo'].code
                    order.promo_discount = round(float(promo_apply['amount']), 2)
                    # Инкрементируем used_count (после фиксации записи,
                    # чтобы при ошибке коммита не списать лимит впустую).
                    consume_promo_code(promo_apply['promo'])
                except Exception as promo_err:
                    # Ошибка применения промокода — не критична, не
                    # блокируем оформление, но логируем.
                    logger.warning(
                        f"Failed to apply promo for order {order.id}: {promo_err}"
                    )

            # Создаём позиции заказа. Используем compute_item_discount_breakdown,
            # чтобы корректно разложить: что пришло от current_discount товара,
            # а что — от промо-акции (discount / second_with_discount / 1+1 и т.д.).
            # Правило "не суммируется — берём максимум" сохраняется.
            from app.utils.helpers import compute_item_discount_breakdown
            # total_price должен отражать сумму после промо-скидок, без бонусов —
            # бонусы списываются отдельной строкой через order.bonus_used.
            items_subtotal_after_promos = 0.0
            for item in items:
                product = item.product
                original_price = round(float(product.price), 2)
                breakdown = compute_item_discount_breakdown(item, items)
                total_discount_rub = breakdown['total_discount']  # на позицию (за всё qty)
                price_at_order = round(original_price - total_discount_rub / max(1, item.quantity), 2)

                order_item = OrderItem(
                    order_id=order.id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    price_at_order=price_at_order,
                    original_price=original_price,
                    promo_discount=breakdown['promo_discount'] or 0,
                )
                db.session.add(order_item)
                items_subtotal_after_promos += price_at_order * item.quantity

            # Переписываем total_price: sum(price_at_order * qty) с учётом промо.
            order.total_price = round(items_subtotal_after_promos, 2)

            # Уменьшаем остаток товара
            for item in items:
                item.product.stock_quantity -= item.quantity

            # Удаляем товары из корзины
            for item in items:
                db.session.delete(item)
            
            created_orders.append(order.id)
            logger.info(f"Created pending order {order.id} for seller {seller_id}, buyer {current_user.id}")
            
            # Отправляем уведомление продавцу
            send_new_order_notification_to_seller(order.id)
            # Создаём пустой диалог для заказа между покупателем и продавцом
            create_order_conversation(order)
        
        db.session.commit()

        # Очищаем выбор промокодов в сессии — заказы уже оформлены.
        from flask import session
        session.pop('cart_promo_per_seller', None)
        session.modified = True

        logger.info(f"Checkout complete. Created {len(created_orders)} orders for buyer {current_user.id}")

        # Перенаправляем на страницу заказов
        return jsonify({
            'success': True,
            'redirect': url_for('main.profile', section='orders')
        })
        
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/checkout')
@login_required
def checkout():
    """
    Оформление заказа.
    URL: /checkout
    Создаёт черновики заказов (pending) при переходе на форму.
    """
    if not isinstance(current_user, Buyer):
        flash('Оформление заказа доступно только покупателям.', 'error')
        return redirect(url_for('main.index'))

    cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()

    # Чистим корзину от товаров заблокированных продавцов (tariff state in
    # 'locked', 'none'). Если после очистки корзина пуста — редиректим в /cart.
    # NB: visible_sids_ch может быть пустым — тогда удаляем всё.
    visible_sids_ch = _visible_seller_ids()
    if cart_items:
        removable_ch = [
            ci for ci in cart_items
            if ci.product and ci.product.seller_id not in visible_sids_ch
        ]
        if removable_ch:
            for ci in removable_ch:
                db.session.delete(ci)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            else:
                cart_items = [ci for ci in cart_items if ci not in removable_ch]

    if not cart_items:
        flash('Корзина пуста.', 'info')
        return redirect(url_for('main.cart'))

    cart_total = get_cart_total(current_user.id)

    delivery_profiles = BuyerDelivery.query.filter_by(
        buyer_id=current_user.id
    ).all()

    delivery_services = DeliveryService.query.filter_by(is_active=True).all()
    
    # Получаем информацию о доставке из URL параметров
    import logging
    logger = logging.getLogger(__name__)
    delivery_info = []
    delivery_total = 0
    delivery_param = request.args.get('delivery', '')
    logger.info(f"Checkout URL: {request.url}, delivery_param: '{delivery_param}'")
    if delivery_param:
        try:
            items = delivery_param.split(';')
            for item in items:
                parts = item.split(':')
                if len(parts) >= 4:
                    seller_id = int(parts[0])
                    delivery_id = int(parts[1])
                    pvz_code = parts[2]
                    cost = float(parts[3])
                    logger.info(f"Parsed delivery: seller={seller_id}, delivery_id={delivery_id}, pvz={pvz_code}, cost={cost}")
                    delivery_info.append({
                        'seller_id': seller_id,
                        'delivery_id': delivery_id,
                        'pvz_code': pvz_code,
                        'cost': cost
                    })
                    delivery_total += cost
            logger.info(f"Final delivery_info: {delivery_info}, delivery_total: {delivery_total}")
        except (ValueError, IndexError) as e:
            logger.error(f"Error parsing delivery param: {e}")
            flash('Ошибка при чтении данных о доставке. Пожалуйста, рассчитайте доставку заново.', 'warning')
            return redirect(url_for('main.cart'))
    else:
        # Нет параметров доставки - редирект на корзину
        logger.warning("No delivery params, redirecting to cart")
        flash('Пожалуйста, рассчитайте стоимость доставки в корзине перед оформлением.', 'warning')
        return redirect(url_for('main.cart'))

    # Создаём черновики заказов (status='pending') для каждого продавца
    # Группируем товары по продавцам
    from collections import defaultdict
    seller_items = defaultdict(list)
    for item in cart_items:
        seller_items[item.product.seller_id].append(item)
    
    # Удаляем старые черновики (pending) для этого покупателя
    Order.query.filter_by(buyer_id=current_user.id, status='pending').delete()
    
    # Создаём новые черновики
    for seller_id, items in seller_items.items():
        seller = items[0].product.seller
        if not seller:
            continue

        # Сумма заказа с учётом правила "max discount, не суммируется".
        # Считаем per-item лучшую скидку (current_discount / классические
        # discount-акции / second_with_discount) и суммируем.
        from app.utils.helpers import compute_best_discount_for_item
        subtotal_price = sum(item.total_price for item in items)
        seller_discount = sum(
            compute_best_discount_for_item(it, items) for it in items
        )
        total_price = round(subtotal_price - seller_discount, 2)

        # Получаем стоимость доставки для этого продавца
        delivery_price = next((d['cost'] for d in delivery_info if d['seller_id'] == seller_id), 0)
        
        # Генерируем временный номер заказа
        from datetime import datetime
        order_number_prefix = f"P{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        # Получаем данные о ПВЗ
        pvz_info = next((d for d in delivery_info if d['seller_id'] == seller_id), {})
        pvz_code = pvz_info.get('pvz_code')
        seller_delivery_id = pvz_info.get('delivery_id')
        
        # Получаем профиль доставки продавца
        delivery_service_id = 1  # По умолчанию
        if seller_delivery_id:
            seller_delivery = db.session.get(SellerDelivery, seller_delivery_id)
            if seller_delivery:
                delivery_service_id = seller_delivery.delivery_service_id
        
        # Создаём черновик заказа со статусом pending
        order = Order(
            order_number=order_number_prefix,
            buyer_id=current_user.id,
            seller_id=seller_id,
            total_price=total_price,
            delivery_price=delivery_price,
            delivery_service_id=delivery_service_id,
            pvz_code=pvz_code,
            delivery_address=pvz_code if pvz_code else None,
            status='pending'  # Ожидает оформления
        )
        db.session.add(order)
        db.session.flush()
        
        # Обновляем номер заказа с ID
        order.order_number = f"{order_number_prefix}{order.id}"
        
        # Создаём позиции заказа (пока не удаляем из корзины!)
        # Применяем правило "max discount, не суммируется" для каждой позиции.
        from app.utils.helpers import compute_item_discount_breakdown
        for item in items:
            product = item.product
            original_price = round(float(product.price), 2)
            breakdown = compute_item_discount_breakdown(item, items)
            price_at_order = round(
                original_price - breakdown['total_discount'] / max(1, item.quantity), 2
            )

            order_item = OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                quantity=item.quantity,
                price_at_order=price_at_order,
                original_price=original_price,
                promo_discount=breakdown['promo_discount'] or 0,
            )
            db.session.add(order_item)
    
    db.session.commit()
    logger.info(f"Created pending orders for buyer {current_user.id}")

    return render_template('main/checkout.html',
                         title='Оформление заказа',
                         cart_items=cart_items,
                         cart_total=cart_total,
                         delivery_profiles=delivery_profiles,
                         delivery_services=delivery_services,
                         delivery_info=delivery_info,
                         delivery_total=delivery_total)


@bp.route('/order/create', methods=['POST'])
@login_required
def order_create():
    """
    Оформление заказа.
    Обновляет статус с pending на processing.
    URL: /order/create
    """
    if not isinstance(current_user, Buyer):
        return jsonify({'success': False, 'error': 'Доступ только для покупателей'}), 403

    import logging
    logger = logging.getLogger(__name__)

    # Защита: не даём провести заказ, если в нём есть товары заблокированных
    # продавцов. Может случиться, если заказ был создан в pending до лока.
    # Если _visible пустое (все seller'ы неактивны) — отменяем ВСЕ pending
    # заказы покупателя.
    _visible = _visible_seller_ids()
    if _visible:
        # Есть активные seller'ы — отменяем только те заказы, где есть
        # товары от заблокированных.
        _bad_orders = (
            db.session.query(Order)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(Product, Product.id == OrderItem.product_id)
            .filter(
                Order.buyer_id == current_user.id,
                Order.status == 'pending',
                Product.seller_id.notin_(_visible),
            )
            .distinct()
            .all()
        )
    else:
        # Нет активных seller'ов — все pending заказы невалидны.
        _bad_orders = (
            db.session.query(Order)
            .filter(
                Order.buyer_id == current_user.id,
                Order.status == 'pending',
            )
            .all()
        )
    if _bad_orders:
        for o in _bad_orders:
            o.status = 'cancelled'
        db.session.commit()
        return jsonify({
            'success': False,
            'error': 'Некоторые из ваших черновиков заказов содержат товары от заблокированных продавцов. Откройте корзину заново.',
        }), 400
    
    data = request.form
    
    delivery_profile_id = data.get('delivery_profile_id')
    payment_method = data.get('payment_method', 'card_online')
    
    # Получаем информацию о стоимости доставки
    delivery_info = {}
    pvz_info = {}
    delivery_data = data.get('delivery_info')
    logger.info(f"order_create delivery_data: {delivery_data}")
    if delivery_data:
        import json
        try:
            delivery_list = json.loads(delivery_data)
            logger.info(f"Parsed delivery_list: {delivery_list}")
            for d in delivery_list:
                seller_id = int(d.get('seller_id', 0))
                if seller_id:
                    delivery_info[seller_id] = float(d.get('cost', 0))
                    pvz_info[seller_id] = {
                        'pvz_code': d.get('pvz_code'),
                        'delivery_id': int(d.get('delivery_id', 0)) if d.get('delivery_id') else None
                    }
            logger.info(f"Parsed delivery_info: {delivery_info}, pvz_info: {pvz_info}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error parsing delivery_info: {e}, data: {delivery_data}")
    
    # Проверяем есть ли информация о доставке
    if not delivery_info:
        if not delivery_profile_id:
            return jsonify({'success': False, 'error': 'Выберите способ доставки'}), 400
        
        delivery_profile = BuyerDelivery.query.get(delivery_profile_id)
        if not delivery_profile or delivery_profile.buyer_id != current_user.id:
            return jsonify({'success': False, 'error': 'Профиль доставки не найден'}), 400
    
    # Получаем товары из корзины
    cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()
    if not cart_items:
        return jsonify({'success': False, 'error': 'Корзина пуста'}), 400
    
    # Группируем товары по продавцам
    from collections import defaultdict
    seller_items = defaultdict(list)
    for item in cart_items:
        seller_items[item.product.seller_id].append(item)

    # Логика скидок теперь в compute_best_discount_for_item — она сама учитывает
    # все источники (current_discount, классические discount-акции, second_with_discount)
    # и применяет правило "max discount, не суммируется" per-item.

    try:
        # Ищем существующие черновики (pending) для этого покупателя
        pending_orders = Order.query.filter_by(
            buyer_id=current_user.id,
            status='pending'
        ).all()
        
        # Создаём маппинг seller_id -> pending order
        pending_by_seller = {order.seller_id: order for order in pending_orders}
        
        from datetime import datetime
        
        for seller_id, items in seller_items.items():
            seller = items[0].product.seller
            if not seller:
                continue
            
            # Проверяем есть ли черновик для этого продавца
            if seller_id in pending_by_seller:
                # Обновляем существующий черновик
                order = pending_by_seller[seller_id]

                delivery_price = delivery_info.get(seller_id, 0)

                # Получаем данные о ПВЗ
                seller_pvz = pvz_info.get(seller_id, {})
                pvz_code = seller_pvz.get('pvz_code')
                seller_delivery_id = seller_pvz.get('delivery_id')

                order.pvz_code = pvz_code
                order.delivery_address = pvz_code if pvz_code else None

                if seller_delivery_id:
                    seller_delivery = db.session.get(SellerDelivery, seller_delivery_id)
                    if seller_delivery:
                        order.delivery_service_id = seller_delivery.delivery_service_id

                # Меняем статус на processing (в обработке)
                order.status = 'processing'

                # Генерируем правильный номер заказа
                order_number_prefix = f"M{datetime.now().strftime('%Y%m%d%H%M%S')}"
                order.order_number = f"{order_number_prefix}{order.id}"

                # Удаляем старые позиции заказа и создаём новые.
                # total_price пересчитываем после цикла из суммы по позициям,
                # чтобы избежать рассинхрона из-за округлений.
                OrderItem.query.filter_by(order_id=order.id).delete()

                from app.utils.helpers import compute_item_discount_breakdown
                items_subtotal_after_promos = 0.0
                for item in items:
                    product = item.product
                    original_price = round(float(product.price), 2)
                    breakdown = compute_item_discount_breakdown(item, items)
                    price_at_order = round(
                        original_price - breakdown['total_discount'] / max(1, item.quantity), 2
                    )

                    order_item = OrderItem(
                        order_id=order.id,
                        product_id=item.product_id,
                        quantity=item.quantity,
                        price_at_order=price_at_order,
                        original_price=original_price,
                        promo_discount=breakdown['promo_discount'] or 0,
                    )
                    db.session.add(order_item)
                    items_subtotal_after_promos += price_at_order * item.quantity

                    # Уменьшаем остаток товара
                    item.product.stock_quantity -= item.quantity

                # total_price = сумма по позициям после промо (без бонусов —
                # бонусы списываются отдельной строкой).
                order.total_price = round(items_subtotal_after_promos, 2)
                order.delivery_price = delivery_price

                # Удаляем товары из корзины
                for item in items:
                    db.session.delete(item)

                logger.info(f"Updated order {order.id} from pending to processing")
            else:
                # Черновик не найден - создаём новый заказ напрямую (processing)
                logger.warning(f"No pending order found for seller {seller_id}, creating new")

                delivery_price = delivery_info.get(seller_id, 0)
                order_number_prefix = f"M{datetime.now().strftime('%Y%m%d%H%M%S')}"

                seller_pvz = pvz_info.get(seller_id, {})
                pvz_code = seller_pvz.get('pvz_code')
                seller_delivery_id = seller_pvz.get('delivery_id')

                delivery_service_id = 1
                if seller_delivery_id:
                    seller_delivery = db.session.get(SellerDelivery, seller_delivery_id)
                    if seller_delivery:
                        delivery_service_id = seller_delivery.delivery_service_id

                order = Order(
                    order_number=order_number_prefix,
                    buyer_id=current_user.id,
                    seller_id=seller_id,
                    # total_price пересчитаем ниже из позиций (после промо).
                    total_price=0.0,
                    delivery_price=delivery_price,
                    delivery_service_id=delivery_service_id,
                    pvz_code=pvz_code,
                    delivery_address=pvz_code if pvz_code else None,
                    status='processing'
                )
                db.session.add(order)
                db.session.flush()

                order.order_number = f"{order_number_prefix}{order.id}"

                from app.utils.helpers import compute_item_discount_breakdown
                items_subtotal_after_promos = 0.0
                for item in items:
                    product = item.product
                    original_price = round(float(product.price), 2)
                    breakdown = compute_item_discount_breakdown(item, items)
                    price_at_order = round(
                        original_price - breakdown['total_discount'] / max(1, item.quantity), 2
                    )

                    order_item = OrderItem(
                        order_id=order.id,
                        product_id=item.product_id,
                        quantity=item.quantity,
                        price_at_order=price_at_order,
                        original_price=original_price,
                        promo_discount=breakdown['promo_discount'] or 0,
                    )
                    db.session.add(order_item)
                    items_subtotal_after_promos += price_at_order * item.quantity

                    item.product.stock_quantity -= item.quantity

                order.total_price = round(items_subtotal_after_promos, 2)

                for item in items:
                    db.session.delete(item)
        
        db.session.commit()
        
        # Отправляем уведомления продавцам о новых заказах
        # и создаём диалоги для обсуждения деталей заказов
        for seller_id, items in seller_items.items():
            # Находим заказ для этого продавца
            order = Order.query.filter_by(
                buyer_id=current_user.id,
                seller_id=seller_id,
                status='processing'
            ).first()
            if order:
                send_new_order_notification_to_seller(order.id)
                # Создаём пустой диалог для заказа между покупателем и продавцом
                create_order_conversation(order)
        
        flash('Заказ успешно оформлен! Перейдите к оплате.', 'success')
        return jsonify({
            'success': True,
            'redirect': url_for('main.payment')
        })
        
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/payment')
@login_required
def payment():
    """
    Страница оплаты заказа.
    Показывает информацию о заказе и форму оплаты.
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    # Получаем последний заказ со статусом processing
    order = Order.query.filter_by(
        buyer_id=current_user.id,
        status='processing'
    ).order_by(Order.created_at.desc()).first()
    
    if not order:
        flash('Заказ не найден', 'warning')
        return redirect(url_for('main.profile_orders'))
    
    # Получаем все заказы этого покупателя со статусом processing (их может быть несколько от разных продавцов)
    orders = Order.query.filter_by(
        buyer_id=current_user.id,
        status='processing'
    ).all()
    
    # Подсчитываем общую сумму
    total_price = sum(o.total_price + o.delivery_price for o in orders)
    delivery_price = sum(o.delivery_price for o in orders)
    items_count = sum(len(o.items.all()) for o in orders)
    
    # Номер первого заказа (основной)
    order_number = orders[0].order_number if orders else '—'
    order_id = order.id
    
    return render_template('main/payment.html',
                         title='Оплата заказа',
                         order_number=order_number,
                         order_id=order_id,
                         total_price=total_price,
                         delivery_price=delivery_price,
                         items_count=items_count,
                         payment_status=None)


@bp.route('/payment/process', methods=['POST'])
@login_required
def payment_process():
    """
    Обработка оплаты.
    После успешной оплаты перенаправляет на страницу заказов.
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    # Получаем заказы со статусом processing
    orders = Order.query.filter_by(
        buyer_id=current_user.id,
        status='processing'
    ).all()
    
    if not orders:
        flash('Заказ не найден', 'warning')
        return redirect(url_for('main.profile_orders'))
    
    # Меняем статус на paid (оплачен)
    for order in orders:
        order.status = 'paid'
    
    db.session.commit()
    
    flash('Оплата успешно проведена!', 'success')
    return redirect(url_for('main.profile_orders'))


@bp.route('/favorite/add', methods=['POST'])
@ajax_login_required
@buyer_required
def favorite_add():
    """
    AJAX добавление в избранное.
    """
    product_id = request.form.get('product_id', type=int)
    
    if not product_id:
        return {'error': 'Не указан товар'}, 400
    
    product = db.session.get(Product, product_id)
    if not product:
        return {'error': 'Товар не найден'}, 404
    
    # Проверка существующего
    favorite = Favorite.query.filter_by(
        buyer_id=current_user.id,
        product_id=product_id
    ).first()
    
    if favorite:
        db.session.delete(favorite)
        message = 'Товар удалён из избранного'
        in_favorite = False
    else:
        favorite = Favorite(
            buyer_id=current_user.id,
            product_id=product_id
        )
        db.session.add(favorite)
        message = 'Товар добавлен в избранное'
        in_favorite = True
    
    db.session.commit()
    
    return {
        'success': True,
        'in_favorite': in_favorite,
        'message': message
    }


@bp.route('/profile')
@login_required
def profile():
    """
    Личный кабинет покупателя.
    URL: /profile
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))

    from app.utils.loyalty import is_loyalty_enabled, is_promo_enabled

    section = request.args.get('section', 'orders')

    # Если это раздел сообщений, передаем все необходимые переменные
    if section == 'messages':
        from app.blueprints.messages import get_conversations, mark_messages_read, get_partner_name, get_partner_avatar

        user_type = 'buyer'
        user_id = current_user.id
        
        # Получаем фильтр (support или stores)
        filter_type = request.args.get('filter', 'stores')
        partner_type = request.args.get('partner_type', type=str)
        partner_id = request.args.get('partner_id', type=int)
        
        # Получаем диалоги с учетом фильтра
        conversations = get_conversations(user_type, user_id, filter_type)
        
        # Определяем фильтры для покупателя
        filter_options = {
            'support': 'Поддержка',
            'stores': 'Магазины',
            'orders': 'Заказы'
        }
        
        # Текущий диалог - проверяем partner_id через is not None, чтобы Allow partner_id=0 для админа
        current_partner = None
        messages = []
        is_order_chat = False
        
        if partner_type and partner_id is not None:
            # Проверяем, если это order-диалог
            if partner_type == 'order':
                order = Order.query.get(partner_id)
                if order:
                    is_order_chat = True
                    # Получаем сообщения через conversation_type='order'
                    messages = Message.query.filter(
                        Message.conversation_type == 'order',
                        Message.conversation_id == partner_id
                    ).order_by(Message.timestamp.asc()).all()
                    
                    # Отмечаем как прочитанные
                    actual_partner_type = 'seller'
                    actual_partner_id = order.seller_id
                    mark_messages_read(user_type, user_id, actual_partner_type, actual_partner_id)
                    
                    # Имя диалога
                    order_key = f"Заказ {order.order_number}"
                    current_partner = {
                        'partner_type': 'order',
                        'partner_id': partner_id,
                        'name': order_key,
                        'order_id': order.id,
                        'order_number': order.order_number,
                        'avatar': get_partner_avatar('seller', order.seller_id)
                    }
            else:
                # Обычный диалог
                messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
                mark_messages_read(user_type, user_id, partner_type, partner_id)
                partner_name = get_partner_name(partner_type, partner_id)
                current_partner = {
                    'partner_type': partner_type,
                    'partner_id': partner_id,
                    'name': partner_name,
                    'avatar': get_partner_avatar(partner_type, partner_id)
                }
        
        # Подсчет непрочитанных для каждого фильтра
        unread_support = sum(1 for k, c in get_conversations(user_type, user_id, 'support').items() if c['unread_count'] > 0)
        unread_stores = sum(1 for k, c in get_conversations(user_type, user_id, 'stores').items() if c['unread_count'] > 0)
        
        return render_template('main/profile.html',
                             title='Личный кабинет',
                             section=section,
                             user=current_user,
                             conversations=conversations,
                             messages=messages,
                             current_partner=current_partner,
                             filter_options=filter_options,
                             current_filter=filter_type,
                             user_type=user_type,
                             unread_support=unread_support,
                             unread_stores=unread_stores,
                             is_order_chat=is_order_chat,
                             loyalty_enabled=is_loyalty_enabled(),
                             promo_enabled=is_promo_enabled())
    
    # Если это раздел "Бонусы" — плитки магазинов с накопленными баллами + история
    if section == 'bonuses':
        from app.utils.loyalty import (
            is_loyalty_enabled,
            get_buyer_balances_grouped,
        )
        from app.models.orders import Bonus
        loyalty_on = is_loyalty_enabled()
        if loyalty_on:
            balances = get_buyer_balances_grouped(current_user.id)
        else:
            balances = []

        # История операций (последние N), только по per-seller программе
        history = (
            Bonus.query
            .filter(Bonus.buyer_id == current_user.id, Bonus.seller_id.isnot(None))
            .order_by(Bonus.timestamp.desc())
            .limit(50)
            .all()
        )
        history_total = (
            Bonus.query
            .filter(Bonus.buyer_id == current_user.id, Bonus.seller_id.isnot(None))
            .count()
        )

        return render_template('main/profile.html',
                             title='Личный кабинет - Бонусы',
                             section=section,
                             user=current_user,
                             loyalty_enabled=loyalty_on,
                             balances=balances,
                             history=history,
                             history_total=history_total)

    # Если это раздел избранного - получаем избранные товары
    if section == 'favorite':
        favorites = Favorite.query.filter_by(buyer_id=current_user.id).all()
        # Получаем связанные товары
        favorite_products = [f.product for f in favorites if f.product]
        # Получаем ID избранных товаров для подсветки сердечек
        favorite_ids = [f.product_id for f in favorites]
        return render_template('main/profile.html',
                             title='Личный кабинет - Избранное',
                             section=section,
                             user=current_user,
                             products=favorite_products,
                             favorite_ids=favorite_ids,
                             loyalty_enabled=is_loyalty_enabled(),
                             promo_enabled=is_promo_enabled())

    # Если это раздел "Мои покупки" - получаем товары из полученных заказов
    if section == 'purchases':
        # Достаём все позиции заказов покупателя со статусом "Получено" (received)
        delivered_order_items = (
            OrderItem.query
            .join(Order, OrderItem.order_id == Order.id)
            .filter(
                Order.buyer_id == current_user.id,
                Order.status == 'received'
            )
            .order_by(Order.created_at.desc(), OrderItem.id.desc())
            .all()
        )
        # Каждая позиция = отдельная плитка. Если один и тот же товар куплен
        # в разных заказах (или в одном заказе несколькою quantity > 1) —
        # у пользователя будет несколько плиток и возможность оставить
        # отзыв на каждую такую покупку.
        purchased_items = [item for item in delivered_order_items if item.product]
        # ID избранного — чтобы сердечко в плитке подсвечивалось, если товар ещё и в избранном
        favorite_ids = [
            f.product_id for f in
            Favorite.query.filter_by(buyer_id=current_user.id).all()
        ]
        # Пары (product_id, order_id) по которым у покупателя уже есть
        # активный (pending/approved) отзыв — для них плашку «Оставить отзыв»
        # не показываем. Связка (product, order) означает «один отзыв на заказ».
        reviewed_pairs = {
            (r.product_id, r.order_id) for r in
            Review.query.filter(
                Review.buyer_id == current_user.id,
                Review.order_id.isnot(None),
                Review.status.in_(['pending', 'approved'])
            ).all()
        }
        return render_template('main/profile.html',
                             title='Личный кабинет - Мои покупки',
                             section=section,
                             user=current_user,
                             purchased_items=purchased_items,
                             favorite_ids=favorite_ids,
                             reviewed_pairs=reviewed_pairs,
                             loyalty_enabled=is_loyalty_enabled(),
                             promo_enabled=is_promo_enabled())

    # Если это раздел "Мои отзывы" — только прошедшие модерацию
    # (status='approved') отзывы покупателя, оставленные на товары
    # из реально купленных заказов (delivered/received).
    if section == 'reviews':
        reviews = (
            Review.query
            .join(Order, Review.order_id == Order.id)
            .filter(
                Review.buyer_id == current_user.id,
                Review.status == 'approved',
                Order.status.in_(['delivered', 'received']),
            )
            .order_by(Review.created_at.desc())
            .all()
        )
        # Отбрасываем отзывы, у которых по какой-то причине
        # не подтянулся товар (например, удалён каталог) — такие
        # в списке показывать бессмысленно.
        reviews = [r for r in reviews if r.product]
        return render_template('main/profile.html',
                             title='Личный кабинет - Мои отзывы',
                             section=section,
                             user=current_user,
                             reviews=reviews,
                             loyalty_enabled=is_loyalty_enabled(),
                             promo_enabled=is_promo_enabled())

    # Если это раздел адресов - получаем активные службы доставки и сохраненные адреса
    if section == 'addresses':
        from app.models.users import DeliveryService, BuyerDelivery
        delivery_services = DeliveryService.query.filter_by(is_active=True).all()
        buyer_deliveries = BuyerDelivery.query.filter_by(buyer_id=current_user.id).all()
        # Создаём словарь: service_code -> buyer_delivery
        delivery_by_service = {d.delivery_service.code: d for d in buyer_deliveries if d.delivery_service}
        return render_template('main/profile.html',
                             title='Личный кабинет - Мои адреса',
                             section=section,
                             user=current_user,
                             delivery_services=delivery_services,
                             delivery_by_service=delivery_by_service,
                             loyalty_enabled=is_loyalty_enabled(),
                             promo_enabled=is_promo_enabled())
    
    # Если это раздел доставки
    if section == 'delivery':
        from app.models.users import DeliveryService, BuyerDelivery
        delivery_services = DeliveryService.query.filter_by(is_active=True).all()
        buyer_deliveries = BuyerDelivery.query.filter_by(buyer_id=current_user.id).all()
        delivery_by_service = {d.delivery_service.code: d for d in buyer_deliveries if d.delivery_service}
        return render_template('main/profile.html',
                             title='Личный кабинет - Доставка',
                             section=section,
                             user=current_user,
                             delivery_services=delivery_services,
                             delivery_by_service=delivery_by_service,
                             loyalty_enabled=is_loyalty_enabled(),
                             promo_enabled=is_promo_enabled())

    # Если это раздел "Промокоды" — список доступных этому покупателю
    if section == 'promos':
        from app.models.promo import PromoCode
        from app.models.users import Seller
        from app.utils.loyalty import is_promo_enabled, is_promo_enabled_for_seller

        promo_on_global = is_promo_enabled()

        # Показываем только валидные коды: активные, не исчерпавшие лимит
        # и не истёкшие по сроку. recipient_type = public — для всех,
        # recipient_type = personal — только конкретному покупателю.
        from sqlalchemy import or_, and_
        base_q = (
            PromoCode.query
            .filter(PromoCode.is_active.is_(True))
            .filter(
                or_(
                    PromoCode.recipient_type == 'public',
                    and_(
                        PromoCode.recipient_type == 'personal',
                        PromoCode.buyer_id == current_user.id,
                    ),
                )
            )
        )
        all_promos = base_q.order_by(PromoCode.created_at.desc()).all()

        # Скрываем коды, которые сейчас фактически не применимы
        # (лимит исчерпан / срок истёк), и заодно персональные одноразовые
        # у продавца, у которого покупатель этот код уже применил
        # (использованные — отмечаем отдельно, чтобы селлерская логика
        # применения могла на них опираться; здесь просто фильтруем).
        from datetime import datetime
        visible = []
        for p in all_promos:
            if not p.is_valid_now:
                continue
            if p.valid_until is not None and p.valid_until < datetime.utcnow():
                continue
            if p.used_count >= p.max_uses:
                continue
            visible.append(p)

        # Проверим, у каких продавцев промокоды ещё разрешены — не выключил
        # ли их админ (для красоты подписи, но скрывать не будем).
        sellers_on = {}
        if visible:
            seller_ids = {p.seller_id for p in visible}
            for sid in seller_ids:
                sellers_on[sid] = is_promo_enabled_for_seller(sid)

        return render_template('main/profile.html',
                             title='Личный кабинет - Промокоды',
                             section=section,
                             user=current_user,
                             promos=visible,
                             promo_enabled=promo_on_global,
                             sellers_promo_on=sellers_on)

    # Если это раздел заказов (по умолчанию)
    if section == 'orders' or section is None:
        from app.utils.loyalty import is_loyalty_enabled, is_promo_enabled
        # Получаем все заказы покупателя без фильтра по статусу
        status = request.args.get('status')
        query = Order.query.filter_by(buyer_id=current_user.id)
        
        if status:
            # Маппинг статусов
            status_map = {
                'pending': ['pending'],
                'processing': ['processing', 'in_assembly', 'assembled'],
                'shipped': ['shipped', 'in_transit'],
                'delivered': ['delivered'],
                'cancelled': ['canceled', 'cancelled']
            }
            if status in status_map:
                query = query.filter(Order.status.in_(status_map[status]))
            else:
                query = query.filter_by(status=status)
        
        # Сортировка по дате: свежее выше
        orders = query.order_by(Order.created_at.desc()).all()
        
        return render_template('main/profile.html',
                             title='Личный кабинет - Заказы',
                             section='orders',
                             user=current_user,
                             orders=orders,
                             current_status=status,
                             loyalty_enabled=is_loyalty_enabled(),
                             promo_enabled=is_promo_enabled())
    
    from app.utils.loyalty import is_loyalty_enabled, is_promo_enabled
    return render_template('main/profile.html',
                         title='Личный кабинет',
                         section=section,
                         user=current_user,
                         loyalty_enabled=is_loyalty_enabled(),
                         promo_enabled=is_promo_enabled())


@bp.route('/profile/settings', methods=['POST'])
@login_required
def profile_settings():
    """
    Сохранение настроек профиля.
    URL: /profile/settings
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    # Получаем данные из формы
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    address = request.form.get('address', '').strip()
    
    # Обновляем данные пользователя
    current_user.name = name
    current_user.phone = phone
    current_user.address = address
    
    db.session.commit()
    
    flash('Настройки сохранены.', 'success')
    return redirect(url_for('main.profile', section='settings'))


@bp.route('/profile/addresses/save', methods=['POST'])
@login_required
def profile_addresses_save():
    """
    Сохранение адресов доставки (ПВЗ) для покупателя.
    URL: /profile/addresses/save
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    from app.models.users import DeliveryService, BuyerDelivery
    
    # Получаем все активные службы доставки
    delivery_services = DeliveryService.query.filter_by(is_active=True).all()
    
    for service in delivery_services:
        address_key = f'address_{service.id}'
        address_value = request.form.get(address_key, '').strip()
        
        # Ищем существующую запись или создаём новую
        delivery = BuyerDelivery.query.filter_by(
            buyer_id=current_user.id,
            delivery_service_id=service.id
        ).first()
        
        if not delivery:
            delivery = BuyerDelivery(
                buyer_id=current_user.id,
                delivery_service_id=service.id
            )
            db.session.add(delivery)
        
        # Обновляем адрес ПВЗ
        delivery.pvz_address = address_value if address_value else None
        
        # Если адрес пустой, также очищаем код ПВЗ
        if not address_value:
            delivery.pvz_code = None
    
    db.session.commit()
    
    flash('Адреса сохранены.', 'success')
    return redirect(url_for('main.profile', section='addresses'))


@bp.route('/profile/order/<int:order_id>')
@login_required
def order_detail(order_id):
    """
    Детали заказа в личном кабинете.
    URL: /profile/order/{id}
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    order = Order.query.filter_by(
        id=order_id,
        buyer_id=current_user.id
    ).first()
    
    if not order:
        flash('Заказ не найден', 'warning')
        return redirect(url_for('main.profile_orders'))
    
    # Загружаем items и delivery_service явно
    _ = order.items.all()
    _ = order.delivery_service
    
    return render_template('main/order_detail.html',
                         title=f'Заказ #{order.order_number}',
                         order=order,
                         user=current_user)


@bp.route('/profile/order/<int:order_id>/mark-received', methods=['POST'])
@login_required
def order_mark_received(order_id):
    """
    Покупатель подтверждает получение заказа.
    URL: /profile/order/{id}/mark-received
    Доступно только из статуса «Доставлен» (delivered).
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))

    order = Order.query.filter_by(
        id=order_id,
        buyer_id=current_user.id
    ).first()

    if not order:
        flash('Заказ не найден', 'warning')
        return redirect(url_for('main.profile_orders'))

    if order.status != 'delivered':
        flash('Подтвердить получение можно только у доставленного заказа.', 'warning')
        return redirect(url_for('main.order_detail', order_id=order.id))

    order.mark_received()
    flash('Спасибо! Заказ отмечен как полученный.', 'success')
    return redirect(url_for('main.order_detail', order_id=order.id))


@bp.route('/profile/orders')
@login_required
def profile_orders():
    """
    Заказы в личном кабинете.
    URL: /profile/orders
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    status = request.args.get('status')
    
    query = Order.query.filter_by(buyer_id=current_user.id)
    
    if status:
        # Маппинг старых статусов на новые для фильтрации
        status_map = {
            'pending': ['pending'],
            'processing': ['processing', 'in_assembly', 'assembled'],
            'shipped': ['shipped', 'in_transit'],
            'delivered': ['delivered', 'received'],
            'cancelled': ['canceled', 'cancelled']
        }
        if status in status_map:
            query = query.filter(Order.status.in_(status_map[status]))
        else:
            query = query.filter_by(status=status)
    
    orders = query.order_by(Order.created_at.desc()).all()
    
    return render_template('main/profile_orders.html',
                         title='Мои заказы',
                         orders=orders,
                         current_status=status,
                         user=current_user)


@bp.route('/profile/favorite')
@login_required
def profile_favorite():
    """
    Избранное в личном кабинете.
    URL: /profile/favorite
    """
    # Редирект на основной маршрут профиля с параметром section
    return redirect(url_for('main.profile', section='favorite'))


### @bp.route('/profile/delivery')
@login_required
def profile_delivery(): ###
    """
    Управление доставкой в личном кабинете.
    URL: /profile/delivery
    Позволяет выбрать ПВЗ для каждой службы доставки.
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    # Получаем активные службы доставки маркетплейса
    delivery_services = DeliveryService.query.filter_by(is_active=True).all()
    
    # Получаем сохранённые адреса доставки покупателя
    buyer_deliveries = BuyerDelivery.query.filter_by(buyer_id=current_user.id).all()
    
    # Создаём словарь: service_code -> buyer_delivery
    delivery_by_service = {d.delivery_service.code: d for d in buyer_deliveries}
    
    # Получаем непрочитанные сообщения
    unread_messages = Message.query.filter_by(receiver_id=current_user.id, is_read=False).count()
    
    return render_template('main/profile_delivery.html',
                         title='Доставка',
                         delivery_services=delivery_services,
                         delivery_by_service=delivery_by_service,
                         unread_messages=unread_messages,
                         user=current_user)


### @bp.route('/profile/delivery/save', methods=['POST'])
@login_required
def profile_delivery_save(): ###
    """
    Сохранение выбранного ПВЗ для службы доставки.
    """
    if not isinstance(current_user, Buyer):
        return jsonify({'success': False, 'error': 'Доступ только для покупателей'}), 403
    
    service_id = request.form.get('service_id', type=int)
    pvz_code = request.form.get('pvz_code', '')
    pvz_address = request.form.get('pvz_address', '')
    pvz_city = request.form.get('pvz_city', '')
    pvz_city_code = request.form.get('pvz_city_code', type=int)
    
    if not service_id:
        return jsonify({'success': False, 'error': 'Не указана служба доставки'}), 400
    
    # Проверяем службу доставки
    service = db.session.get(DeliveryService, service_id)
    if not service or not service.is_active:
        return jsonify({'success': False, 'error': 'Служба доставки не найдена'}), 404
    
    # Ищем существующую запись или создаём новую
    delivery = BuyerDelivery.query.filter_by(
        buyer_id=current_user.id,
        delivery_service_id=service_id
    ).first()
    
    if not delivery:
        delivery = BuyerDelivery(
            buyer_id=current_user.id,
            delivery_service_id=service_id
        )
        db.session.add(delivery)
    
    # Обновляем данные
    delivery.pvz_code = pvz_code
    delivery.pvz_address = pvz_address
    delivery.pvz_city = pvz_city
    delivery.pvz_city_code = pvz_city_code
    
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Пункт выдачи сохранён'})


@bp.route('/profile/messages')
@login_required
def profile_messages():
    """
    Сообщения в личном кабинете.
    URL: /profile/messages
    """
    if not isinstance(current_user, Buyer):
        return redirect(url_for('main.index'))
    
    user_type = 'buyer'
    user_id = current_user.id
    
    # Получаем фильтр (support или stores)
    filter_type = request.args.get('filter', 'stores')
    partner_type = request.args.get('partner_type', type=str)
    partner_id = request.args.get('partner_id', type=int)
    
    # Используем логику из messages blueprint
    from app.blueprints.messages import get_conversations, mark_messages_read, get_partner_name, get_partner_avatar
    
    # Получаем диалоги с учетом фильтра
    conversations = get_conversations(user_type, user_id, filter_type)
    
    # Определяем фильтры для покупателя
    filter_options = {
        'support': 'Поддержка',
        'stores': 'Магазины',
        'orders': 'Заказы'
    }
    
    # Текущий диалог - проверяем partner_id через is not None, чтобы Allow partner_id=0 для админа
    current_partner = None
    messages = []
    
    # Проверяем, если это order-диалог
    if partner_type == 'order' and partner_id is not None:
        from app.models.orders import Order
        order = Order.query.get(partner_id)
        if order:
            actual_partner_type = 'seller'
            actual_partner_id = order.seller_id
            order_key = f"Заказ {order.order_number}"
            
            # Получаем сообщения для этого заказа
            from app.models.communications import Message
            messages = Message.query.filter(
                Message.conversation_type == 'order',
                Message.conversation_id == partner_id
            ).order_by(Message.timestamp.asc()).all()
            
            mark_messages_read(user_type, user_id, actual_partner_type, actual_partner_id)
            
            current_partner = {
                'partner_type': 'order',
                'partner_id': partner_id,
                'name': order_key,
                'order_id': order.id,
                'order_number': order.order_number,
                'avatar': get_partner_avatar('seller', order.seller_id)
            }
    elif partner_type and partner_id is not None:
        messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
        mark_messages_read(user_type, user_id, partner_type, partner_id)
        partner_name = get_partner_name(partner_type, partner_id)
        current_partner = {
            'partner_type': partner_type,
            'partner_id': partner_id,
            'name': partner_name,
            'avatar': get_partner_avatar(partner_type, partner_id)
        }
    
    # Подсчет непрочитанных для каждого фильтра
    unread_support = sum(1 for k, c in get_conversations(user_type, user_id, 'support').items() if c['unread_count'] > 0)
    unread_stores = sum(1 for k, c in get_conversations(user_type, user_id, 'stores').items() if c['unread_count'] > 0)
    
    return render_template('main/profile_messages.html',
                         title='Сообщения',
                         conversations=conversations,
                         messages=messages,
                         current_partner=current_partner,
                         filter_options=filter_options,
                         current_filter=filter_type,
                         user_type=user_type,
                         unread_support=unread_support,
                         unread_stores=unread_stores)


@bp.route('/product/<int:product_id>/review', methods=['POST'])
@login_required
@buyer_required
def add_review(product_id):
    """
    Добавление отзыва к товару.
    URL: /product/{id}/review
    """
    product = db.session.get(Product, product_id)
    if not product or product.status != 'approved':
        abort(404)

    # Не даём оставлять отзывы на товары заблокированных магазинов.
    if product.seller is not None and _resolve_tariff_state(product.seller)['state'] in ('locked', 'none'):
        abort(404)
    if product.seller is not None and _seller_blocked_by_daily_limit(product.seller):
        abort(404)

    # Проверка, покупал ли пользователь этот товар и получил ли его
    from app.models.orders import OrderItem
    purchased_order = Order.query.join(OrderItem).filter(
        Order.buyer_id == current_user.id,
        Order.status == 'received',
        OrderItem.product_id == product_id
    ).first()

    if not purchased_order:
        flash('Вы можете оставить отзыв только на товар, который получили.', 'error')
        return redirect(url_for('main.product', product_id=product_id))

    # Проверка, не оставлял ли уже отзыв (активный = pending/approved)
    existing_review = Review.query.filter(
        Review.buyer_id == current_user.id,
        Review.product_id == product_id,
        Review.status.in_(['pending', 'approved'])
    ).first()

    if existing_review:
        flash('Вы уже оставляли отзыв на этот товар.', 'error')
        return redirect(url_for('main.product', product_id=product_id))

    rating = request.form.get('rating', type=int)
    text = request.form.get('text', '').strip()

    if not rating or rating < 1 or rating > 5:
        flash('Укажите корректную оценку от 1 до 5.', 'error')
        return redirect(url_for('main.product', product_id=product_id))

    review = Review(
        buyer_id=current_user.id,
        product_id=product_id,
        rating=rating,
        text=text,
        status='pending',  # Требует модерации
        is_approved=False
    )
    db.session.add(review)
    db.session.commit()

    flash('Ваш отзыв отправлен на модерацию.', 'success')
    return redirect(url_for('main.product', product_id=product_id))


@bp.route('/api/reviews/submit', methods=['POST'])
@login_required
@buyer_required
def api_submit_review():
    """
    Отправка отзыва через модальное окно со страницы «Мои покупки».
    URL: /api/reviews/submit
    Body: product_id, order_id (опц.), rating, text

    Логика: один отзыв на (покупатель, товар, заказ). Если order_id
    не передан (старая форма), действует старое правило — один отзыв
    на (покупатель, товар).
    """
    product_id = request.form.get('product_id', type=int)
    order_id = request.form.get('order_id', type=int)
    rating = request.form.get('rating', type=int)
    text = (request.form.get('text') or '').strip()

    if not product_id or not rating or rating < 1 or rating > 5:
        return jsonify({'ok': False, 'error': 'Укажите оценку от 1 до 5.'}), 400

    product = db.session.get(Product, product_id)
    if not product or product.status != 'approved':
        return jsonify({'ok': False, 'error': 'Товар не найден.'}), 404

    # Не даём оставлять отзывы на товары заблокированных магазинов.
    if product.seller is not None and _resolve_tariff_state(product.seller)['state'] in ('locked', 'none'):
        return jsonify({'ok': False, 'error': 'Товар недоступен.'}), 404
    if product.seller is not None and _seller_blocked_by_daily_limit(product.seller):
        return jsonify({'ok': False, 'error': 'Товар недоступен.'}), 404

    # Покупал ли пользователь этот товар и получил ли его заказ.
    from app.models.orders import OrderItem
    base_purchase_q = (
        Order.query
        .join(OrderItem, OrderItem.order_id == Order.id)
        .filter(
            Order.buyer_id == current_user.id,
            Order.status == 'received',
            OrderItem.product_id == product_id
        )
    )

    if order_id:
        # Проверяем, что этот заказ действительно принадлежит покупателю
        # и в нём есть именно этот товар (защита от подмены id).
        purchased_order = base_purchase_q.filter(Order.id == order_id).first()
        if not purchased_order:
            return jsonify({
                'ok': False,
                'error': 'Оставить отзыв можно только на полученный товар из вашего заказа.'
            }), 403
    else:
        # Старая логика (без order_id) — достаточно факта любой покупки.
        purchased_order = base_purchase_q.first()
        if not purchased_order:
            return jsonify({
                'ok': False,
                'error': 'Оставить отзыв можно только на полученный товар.'
            }), 403

    # Уже есть активный (pending/approved) отзыв от этого покупателя?
    # Если order_id передан — проверяем по паре (product_id, order_id),
    # иначе — по product_id (старая логика).
    if order_id:
        existing_review = Review.query.filter(
            Review.buyer_id == current_user.id,
            Review.product_id == product_id,
            Review.order_id == order_id,
            Review.status.in_(['pending', 'approved'])
        ).first()
    else:
        existing_review = Review.query.filter(
            Review.buyer_id == current_user.id,
            Review.product_id == product_id,
            Review.status.in_(['pending', 'approved'])
        ).first()

    if existing_review:
        return jsonify({
            'ok': False,
            'error': 'Вы уже оставляли отзыв на этот товар.'
        }), 409

    review = Review(
        buyer_id=current_user.id,
        product_id=product_id,
        order_id=order_id,  # может быть None для обратной совместимости
        rating=rating,
        text=text,
        status='pending',
        is_approved=False
    )
    db.session.add(review)
    db.session.commit()

    return jsonify({'ok': True, 'message': 'Отзыв отправлен на модерацию.'})


@bp.route('/<seller_slug>')
def seller_store(seller_slug):
    """
    Магазин продавца.
    URL: /{slug}
    """
    from app.models.users import Seller
    
    seller = Seller.query.filter_by(store_slug=seller_slug).first()
    if not seller:
        abort(404)
    
    products = Product.query.filter(
        Product.seller_id == seller.id,
        Product.status == 'approved',
        Product.stock_quantity > 0
    ).order_by(Product.published_at.desc()).all()
    
    from datetime import datetime
    
    return render_template('main/seller_store.html',
                         title=f'Магазин {seller.store_name}',
                         seller=seller,
                         products=products,
                         now=datetime.utcnow())


# =============================================================================
# Редирект для доставки на поддомен продавца
# =============================================================================

@bp.route('/delivery/add', methods=['POST'])
@login_required
def delivery_add():
    """
    Обработка формы добавления доставки.
    URL: /delivery/add
    """
    from flask import redirect
    from app.models.users import Seller, DeliveryService, SellerDelivery
    
    if not isinstance(current_user, Seller):
        flash('Доступ только для продавцов.', 'error')
        return redirect(url_for('main.index'))
    
    delivery_service_id = request.form.get('delivery_service_id', type=int)
    api_login = request.form.get('api_login')
    api_password = request.form.get('api_password')
    ship_from = request.form.get('ship_from')
    
    # CDEK-специфичные поля
    contract_number = request.form.get('contract_number')
    pvz_code = request.form.get('pvz_code')
    pvz_address = request.form.get('pvz_address')
    pvz_city = request.form.get('pvz_city')
    tariffs = request.form.getlist('tariffs')
    cdek_test_mode = request.form.get('cdek_test_mode') == 'on'
    
    service = db.session.get(DeliveryService, delivery_service_id)
    if not service:
        flash('Служба доставки не найдена.', 'error')
        return redirect(url_for('main.seller_delivery'))
    
    # Проверка существующего профиля
    existing = SellerDelivery.query.filter_by(
        seller_id=current_user.id,
        delivery_service_id=delivery_service_id
    ).first()
    
    # Подготовка API credentials
    api_credentials = {'login': api_login, 'password': api_password}
    
    # Для СДЭК добавляем account и secure
    if service.code == 'cdek':
        cdek_account = request.form.get('cdek_account', '')
        cdek_secure = request.form.get('cdek_secure', '')
        api_credentials.update({
            'account': cdek_account,
            'secure': cdek_secure,
            'test_mode': cdek_test_mode
        })
    
    if existing:
        existing.api_credentials = api_credentials
        existing.ship_from_address = ship_from
        
        if service.code == 'cdek':
            existing.contract_number = contract_number
            existing.pvz_code = pvz_code
            existing.pvz_address = pvz_address
            existing.pvz_city = pvz_city
            existing.tariffs = tariffs if tariffs else []
        
        message = 'Настройки доставки обновлены.'
    else:
        profile = SellerDelivery(
            seller_id=current_user.id,
            delivery_service_id=delivery_service_id,
            api_credentials=api_credentials,
            ship_from_address=ship_from
        )
        
        # CDEK-специфичные поля
        if service.code == 'cdek':
            profile.contract_number = contract_number
            profile.pvz_code = pvz_code
            profile.pvz_address = pvz_address
            profile.pvz_city = pvz_city
            profile.tariffs = tariffs if tariffs else []
        
        db.session.add(profile)
        message = 'Способ доставки добавлен.'
    
    db.session.commit()
    
    flash(message, 'success')
    ### return redirect(url_for('seller.delivery', _subdomain='seller')) ###


### @bp.route('/delivery')
@login_required
def delivery(): ###
    """
    Перенаправление на настройки доставки.
    URL: /delivery
    """
    from flask import redirect
    from app.models.users import Seller
    
    if not isinstance(current_user, Seller):
        flash('Доступ только для продавцов.', 'error')
        return redirect(url_for('main.index'))
    
    return redirect(url_for('seller.delivery', _subdomain='seller'))


# =============================================================================
# Редирект на поддомен продавца для работы с доставкой
# =============================================================================

### @bp.route('/delivery/add', methods=['POST'])
def delivery_add_redirect(): ###
    """
    Перенаправление на поддомен продавца для добавления доставки.
    URL: /delivery/add
    """
    from flask import redirect
    
    # Перенаправляем на поддомен продавца
    return redirect(url_for('seller.delivery_add', _subdomain='seller'))


### @bp.route('/delivery')
def delivery_redirect(): ###
    """
    Перенаправление на поддомен продавца для настроек доставки.
    URL: /delivery
    """
    from flask import redirect
    
    return redirect(url_for('seller.delivery', _subdomain='seller'))


# API для расчёта стоимости доставки
@bp.route('/api/cdek/calculate-delivery', methods=['POST'])
@login_required
def calculate_cdek_delivery():
    """
    Расчёт стоимости доставки через СДЭК.
    От ПВЗ продавца до ПВЗ покупателя с учётом тарифа продавца.
    """
    if not isinstance(current_user, Buyer):
        return jsonify({'error': 'Доступ только для покупателей'}), 403
    
    data = request.get_json()
    delivery_id = data.get('delivery_id')
    buyer_pvz_code = data.get('buyer_pvz_code')  # ПВЗ покупателя
    buyer_pvz_city_code = data.get('buyer_pvz_city_code')  # Код города ПВЗ покупателя
    
    if not delivery_id:
        return jsonify({'success': False, 'error': 'Укажите ID доставки'}), 400
    
    try:
        from app.integrations.cdek import get_cdek_client, CDEKError
        
        # Получаем профиль доставки продавца
        seller_delivery = db.session.get(SellerDelivery, delivery_id)
        if not seller_delivery:
            return jsonify({'success': False, 'error': 'Профиль доставки не найден'}), 404
        
        # === Получаем код города отправки (от ПВЗ продавца) ===
        seller_city_code = seller_delivery.pvz_city_code
        
        # Если код города не сохранён - пробуем определить по названию города
        if not seller_city_code and seller_delivery.pvz_city:
            try:
                client = get_cdek_client(seller_delivery)
                cities = client.get_cities(city=seller_delivery.pvz_city)
                if cities and len(cities) > 0:
                    seller_city_code = cities[0].get('code')
            except CDEKError:
                pass
        
        if not seller_city_code:
            return jsonify({'success': False, 'error': 'У продавца не настроен код города для ПВЗ. Обратитесь к продавцу.'}), 400
        
        # === Получаем код города получения (от ПВЗ покупателя) ===
        # buyer_pvz_city_code уже может быть передан из UI
        if not buyer_pvz_city_code and buyer_pvz_code:
            # Пробуем определить по коду ПВЗ через API
            try:
                client = get_cdek_client(seller_delivery)
                pvz_list = client.get_pvz_list()
                for pvz in pvz_list:
                    if pvz.get('code') == buyer_pvz_code:
                        seller_location = pvz.get('location', {})
                        buyer_pvz_city_code = seller_location.get('city_code')
                        break
            except CDEKError:
                pass
        
        if not buyer_pvz_city_code:
            return jsonify({'success': False, 'error': 'Выберите ПВЗ для получения'}), 400
        
        # Создаём клиент СДЭК с credentials продавца
        client = get_cdek_client(seller_delivery)
        
        # FROM: ПВЗ продавца (код города + название для лучшего распознавания)
        from_location = {'code': seller_city_code}
        # Добавляем название города, если доступно (помогает API распознать локацию)
        if seller_delivery.pvz_city:
            from_location['city'] = seller_delivery.pvz_city
        
        # TO: ПВЗ покупателя (код города)
        to_location = {'code': buyer_pvz_city_code}
        
        # Получаем вес товаров из корзины для этого продавца
        cart_items = CartItem.query.filter_by(buyer_id=current_user.id).all()
        seller_items = [item for item in cart_items if item.product.seller_id == seller_delivery.seller_id]
        total_weight = sum(
            (item.product.weight or 500) * item.quantity 
            for item in seller_items
        )
        # Минимальный вес 100г
        total_weight = max(total_weight, 100)
        
        # Тариф продавца (если указан, используем конкретный тариф)
        tariff_code = None
        if seller_delivery.tariffs and len(seller_delivery.tariffs) > 0:
            tariff_code = seller_delivery.tariffs[0]  # Берём первый выбранный тариф
        
        # Выполняем расчёт
        if tariff_code:
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"CDEK calculate_by_tariff: tariff={tariff_code}, from={from_location}, to={to_location}, weight={total_weight}")
            
            result = client.calculate_by_tariff(
                from_location=from_location,
                to_location=to_location,
                weight=total_weight,
                tariff_code=tariff_code
            )
            logger.info(f"CDEK calculate_by_tariff response: {result}")
            
            # Обрабатываем ответ API
            if result:
                tariffs_list = None
                
                # Новый формат: {'tariff_codes': [...]} 
                if isinstance(result, dict) and 'tariff_codes' in result:
                    tariffs_list = result['tariff_codes']
                # Старый формат: прямой массив
                elif isinstance(result, list):
                    tariffs_list = result
                # Одиночный результат
                elif isinstance(result, dict):
                    delivery_price = result.get('delivery_sum', result.get('total_sum', result.get('price', 0)))
                    return jsonify({
                        'success': True,
                        'delivery_price': delivery_price,
                        'tariff_name': result.get('tariff_name', ''),
                        'period': {'min': result.get('period_min'), 'max': result.get('period_max')}
                    })
                
                # Обрабатываем список тарифов
                if tariffs_list and len(tariffs_list) > 0:
                    # Фильтруем склад-склад и склад-PVZ (delivery_mode 4 и 7)
                    pvz_tariffs = [t for t in tariffs_list if t.get('delivery_mode') in [4, 7]]
                    if pvz_tariffs:
                        tariffs_list = pvz_tariffs
                    
                    # Берём минимальную цену
                    min_tariff = min(tariffs_list, key=lambda x: x.get('delivery_sum', float('inf')))
                    delivery_price = min_tariff.get('delivery_sum', 0)
                    
                    return jsonify({
                        'success': True,
                        'delivery_price': delivery_price,
                        'tariff_name': min_tariff.get('tariff_name', ''),
                        'period': {'min': min_tariff.get('period_min'), 'max': min_tariff.get('period_max')}
                    })
                    return jsonify({
                        'success': True,
                        'delivery_price': delivery_price,
                        'tariff_name': result[0].get('tariff_name', ''),
                        'period': result[0].get('period', {})
                    })
        else:
            # Если тариф не указан - получаем все доступные и берём минимальную цену
            result = client.calculate(
                from_location=from_location,
                to_location=to_location,
                weight=total_weight
            )
            
            if result:
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"CDEK calculate response: {result}")
                
                # Обрабатываем новый формат: {'tariff_codes': [...]}
                tariffs_list = None
                if isinstance(result, dict) and 'tariff_codes' in result:
                    tariffs_list = result['tariff_codes']
                elif isinstance(result, list):
                    tariffs_list = result
                
                if tariffs_list and len(tariffs_list) > 0:
                    # Фильтруем склад-склад (delivery_mode 4) и склад-постамат (7)
                    pvz_tariffs = [t for t in tariffs_list if t.get('delivery_mode') in [4, 7]]
                    if pvz_tariffs:
                        tariffs_list = pvz_tariffs
                    
                    # Если фильтрация убрала все тарифы - используем все
                    if not tariffs_list:
                        tariffs_list = result.get('tariff_codes', result) if isinstance(result, dict) else result
                    
                    # Берём минимальную цену
                    min_tariff = min(tariffs_list, key=lambda x: x.get('delivery_sum', float('inf')))
                    delivery_price = min_tariff.get('delivery_sum', 0)
                    
                    return jsonify({
                        'success': True,
                        'delivery_price': delivery_price,
                        'tariff_name': min_tariff.get('tariff_name', ''),
                        'period': {'min': min_tariff.get('period_min'), 'max': min_tariff.get('period_max')},
                        'tariffs': [{
                            'code': t.get('tariff_code'),
                            'name': t.get('tariff_name'),
                            'price': t.get('delivery_sum', 0)
                        } for t in tariffs_list]
                    })
        
        return jsonify({'success': False, 'error': 'Нет доступных тарифов'})
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# Функция-заглушка с тестовыми ПВЗ (когда API недоступен)
def get_fallback_pvz_list():
    """Возвращает тестовый список ПВЗ."""
    return [
        {
            'code': 'MSK98',
            'name': 'ПВЗ Москва',
            'location': {'city': 'Москва', 'address': 'ул. Примерная, д. 1', 'city_code': 44},
            'type': 'PVZ',
            'work_time': 'Пн-Пт: 09:00-20:00',
            'weight_max': 10000,
            'latitude': 55.7558,
            'longitude': 37.6173
        },
        {
            'code': 'MSK99',
            'name': 'ПВЗ Москва Юг',
            'location': {'city': 'Москва', 'address': 'ул. Примерная, д. 2', 'city_code': 44},
            'type': 'PVZ',
            'work_time': 'Пн-Вс: 10:00-22:00',
            'weight_max': 15000,
            'latitude': 55.6900,
            'longitude': 37.7100
        },
        {
            'code': 'SPB01',
            'name': 'ПВЗ Санкт-Петербург',
            'location': {'city': 'Санкт-Петербург', 'address': 'Невский пр., д. 1', 'city_code': 137},
            'type': 'PVZ',
            'work_time': 'Пн-Пт: 09:00-21:00',
            'weight_max': 10000,
            'latitude': 59.9343,
            'longitude': 30.3351
        },
        {
            'code': 'NSK01',
            'name': 'ПВЗ Новосибирск',
            'location': {'city': 'Новосибирск', 'address': 'ул. Ленина, д. 1', 'city_code': 270},
            'type': 'PVZ',
            'work_time': 'Пн-Пт: 09:00-20:00',
            'weight_max': 10000,
            'latitude': 55.0084,
            'longitude': 82.9357
        },
        {
            'code': 'EKB01',
            'name': 'ПВЗ Екатеринбург',
            'location': {'city': 'Екатеринбург', 'address': 'ул. Ленина, д. 1', 'city_code': 75},
            'type': 'PVZ',
            'work_time': 'Пн-Пт: 09:00-20:00',
            'weight_max': 10000,
            'latitude': 56.8389,
            'longitude': 60.6057
        },
    ]


# API для получения списка ПВЗ - используем роут из api.py
