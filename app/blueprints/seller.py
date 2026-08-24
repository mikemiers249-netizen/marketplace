"""
Blueprint панели продавца.
Работает на поддомене seller.domain
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func, or_, and_
from datetime import datetime, timedelta
from app import db, csrf
from app.models.users import Seller, DeliveryService, SellerDelivery
from app.models.products import Category, Product, ProductPhoto, ProductParameter, Parameter, ProductCard, ProductCard, ProductEvent
from app.models.orders import Order, OrderItem, CartItem, Promotion, PromotionProduct
from app.models.communications import Message, Review, InfoPost, EduMaterial, RoadmapEvent, TariffBlock, TariffRow
from app.models.tariffs import (
    SellerTariffSubscription, TariffTransaction
)
from app.utils.helpers import format_price, slugify, PaginationHelper
from app.utils.decorators import seller_required


bp = Blueprint('seller', __name__, subdomain='seller')


# Количество дней грейс-периода после истечения оплаченного срока тарифа.
# В эти дни магазин селлера продолжает работать, но на /tariffs висит
# предупреждение. После — магазин блокируется (см. @require_active_tariff).
TARIFF_GRACE_DAYS = 5

# За сколько дней до истечения тарифа показывать баннер-предупреждение
# на всех страницах seller ЛК.
TARIFF_WARN_DAYS = 5


# ВАЖНО: в path-режиме (по умолчанию) этот blueprint пересоздаётся в
# register_blueprints() — копируются только view-функции (deferred_functions),
# а before_request / context_processor — НЕТ. Поэтому реальные
# реализации живут на уровне app в app/__init__.py:
#   - @app.before_request  _app_seller_tariff_state
#   - @app.context_processor _app_seller_tariff_state_processor
#   - @app.context_processor _app_seller_delivery_processor
# Здесь оставлены лишь безопасные no-op заглушки для subdomain-режима
# (USE_SELLER_SUBDOMAIN=1) и чтобы тесты/импорты не падали.
@bp.before_request
def inject_delivery_context():
    # no-op: см. app/__init__.py
    return None


@bp.context_processor
def delivery_context_processor():
    return {}


@bp.context_processor
def tariff_state_processor():
    return {
        'tariff_state': None,
        'tariff_warning_banner': False,
        'tariff_locked': False,
    }


@bp.route('/')
@bp.route('/dashboard/')
def dashboard():
    """
    Главная страница панели продавца.
    URL: seller.domain/
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    seller = current_user
    
    # Статистика
    total_products = Product.query.filter_by(seller_id=seller.id).count()
    active_products = Product.query.filter(
        Product.seller_id == seller.id,
        Product.status == 'approved',
        Product.stock_quantity > 0
    ).count()
    
    pending_orders = Order.query.filter(
        Order.seller_id == seller.id,
        Order.status.in_(['processing'])
    ).count()
    
    shipped_orders = Order.query.filter(
        Order.seller_id == seller.id,
        Order.status == 'shipped'
    ).count()
    
    # Заказы за последние 7 дней
    week_ago = datetime.utcnow() - timedelta(days=7)
    weekly_orders = Order.query.filter(
        Order.seller_id == seller.id,
        Order.created_at >= week_ago
    ).count()
    
    # Выручка за последние 30 дней
    month_ago = datetime.utcnow() - timedelta(days=30)
    revenue = db.session.query(func.sum(Order.total_price)).filter(
        Order.seller_id == seller.id,
        Order.created_at >= month_ago,
        Order.status.in_((['delivered', 'shipped']))
    ).scalar() or 0
    
    # Недавние заказы
    recent_orders = Order.query.filter_by(seller_id=seller.id).order_by(
        Order.created_at.desc()
    ).limit(5).all()
    
    # Отзывы
    recent_reviews = Review.query.join(Product).filter(
        Product.seller_id == seller.id
    ).order_by(Review.created_at.desc()).limit(5).all()
    
    # Товары на модерации
    moderation_products = Product.query.filter(
        Product.seller_id == seller.id,
        Product.status == 'on_moderation'
    ).all()
    
    return render_template('seller/dashboard.html',
                         title='Панель продавца',
                         seller=seller,
                         total_products=total_products,
                         active_products=active_products,
                         pending_orders=pending_orders,
                         shipped_orders=shipped_orders,
                         weekly_orders=weekly_orders,
                         revenue=revenue,
                         recent_orders=recent_orders,
                         recent_reviews=recent_reviews,
                         moderation_products=moderation_products)


# ========== Информация / новости ==========

# Содержимое трёх верхних плашек (продавец видит то же, что и админ,
# но без редактирования).
SELLER_INFO_TILES = {
    'roadmap': {
        'title': 'Траектория развития проекта',
        'icon': 'bi-signpost-split',
        # Тело плитки пустое — в шаблоне info_section.html рендерится полноценный
        # блок roadmap (FullCalendar + лента событий) вместо статичного текста.
        'body': '',
    },
    'tariffs': {
        'title': 'Тарифы',
        'icon': 'bi-cash-stack',
        'body': (
            'Тарифы для продавцов: комиссия, лимиты, фичи. '
            'Когда подключите биллинг — здесь появится актуальная информация.'
        ),
    },
    'education': {
        'title': 'Обучение',
        'icon': 'bi-mortarboard',
        'body': (
            'Подборка материалов: как завести магазин, оформить карточку товара, '
            'работать с акциями и аналитикой.'
        ),
    },
}


@bp.route('/info')
def info():
    """
    Раздел «Информация» в дашборде продавца: плашки + лента новостей.
    Поддерживает фильтр ?tag=<slug> и сортировку ?sort=new|old.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    sort = request.args.get('sort', 'new')
    if sort not in ('new', 'old'):
        sort = 'new'
    order = InfoPost.sort_date.desc() if sort == 'new' else InfoPost.sort_date.asc()

    # Базовый скоуп: только видимые продавцу опубликованные новости
    base_query = (
        InfoPost.query
        .filter(InfoPost.audience.in_(('all', 'seller')))
        .filter(InfoPost.is_published == True)
    )

    # Список тегов, доступных продавцу (для панели фильтров)
    visible_tags = [
        t for (t,) in (
            base_query.with_entities(InfoPost.tag)
            .filter(InfoPost.tag.isnot(None))
            .filter(InfoPost.tag != '')
            .distinct()
            .order_by(InfoPost.tag)
            .all()
        )
    ]

    # Активный фильтр по тегу (по слагу — кириллица транслитерируется)
    active_tag = None
    tag_filter = (request.args.get('tag') or '').strip()
    if tag_filter:
        # Найдём тег в visible_tags по совпадению слага
        for t in visible_tags:
            if InfoPost.tag_slug(t) == tag_filter:
                active_tag = t
                break
        if active_tag is not None:
            base_query = base_query.filter(InfoPost.tag == active_tag)

    posts = base_query.order_by(order).all()

    return render_template(
        'seller/info.html',
        title='Информация',
        tiles=SELLER_INFO_TILES,
        posts=posts,
        sort=sort,
        can_manage=False,
        tags=visible_tags,
        active_tag=active_tag,
        active_tag_slug=tag_filter,
        roadmap_events=_seller_roadmap_events(),
        roadmap_categories=[
            {'code': code, 'label': label, 'color': color}
            for code, label, color in RoadmapEvent.CATEGORIES
        ],
    )


def _sanitize_html(value):
    """
    Базовая санитизация HTML из формы (для описания товара / магазина).
    Удаляет опасные теги (<script>, <iframe>, <object>, <embed>, <style>, <link>)
    и атрибуты on* (onclick, onerror и т.п.) + javascript: в href/src.

    НЕ полноценный HTMLPurifier — оставляет разрешённые теги (p, ul, ol, li,
    strong, em, b, i, h2, h3, a, br, span). Для админских/продавцовских
    полей этого достаточно; для публичных мест с пользовательским вводом
    стоит подключить bleach или html-sanitizer.
    """
    import re
    if not value:
        return ''
    text = str(value)
    # Вырезаем целиком опасные блоки: <script ...>...</script>, <iframe ...>...</iframe>,
    # <style>...</style>, <link ...>, <object>...</object>, <embed ...>
    text = re.sub(
        r'<\s*(script|iframe|object|embed|style|link|meta|form)\b[^>]*>.*?<\s*/\s*\1\s*>',
        '',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Самозакрывающиеся опасные теги
    text = re.sub(
        r'<\s*(script|iframe|object|embed|style|link|meta|form)\b[^>]*/?>',
        '',
        text,
        flags=re.IGNORECASE,
    )
    # Удаляем on* атрибуты (onclick, onerror, onload, ...)
    text = re.sub(r'\s+on[a-z]+\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+on[a-z]+\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)
    # Удаляем javascript: / vbscript: / data:text/html в href/src
    text = re.sub(r'(href|src)\s*=\s*"(?:\s*)(?:javascript|vbscript|data)\s*:[^"]*"',
                  r'\1="#"', text, flags=re.IGNORECASE)
    text = re.sub(r"(href|src)\s*=\s*'(?:\s*)(?:javascript|vbscript|data)\s*:[^']*'",
                  r'\1="#"', text, flags=re.IGNORECASE)
    return text


def _seller_roadmap_events():
    """События roadmap, видимые продавцу (audience=all|seller)."""
    return (
        RoadmapEvent.query
        .filter(RoadmapEvent.audience.in_(('all', 'seller')))
        .filter(RoadmapEvent.is_published == True)
        .order_by(RoadmapEvent.event_date.desc(), RoadmapEvent.id.desc())
        .all()
    )


# Совместимость со старой ссылкой: /seller/roadmap → /seller/info/roadmap.
@bp.route('/roadmap')
def _legacy_roadmap_redirect():
    return redirect(url_for('seller.info_roadmap'), code=301)


@bp.route('/info/roadmap')
def info_roadmap():
    """
    Страница «Траектория развития проекта» (продавец).
    URL: /seller/info/roadmap
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    return render_template(
        'seller/info_roadmap.html',
        title='Траектория развития проекта',
        roadmap_events=_seller_roadmap_events(),
        roadmap_categories=[
            {'code': code, 'label': label, 'color': color}
            for code, label, color in RoadmapEvent.CATEGORIES
        ],
        can_manage=False,
    )


@bp.route('/info/tariffs')
def info_tariffs():
    """
    Страница «Тарифы» (продавец).
    URL: /seller/info/tariffs
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    return render_template(
        'seller/info_tariffs.html',
        title='Тарифы',
        tile=SELLER_INFO_TILES['tariffs'],
    )


# =============================================================================
# Тариф: кабинет продавца (вкладки: Клиенты, Расчёты)
# =============================================================================
#
# Логика отображения та же, что в админке, но в роли "Клиентов" — только
# текущий селлер. Других селлеров он, естественно, не видит.
# -----------------------------------------------------------------------------

@bp.route('/tariffs')
def tariffs():
    """
    Раздел «Тариф» в кабинете продавца.
    URL: /seller/tariffs?tab=my|shop

    Вкладки:
        my   — «Мои тарифы»: все оплаченные подписки текущего продавца
               (активные, приостановленные, истёкшие, отключённые),
               для каждой есть кнопка «Продлить».
        shop — «Магазин тарифов»: плашки строк TariffRow с kind='cards',
               is_published=True и заполненными price_amount + duration_days
               (см. TariffRow.is_purchasable). По кнопке «Купить»
               открывается модалка с гипотетической оплатой.

    Глобальные правила (kind in cards_turnover / card_sale / category_sale)
    НЕ применяются автоматически — селлер должен явно нажать
    «Активировать» на плашке текущего тарифа. После активации
    создаётся подписка source='global_auto'. Список активных
    глобальных правил показывается баннером на обеих вкладках.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    tab = request.args.get('tab', 'my')
    if tab not in ('my', 'shop'):
        tab = 'my'

    seller = current_user

    # Глобальные активные правила — для баннера на обеих вкладках.
    # Берём из блоков, видимых продавцам (section in sellers/all).
    global_rules = (
        TariffRow.query
        .join(TariffBlock, TariffBlock.id == TariffRow.block_id)
        .filter(TariffRow.is_published.is_(True))
        .filter(TariffRow.is_active.is_(True))
        .filter(TariffBlock.section.in_(('sellers', 'all')))
        .filter(TariffRow.kind.in_((
            TariffRow.KIND_CARDS_TURNOVER,
            TariffRow.KIND_CARD_SALE,
            TariffRow.KIND_CATEGORY_SALE,
        )))
        .order_by(TariffBlock.sort_order, TariffRow.sort_order, TariffRow.id)
        .all()
    )

    if tab == 'shop':
        # Только фикс-тарифы (kind='cards'), у которых заполнены
        # числовые цена и срок (см. is_purchasable).
        rows = (
            TariffRow.query
            .join(TariffBlock, TariffBlock.id == TariffRow.block_id)
            .filter(TariffRow.is_published.is_(True))
            .filter(TariffBlock.section.in_(('sellers', 'all')))
            .order_by(TariffBlock.sort_order, TariffRow.sort_order, TariffRow.id)
            .all()
        )
        rows = [r for r in rows if r.is_purchasable]
        return render_template(
            'seller/tariffs.html',
            title='Тариф',
            tab='shop',
            rows=rows,
            subscriptions=[],
            global_rules=global_rules,
        )

    # tab == 'my' — подписки текущего продавца.
    # Показываем:
    #   • оплаченные индивидуальные (is_paid=True) — это обычные «купленные» тарифы;
    #   • «зафиксированные» глобальные правила (source='global_auto', status='active')
    #     — они не оплачены (is_paid=False), но это тоже «мои тарифы», которые
    #     зафиксированы за продавцом и открывают ему доступ к магазину.
    subscriptions = (
        SellerTariffSubscription.query
        .join(TariffRow, TariffRow.id == SellerTariffSubscription.row_id)
        .filter(SellerTariffSubscription.seller_id == seller.id)
        .filter(
            or_(
                SellerTariffSubscription.is_paid.is_(True),
                and_(
                    SellerTariffSubscription.source == SellerTariffSubscription.SOURCE_GLOBAL_AUTO,
                    SellerTariffSubscription.status == SellerTariffSubscription.STATUS_ACTIVE,
                ),
            )
        )
        .order_by(SellerTariffSubscription.expires_at.desc())
        .all()
    )
    return render_template(
        'seller/tariffs.html',
        title='Тариф',
        tab='my',
        subscriptions=subscriptions,
        rows=[],
        global_rules=global_rules,
    )


def _resolve_active_tariff(seller) -> dict:
    """
    Определить, какой тариф сейчас применяется к селлеру.

    Логика приоритета (для будущего биллинг-движка):
      1. Если у селлера есть активная подписка с source='self'
         (оплаченный фикс-тариф, который он сам купил) — её правила.
      2. Иначе — глобальные активные правила (TariffRow.is_active=True
         и kind in cards_turnover/card_sale/category_sale).

    Возвращает dict:
        {
            'source': 'self' | 'global' | 'none',
            'label':  'Ваш тариф: ...' | 'Глобальные правила: ...' | 'Нет активного тарифа',
            'self_subscription': SellerTariffSubscription | None,
            'global_rules': [TariffRow, ...],
        }
    """
    # 1) Ищем оплаченную подписку селлера (source='self') с is_active_now=True.
    self_sub = (
        SellerTariffSubscription.query
        .filter(
            SellerTariffSubscription.seller_id == seller.id,
            SellerTariffSubscription.source == SellerTariffSubscription.SOURCE_SELF,
            SellerTariffSubscription.is_paid.is_(True),
        )
        .order_by(SellerTariffSubscription.expires_at.desc())
        .first()
    )
    if self_sub and self_sub.is_active_now:
        return {
            'source': 'self',
            'label': f'Ваш тариф: {self_sub.row.name}',
            'self_subscription': self_sub,
            'global_rules': [],
        }

    # 2) Иначе — глобальные активные правила.
    global_rules = (
        TariffRow.query
        .join(TariffBlock, TariffBlock.id == TariffRow.block_id)
        .filter(TariffRow.is_published.is_(True))
        .filter(TariffRow.is_active.is_(True))
        .filter(TariffBlock.section.in_(('sellers', 'all')))
        .filter(TariffRow.kind.in_((
            TariffRow.KIND_CARDS_TURNOVER,
            TariffRow.KIND_CARD_SALE,
            TariffRow.KIND_CATEGORY_SALE,
        )))
        .all()
    )
    if global_rules:
        names = ', '.join(r.name for r in global_rules[:3])
        suffix = '' if len(global_rules) <= 3 else f' и ещё {len(global_rules) - 3}'
        return {
            'source': 'global',
            'label': f'Действуют глобальные правила: {names}{suffix}',
            'self_subscription': self_sub,  # может быть None или истёкшая
            'global_rules': global_rules,
        }

    return {
        'source': 'none',
        'label': 'Нет активного тарифа',
        'self_subscription': None,
        'global_rules': [],
    }


def _get_active_global_rules():
    """Возвращает список активных глобальных правил (TariffRow с kind in
    cards_turnover/card_sale/category_sale, is_published=True, is_active=True,
    block.section in ('sellers', 'all'))."""
    return (
        TariffRow.query
        .join(TariffBlock, TariffBlock.id == TariffRow.block_id)
        .filter(TariffRow.is_published.is_(True))
        .filter(TariffRow.is_active.is_(True))
        .filter(TariffBlock.section.in_(('sellers', 'all')))
        .filter(TariffRow.kind.in_((
            TariffRow.KIND_CARDS_TURNOVER,
            TariffRow.KIND_CARD_SALE,
            TariffRow.KIND_CATEGORY_SALE,
        )))
        .order_by(TariffBlock.sort_order, TariffRow.sort_order, TariffRow.id)
        .all()
    )


def _get_active_subscription(seller):
    """Возвращает «актуальную» подписку селлера (если есть).

    Приоритет:
      1) source='self' с is_paid=True — даже если истёкшая (нужна для
         грейс-периода).
      2) source='global_auto' — без фильтра по is_paid. Глобальная
         авто-активация создаёт sub с is_paid=False (это не покупка,
         а «зафиксировал правило»). Если у такого селлера нет self-подписки,
         мы всё равно должны учитывать global_auto как активную.
    Возвращает последнюю (по expires_at) или None.
    """
    # 1) self-подписка (приоритет)
    sub = (
        SellerTariffSubscription.query
        .filter(
            SellerTariffSubscription.seller_id == seller.id,
            SellerTariffSubscription.is_paid.is_(True),
            SellerTariffSubscription.source == SellerTariffSubscription.SOURCE_SELF,
        )
        .order_by(SellerTariffSubscription.expires_at.desc())
        .first()
    )
    if sub:
        return sub
    # 2) global_auto (любой is_paid, главное — есть запись)
    sub = (
        SellerTariffSubscription.query
        .filter(
            SellerTariffSubscription.seller_id == seller.id,
            SellerTariffSubscription.source == SellerTariffSubscription.SOURCE_GLOBAL_AUTO,
        )
        .order_by(SellerTariffSubscription.expires_at.desc())
        .first()
    )
    return sub


def _resolve_tariff_state(seller) -> dict:
    """Определить состояние тарифа у селлера для UI и блокировок.

    Возвращает словарь:
        {
            'state':           'paid' | 'grace' | 'locked' | 'global' | 'none',
            'subscription':    SellerTariffSubscription | None,
            'global_rules':    [TariffRow, ...],
            'days_to_expire':  int,  # до конца оплаченного периода
            'days_to_grace_end': int,  # до конца грейса (если в грейсе)
            'show_warning_banner': bool,  # показывать баннер на всех страницах
            'billed_amount':   float,  # пересчёт по обороту за текущий период
        }

    Логика:
      • есть self/global_auto подписка is_active_now=True → 'paid'
      • подписка оплачена, но в грейсе → 'grace'
      • подписка оплачена, грейс истёк → 'locked' (магазин блокируется)
      • нет подписки, но есть активные глобальные правила → 'global'
      • иначе → 'none'
    """
    sub = _get_active_subscription(seller)
    global_rules = _get_active_global_rules()

    state = {
        'state': 'none',
        'subscription': sub,
        'global_rules': global_rules,
        'days_to_expire': 0,
        'days_to_grace_end': 0,
        'show_warning_banner': False,
        'billed_amount': 0.0,
        # True для state='global', когда до expires_at осталось ≤ TARIFF_WARN_DAYS дней.
        # В этом случае на /tariffs?tab=my и /tariffs?tab=shop показываем кнопку
        # «Продлить ещё на 30 дней» (пора готовиться к грейсу). В остальных
        # состояниях кнопка не нужна (grace/locked/paid управляются отдельно,
        # а для state='global' с запасом по времени кнопка только сбивает с толку).
        'near_expiry': False,
    }

    if sub and sub.is_active_now:
        state['state'] = 'paid'
        state['days_to_expire'] = sub.days_to_expire
        state['show_warning_banner'] = state['days_to_expire'] <= TARIFF_WARN_DAYS
        # Пересчёт стоимости по обороту для текущего периода.
        if sub.row and sub.row.is_global_rule:
            period_start = sub.activated_at
            period_end = sub.expires_at
            state['billed_amount'] = sub.row.compute_billed_amount(
                seller, period_start, period_end
            )
        return state

    if sub and sub.is_in_grace:
        state['state'] = 'grace'
        state['days_to_grace_end'] = sub.days_to_grace_end
        state['show_warning_banner'] = True
        # Пересчёт суммы по обороту за истекший оплаченный период —
        # её увидит селлер на плашке «Глобальный процент — X ₽».
        if sub.row and sub.row.is_global_rule:
            state['billed_amount'] = sub.row.compute_billed_amount(
                seller, sub.activated_at, sub.expires_at
            )
        return state

    if sub and sub.is_locked:
        state['state'] = 'locked'
        state['show_warning_banner'] = True
        # В локе — тоже показываем последнюю начисленную сумму
        # (за истекший период), чтобы селлер знал, сколько надо оплатить.
        if sub.row and sub.row.is_global_rule:
            state['billed_amount'] = sub.row.compute_billed_amount(
                seller, sub.activated_at, sub.expires_at
            )
        return state

    # Явно активированный глобальный процент (sub.source='global_auto',
    # status='active', is_paid=False — это «зафиксировал правило»,
    # не покупка). Считается активным, если expires_at ещё не истёк.
    # Это ЕДИНСТВЕННЫЙ способ попасть в state='global' после фикса:
    # глобальный процент больше не применяется автоматически.
    if (
        sub
        and sub.source == SellerTariffSubscription.SOURCE_GLOBAL_AUTO
        and sub.status == SellerTariffSubscription.STATUS_ACTIVE
        and sub.is_paid is False
        and sub.expires_at > datetime.utcnow()
    ):
        state['state'] = 'global'
        state['days_to_expire'] = sub.days_to_expire
        state['show_warning_banner'] = state['days_to_expire'] <= TARIFF_WARN_DAYS
        state['near_expiry'] = state['days_to_expire'] <= TARIFF_WARN_DAYS
        if sub.row and sub.row.is_global_rule:
            state['billed_amount'] = sub.row.compute_billed_amount(
                seller, sub.activated_at, sub.expires_at
            )
        return state

    # Без подписки и без явной активации — состояние 'none'.
    # Глобальные правила НЕ применяются автоматически: селлер должен
    # сам нажать «Активировать» в /tariffs. До активации его магазин
    # заблокирован (@require_active_tariff).
    return state


def _is_seller_locked(seller) -> bool:
    """Селлер в состоянии 'locked' (магазин должен быть заблокирован)?"""
    return _resolve_tariff_state(seller)['state'] == 'locked'


def get_active_seller_ids() -> set:
    """Возвращает множество seller_id, товары которых должны быть видны
    покупателю в публичном каталоге.

    Селлер «видим» покупателю, если его тарифное состояние — 'paid',
    'grace' или 'global':
      • paid   — есть активная оплаченная индивидуальная подписка
                 (source='self'/'admin', is_paid=True, status='active',
                 expires_at > now);
      • grace  — подписка оплачена, но истёк обычный срок; грейс-период
                 ещё не закончился (grace_until > now);
      • global — селлер ЯВНО активировал глобальный процент через
                 /tariffs (source='global_auto', is_paid=False,
                 status='active', expires_at > now). Глобальный процент
                 НЕ применяется автоматически — только по кнопке
                 «Активировать» в кабинете продавца.

    Селлеры в состоянии 'locked' (оплаченная подписка с истёкшим
    грейсом) и 'none' (нет подписки и нет активации) НЕ попадают в
    результат — их товары скрываются из каталога.

    Возвращает ``set[int]`` (может быть пустым).
    """
    now = datetime.utcnow()

    # paid: оплаченная, status=active, срок не истёк.
    paid_ids = {
        sid for (sid,) in db.session.query(SellerTariffSubscription.seller_id)
        .filter(
            SellerTariffSubscription.is_paid.is_(True),
            SellerTariffSubscription.status == SellerTariffSubscription.STATUS_ACTIVE,
            SellerTariffSubscription.expires_at > now,
        )
        .distinct()
        .all()
    }

    # grace: оплаченная, expires_at <= now, grace_until > now.
    grace_ids = {
        sid for (sid,) in db.session.query(SellerTariffSubscription.seller_id)
        .filter(
            SellerTariffSubscription.is_paid.is_(True),
            SellerTariffSubscription.expires_at <= now,
            SellerTariffSubscription.grace_until.isnot(None),
            SellerTariffSubscription.grace_until > now,
        )
        .distinct()
        .all()
    }

    # global: селлер явно активировал глобальный процент
    # (sub.source='global_auto', status='active', expires_at > now,
    # is_paid=False). Глобальный процент НЕ применяется автоматически
    # — только после явной активации через /tariffs → «Активировать».
    global_ids = {
        sid for (sid,) in db.session.query(SellerTariffSubscription.seller_id)
        .filter(
            SellerTariffSubscription.source == SellerTariffSubscription.SOURCE_GLOBAL_AUTO,
            SellerTariffSubscription.status == SellerTariffSubscription.STATUS_ACTIVE,
            SellerTariffSubscription.expires_at > now,
        )
        .distinct()
        .all()
    }

    return paid_ids | grace_ids | global_ids


def require_active_tariff(view):
    """Декоратор: блокирует запросы селлера, если магазин в 'locked' или 'none'.

    Селлер в состоянии 'paid' / 'grace' / 'global' работает нормально.
    В 'locked' (грейс истёк) и 'none' (никогда не было тарифа и нет
    глобального правила) — редирект на /tariffs с флешкой.

    Исключения (всегда пропускаются): /tariffs/* (работа с тарифом),
    /api/cdek, /seller/logout.
    """
    from functools import wraps
    from flask import request

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, Seller):
            return view(*args, **kwargs)
        # Пути-исключения, на которые блокировка не действует
        path = request.path or ''
        if (
            path.startswith('/tariffs')
            or path.startswith('/api/cdek')
            or path.endswith('/logout')
        ):
            return view(*args, **kwargs)
        st = _resolve_tariff_state(current_user)['state']
        if st in ('locked', 'none'):
            if st == 'locked':
                flash(
                    'Магазин заблокирован: оплатите тариф, чтобы продолжить работу.',
                    'error',
                )
            else:
                flash(
                    'Чтобы продолжить работу, активируйте тариф.',
                    'error',
                )
            return redirect(url_for('seller.tariffs', tab='shop') + '#blocked')
        return view(*args, **kwargs)

    return wrapper


@bp.route('/tariffs/rows/<int:row_id>/buy', methods=['POST'])
def tariff_buy(row_id):
    """
    Купить тариф: гипотетическая оплата.

    Создаёт:
      • SellerTariffSubscription (is_paid=True, status=active)
      • TariffTransaction (amount=row.price_amount)

    Платёжный модуль ещё не подключён — кнопка «Оплатить» в модалке
    сразу помечает платёж как совершённый. Когда появится реальный
    модуль оплаты, эту точку нужно будет перевести в режим «pending»
    и подтверждать только по callback'у от платёжной системы.

    Цена берётся из row.price_amount (float), срок — из row.duration_days
    (int). Если строка не опубликована или одно из чисел не заполнено —
    покупка отклоняется.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    row = db.session.get(TariffRow, row_id)
    if not row or not row.is_purchasable:
        flash('Тариф недоступен для покупки.', 'error')
        return redirect(url_for('seller.tariffs', tab='shop'))

    now = datetime.utcnow()
    activated_at = now
    expires_at = now + timedelta(days=row.duration_days)

    sub = SellerTariffSubscription(
        seller_id=current_user.id,
        row_id=row.id,
        is_paid=True,
        status=SellerTariffSubscription.STATUS_ACTIVE,
        activated_at=activated_at,
        expires_at=expires_at,
    )
    db.session.add(sub)
    db.session.flush()  # получаем sub.id до commit

    tx = TariffTransaction(
        seller_id=current_user.id,
        row_id=row.id,
        subscription_id=sub.id,
        amount=float(row.price_amount or 0.0),
        paid_at=now,
        note='Гипотетическая оплата',
    )
    db.session.add(tx)
    db.session.commit()

    flash(f'Тариф «{row.name}» успешно оплачен и активирован.', 'success')
    return redirect(url_for('seller.tariffs', tab='my'))


@bp.route('/tariffs/activate', methods=['POST'])
def tariff_activate_global():
    """
    «Зафиксировать» глобальное правило: превратить автоматически
    применяемое правило в явную подписку селлера.

    Логика:
      • Берём самое приоритетное активное глобальное правило (TariffRow
        с is_active=True, is_published=True, kind in
        cards_turnover/card_sale/category_sale, в блоках section in
        ('sellers', 'all')).
      • Создаём SellerTariffSubscription с source='global_auto',
        is_paid=True, status='active'.
      • expires_at = now + row.effective_period_days (period_days для
        глобальных, duration_days для cards, фолбэк 30).
      • grace_until = expires_at + 5 дней.
      • Сразу эмулируем списание: создаём TariffTransaction с суммой,
        пересчитанной по обороту за последние effective_period_days
        (см. TariffRow.compute_billed_amount).
      • Приоритет правил: сортируем по sort_order блока, потом строки,
        потом id — выбираем первое.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    global_rules = _get_active_global_rules()
    if not global_rules:
        flash('Сейчас нет активных глобальных правил для активации.', 'error')
        return redirect(url_for('seller.tariffs', tab='shop'))

    row = global_rules[0]
    now = datetime.utcnow()
    period_days = row.effective_period_days

    expires_at = now + timedelta(days=period_days)
    sub = SellerTariffSubscription(
        seller_id=current_user.id,
        row_id=row.id,
        source=SellerTariffSubscription.SOURCE_GLOBAL_AUTO,
        # is_paid=False — это «зафиксировал правило», а не покупка.
        # Деньги списываются не подпиской, а транзакцией (TariffTransaction
        # ниже). sub.is_paid остаётся False, чтобы не путать с
        # индивидуальным тарифом paid.
        is_paid=False,
        status=SellerTariffSubscription.STATUS_ACTIVE,
        activated_at=now,
        expires_at=expires_at,
    )
    sub.recompute_grace(TARIFF_GRACE_DAYS)
    sub.last_billed_at = now
    db.session.add(sub)
    db.session.flush()

    # Эмулируем списание по обороту за последний период.
    amount = row.compute_billed_amount(
        current_user, now - timedelta(days=period_days), now
    )
    tx = TariffTransaction(
        seller_id=current_user.id,
        row_id=row.id,
        subscription_id=sub.id,
        amount=float(amount or 0.0),
        paid_at=now,
        note='Фиксация глобального правила (эмуляция списания по обороту)',
    )
    db.session.add(tx)
    db.session.commit()

    flash(
        f'Глобальное правило «{row.name}» зафиксировано. '
        f'Срок действия — до {expires_at.strftime("%d.%m.%Y")}, '
        f'списано {format_price(amount)} ₽.',
        'success',
    )
    return redirect(url_for('seller.tariffs', tab='my'))


@bp.route('/tariffs/subscriptions/<int:subscription_id>/extend', methods=['POST'])
def tariff_subscription_extend(subscription_id):
    """
    Продлить существующую подписку (в т.ч. в грейсе или после блокировки).

    Поведение:
      • Подписка может быть в любом состоянии (active/paused/disabled/expired).
      • Если есть self-подписка с истёкшим сроком — это «оживление» после
        лока: новый expires_at = now + row.effective_period_days.
      • Если подписка ещё жива — продлеваем от её expires_at.
      • Если подписки в грейсе/локе — стартуем от now, grace пересчитываем
        заново (expires + 5 дней).
      • Стоимость продления для self-тарифа: row.price_amount (фикс).
        Для global_auto: пересчёт по обороту за последний период.
      • Создаётся новая SellerTariffSubscription (новая запись, чтобы
        сохранить историю), статус старой переводится в 'disabled'.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub or sub.seller_id != current_user.id:
        abort(404)

    row = sub.row
    if not row:
        flash('Тариф больше не доступен.', 'error')
        return redirect(url_for('seller.tariffs', tab='my'))

    now = datetime.utcnow()
    period_days = row.effective_period_days

    # База для продления: если старая ещё жива — от её expires_at,
    # иначе — от now.
    if (
        sub.status == SellerTariffSubscription.STATUS_ACTIVE
        and sub.expires_at
        and sub.expires_at > now
    ):
        base = sub.expires_at
    else:
        base = now
    expires_at = base + timedelta(days=period_days)

    # Закрываем старую запись.
    if sub.status != SellerTariffSubscription.STATUS_DISABLED:
        sub.disable()

    new_sub = SellerTariffSubscription(
        seller_id=current_user.id,
        row_id=row.id,
        source=sub.source,  # сохраняем источник (self или global_auto)
        is_paid=True,
        status=SellerTariffSubscription.STATUS_ACTIVE,
        activated_at=now,
        expires_at=expires_at,
    )
    new_sub.recompute_grace(TARIFF_GRACE_DAYS)
    new_sub.last_billed_at = now
    db.session.add(new_sub)
    db.session.flush()

    # Стоимость: для self-тарифа — фикс, для global_auto — пересчёт.
    if row.is_purchasable:
        amount = float(row.price_amount or 0.0)
        note = 'Продление тарифа'
    else:
        amount = float(
            row.compute_billed_amount(
                current_user, now - timedelta(days=period_days), now
            ) or 0.0
        )
        note = 'Продление глобального правила (пересчёт по обороту)'

    tx = TariffTransaction(
        seller_id=current_user.id,
        row_id=row.id,
        subscription_id=new_sub.id,
        amount=amount,
        paid_at=now,
        note=note,
    )
    db.session.add(tx)
    db.session.commit()

    flash(
        f'Тариф «{row.name}» продлён до {expires_at.strftime("%d.%m.%Y")}. '
        f'Списано {format_price(amount)} ₽.',
        'success',
    )
    return redirect(url_for('seller.tariffs', tab='my'))


@bp.route('/tariffs/subscriptions/<int:subscription_id>/renew', methods=['POST'])
def tariff_subscription_renew(subscription_id):
    """
    Продлить тариф: создаёт новую подписку на ту же строку TariffRow.

    • Если старая подписка ещё активна и не истекла — новая
      активируется с момента истечения старой.
    • Если истекла / приостановлена / отключена — с текущего момента.
    Длительность прибавки = row.duration_days.
    Создаётся новая TariffTransaction.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub or sub.seller_id != current_user.id:
        abort(404)

    row = sub.row
    if not row or not row.is_purchasable:
        flash('Тариф больше недоступен.', 'error')
        return redirect(url_for('seller.tariffs', tab='my'))

    now = datetime.utcnow()
    if (
        sub.status == SellerTariffSubscription.STATUS_ACTIVE
        and sub.expires_at
        and sub.expires_at > now
    ):
        activated_at = sub.expires_at
    else:
        activated_at = now
    expires_at = activated_at + timedelta(days=row.duration_days)

    new_sub = SellerTariffSubscription(
        seller_id=current_user.id,
        row_id=row.id,
        is_paid=True,
        status=SellerTariffSubscription.STATUS_ACTIVE,
        activated_at=activated_at,
        expires_at=expires_at,
    )
    db.session.add(new_sub)
    db.session.flush()

    tx = TariffTransaction(
        seller_id=current_user.id,
        row_id=row.id,
        subscription_id=new_sub.id,
        amount=float(row.price_amount or 0.0),
        paid_at=now,
        note='Продление тарифа',
    )
    db.session.add(tx)
    db.session.commit()

    flash(f'Тариф «{row.name}» продлён до {expires_at.strftime("%d.%m.%Y")}.', 'success')
    return redirect(url_for('seller.tariffs', tab='my'))


@bp.route('/tariffs/subscriptions/<int:subscription_id>/pause', methods=['POST'])
def tariff_subscription_pause(subscription_id):
    """Приостановить действие тарифа в своём магазине."""
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub or sub.seller_id != current_user.id:
        abort(404)
    sub.pause()
    db.session.commit()
    flash('Тариф приостановлен.', 'success')
    return redirect(url_for('seller.tariffs', tab='my'))


@bp.route('/tariffs/subscriptions/<int:subscription_id>/resume', methods=['POST'])
def tariff_subscription_resume(subscription_id):
    """Возобновить приостановленный тариф в своём магазине."""
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub or sub.seller_id != current_user.id:
        abort(404)
    sub.resume()
    db.session.commit()
    flash('Тариф возобновлён.', 'success')
    return redirect(url_for('seller.tariffs', tab='my'))


@bp.route('/tariffs/subscriptions/<int:subscription_id>/disable', methods=['POST'])
def tariff_subscription_disable(subscription_id):
    """Отключить тариф в своём магазине."""
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub or sub.seller_id != current_user.id:
        abort(404)
    sub.disable()
    db.session.commit()
    flash('Тариф отключён.', 'success')
    return redirect(url_for('seller.tariffs', tab='my'))


@bp.route('/info/education')
def info_education():
    """
    Страница «Обучение» (продавец).
    URL: /seller/info/education?tag=<slug>
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    tag_slug = (request.args.get('tag') or '').strip()
    active_tag = None
    if tag_slug:
        # Ищем оригинальный тег по slugу — обратно через tag_slug всех тегов.
        for t in EduMaterial.all_tags():
            if EduMaterial.tag_slug(t) == tag_slug:
                active_tag = t
                break

    # Продавцу показываем только материалы с аудиторией all/seller
    # (материалы audience='admin' ему видеть не нужно).
    q = (
        EduMaterial.query
        .filter(EduMaterial.audience.in_(('all', 'seller')))
        .filter(EduMaterial.is_published == True)
    )
    if active_tag:
        q = q.filter(EduMaterial.tag == active_tag)
    materials = q.order_by(EduMaterial.created_at.desc()).all()

    all_tags_with_slug = [(t, EduMaterial.tag_slug(t)) for t in EduMaterial.all_tags()]

    return render_template(
        'seller/info_education.html',
        title='Обучение',
        tile=SELLER_INFO_TILES['education'],
        materials=materials,
        all_tags=all_tags_with_slug,
        active_tag=active_tag,
        active_tag_slug=tag_slug,
    )


@bp.route('/info/education/materials/<int:material_id>', methods=['GET'])
def edu_view(material_id):
    """
    Страница одного учебного материала (продавец).
    URL: /seller/info/education/materials/<id>
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    material = db.session.get(EduMaterial, material_id)
    # 404, если материала нет, он не опубликован,
    # или он предназначен только для админа.
    if not material or not material.is_published or material.audience == 'admin':
        abort(404)

    return render_template(
        'seller/edu_view.html',
        title=material.title,
        material=material,
        active_tag=material.tag,
        active_tag_slug=EduMaterial.tag_slug(material.tag) if material.tag else '',
    )


@bp.route('/info/roadmap/events.json')
def roadmap_events_json():
    """
    JSON-эндпоинт для FullCalendar на странице продавца: список событий
    «Траектории развития», видимых продавцу (audience=all|seller).
    URL: seller.domain/info/roadmap/events.json
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return jsonify({'error': 'forbidden'}), 403

    q = (
        RoadmapEvent.query
        .filter(RoadmapEvent.audience.in_(('all', 'seller')))
        .filter(RoadmapEvent.is_published == True)
    )

    from datetime import date as _date
    start_raw = request.args.get('start')
    end_raw = request.args.get('end')
    if start_raw:
        try:
            q = q.filter(RoadmapEvent.event_date >= _date.fromisoformat(start_raw[:10]))
        except ValueError:
            pass
    if end_raw:
        try:
            q = q.filter(RoadmapEvent.event_date <= _date.fromisoformat(end_raw[:10]))
        except ValueError:
            pass

    return jsonify([ev.to_fullcalendar() for ev in q.all()])


@bp.route('/products')
def products():
    """
    Список товаров продавца.
    URL: seller.domain/products
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status')
    
    query = Product.query.filter_by(seller_id=current_user.id)
    
    if status:
        query = query.filter_by(status=status)
    
    pagination = query.order_by(Product.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('seller/products.html',
                         title='Товары',
                         products=pagination.items,
                         pagination=pagination,
                         current_status=status)


@bp.route('/products/new', methods=['GET', 'POST'])
@require_active_tariff
def product_new():
    """
    Создание нового товара.
    URL: seller.domain/products/new
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    # Категории - получаем все для каскадного выбора
    categories = Category.query.filter(
        Category.is_active == True
    ).order_by(Category.name).all()
    
    # Параметры для категорий
    category_params = {}
    for cat in categories:
        params = cat.get_all_parameters()
        if params:
            category_params[cat.id] = [
                {
                    'id': p.id,
                    'name': p.name,
                    'type': p.type,
                    'predefined_values': list(p.predefined_values) if p.predefined_values else []
                }
                for p in params
            ]
    
    # Общие карточки продавца
    common_cards = db.session.query(Product.common_card).filter(
        Product.seller_id == current_user.id,
        Product.common_card != None
    ).distinct().all()
    common_cards = [c[0] for c in common_cards]
    
    if request.method == 'POST':
        name = request.form.get('name')
        category_id = request.form.get('category_id', type=int)
        price = request.form.get('price', type=float)
        article = request.form.get('article')
        description = _sanitize_html(request.form.get('description'))
        stock_quantity = request.form.get('stock_quantity', 0, type=int)
        common_card = request.form.get('common_card')
        max_discount = request.form.get('max_discount', 0, type=int)
        
        # Валидация
        if not all([name, category_id, price, article]):
            flash('Заполните обязательные поля.', 'error')
            return render_template('seller/product_form.html',
                                 title='Новый товар',
                                 categories=categories,
                                 common_cards=common_cards,
                                 category_params=category_params,
                                 product=None,
                                 product_params={})
        
        # Проверка уникальности артикула
        if Product.query.filter_by(article=article).first():
            flash('Артикул уже существует.', 'error')
            return render_template('seller/product_form.html',
                                 title='Новый товар',
                                 categories=categories,
                                 common_cards=common_cards,
                                 category_params=category_params,
                                 product=None,
                                 product_params={})
        
        # Создание товара
        product = Product(
            name=name,
            slug=slugify(name),
            description=description,
            price=price,
            article=article,
            stock_quantity=stock_quantity,
            max_discount_percent=max_discount,
            common_card=common_card or None,
            seller_id=current_user.id,
            category_id=category_id,
            status='on_moderation'
        )
        
        db.session.add(product)
        db.session.flush()  # Получаем ID

        # Генерация slug с ID
        product.slug = f"{slugify(name)}-{product.id}"

        # Системный артикул — генерируется автоматически (WML-{seller_id}-{ts}).
        # Присваивается только если продавец не указал свой явно и не
        # воспользовался кнопкой «Использовать системный артикул».
        if not product.system_sku:
            product.system_sku = Product.generate_system_sku(current_user.id)
        
        # Pillow опционален: если установлен — масштабируем и оптимизируем,
        # если нет — сохраняем как есть. Импорт НЕ должен валить POST
        # целиком, иначе товар не создать без картинки.
        try:
            from PIL import Image
        except ImportError:
            Image = None

        # Обработка загрузки изображения с масштабированием
        if 'main_image' in request.files:
            file = request.files['main_image']
            if file and file.filename:
                from werkzeug.utils import secure_filename
                import os
                from flask import current_app

                # Получаем абсолютный путь к директории загрузок
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
                os.makedirs(upload_dir, exist_ok=True)

                # Генерируем уникальное имя файла
                ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
                filename = f"{product.id}_{secure_filename(file.filename.rsplit('.', 1)[0])}.{ext}"
                filepath = os.path.join(upload_dir, filename)

                # Читаем изображение и масштабируем
                saved_ok = False
                try:
                    if Image is not None:
                        # Pillow установлен — масштабируем и сохраняем как JPEG.
                        img = Image.open(file)
                        max_size = (800, 800)
                        img.thumbnail(max_size, Image.Resampling.LANCZOS)
                        if img.mode in ('RGBA', 'P'):
                            img = img.convert('RGB')
                        img.save(filepath, 'JPEG', quality=85, optimize=True)
                        saved_ok = True
                    else:
                        # Pillow не установлен — сохраняем файл как есть.
                        file.seek(0)
                        file.save(filepath)
                        saved_ok = True
                except Exception as e:
                    # Если не удалось обработать, пробуем сохранить как есть
                    try:
                        file.seek(0)  # Возвращаемся в начало файла
                        file.save(filepath)
                        saved_ok = os.path.exists(filepath) and os.path.getsize(filepath) > 0
                    except Exception:
                        saved_ok = False

                # Не пишем запись в БД, если файл реально не сохранился — иначе
                # карточка товара будет показывать битый URL (404 на /static/uploads/...).
                if not saved_ok or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    flash('Не удалось сохранить основное фото — попробуйте другой файл (jpg/png, до ~10 МБ).', 'warning')
                else:
                    # Сохраняем фото в БД через ProductPhoto (основное изображение)
                    main_photo = ProductPhoto(
                        product_id=product.id,
                        path=filename,
                        is_main=True,
                        sort_order=0
                    )
                    db.session.add(main_photo)

        # Обработка дополнительных изображений (новый формат с additional_images[])
        additional_files = request.files.getlist('additional_images[]')
        for idx, file in enumerate(additional_files):
            if file and file.filename:
                from werkzeug.utils import secure_filename
                import os
                from flask import current_app

                # Получаем абсолютный путь к директории загрузок
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
                os.makedirs(upload_dir, exist_ok=True)

                # Генерируем уникальное имя файла с timestamp
                import time
                ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
                timestamp = str(int(time.time()))
                filename = f"{product.id}_{timestamp}_{idx}_{secure_filename(file.filename.rsplit('.', 1)[0])}.{ext}"
                filepath = os.path.join(upload_dir, filename)

                # Читаем изображение и масштабируем
                try:
                    if Image is not None:
                        img = Image.open(file)
                        max_size = (800, 800)
                        img.thumbnail(max_size, Image.Resampling.LANCZOS)
                        if img.mode in ('RGBA', 'P'):
                            img = img.convert('RGB')
                        img.save(filepath, 'JPEG', quality=85, optimize=True)
                    else:
                        file.seek(0)
                        file.save(filepath)
                except Exception as e:
                    try:
                        file.seek(0)
                        file.save(filepath)
                    except:
                        pass
                
                # Сохраняем фото в БД
                photo = ProductPhoto(
                    product_id=product.id,
                    path=filename,
                    is_main=False,
                    sort_order=idx + 1
                )
                db.session.add(photo)
        
        # Сохранение параметров
        param_ids = request.form.getlist('param_ids')
        param_values = request.form.getlist('param_values')
        
        for param_id, value in zip(param_ids, param_values):
            if value:
                product_param = ProductParameter(
                    product_id=product.id,
                    parameter_id=int(param_id),
                    value=value
                )
                db.session.add(product_param)
        
        # Обработка карточки товара
        card_option = request.form.get('card_option', 'none')
        
        if card_option == 'new':
            # Создание новой карточки
            card_name = request.form.get('card_name')
            card_parameter_id = request.form.get('card_parameter_id', type=int)
            
            if card_name and card_parameter_id:
                new_card = ProductCard(
                    name=card_name,
                    seller_id=current_user.id,
                    category_id=category_id,
                    grouping_parameter_id=card_parameter_id
                )
                db.session.add(new_card)
                db.session.flush()
                product.product_card_id = new_card.id
        
        elif card_option == 'existing':
            # Привязка к существующей карточке
            existing_card_id = request.form.get('existing_card_id', type=int)
            if existing_card_id:
                # Проверяем, что карточка принадлежит продавцу и той же категории
                card = ProductCard.query.filter_by(
                    id=existing_card_id,
                    seller_id=current_user.id,
                    category_id=category_id
                ).first()
                if card:
                    product.product_card_id = card.id
        
        db.session.commit()
        
        flash('Товар отправлен на модерацию.', 'success')
        return redirect(url_for('seller.products'))
    
    # Категории - получаем все для каскадного выбора
    categories = Category.query.filter(
        Category.is_active == True
    ).order_by(Category.name).all()
    
    # Параметры для категорий - нужно передать структуру category_id -> [параметры]
    category_params = {}
    for cat in categories:
        params = cat.get_all_parameters()
        if params:
            category_params[cat.id] = [{'id': p.id, 'name': p.name, 'type': p.type, 'predefined_values': p.predefined_values} for p in params]
    
    # Общие карточки продавца
    common_cards = db.session.query(Product.common_card).filter(
        Product.seller_id == current_user.id,
        Product.common_card != None
    ).distinct().all()
    common_cards = [c[0] for c in common_cards]
    
    # Карточки товаров продавца (для каждой категории)
    product_cards = ProductCard.query.filter_by(seller_id=current_user.id).all()
    product_cards_by_category = {}
    for card in product_cards:
        if card.category_id not in product_cards_by_category:
            product_cards_by_category[card.category_id] = []
        product_cards_by_category[card.category_id].append({
            'id': card.id,
            'name': card.name,
            'parameter_id': card.grouping_parameter_id,
            'parameter_name': card.grouping_parameter.name if card.grouping_parameter else ''
        })
    
    return render_template('seller/product_form.html',
                         title='Новый товар',
                         categories=categories,
                         common_cards=common_cards,
                         category_params=category_params,
                         product_cards_by_category=product_cards_by_category,
                         product=None,
                         product_params={})


@bp.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
@require_active_tariff
def product_edit(product_id):
    """
    Редактирование товара.
    URL: seller.domain/products/{id}/edit
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    product = db.session.get(Product, product_id)
    if not product or product.seller_id != current_user.id:
        abort(404)
    
    if request.method == 'POST':
        product.name = request.form.get('name')
        product.description = _sanitize_html(request.form.get('description'))
        product.price = request.form.get('price', type=float)
        product.stock_quantity = request.form.get('stock_quantity', 0, type=int)
        product.max_discount_percent = request.form.get('max_discount', 0, type=int)
        product.common_card = request.form.get('common_card') or None

        # Артикул — редактируемый. Уникальность проверяем только если меняется.
        new_article = request.form.get('article')
        if new_article and new_article != product.article:
            if Product.query.filter(Product.id != product.id,
                                    Product.article == new_article).first():
                flash('Артикул уже используется другим товаром.', 'error')
                return render_template('seller/product_form.html',
                                     title='Редактирование товара',
                                     categories=categories,
                                     common_cards=common_cards,
                                     category_params=category_params,
                                     product=product,
                                     product_params=product_params,
                                     moderation_remarks=moderation_remarks)
            product.article = new_article
        
        # Изменение категории требует модерации
        new_category_id = request.form.get('category_id', type=int)
        if new_category_id != product.category_id:
            product.category_id = new_category_id
            product.status = 'on_moderation'
        
        # Обработка изображений при редактировании
        keep_main_image = request.form.get('keep_main_image')
        keep_photos = request.form.getlist('keep_photos[]')
        
        # Удаляем фото, которые не были отмечены для сохранения
        for photo in product.photos.all():
            if photo.is_main:
                # Основное фото
                if not keep_main_image:
                    # Удаляем файл с диска
                    try:
                        import os
                        from flask import current_app
                        upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
                        filepath = os.path.join(upload_dir, photo.path)
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    except:
                        pass
                    db.session.delete(photo)
            else:
                # Дополнительные фото
                if str(photo.id) not in keep_photos:
                    # Удаляем файл с диска
                    try:
                        import os
                        from flask import current_app
                        upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
                        filepath = os.path.join(upload_dir, photo.path)
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    except:
                        pass
                    db.session.delete(photo)
        
        # Загрузка нового основного изображения
        if 'main_image' in request.files:
            file = request.files['main_image']
            if file and file.filename:
                from werkzeug.utils import secure_filename
                import os
                from PIL import Image
                from flask import current_app
                
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
                os.makedirs(upload_dir, exist_ok=True)
                
                import time
                ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
                timestamp = str(int(time.time()))
                filename = f"{product.id}_{timestamp}_main_{secure_filename(file.filename.rsplit('.', 1)[0])}.{ext}"
                filepath = os.path.join(upload_dir, filename)
                
                saved_ok = False
                try:
                    img = Image.open(file)
                    max_size = (800, 800)
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)
                    if img.mode in ('RGBA', 'P'):
                        img = img.convert('RGB')
                    img.save(filepath, 'JPEG', quality=85, optimize=True)
                    saved_ok = True
                except Exception:
                    try:
                        file.seek(0)
                        file.save(filepath)
                        saved_ok = os.path.exists(filepath) and os.path.getsize(filepath) > 0
                    except Exception:
                        saved_ok = False

                if not saved_ok or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    flash('Не удалось сохранить новое основное фото — попробуйте другой файл.', 'warning')
                else:
                    # Добавляем как основное фото
                    main_photo = ProductPhoto(
                        product_id=product.id,
                        path=filename,
                        is_main=True,
                        sort_order=0
                    )
                    db.session.add(main_photo)
        
        # Загрузка новых дополнительных изображений
        additional_files = request.files.getlist('additional_images[]')
        for idx, file in enumerate(additional_files):
            if file and file.filename:
                from werkzeug.utils import secure_filename
                import os
                from PIL import Image
                from flask import current_app
                
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
                os.makedirs(upload_dir, exist_ok=True)
                
                import time
                ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'jpg'
                timestamp = str(int(time.time()))
                filename = f"{product.id}_{timestamp}_{idx}_{secure_filename(file.filename.rsplit('.', 1)[0])}.{ext}"
                filepath = os.path.join(upload_dir, filename)
                
                saved_ok = False
                try:
                    img = Image.open(file)
                    max_size = (800, 800)
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)
                    if img.mode in ('RGBA', 'P'):
                        img = img.convert('RGB')
                    img.save(filepath, 'JPEG', quality=85, optimize=True)
                    saved_ok = True
                except Exception:
                    try:
                        file.seek(0)
                        file.save(filepath)
                        saved_ok = os.path.exists(filepath) and os.path.getsize(filepath) > 0
                    except Exception:
                        saved_ok = False

                if not saved_ok or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    flash(f'Не удалось сохранить дополнительное фото #{idx + 1} — попробуйте другой файл.', 'warning')
                else:
                    # Добавляем как дополнительное фото
                    photo = ProductPhoto(
                        product_id=product.id,
                        path=filename,
                        is_main=False,
                        sort_order=idx + 100  # Чтобы не конфликтовать с существующими
                    )
                    db.session.add(photo)
        
        # Изменение параметров требует модерации
        param_ids = request.form.getlist('param_ids')
        param_values = request.form.getlist('param_values')
        
        # Удаление старых параметров
        ProductParameter.query.filter_by(product_id=product.id).delete()
        
        # Добавление новых
        for param_id, value in zip(param_ids, param_values):
            if value:
                product_param = ProductParameter(
                    product_id=product.id,
                    parameter_id=int(param_id),
                    value=value
                )
                db.session.add(product_param)
        
        # Обработка карточки товара
        card_option = request.form.get('card_option', 'none')
        
        if card_option == 'none':
            # Без карточки
            product.product_card_id = None
        
        elif card_option == 'new':
            # Создание новой карточки
            card_name = request.form.get('card_name')
            card_parameter_id = request.form.get('card_parameter_id', type=int)
            
            if card_name and card_parameter_id:
                new_card = ProductCard(
                    name=card_name,
                    seller_id=current_user.id,
                    category_id=product.category_id,
                    grouping_parameter_id=card_parameter_id
                )
                db.session.add(new_card)
                db.session.flush()
                product.product_card_id = new_card.id
        
        elif card_option == 'existing':
            # Привязка к существующей карточке
            existing_card_id = request.form.get('existing_card_id', type=int)
            if existing_card_id:
                card = ProductCard.query.filter_by(
                    id=existing_card_id,
                    seller_id=current_user.id,
                    category_id=product.category_id
                ).first()
                if card:
                    product.product_card_id = card.id
        
        # Изменение цены или количества не требует модерации
        db.session.commit()
        
        flash('Изменения сохранены.', 'success')
        return redirect(url_for('seller.products'))
    
    # Категории - получаем все для каскадного выбора
    categories = Category.query.filter(
        Category.is_active == True
    ).order_by(Category.name).all()
    
    # Параметры для категорий
    category_params = {}
    for cat in categories:
        params = cat.get_all_parameters()
        if params:
            category_params[cat.id] = [
                {
                    'id': p.id,
                    'name': p.name,
                    'type': p.type,
                    'predefined_values': list(p.predefined_values) if p.predefined_values else []
                }
                for p in params
            ]
    
    common_cards = db.session.query(Product.common_card).filter(
        Product.seller_id == current_user.id,
        Product.common_card != None
    ).distinct().all()
    common_cards = [c[0] for c in common_cards]
    
    # Карточки товаров продавца (для каждой категории)
    product_cards = ProductCard.query.filter_by(seller_id=current_user.id).all()
    product_cards_by_category = {}
    for card in product_cards:
        if card.category_id not in product_cards_by_category:
            product_cards_by_category[card.category_id] = []
        product_cards_by_category[card.category_id].append({
            'id': card.id,
            'name': card.name,
            'parameter_id': card.grouping_parameter_id,
            'parameter_name': card.grouping_parameter.name if card.grouping_parameter else ''
        })
    
    # Параметры товара
    product_params = {p.parameter_id: p.value for p in product.parameters.all()}
    
    # Примечания модерации (причина отклонения)
    moderation_remarks = product.moderation_remarks.order_by(
        db.desc('created_at')
    ).all() if product.status == 'rejected' else []
    
    return render_template('seller/product_form.html',
                         title='Редактирование товара',
                         categories=categories,
                         common_cards=common_cards,
                         category_params=category_params,
                         product_cards_by_category=product_cards_by_category,
                         product=product,
                         product_params=product_params,
                         moderation_remarks=moderation_remarks)


@bp.route('/products/<int:product_id>/toggle-status', methods=['POST'])
@require_active_tariff
def product_toggle_status(product_id):
    """
    Переключение статуса товара.
    URL: seller.domain/products/{id}/toggle-status
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    product = db.session.get(Product, product_id)
    if not product or product.seller_id != current_user.id:
        abort(404)
    
    # Переключаем статус
    if product.status == 'active':
        product.status = 'inactive'
        flash('Товар деактивирован.', 'success')
    elif product.status == 'inactive':
        product.status = 'active'
        flash('Товар активирован.', 'success')
    else:
        flash('Невозможно изменить статус товара на модерации.', 'error')
    
    db.session.commit()
    return redirect(url_for('seller.products'))


@bp.route('/products/<int:product_id>/delete', methods=['POST'])
@require_active_tariff
def product_delete(product_id):
    """
    Удаление товара.
    URL: seller.domain/products/{id}/delete
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    product = db.session.get(Product, product_id)
    if not product or product.seller_id != current_user.id:
        abort(404)
    
    db.session.delete(product)
    db.session.commit()

    flash('Товар удалён.', 'success')
    return redirect(url_for('seller.products'))


@bp.route('/products/<int:product_id>/copy', methods=['POST'])
@require_active_tariff
def product_copy(product_id):
    """
    Создание копии товара.
    Копируются: name (с суффиксом «(копия)»), описание, цена, остаток,
    скидки, общая карточка, параметры (ProductParameter), фото (с копированием
    файлов на диск). Артикул и системный артикул — новые. Статус —
    on_moderation (копия проходит модерацию как новый товар).

    URL: seller.domain/products/{id}/copy
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    src = db.session.get(Product, product_id)
    if not src or src.seller_id != current_user.id:
        abort(404)

    import os
    import shutil
    import time as _time
    from werkzeug.utils import secure_filename
    from flask import current_app
    from app.models.products import ProductParameter, ProductPhoto

    # Уникализируем имя файла для копий фото, чтобы не было коллизий.
    def _new_photo_path(old_path: str) -> str:
        name, ext = os.path.splitext(old_path)
        ext = (ext or '.jpg').lower()
        stamp = int(_time.time() * 1000)
        return f"copy_{src.id}_{stamp}_{secure_filename(name)}{ext}"

    # 1. Создаём новый товар (без привязки к product_card — карточки не копируем,
    # продавец при желании подключит заново).
    new_product = Product(
        name=f"{src.name} (копия)",
        slug=None,  # пересоберём после flush
        description=src.description,
        price=src.price,
        max_discount_percent=src.max_discount_percent,
        current_discount=0,  # скидка не наследуется — это «новый» товар
        # article должен быть уникальным; добавим суффикс копии.
        article=f"{src.article}-copy-{int(_time.time())}"[:50],
        system_sku=Product.generate_system_sku(current_user.id),
        stock_quantity=src.stock_quantity,
        low_stock_threshold=src.low_stock_threshold,
        weight=src.weight,
        volume=src.volume,
        common_card=src.common_card,
        product_card_id=None,  # карточки не копируем
        seller_id=current_user.id,
        category_id=src.category_id,
        status='on_moderation',  # копия проходит модерацию
        moderated_at=None,
        published_at=None,
        views_count=0,
        cart_adds_count=0,
    )

    db.session.add(new_product)
    db.session.flush()  # получаем id

    new_product.slug = f"{slugify(new_product.name)}-{new_product.id}"

    # 2. Копируем фото: и запись в БД, и сам файл на диске.
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'products')
    os.makedirs(upload_dir, exist_ok=True)

    for photo in src.photos.all():
        new_filename = _new_photo_path(photo.path)
        src_path = os.path.join(upload_dir, photo.path)
        dst_path = os.path.join(upload_dir, new_filename)
        try:
            if os.path.exists(src_path):
                shutil.copy2(src_path, dst_path)
        except Exception:
            # Если файл не скопировался — всё равно создаём запись,
            # но без битой ссылки: проверим ниже и пропустим, если пусто.
            pass

        if os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
            db.session.add(ProductPhoto(
                product_id=new_product.id,
                path=new_filename,
                is_main=photo.is_main,
                sort_order=photo.sort_order,
            ))

    # 3. Копируем параметры товара.
    for param in src.parameters.all():
        db.session.add(ProductParameter(
            product_id=new_product.id,
            parameter_id=param.parameter_id,
            value=param.value,
            display_value=param.display_value,
        ))

    db.session.commit()

    flash('Копия товара создана и отправлена на модерацию.', 'success')
    return redirect(url_for('seller.product_edit', product_id=new_product.id))


@bp.route('/orders')
def orders():
    """
    Список заказов.
    URL: seller.domain/orders
    По умолчанию показывает актуальные заказы (без доставленных и отменённых),
    отсортированные по дате - свежие сверху.

    Поддерживает фильтры:
      - status: pending / processing / shipped / delivered / canceled / actual
      - from_date / to_date: диапазон по Order.created_at (формат YYYY-MM-DD)
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    status = request.args.get('status')
    from_date = request.args.get('from_date') or None
    to_date = request.args.get('to_date') or None
    page = request.args.get('page', 1, type=int)

    # Парсим даты в datetime, чтобы фильтр по created_at работал корректно
    # на границах дня. to_date — конец дня (23:59:59.999999).
    from datetime import datetime as _dt, time as _time
    def _parse_date(s, end_of_day=False):
        if not s:
            return None
        try:
            d = _dt.strptime(s, '%Y-%m-%d')
        except ValueError:
            return None
        return _dt.combine(d, _time.max if end_of_day else _time.min)
    from_dt = _parse_date(from_date, end_of_day=False)
    to_dt = _parse_date(to_date, end_of_day=True)

    query = Order.query.filter_by(seller_id=current_user.id)
    
    # Актуальные статусы (исключаем только canceled)
    actual_statuses = ['pending', 'processing', 'in_assembly', 'assembled', 'paid', 'shipped', 'in_transit', 'delivered', 'received']
    
    if status:
        # Маппинг статусов для фильтрации (учитываем старые и новые статусы)
        status_map = {
            'pending': ['pending'],
            'processing': ['processing', 'in_assembly', 'assembled'],
            'shipped': ['shipped', 'in_transit'],
            'delivered': ['delivered', 'received'],
            'canceled': ['canceled', 'cancelled']
        }
        if status in status_map:
            query = query.filter(Order.status.in_(status_map[status]))
        else:
            query = query.filter_by(status=status)
    else:
        # По умолчанию показываем только актуальные заказы
        query = query.filter(Order.status.in_(actual_statuses))
    
    # Фильтр по датам создания заказа
    if from_dt is not None:
        query = query.filter(Order.created_at >= from_dt)
    if to_dt is not None:
        query = query.filter(Order.created_at <= to_dt)

    # Сортировка по дате: свежие сверху
    pagination = query.order_by(Order.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('seller/orders.html',
                         title='Заказы',
                         orders=pagination.items,
                         pagination=pagination,
                         current_status=status if status else 'actual',
                         from_date=from_date or '',
                         to_date=to_date or '')


@bp.route('/orders/<int:order_id>')
def order_detail(order_id):
    """
    Детали заказа.
    URL: seller.domain/orders/{id}
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        abort(404)

    # items — это dynamic relationship, joinedload к нему применить нельзя.
    # Прогреваем позиции и связанные товары отдельным запросом через
    # joinedload по OrderItem.product, чтобы в шаблоне обращения к
    # item.product / item.line_subtotal не уходили в N+1.
    from sqlalchemy.orm import joinedload as _joinedload
    _ = (
        db.session.query(OrderItem)
        .options(_joinedload(OrderItem.product))
        .filter(OrderItem.order_id == order.id)
        .all()
    )

    return render_template('seller/order_detail.html',
                         title=f'Заказ {order.order_number}',
                         order=order)


@bp.route('/orders/<int:order_id>/assemble', methods=['POST'])
@require_active_tariff
def order_assemble(order_id):
    """
    Сборка заказа.
    URL: seller.domain/orders/{id}/assemble
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        abort(404)
    
    if order.status != 'processing':
        flash('Недопустимый статус.', 'error')
        return redirect(url_for('seller.order_detail', order_id=order_id))
    
    order.assemble()
    flash('Заказ собран.', 'success')
    return redirect(url_for('seller.order_detail', order_id=order_id))


@bp.route('/orders/<int:order_id>/mark-shipped', methods=['POST'])
@require_active_tariff
def order_mark_shipped(order_id):
    """
    Шаг 1: продавец подтверждает, что товар отправлен.
    Меняет статус на 'shipped'. Трек-номер вводится следующим шагом.
    URL: seller.domain/orders/{id}/mark-shipped
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        abort(404)

    if order.status not in ['processing', 'in_assembly', 'assembled', 'shipped']:
        flash('Недопустимый статус для отправки.', 'error')
        return redirect(url_for('seller.order_detail', order_id=order_id))

    if order.status != 'shipped':
        order.status = 'shipped'
        if not order.shipped_at:
            order.shipped_at = datetime.utcnow()
        db.session.commit()
        flash('Заказ переведён в статус "Отправлен". Укажите трек-номер.', 'success')

    return redirect(url_for('seller.order_detail', order_id=order_id))


@bp.route('/orders/<int:order_id>/set-track', methods=['POST'])
@require_active_tariff
def order_set_track(order_id):
    """
    Шаг 2: сохранение трек-номера.
    Пишет сообщение покупателю в чат заказа.
    URL: seller.domain/orders/{id}/set-track
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        abort(404)

    track_number = (request.form.get('track_number') or '').strip()
    if not track_number:
        flash('Введите трек-номер.', 'error')
        return redirect(url_for('seller.order_detail', order_id=order_id))

    if order.status != 'shipped':
        order.status = 'shipped'
        order.shipped_at = datetime.utcnow()
    order.track_number = track_number
    db.session.commit()

    # Сообщение покупателю в чат заказа
    try:
        msg = Message(
            sender_type='seller',
            sender_id=current_user.id,
            receiver_type='buyer',
            receiver_id=order.buyer_id,
            text=f'Заказ {order.order_number} отправлен. Трек-номер: {track_number}',
            conversation_type='order',
            conversation_id=order.id,
            is_system=True,
        )
        db.session.add(msg)
        db.session.commit()
    except Exception as e:
        # Не блокируем сохранение трека из-за ошибки уведомления
        db.session.rollback()
        try:
            order.track_number = track_number
            db.session.commit()
        except Exception:
            pass
        print(f"order_set_track: notify failed: {e}")

    flash('Трек-номер сохранён, покупатель уведомлён.', 'success')
    return redirect(url_for('seller.order_detail', order_id=order_id))


@bp.route('/orders/<int:order_id>/mark-delivered', methods=['POST'])
@require_active_tariff
def order_mark_delivered(order_id):
    """
    Перевод заказа в статус «Доставлен» (продавец подтверждает получение).
    Доступно только для заказов со статусом «Отправлен».
    URL: seller.domain/orders/{id}/mark-delivered
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        abort(404)

    if order.status != 'shipped':
        flash('Заказ можно отметить доставленным только из статуса «Отправлен».', 'error')
        return redirect(url_for('seller.order_detail', order_id=order_id))

    order.deliver()
    flash('Заказ отмечен как доставленный.', 'success')
    return redirect(url_for('seller.order_detail', order_id=order_id))


@bp.route('/orders/<int:order_id>/ship', methods=['POST'])
@require_active_tariff
def order_ship(order_id):
    """
    Отправка заказа.
    URL: seller.domain/orders/{id}/ship
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        abort(404)
    
    track_number = request.form.get('track_number')
    
    if order.status not in ['processing', 'shipped']:
        flash('Недопустимый статус.', 'error')
        return redirect(url_for('seller.order_detail', order_id=order_id))
    
    order.ship(track_number=track_number)
    flash('Заказ отправлен.', 'success')
    return redirect(url_for('seller.order_detail', order_id=order_id))


@bp.route('/orders/<int:order_id>/cancel', methods=['POST'])
@csrf.exempt
@require_active_tariff
def order_cancel(order_id):
    """
    Отмена заказа.
    URL: seller.domain/orders/{id}/cancel
    
    CSRF exemption: This is a seller-only endpoint that requires authentication.
    The CSRF token is still included in the form for security, but validation
    is exempted due to subdomain session handling issues.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        logger.warning(f"Unauthorized cancel attempt for order {order_id}")
        return redirect(url_for('auth_seller.seller_login'))
    
    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        logger.warning(f"Order {order_id} not found or access denied for seller {current_user.id}")
        abort(404)
    
    logger.info(f"Cancel order {order_id}: current status = '{order.status}'")
    
    # Допустимые статусы для отмены
    cancellable_statuses = ['processing', 'in_assembly', 'assembled']
    if order.status not in cancellable_statuses:
        logger.warning(f"Cannot cancel order {order_id}: status is '{order.status}', expected one of {cancellable_statuses}")
        flash(f'Невозможно отменить заказ в статусе "{order.status}".', 'error')
        return redirect(url_for('seller.order_detail', order_id=order_id))
    
    try:
        order.cancel()
        logger.info(f"Order {order_id} successfully canceled, new status = '{order.status}'")
        flash('Заказ отменён.', 'success')
    except Exception as e:
        logger.error(f"Error canceling order {order_id}: {str(e)}")
        db.session.rollback()
        flash(f'Ошибка при отмене заказа: {str(e)}', 'error')
    
    return redirect(url_for('seller.order_detail', order_id=order_id))


# =============================================================================
# API для создания отгрузок
# =============================================================================

@bp.route('/api/shipment/check/<int:order_id>', methods=['GET'])
@csrf.exempt
def shipment_check(order_id):
    """
    Проверка данных для создания отгрузки.
    Возвращает информацию о том, какие данные доступны, а какие нужно запросить у пользователя.
    URL: seller.domain/api/shipment/check/{id}
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return jsonify({'error': 'Unauthorized'}), 401
    
    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        return jsonify({'error': 'Order not found'}), 404
    
    # Допустимые статусы для создания отгрузки (включая старые и paid)
    valid_statuses = ['processing', 'assembled', 'in_assembly', 'paid']
    if order.status not in valid_statuses:
        return jsonify({'error': f'Invalid order status: {order.status}. Expected one of: {", ".join(valid_statuses)}'}), 400
    
    # Информация о службе доставки
    delivery_service = order.delivery_service
    if not delivery_service:
        return jsonify({
            'success': False,
            'error': 'Служба доставки не выбрана',
            'missing_fields': ['delivery_service']
        }), 400
    
    # Проверяем, настроен ли профиль доставки у продавца
    seller_delivery = SellerDelivery.query.filter_by(
        seller_id=current_user.id,
        delivery_service_id=delivery_service.id,
        is_active=True
    ).first()
    
    missing_fields = []
    warnings = []
    product_data = []
    
    # Проверка данных товаров
    for item in order.items:
        product = item.product
        product_info = {
            'id': product.id,
            'name': product.name,
            'article': product.article,
            'weight': None,
            'length': None,
            'width': None,
            'height': None,
            'price': item.price_at_order,
            'quantity': item.quantity
        }
        
        # Получаем вес товара
        if product.weight:
            product_info['weight'] = float(product.weight)
        else:
            missing_fields.append(f"weight_{product.id}")
            warnings.append(f"Не указан вес для товара '{product.name}'")
        
        # Пытаемся получить габариты из параметров товара
        params = {p.parameter_id: p for p in product.parameters.all()}
        
        # Ищем параметры размеров
        for param in product.category.get_all_parameters():
            param_value = params.get(param.id)
            if param_value:
                value = param_value.value
                # Ищем по коду параметра или названию
                code = param.code.lower() if param.code else ''
                name = param.name.lower()
                
                if 'length' in code or 'длин' in name:
                    if isinstance(value, list):
                        product_info['length'] = float(value[0]) if value else None
                    else:
                        product_info['length'] = float(value)
                elif 'width' in code or 'ширин' in name:
                    if isinstance(value, list):
                        product_info['width'] = float(value[0]) if value else None
                    else:
                        product_info['width'] = float(value)
                elif 'height' in code or 'высот' in name:
                    if isinstance(value, list):
                        product_info['height'] = float(value[0]) if value else None
                    else:
                        product_info['height'] = float(value)
        
        # Если габариты не найдены, добавляем предупреждение
        if not product_info['length'] or not product_info['width'] or not product_info['height']:
            missing_dimensions = []
            if not product_info['length']:
                missing_dimensions.append('длина')
            if not product_info['width']:
                missing_dimensions.append('ширина')
            if not product_info['height']:
                missing_dimensions.append('высота')
            if missing_dimensions:
                warnings.append(f"Не указаны габариты ({', '.join(missing_dimensions)}) для товара '{product.name}'")
        
        product_data.append(product_info)
    
    # Проверка адреса доставки / ПВЗ
    if order.pvz_code:
        delivery_address = order.pvz_code
    elif order.delivery_address:
        delivery_address = order.delivery_address
    else:
        missing_fields.append('delivery_address')
        warnings.append('Не указан адрес доставки или ПВЗ')
        delivery_address = None
    
    # Проверка профиля доставки продавца
    is_delivery_configured = False
    if seller_delivery:
        if delivery_service.code == 'cdek':
            creds = seller_delivery.api_credentials or {}
            is_delivery_configured = bool(creds.get('account') and creds.get('secure'))
            if seller_delivery.pvz_city_code:
                from_city_code = str(seller_delivery.pvz_city_code)
            else:
                missing_fields.append('from_city_code')
                warnings.append('Не указан код города отправки в настройках доставки')
                from_city_code = None
        elif delivery_service.code == 'yandex':
            creds = seller_delivery.api_credentials or {}
            is_delivery_configured = bool(creds.get('client_id') and creds.get('client_secret'))
            from_city_code = seller_delivery.pvz_city_code
        else:
            is_delivery_configured = bool(seller_delivery.api_credentials)
            from_city_code = seller_delivery.pvz_city_code
    else:
        missing_fields.append('seller_delivery_profile')
        warnings.append(f'Не настроен профиль доставки для {delivery_service.name}')
        is_delivery_configured = False
        from_city_code = None
    
    return jsonify({
        'success': True,
        'order_id': order.id,
        'order_number': order.order_number,
        'delivery_service': {
            'id': delivery_service.id,
            'code': delivery_service.code,
            'name': delivery_service.name
        },
        'seller_delivery': {
            'configured': is_delivery_configured,
            'profile_id': seller_delivery.id if seller_delivery else None,
            'from_city_code': from_city_code,
            'ship_from_address': seller_delivery.ship_from_address if seller_delivery else None
        } if seller_delivery else None,
        'products': product_data,
        'delivery_address': delivery_address,
        'pvz_code': order.pvz_code,
        'missing_fields': list(set(missing_fields)),
        'warnings': list(set(warnings)),
        'has_missing_data': len(list(set(missing_fields))) > 0
    })


@bp.route('/api/shipment/create/<int:order_id>', methods=['POST'])
@csrf.exempt
@require_active_tariff
def shipment_create(order_id):
    """
    Создание отгрузки в системе транспортной компании.
    URL: seller.domain/api/shipment/create/{id}
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return jsonify({'error': 'Unauthorized'}), 401
    
    order = db.session.get(Order, order_id)
    if not order or order.seller_id != current_user.id:
        return jsonify({'error': 'Order not found'}), 404
    
    # Допустимые статусы (включая старый in_assembly и paid)
    valid_statuses = ['processing', 'assembled', 'in_assembly', 'paid']
    if order.status not in valid_statuses:
        return jsonify({'error': 'Invalid order status', 'message': f'Заказ должен быть в статусе "В обработке". Текущий статус: {order.status}'}), 400
    
    # Получаем данные из запроса
    data = request.get_json() or {}
    
    # Данные, которые пользователь мог ввести вручную
    override_data = data.get('override_data', {})
    
    delivery_service = order.delivery_service
    if not delivery_service:
        return jsonify({'error': 'No delivery service', 'message': 'Служба доставки не выбрана'}), 400
    
    # Получаем профиль доставки продавца
    seller_delivery = SellerDelivery.query.filter_by(
        seller_id=current_user.id,
        delivery_service_id=delivery_service.id,
        is_active=True
    ).first()
    
    if not seller_delivery:
        return jsonify({'error': 'No delivery profile', 'message': 'Профиль доставки не настроен'}), 400
    
    try:
        # Формируем данные для создания отгрузки в зависимости от службы доставки
        if delivery_service.code == 'cdek':
            result = _create_cdek_shipment(order, seller_delivery, override_data)
        elif delivery_service.code == 'yandex':
            result = _create_yandex_shipment(order, seller_delivery, override_data)
        else:
            return jsonify({'error': 'Unsupported delivery service', 'message': f'Служба {delivery_service.name} не поддерживается'}), 400
        
        if result['success']:
            # Обновляем статус заказа
            order.status = 'shipped'
            order.shipped_at = datetime.utcnow()
            if result.get('tracking_number'):
                order.track_number = result['tracking_number']
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': 'Отгрузка успешно создана',
                'tracking_number': result.get('tracking_number'),
                'shipment_id': result.get('shipment_id'),
                'details': result.get('details', {})
            })
        else:
            return jsonify({
                'error': 'Shipment creation failed',
                'message': result.get('error', 'Ошибка создания отгрузки'),
                'details': result.get('details', {})
            }), 400
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': 'Server error',
            'message': str(e)
        }), 500


def _create_cdek_shipment(order, seller_delivery, override_data):
    """
    Создание отгрузки через CDEK API.
    Тарифы для интернет-магазина:
      136 - склад→ПВЗ  (требует shipment_point + receiver_delivery_point)
      137 - склад→дверь (требует shipment_point)
    """
    import logging
    import time
    from app.integrations.cdek import CDEKClient, Contact, Location, Package, Item, create_order_request

    logger = logging.getLogger(__name__)
    delivery_service = order.delivery_service

    # --- Учётные данные ---
    creds = seller_delivery.api_credentials or {}
    account = creds.get('account')
    secure = creds.get('secure')
    if not account or not secure:
        return {'success': False, 'error': 'Не настроены учётные данные CDEK'}

    test_mode = seller_delivery.is_test_mode
    if test_mode is None:
        test_mode = creds.get('test_mode', True)

    client = CDEKClient(account=account, secure=secure, test_mode=test_mode)

    # --- Данные ПВЗ отправителя ---
    from_pvz_code = override_data.get('from_pvz_code') or seller_delivery.pvz_code
    from_city_code = override_data.get('from_city_code') or seller_delivery.pvz_city_code
    if not from_city_code:
        return {'success': False, 'error': 'Не указан код города отправки в настройках продавца'}

    # --- Данные ПВЗ/адреса получателя ---
    # Берём из заказа, потом из профиля покупателя
    pvz_code = order.pvz_code
    pvz_address = None
    pvz_city_code = None

    if order.buyer:
        buyer_delivery = order.buyer.delivery_profiles.filter_by(
            delivery_service_id=delivery_service.id
        ).first()
        if buyer_delivery:
            pvz_code = pvz_code or buyer_delivery.pvz_code
            pvz_address = buyer_delivery.pvz_address
            pvz_city_code = buyer_delivery.pvz_city_code

    logger.info(f"CDEK shipment init: pvz_code={pvz_code}, pvz_address={pvz_address}, "
                f"pvz_city_code={pvz_city_code}, from_pvz_code={from_pvz_code}, from_city_code={from_city_code}")

    # --- Тариф ---
    if override_data.get('tariff_code'):
        tariff_code = int(override_data['tariff_code'])
    elif pvz_code:
        tariff_code = 136  # склад→ПВЗ
    else:
        tariff_code = 137  # склад→дверь

    if tariff_code == 136 and not pvz_code:
        return {'success': False, 'error': 'Для доставки до ПВЗ необходимо выбрать пункт выдачи СДЭК'}

    # --- Контакты ---
    sender = Contact.create(
        name=current_user.store_name or current_user.login,
        phone=current_user.phone,
        email=current_user.email,
        company=current_user.store_name
    )
    recipient = Contact.create(
        name=order.buyer.name or order.buyer.login,
        phone=order.buyer.phone,
        email=order.buyer.email
    )

    # --- Локации ---
    # Для shipment_type=internet_shop from_location НЕ передаётся вместе с shipment_point
    from_location = None

    if tariff_code == 136:
        # ПВЗ→ПВЗ: to_location содержит city_code + адрес ПВЗ
        # receiver_delivery_point = код ПВЗ получателя
        to_location = Location.create(
            code=str(pvz_city_code) if pvz_city_code else None,
            address=pvz_address or pvz_code
        )
        receiver_delivery_point = pvz_code
        # shipment_point = ПВЗ отправителя
        shipment_point = from_pvz_code
    else:
        # склад→дверь: to_location = адрес доставки
        to_location = Location.create(
            code=str(pvz_city_code) if pvz_city_code else None,
            address=order.delivery_address or pvz_address
        )
        receiver_delivery_point = None
        shipment_point = from_pvz_code

    logger.info(f"CDEK params: tariff={tariff_code}, shipment_point={shipment_point}, "
                f"receiver_delivery_point={receiver_delivery_point}, to_location={to_location}")

    # --- Упаковка ---
    total_weight = 0
    package_items = []
    length = width = height = 10

    for item in order.items:
        product = item.product
        weight = override_data.get(f'weight_{product.id}') or (product.weight * 1000 if product.weight else 500)
        total_weight += int(weight) * item.quantity
        length = override_data.get(f'length_{product.id}') or 10
        width = override_data.get(f'width_{product.id}') or 10
        height = override_data.get(f'height_{product.id}') or 10
        package_items.append(Item.create(
            name=product.name,
            ware_key=str(product.id),
            cost=float(item.price_at_order),
            payment=0,
            weight=int(weight),
            amount=item.quantity
        ))

    package = Package.create(
        number=str(order.id),
        weight=int(total_weight),
        length=int(length),
        width=int(width),
        height=int(height),
        items=package_items
    )

    # --- Формируем запрос ---
    order_data = create_order_request(
        order_number=order.order_number,
        tariff_code=tariff_code,
        sender=sender,
        recipient=recipient,
        from_location=from_location,
        to_location=to_location,
        packages=[package],
        shipment_point=shipment_point,
        receiver_delivery_point=receiver_delivery_point,
        seller_delivery=seller_delivery
    )

    logger.info(f"CDEK order_data: {order_data}")

    # --- Отправляем в CDEK ---
    try:
        result = client.create_order(order_data)

        entity = result.get('entity', {})
        uuid = entity.get('uuid')
        request_uuid = None
        track_number = None

        requests_list = result.get('requests', [])
        if requests_list:
            request_uuid = requests_list[0].get('request_uuid')
            state = requests_list[0].get('state')
            errors = requests_list[0].get('errors', [])

            if errors:
                error_messages = [f"{e.get('code')}: {e.get('message')}" for e in errors]
                error_text = '; '.join(error_messages)
                logger.error(f"CDEK shipment errors: {error_text}")
                return {'success': False, 'error': f'Ошибка CDEK: {error_text}', 'errors': errors}

            if state == 'ACCEPTED' and uuid:
                for attempt in range(5):
                    time.sleep(2)
                    try:
                        order_info = client.get_order_info(uuid)
                        entity = order_info.get('entity', {})
                        requests_in_info = order_info.get('requests', [])
                        if requests_in_info:
                            errors_in_info = requests_in_info[0].get('errors', [])
                            if errors_in_info:
                                error_messages = [f"{e.get('code')}: {e.get('message')}" for e in errors_in_info]
                                error_text = '; '.join(error_messages)
                                logger.error(f"CDEK get_order_info errors: {error_text}")
                                return {'success': False, 'error': f'Ошибка CDEK: {error_text}', 'errors': errors_in_info}
                        track_number = entity.get('dispatch_number') or entity.get('cdek_number')
                        if track_number:
                            logger.info(f"CDEK dispatch_number={track_number} (attempt {attempt+1})")
                            break
                    except Exception as e:
                        logger.warning(f"CDEK get_order_info attempt {attempt+1} failed: {e}")

        if not track_number and uuid:
            track_number = uuid

        return {
            'success': True,
            'tracking_number': str(track_number) if track_number else None,
            'shipment_id': uuid,
            'request_uuid': request_uuid,
            'details': result
        }
    except Exception as e:
        logger.error(f"CDEK shipment error: {e}")
        return {'success': False, 'error': str(e)}


def _create_yandex_shipment(order, seller_delivery, override_data):
    """
    Создание отгрузки через Яндекс Доставку API.
    """
    from app.integrations.yandex import YandexDeliveryClient
    
    creds = seller_delivery.api_credentials or {}
    client_id = creds.get('client_id')
    client_secret = creds.get('client_secret')
    
    if not client_id or not client_secret:
        return {'success': False, 'error': 'Не настроены учетные данные Яндекс Доставки'}
    
    client = YandexDeliveryClient(client_id=client_id, client_secret=client_secret)
    
    # Получаем оффер для расчёта
    # Эти данные должны быть предварительно рассчитаны и сохранены
    offer_id = override_data.get('offer_id')
    
    if not offer_id:
        return {'success': False, 'error': 'Не выбран тариф доставки. Сначала рассчитайте стоимость.'}
    
    # Данные для заявки
    from_location = {
        'latitude': seller_delivery.pvz_city_code or 55.7558,  # Координаты города отправки (по умолчанию Москва)
        'longitude': 0
    }
    
    # Пытаемся получить код города получателя из профиля доставки покупателя
    to_city_code = None
    buyer_delivery = None
    if order.buyer:
        buyer_delivery = order.buyer.delivery_profiles.filter_by(
            delivery_service_id=delivery_service.id
        ).first()
        if buyer_delivery:
            to_city_code = buyer_delivery.pvz_city_code
    
    to_location = {
        'latitude': to_city_code or 55.7558,  # Координаты города получателя
        'longitude': 0
    }
    
    # Товары
    packages = []
    for item in order.items:
        product = item.product
        weight = override_data.get(f'weight_{product.id}') or (product.weight if product.weight else 0.5)
        length = override_data.get(f'length_{product.id}') or 10
        width = override_data.get(f'width_{product.id}') or 10
        height = override_data.get(f'height_{product.id}') or 10
        
        packages.append({
            'weight': weight,
            'dimensions': {
                'length': length,
                'width': width,
                'height': height
            },
            'items': [{
                'name': product.name,
                'count': item.quantity,
                'price': float(item.price_at_order)
            }]
        })
    
    try:
        result = client.create_request(
            offer_id=offer_id,
            from_location=from_location,
            to_location=to_location,
            sender={
                'name': current_user.store_name or current_user.login,
                'phone': current_user.phone
            },
            recipient={
                'name': order.buyer.name or order.buyer.login,
                'phone': order.buyer.phone
            },
            packages=packages
        )
        
        request_id = result.get('id') or result.get('request_id')
        
        return {
            'success': True,
            'tracking_number': str(request_id),
            'shipment_id': request_id,
            'details': result
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


@bp.route('/messages')
def messages():
    """
    Сообщения продавца.
    URL: seller.domain/messages

    Рендерим унифицированный шаблон messages/user_messages.html прямо
    здесь (внутри blueprint `seller`, subdomain `seller`), чтобы:

      1. Сайдбар и весь layout кабинета селлера получили корректный
         tariff_state / can_work (раньше мы редиректили в blueprint
         `messages` на основном домене — там before_request и
         context_processor seller'а не работают, и кабинет выглядел
         «заблокированным»).
      2. Не дублировать логику диалогов: используем те же хелперы
         get_conversations / mark_messages_read / get_partner_name
         из app.blueprints.messages.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    # Импорт хелперов из унифицированного модуля сообщений.
    from app.blueprints.messages import (
        get_conversations,
        mark_messages_read,
        get_partner_name,
    )
    from app.models.orders import Order

    user_type = 'seller'
    user_id = current_user.id

    # Фильтр диалогов (support / buyers / orders) и текущий диалог.
    filter_type = request.args.get('filter', 'buyers')
    partner_type = request.args.get('partner_type', type=str)
    partner_id = request.args.get('partner_id', type=int)

    conversations = get_conversations(user_type, user_id, filter_type)

    filter_options = {
        'support': 'Поддержка',
        'buyers': 'Покупатели',
        'orders': 'Заказы',
    }

    current_partner = None
    messages = []
    is_order_chat = False

    if partner_type and partner_id is not None:
        if partner_type == 'order':
            order = Order.query.get(partner_id)
            if order:
                is_order_chat = True
                messages = Message.query.filter(
                    Message.conversation_type == 'order',
                    Message.conversation_id == partner_id,
                ).order_by(Message.timestamp.asc()).all()

                actual_partner_type = 'buyer'
                actual_partner_id = order.buyer_id
                mark_messages_read(user_type, user_id, actual_partner_type, actual_partner_id)

                current_partner = {
                    'partner_type': 'order',
                    'partner_id': partner_id,
                    'name': f"Заказ {order.order_number}",
                    'order_id': order.id,
                    'order_number': order.order_number,
                    'receiver_type': 'buyer',
                    'receiver_id': actual_partner_id,
                }
        else:
            messages = Message.get_conversation(user_type, user_id, partner_type, partner_id)
            mark_messages_read(user_type, user_id, partner_type, partner_id)
            current_partner = {
                'partner_type': partner_type,
                'partner_id': partner_id,
                'name': get_partner_name(partner_type, partner_id),
                'receiver_type': partner_type,
                'receiver_id': partner_id,
            }

    unread_support = sum(
        1 for k, c in get_conversations(user_type, user_id, 'support').items()
        if c['unread_count'] > 0
    )
    unread_buyers = sum(
        1 for k, c in get_conversations(user_type, user_id, 'buyers').items()
        if c['unread_count'] > 0
    )
    unread_orders = sum(
        1 for k, c in get_conversations(user_type, user_id, 'orders').items()
        if c['unread_count'] > 0
    )

    return render_template(
        'messages/user_messages.html',
        title='Сообщения',
        conversations=conversations,
        messages=messages,
        current_partner=current_partner,
        filter_options=filter_options,
        current_filter=filter_type,
        user_type=user_type,
        base_template='seller/layout.html',
        site_title='Панель продавца',
        unread_support=unread_support,
        unread_buyers=unread_buyers,
        unread_orders=unread_orders,
        is_order_chat=is_order_chat,
    )


@bp.route('/messages/<partner_type>/<int:partner_id>')
def chat(partner_type, partner_id):
    """
    Чат с покупателем или поддержкой.
    URL: seller.domain/messages/{type}/{id}
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    # Получение истории переписки
    messages = Message.get_conversation(
        'seller', current_user.id,
        partner_type, partner_id
    )
    
    # Помечаем как прочитанные
    for msg in messages:
        if msg.receiver_id == current_user.id and msg.receiver_type == 'seller':
            msg.mark_as_read()
    
    # Имя собеседника
    if partner_type == 'buyer':
        from app.models.users import Buyer
        partner = db.session.get(Buyer, partner_id)
        partner_name = partner.full_name if partner else 'Покупатель'
    elif partner_type == 'admin':
        partner_name = 'Служба поддержки'
    else:
        partner_name = 'Неизвестный'

    # Аватарка собеседника (если есть)
    from app.blueprints.messages import get_partner_avatar
    partner_avatar = get_partner_avatar(partner_type, partner_id)

    return render_template('seller/chat.html',
                         title='Чат',
                         messages=messages,
                         partner_type=partner_type,
                         partner_id=partner_id,
                         partner_name=partner_name,
                         partner_avatar=partner_avatar,
                         user_type='seller')


@bp.route('/messages/<partner_type>/<int:partner_id>/content')
def chat_content(partner_type, partner_id):
    """
    AJAX загрузка контента чата.
    URL: seller.domain/messages/{type}/{id}/content
    """
    from flask import render_template_string
    
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return '<div class="error">Требуется авторизация</div>', 401
    
    # Получение истории переписки
    messages = Message.get_conversation(
        'seller', current_user.id,
        partner_type, partner_id
    )
    
    # Помечаем как прочитанные
    for msg in messages:
        if msg.receiver_id == current_user.id and msg.receiver_type == 'seller':
            msg.mark_as_read()
    
    # Имя собеседника
    if partner_type == 'buyer':
        from app.models.users import Buyer
        partner = db.session.get(Buyer, partner_id)
        partner_name = partner.full_name if partner else 'Покупатель'
    elif partner_type == 'admin':
        partner_name = 'Служба поддержки'
    else:
        partner_name = 'Неизвестный'
    
    # Используем шаблонную строку для возврата HTML
    from flask import current_app
    from flask import render_template
    from app.blueprints.messages import get_partner_avatar
    partner_avatar = get_partner_avatar(partner_type, partner_id)

    return render_template('seller/_chat_content.html',
                         messages=messages,
                         partner_type=partner_type,
                         partner_id=partner_id,
                         partner_name=partner_name,
                         partner_avatar=partner_avatar,
                         user_type='seller')


@bp.route('/messages/send', methods=['POST'])
@csrf.exempt
def message_send():
    """
    Отправка сообщения.
    URL: seller.domain/messages/send
    Поддерживает отправку с изображениями и PDF файлами.
    """
    from flask import jsonify
    
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Пробуем получить JSON данные
    data = request.get_json(silent=True)
    
    if data:
        # JSON запрос (AJAX)
        receiver_type = data.get('receiver_type')
        receiver_id = data.get('receiver_id')
        text = data.get('text', '').strip()
        image_path = data.get('image_path', '').strip() or None
        file_path = data.get('file_path', '').strip() or None
    else:
        # FormData запрос (традиционная форма)
        receiver_type = request.form.get('receiver_type')
        receiver_id = request.form.get('receiver_id', type=int)
        text = request.form.get('text', '').strip()
        image_path = request.form.get('image_path', '').strip() or None
        file_path = request.form.get('file_path', '').strip() or None
    
    # Проверяем что есть текст или вложение
    if not text and not image_path and not file_path:
        return jsonify({'error': 'Добавьте текст сообщения или вложение'}), 400
    
    if not all([receiver_type, receiver_id is not None]):
        return jsonify({'error': 'Укажите получателя'}), 400
    
    # Преобразуем receiver_id в int если это строка
    if isinstance(receiver_id, str):
        try:
            receiver_id = int(receiver_id)
        except ValueError:
            return jsonify({'error': 'Неверный ID получателя'}), 400
    
    msg = Message(
        sender_type='seller',
        sender_id=current_user.id,
        receiver_type=receiver_type,
        receiver_id=receiver_id,
        text=text or None,
        image_path=image_path,
        file_path=file_path
    )
    db.session.add(msg)
    db.session.commit()
    
    return jsonify({'success': True, 'message_id': msg.id})


@bp.route('/messages/<partner_type>/<int:partner_id>/new')
def chat_new_messages(partner_type, partner_id):
    """
    AJAX проверка новых сообщений.
    URL: seller.domain/messages/{type}/{id}/new
    """
    from flask import jsonify
    
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Получаем ID последнего сообщения (параметр last_id)
    last_id = request.args.get('last_id', 0, type=int)
    
    # Получаем новые сообщения после last_id
    new_messages = Message.query.filter(
        Message.id > last_id,
        ((Message.sender_type == 'seller') & (Message.sender_id == current_user.id) &
         (Message.receiver_type == partner_type) & (Message.receiver_id == partner_id)) |
        ((Message.sender_type == partner_type) & (Message.sender_id == partner_id) &
         (Message.receiver_type == 'seller') & (Message.receiver_id == current_user.id))
    ).order_by(Message.timestamp.asc()).all()
    
    # Помечаем новые входящие как прочитанные
    for msg in new_messages:
        if msg.receiver_id == current_user.id and msg.receiver_type == 'seller':
            msg.mark_as_read()
    
    # Формируем данные сообщений
    messages_data = []
    for msg in new_messages:
        messages_data.append({
            'id': msg.id,
            'text': msg.text,
            'image_path': msg.image_path,
            'file_path': msg.file_path,
            'sender_type': msg.sender_type,
            'sender_id': msg.sender_id,
            'timestamp': msg.timestamp.strftime('%Y-%m-%d %H:%M:%S') if msg.timestamp else '',
            'is_read': msg.is_read,
            'is_outgoing': msg.sender_type == 'seller' and msg.sender_id == current_user.id
        })
    
    return jsonify({
        'success': True,
        'messages': messages_data,
        'count': len(messages_data)
    })


@bp.route('/analytics')
def analytics():
    """
    Аналитика продавца.
    URL: seller.domain/analytics
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    # Фильтры по датам
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    
    query = Order.query.filter_by(seller_id=current_user.id)
    
    if from_date:
        query = query.filter(Order.created_at >= from_date)
    if to_date:
        query = query.filter(Order.created_at <= to_date)
    
    orders = query.all()
    
    # Статистика
    total_sales = sum(o.total_price for o in orders if o.status in ['delivered', 'shipped'])
    orders_count = len(orders)
    avg_order_value = total_sales / orders_count if orders_count > 0 else 0
    
    # Штрафы
    penalties = sum(o.penalty for o in orders)
    
    # Просмотры и добавления в корзину (агрегат по всем товарам продавца).
    # Тянем одним SQL-запросом — дешевле, чем тащить все Product в память.
    from sqlalchemy import func as _sa_func
    engagement_totals = (
        db.session.query(
            _sa_func.coalesce(_sa_func.sum(Product.views_count), 0),
            _sa_func.coalesce(_sa_func.sum(Product.cart_adds_count), 0),
        )
        .filter(Product.seller_id == current_user.id)
        .one()
    )
    total_views_all_time = int(engagement_totals[0] or 0)
    total_cart_adds_all_time = int(engagement_totals[1] or 0)

    # Просмотры/добавления в корзину ЗА ПЕРИОД — из событийной таблицы.
    # Раньше конверсия считалась от накопительного кэша total_views_all_time,
    # из-за чего при фильтрации заказов по дате значение уезжало за 100%
    # (за период заказов могло быть больше, чем «свежих» просмотров в кэше).
    from datetime import datetime as _dt, time as _time
    def _parse_date(s, end_of_day=False):
        try:
            d = _dt.strptime(s, '%Y-%m-%d')
            return _dt.combine(d, _time.max if end_of_day else _time.min)
        except Exception:
            return None
    _from = _parse_date(from_date, end_of_day=False) if from_date else None
    _to = _parse_date(to_date, end_of_day=True) if to_date else None

    def _count_event(event_type):
        q = db.session.query(
            _sa_func.coalesce(_sa_func.count(ProductEvent.id), 0)
        ).filter(
            ProductEvent.seller_id == current_user.id,
            ProductEvent.event_type == event_type,
        )
        if _from is not None:
            q = q.filter(ProductEvent.created_at >= _from)
        if _to is not None:
            q = q.filter(ProductEvent.created_at <= _to)
        return int(q.scalar() or 0)

    period_views = _count_event('view')
    period_cart_adds = _count_event('add_to_cart')

    # В шаблон уходит period-значение, чтобы конверсия и карточки
    # «Просмотры / В корзину» отображали согласованную картину за фильтр.
    total_views = period_views
    total_cart_adds = period_cart_adds
    
    # Топ-товары по просмотрам (для понимания, что привлекает внимание)
    top_viewed = (
        Product.query
        .filter(Product.seller_id == current_user.id)
        .order_by(Product.views_count.desc())
        .limit(5)
        .all()
    )
    
    # Топ-товары по добавлениям в корзину
    top_cart = (
        Product.query
        .filter(Product.seller_id == current_user.id)
        .order_by(Product.cart_adds_count.desc())
        .limit(5)
        .all()
    )
    
    # Покупатели: уникальные и постоянные (≥ 2 заказов у этого продавца).
    # Считаем на том же наборе orders, что и orders_count — чтобы
    # метрики были согласованы с фильтрами дат.
    unique_buyers = len({o.buyer_id for o in orders if o.buyer_id is not None})
    
    buyer_order_counts = (
        db.session.query(Order.buyer_id, _sa_func.count(Order.id))
        .filter(Order.seller_id == current_user.id)
    )
    if from_date:
        buyer_order_counts = buyer_order_counts.filter(Order.created_at >= from_date)
    if to_date:
        buyer_order_counts = buyer_order_counts.filter(Order.created_at <= to_date)
    buyer_order_counts = (
        buyer_order_counts.group_by(Order.buyer_id)
        .having(_sa_func.count(Order.id) >= 2)
        .all()
    )
    repeat_buyers = len(buyer_order_counts)
    
    # Заказы, реально полученные покупателем (status='received')
    received_count = sum(1 for o in orders if o.status == 'received')
    
    # Конверсия: просмотры → заказы. Это воронка "видели → купили".
    # Оба значения — за один и тот же период (orders — по фильтру дат,
    # total_views — из ProductEvent по тому же фильтру), чтобы значение
    # было согласовано и не уезжало за 100%.
    view_to_order_rate = (orders_count / total_views * 100) if total_views > 0 else 0.0
    
    # Эффективность: доля заказов, дошедших до "получено".
    fulfillment_rate = (received_count / orders_count * 100) if orders_count > 0 else 0.0
    
    return render_template('seller/analytics.html',
                         title='Аналитика',
                         orders=orders,
                         total_sales=total_sales,
                         orders_count=orders_count,
                         avg_order_value=avg_order_value,
                         penalties=penalties,
                         total_views=total_views,
                         total_cart_adds=total_cart_adds,
                         top_viewed=top_viewed,
                         top_cart=top_cart,
                         unique_buyers=unique_buyers,
                         repeat_buyers=repeat_buyers,
                         received_count=received_count,
                         view_to_order_rate=view_to_order_rate,
                         fulfillment_rate=fulfillment_rate)


@bp.route('/delivery')
def delivery():
    """
    Настройки доставки.
    URL: seller.domain/delivery
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    # Доступные службы доставки
    available_services = DeliveryService.query.filter_by(is_active=True).all()
    
    # Профили продавца
    seller_deliveries = SellerDelivery.query.filter_by(
        seller_id=current_user.id,
        is_active=True
    ).all()
    
    return render_template('seller/delivery.html',
                         title='Доставка',
                         available_services=available_services,
                         seller_deliveries=seller_deliveries)


@bp.route('/delivery/add', methods=['POST'])
@require_active_tariff
def delivery_add():
    """
    Добавление способа доставки.
    URL: seller.domain/delivery/add
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    delivery_service_id = request.form.get('delivery_service_id', type=int)
    api_login = request.form.get('api_login')
    api_password = request.form.get('api_password')
    ship_from = request.form.get('ship_from')
    
    # CDEK-специфичные поля
    contract_number = request.form.get('contract_number')
    pvz_code = request.form.get('pvz_code')
    pvz_address = request.form.get('pvz_address')
    pvz_city = request.form.get('pvz_city')
    pvz_city_code = request.form.get('pvz_city_code')
    tariffs = request.form.getlist('tariffs')
    cdek_test_mode = request.form.get('cdek_test_mode') == 'on'
    
    service = db.session.get(DeliveryService, delivery_service_id)
    if not service:
        flash('Служба доставки не найдена.', 'error')
        return redirect(url_for('seller.delivery'))
    
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
        if cdek_account and cdek_secure:
            api_credentials['account'] = cdek_account
            api_credentials['secure'] = cdek_secure
            api_credentials['test_mode'] = cdek_test_mode
    
    if existing:
        existing.api_credentials = api_credentials
        existing.ship_from_address = ship_from
        existing.is_active = True
        
        # CDEK-специфичные поля
        if service.code == 'cdek':
            existing.contract_number = contract_number
            existing.pvz_code = pvz_code
            existing.pvz_address = pvz_address
            existing.pvz_city = pvz_city
            existing.pvz_city_code = int(pvz_city_code) if pvz_city_code and pvz_city_code.isdigit() else None
            existing.tariffs = tariffs if tariffs else []
    else:
        # Создаём новый профиль
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
            profile.pvz_city_code = int(pvz_city_code) if pvz_city_code and pvz_city_code.isdigit() else None
            profile.tariffs = tariffs if tariffs else []
        
        db.session.add(profile)
    
    db.session.commit()
    
    flash('Способ доставки добавлен.', 'success')
    return redirect(url_for('seller.delivery'))


@bp.route('/delivery/<int:profile_id>/delete')
def delivery_delete(profile_id):
    """
    Удаление способа доставки.
    URL: seller.domain/delivery/{id}/delete
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    profile = db.session.get(SellerDelivery, profile_id)
    if not profile or profile.seller_id != current_user.id:
        abort(404)
    
    profile.is_active = False
    db.session.commit()
    
    flash('Способ доставки удалён.', 'success')
    return redirect(url_for('seller.delivery'))


@bp.route('/delivery/save', methods=['POST'])
@require_active_tariff
def delivery_save():
    """
    Сохранение выбранных служб доставки.
    URL: seller.domain/delivery/save
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    try:
        import json
        selected_ids = json.loads(request.form.get('selected_services', '[]'))
    except json.JSONDecodeError:
        selected_ids = []
    
    # Получаем все активные профили доставки продавца
    current_profiles = SellerDelivery.query.filter_by(
        seller_id=current_user.id
    ).all()
    
    current_service_ids = [p.delivery_service_id for p in current_profiles]
    
    # Деактивируем невыбранные
    for profile in current_profiles:
        if profile.delivery_service_id not in selected_ids:
            profile.is_active = False
    
    # Активируем/создаём выбранные
    for service_id in selected_ids:
        if service_id not in current_service_ids:
            # Проверяем что служба существует
            service = db.session.get(DeliveryService, service_id)
            if service:
                new_profile = SellerDelivery(
                    seller_id=current_user.id,
                    delivery_service_id=service_id,
                    is_active=True
                )
                db.session.add(new_profile)
        else:
            # Активируем существующий
            profile = next(p for p in current_profiles if p.delivery_service_id == service_id)
            profile.is_active = True
    
    db.session.commit()
    flash('Настройки доставки сохранены.', 'success')
    return redirect(url_for('seller.delivery'))


@bp.route('/reviews')
def reviews():
    """
    Отзывы о товарах продавца.
    URL: seller.domain/reviews
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    reviews = Review.query.join(Product).filter(
        Product.seller_id == current_user.id
    ).order_by(Review.created_at.desc()).all()
    
    # Группировка по товарам
    reviews_by_product = {}
    for review in reviews:
        if review.product_id not in reviews_by_product:
            reviews_by_product[review.product_id] = []
        reviews_by_product[review.product_id].append(review)
    
    return render_template('seller/reviews.html',
                         title='Отзывы',
                         reviews=reviews,
                         reviews_by_product=reviews_by_product)


@bp.route('/reviews/<int:review_id>/respond', methods=['POST'])
def review_respond(review_id):
    """
    Ответ на отзыв.
    URL: seller.domain/reviews/{id}/respond
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    review = db.session.get(Review, review_id)
    if not review:
        abort(404)
    
    product = review.product
    if product.seller_id != current_user.id:
        abort(403)
    
    response_text = request.form.get('response')
    
    review.add_seller_response(response_text)
    
    flash('Ответ отправлен на модерацию.', 'success')
    return redirect(url_for('seller.reviews'))


@bp.route('/promotions')
def promotions():
    """
    Акции продавца.
    URL: seller.domain/promotions

    Показываем:
      • Подключённые акции (классические — где выбраны товары,
        и шаблонные — seller-scope и discount — где участвуют либо
        все товары продавца, либо выбранные продавцом).
      • Доступные шаблоны админа, к которым можно подключиться.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import SellerPromotion, PromotionProduct

    # 1) Классические акции, в которых участвуют товары продавца
    classic_promotions = Promotion.query.join(PromotionProduct).filter(
        PromotionProduct.product_id.in_(
            db.session.query(Product.id).filter_by(seller_id=current_user.id)
        )
    ).distinct().all()

    # 2) Подключённые шаблонные акции (все типы)
    seller_links = SellerPromotion.query.filter_by(
        seller_id=current_user.id, is_active=True
    ).all()

    # Список словарей {promotion, effective_percent, link, ...} для секции «Подключённые акции»
    linked_promotions = []
    for link in seller_links:
        promo = link.promotion
        if not promo:
            continue
        # seller-scope (second_with_discount), шаблонные N+1 (выбор товаров),
        # шаблонные discount (выбор товаров) — все попадают сюда
        if not (promo.is_seller_scope
                or (promo.scheme == 'discount' and promo.is_template)
                or (promo.scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one') and promo.is_template)):
            continue
        effective = (link.override_discount_percent
                     if link.override_discount_percent is not None
                     else promo.discount_percent) or 0
        apply_same = (link.apply_same_discount
                      if link.apply_same_discount is not None
                      else promo.apply_same_discount)
        # Кол-во товаров продавца в этой акции (для шаблонных discount / 1+1)
        products_count = 0
        if promo.scheme in ('discount', 'one_plus_one', 'two_plus_one', 'three_plus_one'):
            products_count = (
                PromotionProduct.query
                .join(Product, PromotionProduct.product_id == Product.id)
                .filter(
                    PromotionProduct.promotion_id == promo.id,
                    Product.seller_id == current_user.id,
                ).count()
            )
        linked_promotions.append({
            'promotion': promo,
            'effective_percent': effective,
            'link': link,
            'apply_same_discount': apply_same,
            'products_count': products_count,
        })

    # Уже подключённые id — чтобы не показывать их повторно
    linked_template_ids = {link.promotion_id for link in seller_links}

    # Уже есть активная second_with_discount? Тогда другие шаблоны этой схемы
    # подключать нельзя — фильтруем из «Доступных».
    has_active_second = any(
        link.promotion and link.promotion.scheme == 'second_with_discount'
        for link in seller_links
    )

    # 3) Доступные шаблоны админа, к которым ещё не подключён продавец
    now = datetime.utcnow()
    base_q = Promotion.query.filter(
        Promotion.is_template == True,
        Promotion.status == 'active',
        Promotion.scheme.in_(['second_with_discount', 'discount', 'one_plus_one', 'two_plus_one', 'three_plus_one']),
        (Promotion.start_date.is_(None)) | (Promotion.start_date <= now),
        (Promotion.end_date.is_(None)) | (Promotion.end_date >= now),
    )
    if linked_template_ids:
        base_q = base_q.filter(~Promotion.id.in_(linked_template_ids))
    if has_active_second:
        # уже подключена одна second_with_discount — больше не показываем этот тип
        base_q = base_q.filter(Promotion.scheme != 'second_with_discount')
    available_templates = base_q.order_by(Promotion.created_at.desc()).all()

    return render_template('seller/promotions.html',
                         title='Акции',
                         linked_promotions=linked_promotions,
                         classic_promotions=classic_promotions,
                         available_templates=available_templates)


@bp.route('/promotions/template/<int:promotion_id>/link', methods=['POST'])
@require_active_tariff
def link_template_promotion(promotion_id):
    """
    Подключение шаблона акции продавцом.

    Поддерживает два типа шаблонов:
      • second_with_discount — общая скидка, override_discount_percent.
      • discount (шаблон) — может быть с общей скидкой (override) или
        per-item. apply_same_discount выбирает продавец при подключении.

    Запрещается иметь несколько активных seller-scope акций одной и той же
    схемы second_with_discount (уникальность логическая, см. код).
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import SellerPromotion

    promotion = db.session.get(Promotion, promotion_id)
    if not promotion or not promotion.is_template or promotion.scheme not in ('second_with_discount', 'discount', 'one_plus_one', 'two_plus_one', 'three_plus_one'):
        flash('Шаблон акции не найден или недоступен.', 'error')
        return redirect(url_for('seller.promotions'))

    # Уже подключён?
    existing = SellerPromotion.query.filter_by(
        seller_id=current_user.id, promotion_id=promotion_id
    ).first()
    if existing:
        existing.is_active = True
        # обновим параметры подключения на случай повторного сабмита
        _apply_link_form(existing, promotion, request.form)
        db.session.commit()
        flash('Акция подключена.', 'success')
        return redirect(url_for('seller.promotions'))

    # Запрет нескольких активных одного типа для second_with_discount и N+1:
    # у продавца может быть только одна активная seller-scope-подобная акция такого типа.
    if promotion.scheme in ('second_with_discount', 'one_plus_one', 'two_plus_one', 'three_plus_one'):
        already = SellerPromotion.query.join(Promotion, SellerPromotion.promotion_id == Promotion.id).filter(
            SellerPromotion.seller_id == current_user.id,
            SellerPromotion.is_active == True,
            Promotion.scheme == promotion.scheme,
        ).first()
        if already:
            if promotion.scheme == 'second_with_discount':
                flash('У вас уже подключена акция «Купи один — получи скидку». Сначала отключите её.', 'error')
            else:
                required = Promotion.N_PLUS_ONE_REQUIRED.get(promotion.scheme, 1)
                tag = '1+1' if required == 1 else f'{required}+1'
                flash(f'У вас уже подключена акция «{tag}». Сначала отключите её.', 'error')
            return redirect(url_for('seller.promotions'))

    link = SellerPromotion(
        seller_id=current_user.id,
        promotion_id=promotion_id,
        is_active=True,
    )
    _apply_link_form(link, promotion, request.form)
    db.session.add(link)
    db.session.commit()
    flash('Акция подключена.', 'success')
    return redirect(url_for('seller.promotions'))


def _apply_link_form(link, promotion, form):
    """
    Заполнение SellerPromotion параметрами из формы подключения.
    Используется и при создании, и при повторной отправке формы.
    """
    apply_same_raw = form.get('apply_same_discount', '').strip()
    override_raw = form.get('override_discount_percent', '').strip()

    # apply_same_discount устанавливается продавцом только для discount-шаблона
    if promotion.scheme == 'discount':
        # по умолчанию наследуем Promotion.apply_same_discount
        if apply_same_raw in ('0', '1'):
            link.apply_same_discount = (apply_same_raw == '1')
        else:
            link.apply_same_discount = None

    # override: берём только если админ не задал шаблонный процент
    # и продавец хочет общую скидку (apply_same=True или шаблонное)
    if override_raw:
        try:
            override = int(override_raw)
            if override <= 0 or override > 100:
                raise ValueError
            link.override_discount_percent = override
        except ValueError:
            link.override_discount_percent = None
    else:
        link.override_discount_percent = None

    # Для 1+1 override_discount_percent продавца обычно не нужен (там шаблонный 99%),
    # но если продавец что-то прислал — оставляем возможность переопределить.
    # apply_same_discount у 1+1 нет смысла — оставляем None.


@bp.route('/promotions/template/<int:promotion_id>/unlink', methods=['POST'])
@require_active_tariff
def unlink_template_promotion(promotion_id):
    """Отключение подключённого шаблона акции продавцом."""
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import SellerPromotion

    link = SellerPromotion.query.filter_by(
        seller_id=current_user.id, promotion_id=promotion_id
    ).first()
    if not link:
        flash('Подключение не найдено.', 'error')
        return redirect(url_for('seller.promotions'))

    link.is_active = False
    db.session.commit()
    flash('Акция отключена.', 'success')
    return redirect(url_for('seller.promotions'))


@bp.route('/promotions/template/<int:promotion_id>/settings', methods=['POST'])
@require_active_tariff
def update_template_settings(promotion_id):
    """
    Изменение настроек подключённой шаблонной discount-акции:
    apply_same_discount и/или override_discount_percent.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import SellerPromotion

    link = SellerPromotion.query.filter_by(
        seller_id=current_user.id, promotion_id=promotion_id
    ).first()
    promotion = db.session.get(Promotion, promotion_id)
    if not link or not promotion or promotion.scheme != 'discount' or not promotion.is_template:
        flash('Подключение не найдено.', 'error')
        return redirect(url_for('seller.promotions'))

    _apply_link_form(link, promotion, request.form)
    db.session.commit()
    flash('Настройки акции обновлены.', 'success')
    return redirect(url_for('seller.promotions'))


@bp.route('/promotions/template/<int:promotion_id>/products', methods=['GET'])
def template_promotion_products(promotion_id):
    """
    Страница управления товарами в подключённой шаблонной discount- или
    1+1-акции.
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import SellerPromotion, PromotionProduct

    link = SellerPromotion.query.filter_by(
        seller_id=current_user.id, promotion_id=promotion_id, is_active=True
    ).first()
    promotion = db.session.get(Promotion, promotion_id)
    if not link or not promotion or promotion.scheme not in ('discount', 'one_plus_one', 'two_plus_one', 'three_plus_one') or not promotion.is_template:
        flash('Акция не найдена или не подключена.', 'error')
        return redirect(url_for('seller.promotions'))

    apply_same = (link.apply_same_discount
                  if link.apply_same_discount is not None
                  else promotion.apply_same_discount)

    # Товары продавца, которые сейчас в акции
    in_promo = (
        db.session.query(Product, PromotionProduct)
        .join(PromotionProduct, PromotionProduct.product_id == Product.id)
        .filter(
            PromotionProduct.promotion_id == promotion_id,
            Product.seller_id == current_user.id,
        )
        .all()
    )
    in_ids = {p.id for p, _ in in_promo}
    in_items = [{'product': p, 'discount_percent': pp.discount_percent} for p, pp in in_promo]

    # Все одобренные товары продавца, не в акции
    candidates = (
        Product.query
        .filter(Product.seller_id == current_user.id, Product.status == 'approved')
        .filter(~Product.id.in_(in_ids) if in_ids else db.text('1=1'))
        .order_by(Product.name)
        .all()
    )

    return render_template(
        'seller/promotion_products.html',
        title=f'Товары — {promotion.name}',
        promotion=promotion,
        link=link,
        apply_same_discount=apply_same,
        in_items=in_items,
        candidates=candidates,
    )


@bp.route('/promotions/template/<int:promotion_id>/products/add', methods=['POST'])
@csrf.exempt
@require_active_tariff
def template_promotion_add_product(promotion_id):
    """Добавление товара продавца в шаблонную discount- или 1+1-акцию."""
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import SellerPromotion, PromotionProduct

    link = SellerPromotion.query.filter_by(
        seller_id=current_user.id, promotion_id=promotion_id, is_active=True
    ).first()
    promotion = db.session.get(Promotion, promotion_id)
    if not link or not promotion or promotion.scheme not in ('discount', 'one_plus_one', 'two_plus_one', 'three_plus_one') or not promotion.is_template:
        flash('Акция не найдена или не подключена.', 'error')
        return redirect(url_for('seller.promotions'))

    apply_same = (link.apply_same_discount
                  if link.apply_same_discount is not None
                  else promotion.apply_same_discount)

    product_ids = request.form.getlist('product_id')
    if not product_ids:
        flash('Выберите товары для добавления.', 'error')
        return redirect(url_for('seller.template_promotion_products', promotion_id=promotion_id))

    added = 0
    skipped_min_price = []
    for raw_id in product_ids:
        try:
            pid = int(raw_id)
        except ValueError:
            continue
        product = db.session.get(Product, pid)
        if not product or product.seller_id != current_user.id:
            continue
        existing = PromotionProduct.query.filter_by(
            promotion_id=promotion_id, product_id=pid
        ).first()
        if existing:
            continue

        # Проверка минимальной цены товара (для 1+1 / discount с лимитом)
        if promotion.min_product_price is not None and product.price < promotion.min_product_price:
            skipped_min_price.append(product.name)
            continue

        per_item_percent = None
        if promotion.scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one'):
            # У N+1 скидка общая (по умолчанию 99% из шаблона / override), per-item не нужен
            per_item_percent = None
        elif not apply_same:
            per_item_raw = request.form.get(f'discount_{pid}', '').strip()
            if not per_item_raw:
                flash(f'Укажите процент скидки для товара «{product.name}».', 'error')
                continue
            try:
                per_item_percent = int(per_item_raw)
                if per_item_percent <= 0 or per_item_percent > 100:
                    raise ValueError
            except ValueError:
                flash(f'Некорректный процент скидки для «{product.name}».', 'error')
                continue
        else:
            # общая скидка — берём override (если есть) или шаблонную
            base = (link.override_discount_percent
                    if link.override_discount_percent is not None
                    else promotion.discount_percent)
            if not base or base <= 0:
                flash('Сначала задайте общую скидку для акции (на странице акции).', 'error')
                return redirect(url_for('seller.template_promotion_products', promotion_id=promotion_id))

        pp = PromotionProduct(
            promotion_id=promotion_id,
            product_id=pid,
            discount_percent=per_item_percent,
            added_by_admin=False,
        )
        db.session.add(pp)
        added += 1

    if added:
        db.session.commit()
        flash(f'Добавлено товаров: {added}.', 'success')
    if skipped_min_price:
        flash(
            'Пропущены (ниже минимальной цены для акции): ' + ', '.join(skipped_min_price),
            'warning',
        )
    return redirect(url_for('seller.template_promotion_products', promotion_id=promotion_id))


@bp.route('/promotions/template/<int:promotion_id>/products/<int:product_id>/remove', methods=['POST'])
@csrf.exempt
@require_active_tariff
def template_promotion_remove_product(promotion_id, product_id):
    """Удаление товара из шаблонной discount- или 1+1-акции."""
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import PromotionProduct

    product = db.session.get(Product, product_id)
    if not product or product.seller_id != current_user.id:
        flash('Товар не найден.', 'error')
        return redirect(url_for('seller.promotions'))

    PromotionProduct.query.filter_by(
        promotion_id=promotion_id, product_id=product_id
    ).delete()
    db.session.commit()
    flash('Товар удалён из акции.', 'success')
    return redirect(url_for('seller.template_promotion_products', promotion_id=promotion_id))


@bp.route('/promotions/template/<int:promotion_id>/products/<int:product_id>/update', methods=['POST'])
@csrf.exempt
@require_active_tariff
def template_promotion_update_product(promotion_id, product_id):
    """Изменение индивидуального процента скидки товара в акции (только discount)."""
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    from app.models.orders import PromotionProduct

    pp = PromotionProduct.query.filter_by(
        promotion_id=promotion_id, product_id=product_id
    ).first()
    if not pp:
        flash('Связь не найдена.', 'error')
        return redirect(url_for('seller.promotions'))
    if pp.product and pp.product.seller_id != current_user.id:
        flash('Чужой товар.', 'error')
        return redirect(url_for('seller.promotions'))

    promotion = db.session.get(Promotion, promotion_id)
    if promotion and promotion.scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one'):
        required = Promotion.N_PLUS_ONE_REQUIRED.get(promotion.scheme, 1)
        tag = '1+1' if required == 1 else f'{required}+1'
        flash(f'У акции «{tag}» индивидуальная скидка для товара не задаётся.', 'error')
        return redirect(url_for('seller.template_promotion_products', promotion_id=promotion_id))

    raw = request.form.get('discount_percent', '').strip()
    try:
        v = int(raw)
        if v <= 0 or v > 100:
            raise ValueError
    except ValueError:
        flash('Некорректный процент скидки.', 'error')
        return redirect(url_for('seller.template_promotion_products', promotion_id=promotion_id))

    pp.discount_percent = v
    db.session.commit()
    flash('Скидка обновлена.', 'success')
    return redirect(url_for('seller.template_promotion_products', promotion_id=promotion_id))


# =============================================================================
# API для СДЭК
# =============================================================================

@bp.route('/api/cdek/tariffs', methods=['GET', 'POST'])
@login_required
def cdek_tariffs():
    """
    Получение списка тарифов СДЭК или расчёт стоимости.
    GET: Возвращает статический список тарифов
    POST: Рассчитывает стоимость доставки
    URL: seller.domain/api/cdek/tariffs
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    # GET запрос - возвращаем статический список тарифов
    if request.method == 'GET':
        from app.integrations.cdek import get_cdek_tariffs
        
        tariffs = get_cdek_tariffs()
        return jsonify({
            'success': True,
            'tariffs': [{'code': k, 'name': v} for k, v in tariffs.items()]
        })
    
    # POST запрос - рассчитываем стоимость
    data = request.get_json()
    
    from_city_code = data.get('from_city_code')
    to_city_code = data.get('to_city_code')
    weight = data.get('weight', 1000)
    length = data.get('length', 10)
    width = data.get('width', 10)
    height = data.get('height', 10)
    
    if not from_city_code:
        return jsonify({
            'success': False,
            'error': 'Укажите код города отправления (from_city_code)'
        }), 400
    
    delivery_id = data.get('delivery_id')
    
    try:
        from app.integrations.cdek import get_cdek_client
        
        # Получаем профиль доставки для передачи credentials
        seller_delivery = None
        if delivery_id:
            seller_delivery = db.session.get(SellerDelivery, delivery_id)
        
        client = get_cdek_client(seller_delivery)
        
        # Расчёт без конкретного тарифа - получим все доступные
        result = client.calculate(
            from_location={'code': from_city_code},
            to_location={'code': to_city_code} if to_city_code else None,
            weight=weight,
            length=length,
            width=width,
            height=height
        )
        
        return jsonify({
            'success': True,
            'tariffs': result
        })
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# Убрал дублирующий роут - теперь используется только api.py
# @bp.route('/api/cdek/pvz')
# @login_required
# def cdek_pvz():
#     """
#     Получение списка ПВЗ СДЭК.
#     URL: seller.domain/api/cdek/pvz?city=Москва
#     """
#     # Разрешаем доступ и продавцам, и покупателям
#     from app.models.users import Buyer
#     if not isinstance(current_user, (Seller, Buyer)):
#         return jsonify({'error': 'Доступ только для продавцов и покупателей'}), 403
#     
#     city = request.args.get('city', '')
#     region = request.args.get('region', '')
#     delivery_id = request.args.get('delivery_id', type=int)
#     
#     # Получаем профиль доставки для использования правильных credentials
#     seller_delivery = None
#     if delivery_id:
#         seller_delivery = db.session.get(SellerDelivery, delivery_id)
#     
#     try:
#         from app.integrations.cdek import get_cdek_pvz_list, CDEKError
#         
#         pvz_list = get_cdek_pvz_list(
#             city_code=None, 
#             region_code=region or None,
#             seller_delivery=seller_delivery
#         )
#         
#         # Фильтрация по городу если указан
#         if city:
#             pvz_list = [p for p in pvz_list if city.lower() in p.get('location', {}).get('city', '').lower()]
#         
#         # Форматирование для фронтенда с координатами для карты
#         result = []
#         for p in pvz_list[:50]:
#             location = p.get('location', {})
#             coords = p.get('coordinates', {})
#             
#             result.append({
#                 'code': p.get('code'),
#                 'name': p.get('name'),
#                 'city': location.get('city'),
#                 'address': location.get('address'),
#                 'address_full': location.get('address_full'),
#                 'type': p.get('type'),
#                 'work_time': p.get('work_time'),
#                 'weight_max': p.get('weight_max'),
#                 'latitude': coords.get('latitude') or location.get('latitude') or p.get('latitude'),
#                 'longitude': coords.get('longitude') or location.get('longitude') or p.get('longitude'),
#                 'city_code': p.get('city_code') or location.get('city_code'),
#             })
#         
#         return jsonify({'success': True, 'pvz': result})
#     
#     except CDEKError as e:
#         # Специфичная ошибка CDEK
#         return jsonify({'success': False, 'error': str(e)}), 400
#     except Exception as e:
#         import traceback
#         traceback.print_exc()
#         return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/cdek/calculate', methods=['POST'])
@login_required
def cdek_calculate():
    """
    Расчёт стоимости доставки.
    URL: seller.domain/api/cdek/calculate
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    data = request.get_json()
    
    from_city = data.get('from_city')  # Код города отправки
    to_city = data.get('to_city')  # Код города доставки
    weight = data.get('weight', 1000)  # Вес в граммах
    tariff_code = data.get('tariff_code')  # Код тарифа
    
    if not from_city or not to_city:
        return jsonify({'error': 'Укажите города отправки и доставки'}), 400
    
    try:
        from app.integrations.cdek import get_cdek_client
        
        client = get_cdek_client()
        
        result = client.calculate(
            from_location={'code': from_city},
            to_location={'code': to_city},
            weight=weight,
            tariff_code=tariff_code
        )
        
        return jsonify({
            'success': True,
            'result': result
        })
    
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/cdek/validate', methods=['POST'])
@login_required
def cdek_validate():
    """
    Валидация учетных данных CDEK.
    URL: seller.domain/api/cdek/validate
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    data = request.get_json()
    
    account = data.get('account')
    secure = data.get('secure')
    test_mode = data.get('test_mode', True)
    
    if not account or not secure:
        return jsonify({
            'success': False,
            'message': 'Укажите Account и Secure'
        }), 400
    
    try:
        from app.integrations.cdek import validate_credentials
        
        result = validate_credentials(account, secure, test_mode)
        return jsonify(result)
    
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Ошибка: {str(e)}'
        }), 500


@bp.route('/api/cdek/save_credentials', methods=['POST'])
@login_required
def cdek_save_credentials():
    """
    Сохранение и валидация учетных данных CDEK.
    URL: seller.domain/api/cdek/save_credentials
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    data = request.get_json()
    
    delivery_service_id = data.get('delivery_service_id')
    account = data.get('account')
    secure = data.get('secure')
    contract_number = data.get('contract_number', '')
    pvz_code = data.get('pvz_code', '')
    pvz_address = data.get('pvz_address', '')
    pvz_city = data.get('pvz_city', '')
    pvz_city_code = data.get('pvz_city_code')
    tariffs = data.get('tariffs', [])
    ship_from = data.get('ship_from', '')
    test_mode = data.get('test_mode', True)
    
    if not delivery_service_id:
        return jsonify({
            'success': False,
            'message': 'Укажите службу доставки'
        }), 400
    
    # Проверяем, что это CDEK
    service = db.session.get(DeliveryService, delivery_service_id)
    if not service or service.code != 'cdek':
        return jsonify({
            'success': False,
            'message': 'Валидация доступна только для СДЭК'
        }), 400
    
    # Валидируем credentials
    if account and secure:
        from app.integrations.cdek import validate_credentials
        validation = validate_credentials(account, secure, test_mode)
        
        if not validation['success']:
            return jsonify(validation), 400
    
    # Ищем существующий профиль
    existing = SellerDelivery.query.filter_by(
        seller_id=current_user.id,
        delivery_service_id=delivery_service_id
    ).first()
    
    # Подготовка credentials
    api_credentials = {
        'account': account,
        'secure': secure,
        'test_mode': test_mode
    }
    
    if existing:
        existing.api_credentials = api_credentials
        existing.ship_from_address = ship_from
        existing.contract_number = contract_number
        existing.pvz_code = pvz_code
        existing.pvz_address = pvz_address
        existing.pvz_city = pvz_city
        existing.pvz_city_code = int(pvz_city_code) if pvz_city_code else None
        existing.tariffs = tariffs
        existing.is_active = True
        message = 'Настройки доставки обновлены'
    else:
        profile = SellerDelivery(
            seller_id=current_user.id,
            delivery_service_id=delivery_service_id,
            api_credentials=api_credentials,
            ship_from_address=ship_from,
            contract_number=contract_number,
            pvz_code=pvz_code,
            pvz_address=pvz_address,
            pvz_city=pvz_city,
            pvz_city_code=int(pvz_city_code) if pvz_city_code else None,
            tariffs=tariffs,
            is_active=True
        )
        db.session.add(profile)
        message = 'Способ доставки добавлен'
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': message
    })


@bp.route('/api/cdek/pvz')
@login_required
def seller_cdek_pvz():
    """
    Получение списка ПВЗ СДЭК (алиас для обратной совместимости).
    URL: seller.domain/api/cdek/pvz?city=Москва&delivery_id=1
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    city = request.args.get('city', '')
    region = request.args.get('region', '')
    delivery_id = request.args.get('delivery_id', type=int)
    
    # Получаем профиль доставки для использования правильных credentials
    seller_delivery = None
    if delivery_id:
        seller_delivery = db.session.get(SellerDelivery, delivery_id)
        if seller_delivery and seller_delivery.seller_id != current_user.id:
            abort(403)
    
    try:
        from app.integrations.cdek import get_cdek_pvz_list, get_cdek_pvz_list_public
        
        # Пробуем получить список ПВЗ через API
        try:
            pvz_list = get_cdek_pvz_list(
                city_code=None, 
                region_code=region or None,
                seller_delivery=seller_delivery
            )
        except Exception:
            pvz_list = None
        
        # Если пустой - используем публичный API
        if not pvz_list:
            pvz_list = get_cdek_pvz_list_public()
        
        # Фильтрация по городу если указан
        if city:
            city_lower = city.lower()
            pvz_list = [p for p in pvz_list if city_lower in (p.get('location', {}).get('city', '') or '').lower()]
        
        # Форматирование для фронтенда с координатами
        result = []
        for p in pvz_list[:50]:
            location = p.get('location', {})
            result.append({
                'code': p.get('code'),
                'name': p.get('name'),
                'city': location.get('city'),
                'address': location.get('address'),
                'address_full': location.get('address_full'),
                'type': p.get('type'),
                'work_time': p.get('work_time'),
                'weight_max': p.get('weight_max'),
                'latitude': p.get('latitude') or location.get('latitude'),
                'longitude': p.get('longitude') or location.get('longitude'),
                'city_code': p.get('city_code') or location.get('city_code'),
            })
        
        return jsonify({'success': True, 'pvz': result})
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    """
    Настройки профиля продавца.
    URL: seller.domain/settings
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    seller = current_user
    
    if request.method == 'POST':
        # Обновление данных продавца
        seller.store_name = request.form.get('store_name')
        seller.store_description = _sanitize_html(request.form.get('store_description'))
        seller.phone = request.form.get('phone')

        # Дневной лимит заказов. Пустая строка или нечисловое значение
        # → снимаем лимит (NULL = безлимит). Иначе — целое >= 1.
        limit_raw = (request.form.get('daily_orders_limit') or '').strip()
        if limit_raw == '':
            seller.daily_orders_limit = None
        else:
            try:
                limit_value = int(limit_raw)
            except (TypeError, ValueError):
                flash('Лимит заказов должен быть целым числом.', 'error')
                return redirect(url_for('seller.settings'))
            if limit_value < 1:
                flash('Лимит заказов должен быть не меньше 1. Оставьте поле пустым, чтобы снять ограничение.', 'error')
                return redirect(url_for('seller.settings'))
            seller.daily_orders_limit = limit_value

        # Email для уведомлений (отдельное поле)
        new_email = request.form.get('email')
        if new_email and new_email != seller.email:
            # Проверяем, что email не занят другим продавцом
            existing = Seller.query.filter(
                Seller.email == new_email,
                Seller.id != seller.id
            ).first()
            if existing:
                flash('Этот email уже используется другим продавцом.', 'error')
                return redirect(url_for('seller.settings'))
            seller.email = new_email

        db.session.commit()
        flash('Настройки сохранены.', 'success')
        return redirect(url_for('seller.settings'))
    
    return render_template('seller/settings.html',
                         title='Настройки',
                         seller=seller)


# =============================================================================
# Аватар магазина: загрузка / удаление
# =============================================================================

# Папка для аватарок магазинов (относительно app/static/uploads)
SELLERS_AVATAR_FOLDER = 'sellers'
# Максимальный размер ресайза (квадрат)
SELLERS_AVATAR_SIZE = (400, 400)


def _save_seller_avatar(seller, file_storage):
    """
    Сохраняет аватар магазина в app/static/uploads/sellers/seller_<id>.jpg
    с ресайзом до 400x400 и конвертацией в JPEG. Возвращает относительный
    путь для БД или None при ошибке.
    """
    import os
    from werkzeug.utils import secure_filename
    from PIL import Image
    from flask import current_app
    from app.utils.helpers import allowed_file

    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        return None

    # Абсолютный путь к папке загрузок
    upload_dir = os.path.join(
        current_app.root_path, 'static', 'uploads', SELLERS_AVATAR_FOLDER
    )
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"seller_{seller.id}.jpg"
    filepath = os.path.join(upload_dir, secure_filename(filename))

    try:
        img = Image.open(file_storage.stream)
        img.load()
    except Exception:
        return None

    # Ресайз по большей стороне до 400x400, пропорции сохраняем
    img.thumbnail(SELLERS_AVATAR_SIZE, Image.Resampling.LANCZOS)

    # Создаём квадратный холст с белым фоном (для PNG с прозрачностью
    # или когда картинка не квадратная — центрируем)
    canvas = Image.new('RGB', SELLERS_AVATAR_SIZE, (255, 255, 255))
    offset = ((SELLERS_AVATAR_SIZE[0] - img.size[0]) // 2,
              (SELLERS_AVATAR_SIZE[1] - img.size[1]) // 2)
    # Если у картинки есть альфа — используем её как маску
    if img.mode in ('RGBA', 'LA'):
        canvas.paste(img.convert('RGBA'), offset, img.convert('RGBA').split()[-1])
    else:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        canvas.paste(img, offset)

    try:
        canvas.save(filepath, 'JPEG', quality=88, optimize=True)
    except Exception:
        return None

    # В БД храним только имя файла; префикс uploads/sellers/ добавляют шаблоны.
    return filename


@bp.route('/settings/avatar', methods=['POST'])
def settings_upload_avatar():
    """
    Загрузка/замена аватарки магазина.
    URL: seller.domain/settings/avatar
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    seller = current_user
    file = request.files.get('avatar')

    if not file or not file.filename:
        flash('Выберите файл для аватарки.', 'error')
        return redirect(url_for('seller.settings'))

    rel_path = _save_seller_avatar(seller, file)
    if not rel_path:
        flash('Не удалось загрузить аватар. Допустимы PNG, JPG, GIF, WebP до 16 МБ.', 'error')
        return redirect(url_for('seller.settings'))

    seller.store_logo = rel_path
    db.session.commit()
    flash('Аватар магазина обновлён.', 'success')
    return redirect(url_for('seller.settings'))


@bp.route('/settings/avatar/delete', methods=['POST'])
def settings_delete_avatar():
    """
    Удаление аватарки магазина.
    URL: seller.domain/settings/avatar/delete
    """
    if not current_user.is_authenticated or not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    seller = current_user
    if seller.store_logo:
        try:
            from flask import current_app
            import os
            full_path = os.path.join(
                current_app.root_path, 'static', 'uploads', 'sellers', seller.store_logo
            )
            if os.path.exists(full_path):
                os.remove(full_path)
        except Exception:
            pass
        seller.store_logo = None
        db.session.commit()
        flash('Аватар магазина удалён.', 'success')
    return redirect(url_for('seller.settings'))



@bp.route('/loyalty')
@login_required
def loyalty():
    """
    Раздел «Лояльность» в дашборде продавца.
    URL: seller.domain/loyalty

    Содержит две вкладки: «Бонусы» (программа лояльности) и «Промокоды».
    Если глобальные тумблеры выключены админом — отдельные секции
    показывают заглушку, но страница остаётся доступной.
    """
    from app.utils.loyalty import (
        is_loyalty_enabled,
        is_promo_enabled,
        is_promo_enabled_for_seller,
        get_active_rates,
        get_or_create_seller_loyalty,
    )
    from app.models.communications import Settings
    from app.models.promo import PromoCode

    if not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))

    loyalty_enabled = is_loyalty_enabled()
    promo_enabled_global = is_promo_enabled()
    promo_seller_enabled = is_promo_enabled_for_seller(current_user.id)

    rates = get_active_rates() if loyalty_enabled else []
    sl = get_or_create_seller_loyalty(current_user.id)
    db.session.commit()  # зафиксировать создание записи, если её не было

    promo_codes = (
        PromoCode.query
        .filter_by(seller_id=current_user.id)
        .order_by(PromoCode.created_at.desc())
        .all()
    )

    # Какая вкладка активна: ?tab=promo или ?tab=bonus. По умолчанию — bonus.
    active_tab = (request.args.get('tab') or 'bonus').lower()
    if active_tab not in ('bonus', 'promo'):
        active_tab = 'bonus'

    return render_template(
        'seller/loyalty.html',
        title='Лояльность',
        rates=rates,
        seller_loyalty=sl,
        promo_codes=promo_codes,
        promo_enabled_global=promo_enabled_global,
        promo_seller_enabled=promo_seller_enabled,
        active_tab=active_tab,
    )


@bp.route('/loyalty/save', methods=['POST'])
@login_required
@csrf.exempt
def loyalty_save():
    """
    Сохранение выбора курса и % списания продавцом.
    URL: seller.domain/loyalty/save
    """
    from app.utils.loyalty import is_loyalty_enabled, get_or_create_seller_loyalty
    from app.models.loyalty import LoyaltyRate

    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403

    if not is_loyalty_enabled():
        return jsonify({'error': 'Программа лояльности выключена'}), 400

    sl = get_or_create_seller_loyalty(current_user.id)

    rate_id_raw = request.form.get('rate_id', '').strip()
    rate_id = int(rate_id_raw) if rate_id_raw.isdigit() else None
    if rate_id is not None:
        rate = db.session.get(LoyaltyRate, rate_id)
        if not rate or not rate.is_active:
            return jsonify({'error': 'Выбранный курс недоступен'}), 400
        sl.rate_id = rate_id
    else:
        sl.rate_id = None

    try:
        payback = int(request.form.get('payback_percent', 0))
    except (TypeError, ValueError):
        payback = 0
    if payback < 0:
        payback = 0
    if payback > 100:
        payback = 100
    sl.payback_percent = payback

    # Тумблер бонусной программы у продавца.
    # Если курс не выбран — насильно выключаем, чтобы не было «включено, но
    # без курса» (начисления всё равно не пойдут).
    sl.is_active = (
        request.form.get('is_active') in ('1', 'true', 'on')
        and sl.rate_id is not None
    )
    db.session.commit()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True})

    flash('Настройки лояльности сохранены.', 'success')
    return redirect(url_for('seller.loyalty'))


# =============================================================================
# Промокоды продавца
# =============================================================================

@bp.route('/loyalty/promo-toggle', methods=['POST'])
@login_required
@csrf.exempt
def loyalty_promo_toggle():
    """
    Индивидуальный тумблер «промокоды» для текущего продавца.
    URL: seller.domain/loyalty/promo-toggle
    """
    from app.utils.loyalty import is_promo_enabled, set_promo_enabled_for_seller

    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403

    if not is_promo_enabled():
        return jsonify({'error': 'Промокоды выключены администратором'}), 400

    enabled = request.form.get('enabled') in ('1', 'true', 'on')
    set_promo_enabled_for_seller(current_user.id, enabled)
    return jsonify({'success': True, 'enabled': enabled})


@bp.route('/loyalty/promo-generate', methods=['POST'])
@login_required
@csrf.exempt
def loyalty_promo_generate():
    """
    Сгенерировать уникальный 6-символьный промокод (A-Z, 0-9), который
    ещё не выдан этим продавцом.
    URL: seller.domain/loyalty/promo-generate
    """
    from app.models.promo import PromoCode

    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403

    import secrets
    import string

    alphabet = string.ascii_uppercase + string.digits  # 36 символов
    length = 6
    existing = {
        c.code for c in PromoCode.query
        .filter_by(seller_id=current_user.id)
        .with_entities(PromoCode.code)
        .all()
    }
    for _ in range(50):
        code = ''.join(secrets.choice(alphabet) for _ in range(length))
        if code not in existing:
            return jsonify({'success': True, 'code': code})
    return jsonify({'error': 'Не удалось сгенерировать уникальный код, попробуйте ещё раз'}), 500


@bp.route('/loyalty/promo-create', methods=['POST'])
@login_required
@csrf.exempt
def loyalty_promo_create():
    """
    Создать новый промокод.
    URL: seller.domain/loyalty/promo-create
    """
    from datetime import datetime, timedelta
    from decimal import Decimal, InvalidOperation
    from app.models.promo import PromoCode
    from app.models.users import Buyer
    from app.utils.loyalty import is_promo_enabled, is_promo_enabled_for_seller

    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403

    if not is_promo_enabled() or not is_promo_enabled_for_seller(current_user.id):
        return jsonify({'error': 'Промокоды сейчас выключены'}), 400

    # Код
    code = (request.form.get('code') or '').strip().upper()
    if not code:
        return jsonify({'error': 'Сначала задайте или сгенерируйте промокод'}), 400
    if len(code) > 32:
        return jsonify({'error': 'Слишком длинный код'}), 400
    # Только латиница/цифры
    import re
    if not re.match(r'^[A-Z0-9]+$', code):
        return jsonify({'error': 'Промокод может содержать только латинские буквы и цифры'}), 400
    exists = PromoCode.query.filter_by(
        seller_id=current_user.id, code=code,
    ).first()
    if exists:
        return jsonify({'error': 'Такой промокод у вас уже есть'}), 400

    # Срок действия
    validity_type = (request.form.get('validity_type') or 'forever').strip()
    if validity_type not in ('forever', 'days', 'until'):
        return jsonify({'error': 'Некорректный тип срока действия'}), 400

    valid_days = None
    valid_until = None
    if validity_type == 'days':
        raw = (request.form.get('valid_days') or '').strip()
        try:
            valid_days = int(Decimal(raw))
        except (InvalidOperation, ValueError):
            return jsonify({'error': 'Укажите корректное количество дней'}), 400
        if valid_days < 1:
            return jsonify({'error': 'Срок должен быть не меньше 1 дня'}), 400
    elif validity_type == 'until':
        raw = (request.form.get('valid_until') or '').strip()
        try:
            valid_until = datetime.strptime(raw, '%Y-%m-%d')
            # +1 день, чтобы дата включительно работала весь день
            valid_until = valid_until.replace(hour=23, minute=59, second=59)
        except ValueError:
            return jsonify({'error': 'Укажите корректную дату'}), 400
        if valid_until < datetime.utcnow():
            return jsonify({'error': 'Дата должна быть в будущем'}), 400

    # Многоразовость
    usage_type = (request.form.get('usage_type') or 'single').strip()
    if usage_type not in ('single', 'multiple'):
        return jsonify({'error': 'Некорректный тип использования'}), 400

    if usage_type == 'single':
        max_uses = 1
    else:
        raw = (request.form.get('max_uses') or '').strip()
        try:
            max_uses = int(Decimal(raw))
        except (InvalidOperation, ValueError):
            return jsonify({'error': 'Укажите допустимое число использований'}), 400
        if max_uses < 2:
            return jsonify({'error': 'Для многоразового промокода укажите число больше 1'}), 400

    # Получатель
    recipient_type = (request.form.get('recipient_type') or 'public').strip()
    if recipient_type not in ('public', 'personal'):
        return jsonify({'error': 'Некорректный тип получателя'}), 400

    buyer_id = None
    if recipient_type == 'personal':
        raw = (request.form.get('buyer_id') or '').strip()
        if not raw.isdigit():
            return jsonify({'error': 'Выберите получателя из списка'}), 400
        buyer_id = int(raw)
        if not db.session.get(Buyer, buyer_id):
            return jsonify({'error': 'Покупатель не найден'}), 400

    # Скидка
    discount_type = (request.form.get('discount_type') or 'rub').strip()
    if discount_type not in ('rub', 'percent'):
        return jsonify({'error': 'Некорректный тип скидки'}), 400
    raw = (request.form.get('discount_value') or '').strip().replace(',', '.')
    try:
        discount_value = float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return jsonify({'error': 'Укажите размер скидки'}), 400
    if discount_value <= 0:
        return jsonify({'error': 'Скидка должна быть больше нуля'}), 400
    if discount_type == 'percent' and discount_value > 100:
        return jsonify({'error': 'Процент скидки не может быть больше 100'}), 400

    # Мин. сумма
    min_order_amount = None
    min_order_mode = (request.form.get('min_order_mode') or 'any').strip()
    if min_order_mode == 'from':
        raw = (request.form.get('min_order_amount') or '').strip().replace(',', '.')
        try:
            min_order_amount = float(Decimal(raw))
        except (InvalidOperation, ValueError):
            return jsonify({'error': 'Укажите минимальную сумму заказа'}), 400
        if min_order_amount < 0:
            min_order_amount = 0.0

    # Если выбрано «days» — вычислим valid_until
    if validity_type == 'days':
        valid_until = datetime.utcnow() + timedelta(days=valid_days)

    promo = PromoCode(
        seller_id=current_user.id,
        code=code,
        discount_type=discount_type,
        discount_value=discount_value,
        min_order_amount=min_order_amount,
        recipient_type=recipient_type,
        buyer_id=buyer_id,
        usage_type=usage_type,
        max_uses=max_uses,
        used_count=0,
        validity_type=validity_type,
        valid_days=valid_days,
        valid_until=valid_until,
        is_active=True,
    )
    db.session.add(promo)
    db.session.commit()
    return jsonify({'success': True, 'id': promo.id})


@bp.route('/loyalty/promo-toggle-active', methods=['POST'])
@login_required
@csrf.exempt
def loyalty_promo_toggle_active():
    """
    Включить/выключить конкретный промокод продавца.
    URL: seller.domain/loyalty/promo-toggle-active
    """
    from app.models.promo import PromoCode

    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403

    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return jsonify({'error': 'Некорректный id промокода'}), 400
    promo = db.session.get(PromoCode, int(raw_id))
    if not promo or promo.seller_id != current_user.id:
        return jsonify({'error': 'Промокод не найден'}), 404

    promo.is_active = not bool(promo.is_active)
    db.session.commit()
    return jsonify({
        'success': True,
        'id': promo.id,
        'is_active': promo.is_active,
        'is_valid_now': promo.is_valid_now,
        'status_label': promo.status_label,
    })


@bp.route('/loyalty/promo-delete', methods=['POST'])
@login_required
@csrf.exempt
def loyalty_promo_delete():
    """
    Удалить промокод продавца.
    URL: seller.domain/loyalty/promo-delete
    """
    from app.models.promo import PromoCode

    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403

    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return jsonify({'error': 'Некорректный id промокода'}), 400
    promo = db.session.get(PromoCode, int(raw_id))
    if not promo or promo.seller_id != current_user.id:
        return jsonify({'error': 'Промокод не найден'}), 404

    db.session.delete(promo)
    db.session.commit()
    return jsonify({'success': True, 'id': int(raw_id)})


@bp.route('/loyalty/promo-buyer-search', methods=['POST'])
@login_required
@csrf.exempt
def loyalty_promo_buyer_search():
    """
    Поиск покупателей по логину для индивидуального промокода.
    URL: seller.domain/loyalty/promo-buyer-search?q=...
    """
    from app.models.users import Buyer

    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403

    q = (request.form.get('q') or '').strip()
    if len(q) < 1:
        return jsonify({'results': []})

    like = f'%{q}%'
    buyers = (
        Buyer.query
        .filter(Buyer.is_active.is_(True))
        .filter(Buyer.login.ilike(like))
        .order_by(Buyer.login.asc())
        .limit(20)
        .all()
    )
    results = []
    for b in buyers:
        label = b.login
        if b.email:
            label = f'{b.login} — {b.email}'
        results.append({'id': b.id, 'label': label})
    return jsonify({'results': results})


# =============================================================================
# API для ПВЗ
# =============================================================================

@bp.route('/api/pvz')
@login_required
def seller_pvz_list():
    """
    Получение списка ПВЗ для выбора на карте.
    URL: seller.domain/api/pvz?city=Москва&delivery_id=1
    """
    if not isinstance(current_user, Seller):
        return jsonify({'error': 'Доступ только для продавцов'}), 403
    
    city = request.args.get('city', '')
    region = request.args.get('region', '')
    delivery_id = request.args.get('delivery_id', type=int)
    
    # Получаем профиль доставки для использования правильных credentials
    seller_delivery = None
    if delivery_id:
        seller_delivery = db.session.get(SellerDelivery, delivery_id)
        if seller_delivery and seller_delivery.seller_id != current_user.id:
            abort(403)
    
    try:
        from app.integrations.cdek import get_cdek_pvz_list, get_cdek_pvz_list_public
        
        # Пробуем получить список ПВЗ через API
        try:
            pvz_list = get_cdek_pvz_list(
                city_code=None, 
                region_code=region or None,
                seller_delivery=seller_delivery
            )
        except Exception:
            pvz_list = None
        
        # Если пустой - используем публичный API
        if not pvz_list:
            pvz_list = get_cdek_pvz_list_public()
        
        # Фильтрация по городу если указан
        if city:
            city_lower = city.lower()
            pvz_list = [p for p in pvz_list if city_lower in (p.get('location', {}).get('city', '') or '').lower()]
        
        # Форматирование для фронтенда с координатами
        result = []
        for p in pvz_list[:50]:
            location = p.get('location', {})
            result.append({
                'code': p.get('code'),
                'name': p.get('name'),
                'city': location.get('city'),
                'address': location.get('address'),
                'address_full': location.get('address_full'),
                'type': p.get('type'),
                'work_time': p.get('work_time'),
                'weight_max': p.get('weight_max'),
                'latitude': p.get('latitude') or location.get('latitude'),
                'longitude': p.get('longitude') or location.get('longitude'),
                'city_code': p.get('city_code') or location.get('city_code'),
            })
        
        return jsonify({'success': True, 'pvz': result})
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/delivery/edit/<int:delivery_id>', methods=['GET', 'POST'])
@login_required
@require_active_tariff
def delivery_edit(delivery_id):
    """
    Редактирование профиля доставки.
    URL: seller.domain/delivery/edit/{id}
    """
    if not isinstance(current_user, Seller):
        return redirect(url_for('auth_seller.seller_login'))
    
    delivery = db.session.get(SellerDelivery, delivery_id)
    if not delivery or delivery.seller_id != current_user.id:
        abort(404)
    
    if request.method == 'POST':
        # Общие поля
        api_login = request.form.get('api_login')
        api_password = request.form.get('api_password')
        ship_from = request.form.get('ship_from')
        
        # Сохраняем API credentials в зависимости от типа службы
        if delivery.is_cdek:
            # Для СДЭК - только account и secure
            if request.form.get('cdek_account') or request.form.get('cdek_secure'):
                delivery.api_credentials = {
                    'account': request.form.get('cdek_account', ''),
                    'secure': request.form.get('cdek_secure', '')
                }
        else:
            # Для других служб - login и password
            if api_login and api_password:
                delivery.api_credentials = {
                    'login': api_login,
                    'password': api_password
                }
        
        if ship_from:
            delivery.ship_from_address = ship_from
        
        # CDEK-специфичные поля
        if delivery.is_cdek:
            delivery.contract_number = request.form.get('contract_number')
            delivery.pvz_code = request.form.get('pvz_code')
            delivery.pvz_address = request.form.get('pvz_address')
            delivery.pvz_city = request.form.get('pvz_city')
            
            pvz_city_code = request.form.get('pvz_city_code')
            delivery.pvz_city_code = int(pvz_city_code) if pvz_city_code and pvz_city_code.isdigit() else None
            
            # Тарифы
            tariffs = request.form.getlist('tariffs')
            delivery.tariffs = tariffs if tariffs else []
        
        delivery.is_active = True
        db.session.commit()
        
        flash('Настройки доставки обновлены.', 'success')
        return redirect(url_for('seller.delivery'))
    
    # GET запрос - показать форму
    service = delivery.delivery_service
    return render_template('seller/delivery_edit.html',
                         title='Редактирование доставки',
                         delivery=delivery,
                         service=service)


@bp.route('/api/deliveries/save-active', methods=['POST'])
@login_required
def save_active_deliveries():
    """
    Сохранение активных служб доставки для продавца.
    Используется в панели быстрого выбора доставок.
    URL: seller.domain/api/deliveries/save-active
    """
    if not isinstance(current_user, Seller):
        return jsonify({'success': False, 'message': 'Доступ только для продавцов'}), 403
    
    data = request.get_json()
    service_ids = data.get('service_ids', [])
    
    if not isinstance(service_ids, list):
        service_ids = [service_ids]
    
    seller = current_user
    
    # Получаем все активные профили доставки продавца
    active_profiles = SellerDelivery.query.filter_by(
        seller_id=seller.id,
        is_active=True
    ).all()
    
    # Сбрасываем is_active для всех профилей
    for profile in active_profiles:
        profile.is_active = False
    
    # Активируем выбранные службы
    for service_id in service_ids:
        # Ищем существующий профиль
        profile = SellerDelivery.query.filter_by(
            seller_id=seller.id,
            delivery_service_id=service_id
        ).first()
        
        if profile:
            profile.is_active = True
        else:
            # Создаём новый профиль (без настроек API, только флаг активности)
            profile = SellerDelivery(
                seller_id=seller.id,
                delivery_service_id=service_id,
                is_active=True
            )
            db.session.add(profile)
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': f'Сохранено {len(service_ids)} служб доставки'
    })
