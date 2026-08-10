"""
Blueprint админ-панели.
URL: /main_admin/*
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from datetime import datetime
from app import db, csrf
from app.models.users import Buyer, Seller, Admin, DeliveryService
from app.models.products import Category, Parameter, Product, CategoryParameter, ProductParameter, ProductCard
from app.models.orders import Order, Promotion, Banner
from app.models.communications import Message, Review, Settings, InfoPost, RoadmapEvent, EduMaterial, TariffBlock, TariffRow
from app.models.tariffs import (
    SellerTariffSubscription, TariffTransaction
)
from app.utils.helpers import format_price


bp = Blueprint('admin', __name__, url_prefix='/main_admin')


@bp.route('/auth/login', methods=['GET', 'POST'])
def login():
    """
    Страница входа для администраторов.
    URL: /main_admin/auth/login
    """
    from flask import current_app, session
    from werkzeug.security import check_password_hash
    
    # Проверка уже авторизованного админа
    if session.get('main_admin_authenticated'):
        return redirect(url_for('admin.dashboard'))
    
    if current_user.is_authenticated and isinstance(current_user, Admin):
        return redirect(url_for('admin.dashboard'))
    
    if request.method == 'POST':
        login_field = request.form.get('login')
        password = request.form.get('password')
        
        # Проверка абсолютного админа из конфига
        main_admin_login = current_app.config.get('MAIN_ADMIN_LOGIN')
        main_admin_hash = current_app.config.get('MAIN_ADMIN_PASSWORD_HASH')
        
        if (login_field == main_admin_login and 
            main_admin_hash and 
            check_password_hash(main_admin_hash, password)):
            
            session['main_admin_authenticated'] = True
            session['main_admin_user'] = login_field
            
            return redirect(url_for('admin.dashboard'))
        
        # Проверка админа из БД
        admin = Admin.query.filter(
            (Admin.login == login_field) | (Admin.email == login_field)
        ).first()
        
        if admin and admin.check_password(password):
            from flask_login import login_user
            login_user(admin)
            admin.last_login = db.func.now()
            db.session.commit()
            
            return redirect(url_for('admin.dashboard'))
        
        flash('Неверный логин или пароль.', 'error')
    
    return render_template('auth/admin_login.html', title='Вход для администратора')


@bp.route('/')
def dashboard():
    """
    Главная страница админ-панели.
    URL: /main_admin
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    # Статистика
    buyers_count = Buyer.query.count()
    sellers_count = Seller.query.count()
    products_count = Product.query.count()
    orders_count = Order.query.count()
    
    # Новые товары на модерации
    pending_products = Product.query.filter_by(status='on_moderation').count()
    
    # Заказы сегодня
    today = datetime.utcnow().date()
    today_orders = Order.query.filter(
        func.date(Order.created_at) == today
    ).count()
    
    # Выручка за сегодня
    today_revenue = db.session.query(func.sum(Order.total_price)).filter(
        func.date(Order.created_at) == today,
        Order.status.in_(['delivered', 'shipped'])
    ).scalar() or 0
    
    # Последние заказы
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()
    
    return render_template('admin/dashboard.html',
                         title='Панель администратора',
                         buyers_count=buyers_count,
                         sellers_count=sellers_count,
                         products_count=products_count,
                         orders_count=orders_count,
                         pending_products=pending_products,
                         today_orders=today_orders,
                         today_revenue=today_revenue,
                         recent_orders=recent_orders)


# ========== Информация / новости ==========

# Содержимое трёх верхних плашек. Ключ — якорь секции.
INFO_TILES = {
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
            'Описание тарифов для продавцов: комиссия, лимиты, фичи. '
            'Когда подключите биллинг — замените этот текст реальной таблицей тарифов.'
        ),
    },
    'education': {
        'title': 'Обучение',
        'icon': 'bi-mortarboard',
        'body': (
            'Подборка материалов: как завести магазин, оформить карточку товара, '
            'работать с акциями и аналитикой. Добавьте сюда ссылки на видео и инструкции.'
        ),
    },
}


@bp.route('/info', methods=['GET', 'POST'])
def info():
    """
    Раздел «Информация»: плашки + лента новостей.
    GET  — отобразить страницу.
    POST — создать новую новость (только админ).
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    # Параметр сортировки: 'new' (новые сверху) | 'old' (старые сверху)
    sort = request.args.get('sort', 'new')
    if sort not in ('new', 'old'):
        sort = 'new'
    order = InfoPost.sort_date.desc() if sort == 'new' else InfoPost.sort_date.asc()

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        body = (request.form.get('body') or '').strip()
        media_type = (request.form.get('media_type') or '').strip() or None
        media_url = (request.form.get('media_url') or '').strip() or None
        audience = (request.form.get('audience') or 'all').strip()
        if audience not in ('all', 'admin', 'seller'):
            audience = 'all'
        sort_date_raw = (request.form.get('sort_date') or '').strip()
        tag = (request.form.get('tag') or '').strip()[:50] or None

        if not title:
            flash('Заголовок новости обязателен.', 'error')
        else:
            from datetime import datetime as _dt
            if media_type and media_type not in ('image', 'video'):
                media_type = None
            if media_type and not media_url:
                flash('Укажите ссылку на медиа (URL).', 'error')
            else:
                if sort_date_raw:
                    try:
                        sort_date = _dt.fromisoformat(sort_date_raw)
                    except ValueError:
                        sort_date = _dt.utcnow()
                else:
                    sort_date = _dt.utcnow()
                post = InfoPost(
                    title=title,
                    body=body or None,
                    media_type=media_type,
                    media_url=media_url,
                    tag=tag,
                    audience=audience,
                    is_published=True,
                    sort_date=sort_date,
                    created_at=_dt.utcnow(),
                )
                db.session.add(post)
                db.session.commit()
                flash('Новость добавлена.', 'success')
                return redirect(url_for('admin.info', sort=sort))

    posts = (
        InfoPost.query
        .filter(InfoPost.audience.in_(('all', 'admin')))
        .filter(InfoPost.is_published == True)
        .order_by(order)
        .all()
    )

    # Список существующих тегов для datalist в форме
    existing_tags = [
        t for (t,) in (
            db.session.query(InfoPost.tag)
            .filter(InfoPost.tag.isnot(None))
            .filter(InfoPost.tag != '')
            .distinct()
            .order_by(InfoPost.tag)
            .all()
        )
    ]

    # События «Траектории развития»: админ видит audience=all|admin.
    # Лента сортируется по дате — самые свежие сверху.
    roadmap_events, roadmap_categories = _admin_roadmap_context()

    return render_template(
        'admin/info.html',
        title='Информация',
        tiles=INFO_TILES,
        posts=posts,
        sort=sort,
        can_manage=True,
        existing_tags=existing_tags,
        roadmap_events=roadmap_events,
        roadmap_categories=roadmap_categories,
    )


@bp.route('/info/<int:post_id>/delete', methods=['POST'])
def info_delete(post_id):
    """Удаление новости (только админ)."""
    if not is_admin():
        return jsonify({'error': 'forbidden'}), 403
    post = db.session.get(InfoPost, post_id)
    if not post:
        flash('Новость не найдена.', 'error')
    else:
        db.session.delete(post)
        db.session.commit()
        flash('Новость удалена.', 'success')
    return redirect(url_for('admin.info', sort=request.args.get('sort', 'new')))


# ========== Учебные материалы (раздел «Информация → Обучение») ==========

# Список плиток учебных материалов живёт прямо на /info/education
# (роут info_education ниже), отдельной страницы-списка не делаем —
# иначе будут две почти одинаковых вьюшки. Поэтому здесь только
# создание / просмотр / редактирование / удаление.

# Папка под обложки учебных материалов (относительно UPLOAD_FOLDER).
EDU_COVER_FOLDER = 'edu_covers'

# Допустимые форматы обложки.
EDU_COVER_ALLOWED_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}

# Максимальная ширина/высота обложки после ресайза (сохраняем пропорции).
EDU_COVER_MAX_DIM = 1600


def _save_edu_cover(file_storage):
    """
    Сохраняет загруженный файл-обложку в static/uploads/edu_covers/.
    Возвращает относительный путь вида 'uploads/edu_covers/<file>'
    или None при ошибке валидации.

    Особенность: на этапе создания материала у нас ещё нет material.id,
    поэтому сначала пишем под именем pending_<uuid>.<ext>, а после
    коммита _rename_edu_cover_to_id переименовывает под material.id.
    """
    from werkzeug.utils import secure_filename
    from flask import current_app
    import os
    import uuid

    if not file_storage or not file_storage.filename:
        return None

    # Проверка расширения.
    original = file_storage.filename
    ext = original.rsplit('.', 1)[-1].lower() if '.' in original else ''
    if ext not in EDU_COVER_ALLOWED_EXTS:
        return None
    # Проверка MIME — на всякий случай (браузер может прислать что угодно).
    mimetype = (file_storage.mimetype or '').lower()
    if not mimetype.startswith('image/'):
        return None

    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', EDU_COVER_FOLDER)
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = secure_filename(f'pending_{uuid.uuid4().hex}.{ext}')
    full_path = os.path.join(upload_dir, safe_name)

    try:
        from PIL import Image
        img = Image.open(file_storage.stream)
        img.load()
        # Ресайз по большей стороне до EDU_COVER_MAX_DIM, пропорции сохраняем.
        img.thumbnail((EDU_COVER_MAX_DIM, EDU_COVER_MAX_DIM), Image.Resampling.LANCZOS)
        # RGBA → RGB для JPEG.
        save_ext = ext
        if save_ext in ('jpg', 'jpeg') and img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        img.save(full_path, optimize=True)
    except Exception:
        # Fallback: сохраняем как есть (PIL может отсутствовать).
        file_storage.stream.seek(0)
        file_storage.save(full_path)

    return f'uploads/{EDU_COVER_FOLDER}/{safe_name}'


def _rename_edu_cover_to_id(rel_path, material_id):
    """
    Переименовывает pending_<uuid>.<ext> → edu_<id>.<ext>.
    rel_path — относительный путь вида 'uploads/edu_covers/pending_xxx.jpg'.
    Возвращает новый относительный путь или None при ошибке.
    """
    from werkzeug.utils import secure_filename
    from flask import current_app
    import os

    if not rel_path or not rel_path.startswith(f'uploads/{EDU_COVER_FOLDER}/'):
        return None

    filename = rel_path.split('/')[-1]
    if not filename.startswith('pending_'):
        return None

    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    new_name = secure_filename(f'edu_{material_id}.{ext}') if ext else f'edu_{material_id}'
    if ext and not new_name.endswith('.' + ext):
        new_name = f'edu_{material_id}.{ext}'

    old_full = os.path.join(current_app.root_path, 'static', rel_path)
    new_rel = f'uploads/{EDU_COVER_FOLDER}/{new_name}'
    new_full = os.path.join(current_app.root_path, 'static', new_rel)

    try:
        if os.path.exists(old_full):
            os.replace(old_full, new_full)
        return new_rel
    except OSError:
        return None


def _delete_uploaded_file(rel_path):
    """
    Удаляет файл по относительному пути 'uploads/...'.
    Тихо проглатывает ошибки — лучше оставить мусор, чем сломать коммит.
    """
    from flask import current_app
    import os

    if not rel_path:
        return
    full = os.path.join(current_app.root_path, 'static', rel_path)
    full = os.path.normpath(full)
    # Защита от выхода за пределы static/.
    static_root = os.path.normpath(os.path.join(current_app.root_path, 'static'))
    if not full.startswith(static_root):
        return
    try:
        if os.path.isfile(full):
            os.remove(full)
    except OSError:
        pass

@bp.route('/info/education/materials/new', methods=['GET', 'POST'])
def edu_new():
    """Форма создания нового учебного материала (только админ)."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    return _edu_save(None)


@bp.route('/info/education/materials/<int:material_id>', methods=['GET'])
def edu_view(material_id):
    """Страница одного учебного материала (рендерится в главном окне)."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    material = db.session.get(EduMaterial, material_id)
    if not material or not material.is_published:
        abort(404)

    return render_template(
        'admin/edu_view.html',
        title=material.title,
        material=material,
        active_tag=material.tag,
        active_tag_slug=EduMaterial.tag_slug(material.tag) if material.tag else '',
    )


@bp.route('/info/education/materials/<int:material_id>/edit', methods=['GET', 'POST'])
def edu_edit(material_id):
    """Редактирование учебного материала (только админ)."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    material = db.session.get(EduMaterial, material_id)
    if not material:
        abort(404)
    return _edu_save(material)


@bp.route('/info/education/materials/<int:material_id>/delete', methods=['POST'])
def edu_delete(material_id):
    """Удаление учебного материала (только админ)."""
    if not is_admin():
        return jsonify({'error': 'forbidden'}), 403
    material = db.session.get(EduMaterial, material_id)
    if not material:
        flash('Материал не найден.', 'error')
    else:
        db.session.delete(material)
        db.session.commit()
        flash('Материал удалён.', 'success')
    return redirect(url_for('admin.info_education'))


def _edu_save(material):
    """
    Общая логика создания/редактирования материала.
    Если material is None — создаём новый, иначе обновляем существующий.
    """
    is_new = material is None
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        body = (request.form.get('body') or '').strip() or None
        cover_url = (request.form.get('cover_url') or '').strip() or None
        tag = (request.form.get('tag') or '').strip()[:50] or None
        audience = (request.form.get('audience') or 'all').strip()
        if audience not in ('all', 'admin', 'seller'):
            audience = 'all'

        # Загрузка обложки файлом (если прислали). При успехе cover_url
        # перезаписывается локальным путём uploads/edu_covers/<file>.
        # Если поле cover_url пустое И файл не пришёл — обложка сбрасывается.
        cover_file = request.files.get('cover_file')
        new_cover_path = None
        if cover_file and cover_file.filename:
            saved = _save_edu_cover(cover_file)
            if saved:
                new_cover_path = saved
            else:
                flash('Не удалось сохранить обложку — проверьте формат (jpg/png/webp).', 'error')

        if new_cover_path is not None:
            cover_url = new_cover_path
        elif 'cover_file' in request.files and request.files['cover_file'].filename:
            # Файл прислали, но он не прошёл валидацию — старую обложку не трогаем.
            pass
        elif 'cover_clear' in request.form:
            # Чекбокс «удалить обложку».
            cover_url = None

        if not title:
            flash('Название материала обязательно.', 'error')
        else:
            if is_new:
                material = EduMaterial(
                    title=title,
                    body=body,
                    cover_url=cover_url,
                    tag=tag,
                    audience=audience,
                    is_published=True,
                )
                db.session.add(material)
                db.session.flush()  # нужен material.id для имени файла
                # Если обложка уже загружена во временный путь — переименуем
                # под material.id, чтобы имя было стабильным.
                if cover_url and cover_url.startswith('uploads/edu_covers/pending_'):
                    renamed = _rename_edu_cover_to_id(cover_url, material.id)
                    if renamed:
                        material.cover_url = renamed
            else:
                old_cover = material.cover_url
                material.title = title
                material.body = body
                material.cover_url = cover_url
                material.tag = tag
                material.audience = audience
                # Старый файл удаляем, только если реально меняем/сбрасываем
                # и старый путь — локальный (не внешняя ссылка).
                if old_cover and old_cover != cover_url and old_cover.startswith('uploads/edu_covers/'):
                    _delete_uploaded_file(old_cover)
            db.session.commit()
            flash(
                'Материал добавлен.' if is_new else 'Материал обновлён.',
                'success',
            )
            return redirect(url_for('admin.info_education'))

    # GET (или POST с ошибкой валидации) — рендерим форму
    return render_template(
        'admin/edu_form.html',
        title='Новый материал' if is_new else f'Редактирование: {material.title}',
        material=material,
        is_new=is_new,
        all_tags=EduMaterial.all_tags(),
    )


# ========== Траектория развития: события ==========

def _parse_event_date(raw):
    """
    Парсит дату события из формы ('YYYY-MM-DD' от <input type="date">).
    Возвращает (date | None, error_message | None).
    """
    from datetime import date as _date
    raw = (raw or '').strip()
    if not raw:
        return None, 'Укажите дату события.'
    try:
        return _date.fromisoformat(raw), None
    except ValueError:
        return None, 'Некорректный формат даты. Используйте ГГГГ-ММ-ДД.'


@bp.route('/info/roadmap/create', methods=['POST'])
def roadmap_create():
    """
    Создание нового события «Траектории развития».
    URL: /main_admin/info/roadmap/create
    """
    if not is_admin():
        return jsonify({'error': 'forbidden'}), 403

    title = (request.form.get('title') or '').strip()
    description = (request.form.get('description') or '').strip() or None
    category = (request.form.get('category') or 'event').strip()
    audience = (request.form.get('audience') or 'all').strip()
    event_date, err = _parse_event_date(request.form.get('event_date'))

    valid_categories = {c[0] for c in RoadmapEvent.CATEGORIES}
    if category not in valid_categories:
        category = 'event'
    if audience not in ('all', 'admin', 'seller'):
        audience = 'all'

    if not title:
        flash('Заголовок события обязателен.', 'error')
    elif err:
        flash(err, 'error')
    else:
        ev = RoadmapEvent(
            title=title,
            description=description,
            event_date=event_date,
            category=category,
            audience=audience,
            is_published=True,
        )
        db.session.add(ev)
        db.session.commit()
        flash('Событие добавлено.', 'success')
    return redirect(url_for('admin.info_roadmap'))


@bp.route('/info/roadmap/<int:event_id>/edit', methods=['POST'])
def roadmap_update(event_id):
    """
    Редактирование события «Траектории развития».
    URL: /main_admin/info/roadmap/<id>/edit
    """
    if not is_admin():
        return jsonify({'error': 'forbidden'}), 403

    ev = db.session.get(RoadmapEvent, event_id)
    if not ev:
        flash('Событие не найдено.', 'error')
        return redirect(url_for('admin.info'))

    title = (request.form.get('title') or '').strip()
    description = (request.form.get('description') or '').strip() or None
    category = (request.form.get('category') or 'event').strip()
    audience = (request.form.get('audience') or 'all').strip()
    event_date, err = _parse_event_date(request.form.get('event_date'))

    valid_categories = {c[0] for c in RoadmapEvent.CATEGORIES}
    if category not in valid_categories:
        category = 'event'
    if audience not in ('all', 'admin', 'seller'):
        audience = 'all'

    if not title:
        flash('Заголовок события обязателен.', 'error')
    elif err:
        flash(err, 'error')
    else:
        ev.title = title
        ev.description = description
        ev.event_date = event_date
        ev.category = category
        ev.audience = audience
        db.session.commit()
        flash('Событие обновлено.', 'success')
    return redirect(url_for('admin.info_roadmap'))


@bp.route('/info/roadmap/<int:event_id>/delete', methods=['POST'])
def roadmap_delete(event_id):
    """
    Удаление события «Траектории развития».
    URL: /main_admin/info/roadmap/<id>/delete
    """
    if not is_admin():
        return jsonify({'error': 'forbidden'}), 403
    ev = db.session.get(RoadmapEvent, event_id)
    if not ev:
        flash('Событие не найдено.', 'error')
    else:
        db.session.delete(ev)
        db.session.commit()
        flash('Событие удалено.', 'success')
    return redirect(url_for('admin.info_roadmap'))


# Хелпер: общая выборка событий roadmap + категории — для страниц roadmap.
def _admin_roadmap_context():
    events = (
        RoadmapEvent.query
        .filter(RoadmapEvent.audience.in_(('all', 'admin')))
        .filter(RoadmapEvent.is_published == True)
        .order_by(RoadmapEvent.event_date.desc(), RoadmapEvent.id.desc())
        .all()
    )
    categories = [
        {'code': code, 'label': label, 'color': color}
        for code, label, color in RoadmapEvent.CATEGORIES
    ]
    return events, categories


# ========== Отдельные страницы раздела «Информация» ==========

# Совместимость со старой ссылкой: раньше раздел назывался «Дорожная карта» и
# жил на /main_admin/roadmap. Сейчас он внутри «Информация» → /info/roadmap.
# Оставляем мягкий редирект, чтобы старые закладки/внешние ссылки не падали в 404.
@bp.route('/roadmap')
def _legacy_roadmap_redirect():
    return redirect(url_for('admin.info_roadmap'), code=301)


@bp.route('/info/roadmap')
def info_roadmap():
    """
    Страница «Траектория развития проекта».
    URL: /main_admin/info/roadmap
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    events, categories = _admin_roadmap_context()
    return render_template(
        'admin/info_roadmap.html',
        title='Траектория развития проекта',
        roadmap_events=events,
        roadmap_categories=categories,
        can_manage=True,
    )


@bp.route('/info/tariffs')
def info_tariffs():
    """
    Страница «Тарифы» — таблица тарифов из блоков + строк.
    URL: /main_admin/info/tariffs
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    # Подгружаем блоки с их строками одним запросом (joinedload) — экономим N+1.
    from sqlalchemy.orm import joinedload
    blocks = (
        TariffBlock.query
        .options(joinedload(TariffBlock.rows))
        .order_by(TariffBlock.sort_order, TariffBlock.id)
        .all()
    )

    # Все категории — для select'а «предмет обложения» в форме тарифа.
    all_categories = Category.query.order_by(Category.name).all()

    return render_template(
        'admin/info_tariffs.html',
        title='Тарифы',
        tile=INFO_TILES['tariffs'],
        blocks=blocks,
        sections=TariffBlock.SECTIONS,
        tariff_kinds=TariffRow.KINDS,
        billing_periods=TariffRow.BILLINGS,
        all_categories=all_categories,
    )


# ========== CRUD: блоки и строки таблицы тарифов ==========

def _tariff_next_sort_order(block_id=None):
    """Следующий sort_order для блока/строки."""
    if block_id is None:
        max_so = db.session.query(db.func.max(TariffBlock.sort_order)).scalar()
    else:
        max_so = db.session.query(db.func.max(TariffRow.sort_order)) \
            .filter(TariffRow.block_id == block_id).scalar()
    return (max_so or 0) + 10


def _parse_tariff_numeric(form):
    """
    Достаёт из формы числовые поля price_amount / duration_days.
    Пустая строка или мусор → None. Числа проверяет на здравый смысл.

    Возвращает (price_amount, duration_days, error_message). Если есть
    ошибка — error_message непустой, числовые поля None.
    """
    price_raw = (form.get('price_amount') or '').strip().replace(',', '.')
    duration_raw = (form.get('duration_days') or '').strip()

    price_amount = None
    if price_raw:
        try:
            price_amount = float(price_raw)
            if price_amount < 0:
                return None, None, 'Цена для покупки должна быть ≥ 0.'
        except ValueError:
            return None, None, 'Цена для покупки должна быть числом.'

    duration_days = None
    if duration_raw:
        try:
            duration_days = int(duration_raw)
            if duration_days <= 0:
                return None, None, 'Срок действия должен быть положительным числом дней.'
        except ValueError:
            return None, None, 'Срок действия должен быть целым числом дней.'

    # Логика: либо оба заполнены, либо оба пустые. Иначе селлер не сможет
    # «купить» — пусть лучше админ увидит ошибку.
    if (price_amount is None) != (duration_days is None):
        return None, None, 'Для продажи тарифа селлерам укажите ОБА поля: цену и срок.'

    return price_amount, duration_days, None


@bp.route('/info/tariffs/block/create', methods=['POST'])
def tariff_block_create():
    """
    Создание нового блока тарифов.
    URL: /main_admin/info/tariffs/block/create
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    title = (request.form.get('title') or '').strip()
    section = (request.form.get('section') or 'sellers').strip()
    if section not in ('sellers', 'buyers', 'all'):
        section = 'sellers'
    if not title:
        flash('Название блока обязательно.', 'error')
        return redirect(url_for('admin.info_tariffs'))

    block = TariffBlock(
        title=title,
        section=section,
        sort_order=_tariff_next_sort_order(),
        is_published=True,
    )
    db.session.add(block)
    db.session.commit()
    flash('Блок тарифов создан.', 'success')
    return redirect(url_for('admin.info_tariffs') + f'#block-{block.id}')


@bp.route('/info/tariffs/block/<int:block_id>/edit', methods=['POST'])
def tariff_block_edit(block_id):
    """Редактирование заголовка/секции блока."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    block = db.session.get(TariffBlock, block_id)
    if not block:
        abort(404)

    title = (request.form.get('title') or '').strip()
    section = (request.form.get('section') or block.section).strip()
    if section not in ('sellers', 'buyers', 'all'):
        section = block.section
    if not title:
        flash('Название блока не может быть пустым.', 'error')
        return redirect(url_for('admin.info_tariffs'))

    block.title = title
    block.section = section
    db.session.commit()
    flash('Блок обновлён.', 'success')
    return redirect(url_for('admin.info_tariffs'))


@bp.route('/info/tariffs/block/<int:block_id>/delete', methods=['POST'])
def tariff_block_delete(block_id):
    """Удаление блока (и всех его строк через каскад)."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    block = db.session.get(TariffBlock, block_id)
    if not block:
        flash('Блок не найден.', 'error')
        return redirect(url_for('admin.info_tariffs'))

    db.session.delete(block)
    db.session.commit()
    flash('Блок удалён.', 'success')
    return redirect(url_for('admin.info_tariffs'))


@bp.route('/info/tariffs/row/create', methods=['POST'])
def tariff_row_create():
    """
    Создание строки тарифа в указанном блоке.
    URL: /main_admin/info/tariffs/row/create
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    block_id = request.form.get('block_id', type=int)
    block = db.session.get(TariffBlock, block_id) if block_id else None
    if not block:
        flash('Сначала выберите блок (или создайте новый).', 'error')
        return redirect(url_for('admin.info_tariffs'))

    payload, err = _parse_tariff_row_form(request.form)
    if err:
        flash(err, 'error')
        return redirect(url_for('admin.info_tariffs') + f'#block-{block.id}')

    row = TariffRow(
        block_id=block.id,
        sort_order=_tariff_next_sort_order(block.id),
        is_published=True,
        **payload,
    )
    db.session.add(row)
    db.session.commit()
    flash('Строка тарифа добавлена.', 'success')
    return redirect(url_for('admin.info_tariffs') + f'#block-{block.id}')


@bp.route('/info/tariffs/row/<int:row_id>/edit', methods=['POST'])
def tariff_row_edit(row_id):
    """Редактирование строки тарифа."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    row = db.session.get(TariffRow, row_id)
    if not row:
        abort(404)

    payload, err = _parse_tariff_row_form(request.form)
    if err:
        flash(err, 'error')
        return redirect(url_for('admin.info_tariffs') + f'#block-{row.block_id}')

    for k, v in payload.items():
        setattr(row, k, v)

    db.session.commit()
    flash('Строка обновлена.', 'success')
    return redirect(url_for('admin.info_tariffs') + f'#block-{row.block_id}')


@bp.route('/info/tariffs/row/<int:row_id>/toggle', methods=['POST'])
def tariff_row_toggle(row_id):
    """Переключить глобальный тумблер is_active у строки тарифа.

    При включении глобального правила (is_active=True):
      • для kind='cards'      — без эффекта на селлеров (фикс-тариф покупается
                                ими вручную, тумблер просто скрывает/показывает
                                в магазине);
      • для kind in (.../card_sale/category_sale) — правило начинает действовать
                                на селлеров без своей активной подписки.

    Если у селлера уже есть оплаченная подписка (is_active_now=True) —
    новое правило вступит в силу после её истечения (логика
    _resolve_active_tariff в seller.py).
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    row = db.session.get(TariffRow, row_id)
    if not row:
        abort(404)

    desired = request.form.get('is_active')
    row.is_active = bool(desired and desired in ('1', 'true', 'on', 'yes'))

    db.session.commit()

    state_label = 'включён' if row.is_active else 'выключен'
    if row.is_active and row.is_global_rule:
        flash(
            f'Правило «{row.name}» {state_label}. '
            f'Применяется ко всем селлерам без активной подписки.',
            'success',
        )
    else:
        flash(f'Тариф «{row.name}» {state_label}.', 'success')

    return redirect(url_for('admin.info_tariffs') + f'#block-{row.block_id}')


def _parse_tariff_row_form(form) -> tuple[dict, str | None]:
    """
    Общий парсер полей формы строки тарифа (используется и в create, и в edit).

    Возвращает (payload, error). payload — dict готовый к распаковке в
    `TariffRow(**payload)`. error — None если всё ок, иначе текст ошибки.
    """
    name = (form.get('name') or '').strip()
    description = (form.get('description') or '').strip() or None
    price = (form.get('price') or '').strip() or None
    period = (form.get('period') or '').strip() or None

    # Тип правила: 'cards' / 'cards_turnover' / 'card_sale' / 'category_sale'
    kind = (form.get('kind') or TariffRow.KIND_CARDS).strip()
    if kind not in dict(TariffRow.KINDS):
        kind = TariffRow.KIND_CARDS

    # Глобальный тумблер
    is_active = bool(
        form.get('is_active') and form.get('is_active') in ('1', 'true', 'on', 'yes')
    )

    # Ставка в процентах (только для глобальных правил)
    percent_raw = (form.get('percent_rate') or '').strip().replace(',', '.')
    percent_rate = None
    if percent_raw:
        try:
            percent_rate = float(percent_raw)
        except ValueError:
            return {}, 'Ставка должна быть числом.'

    # Категория предмета обложения (для kind='category_sale')
    subject_category_id = form.get('subject_category_id', type=int)

    # Периодичность: monthly / per_sale
    billing_period = (form.get('billing_period') or '').strip() or None
    if billing_period not in (TariffRow.BILLING_MONTHLY, TariffRow.BILLING_PER_SALE):
        billing_period = None

    # Числовые дублёры для kind='cards'
    price_amount, duration_days, err = _parse_tariff_numeric(form)
    if err:
        return {}, err

    payload = dict(
        name=name,
        description=description,
        price=price,
        period=period,
        kind=kind,
        is_active=is_active,
        percent_rate=percent_rate,
        subject_category_id=subject_category_id,
        billing_period=billing_period,
        price_amount=price_amount,
        duration_days=duration_days,
    )

    # Собственная валидация модели (всё-в-одном месте).
    tmp = TariffRow(**payload)
    err = tmp.validate()
    if err:
        return {}, err
    return payload, None


@bp.route('/info/tariffs/row/<int:row_id>/delete', methods=['POST'])
def tariff_row_delete(row_id):
    """Удаление строки тарифа.

    Удаляются также подписки (SellerTariffSubscription) и транзакции
    (TariffTransaction), ссылающиеся на эту строку.

    ВАЖНО про SQLite и SQLAlchemy: PRAGMA foreign_keys по умолчанию
    выключена, поэтому ON DELETE CASCADE на стороне БД не сработает.
    Каскад на уровне ORM (cascade='all, delete-orphan' в backref
    TariffRow.subscriptions / TariffRow.transactions) сработает только
    если дочерние объекты загружены в текущую сессию. Поэтому:

      1) Сначала явно подтягиваем subs и txs через .all() (lazy load),
         чтобы SQLAlchemy знал о них.
      2) Удаляем их через ORM (db.session.delete) — генерируются
         корректные DELETE-инструкции, никаких SET NULL.
      3) Удаляем сам row.
      4) Commit одним разом.
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    row = db.session.get(TariffRow, row_id)
    if not row:
        flash('Строка не найдена.', 'error')
        return redirect(url_for('admin.info_tariffs'))

    block_id = row.block_id

    # 1) Подтягиваем зависимые записи в сессию (lazy load), чтобы ORM
    #    мог сгенерировать корректные DELETE (а не UPDATE row_id=NULL).
    subs = list(row.subscriptions)
    txs = list(row.transactions)
    sub_count = len(subs)
    tx_count = len(txs)

    # 2) Снимаем подписки и транзакции — через ORM, не bulk delete.
    for sub in subs:
        db.session.delete(sub)
    for tx in txs:
        db.session.delete(tx)

    # 3) Удаляем саму строку.
    db.session.delete(row)

    # 4) Один commit на всё.
    db.session.commit()

    msg = 'Строка тарифа удалена.'
    if sub_count or tx_count:
        msg += f' Снято подписок: {sub_count}, транзакций: {tx_count}.'
    flash(msg, 'success')
    return redirect(url_for('admin.info_tariffs') + f'#block-{block_id}')


@bp.route('/info/education')
def info_education():
    """
    Страница «Обучение» — список плиток учебных материалов + фильтр по тегу.
    URL: /main_admin/info/education?tag=<slug>
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    tag_slug = (request.args.get('tag') or '').strip()
    active_tag = None
    if tag_slug:
        # Ищем оригинальный тег по slugу — обратно через tag_slug всех тегов.
        for t in EduMaterial.all_tags():
            if EduMaterial.tag_slug(t) == tag_slug:
                active_tag = t
                break

    q = (
        EduMaterial.query
        .filter(EduMaterial.audience.in_(('all', 'admin')))
        .filter(EduMaterial.is_published == True)
    )
    if active_tag:
        q = q.filter(EduMaterial.tag == active_tag)
    materials = q.order_by(EduMaterial.created_at.desc()).all()

    # (tag, slug) — slug считаем в view, чтобы в шаблоне не дёргать модель.
    all_tags_with_slug = [(t, EduMaterial.tag_slug(t)) for t in EduMaterial.all_tags()]

    return render_template(
        'admin/info_education.html',
        title='Обучение',
        tile=INFO_TILES['education'],
        materials=materials,
        all_tags=all_tags_with_slug,
        active_tag=active_tag,
        active_tag_slug=tag_slug,
    )


@bp.route('/info/roadmap/events.json')
def roadmap_events_json():
    """
    JSON-эндпоинт для FullCalendar: список событий в формате FullCalendar.
    Поддерживает ?start=YYYY-MM-DD&end=YYYY-MM-DD для диапазона.
    URL: /main_admin/info/roadmap/events.json
    """
    if not is_admin():
        return jsonify({'error': 'forbidden'}), 403

    q = (
        RoadmapEvent.query
        .filter(RoadmapEvent.audience.in_(('all', 'admin')))
        .filter(RoadmapEvent.is_published == True)
    )

    # FullCalendar передаёт ISO-дату начала/конца видимого диапазона
    start_raw = request.args.get('start')
    end_raw = request.args.get('end')
    from datetime import date as _date
    if start_raw:
        try:
            start = _date.fromisoformat(start_raw[:10])
            q = q.filter(RoadmapEvent.event_date >= start)
        except ValueError:
            pass
    if end_raw:
        try:
            end = _date.fromisoformat(end_raw[:10])
            q = q.filter(RoadmapEvent.event_date <= end)
        except ValueError:
            pass

    events = [ev.to_fullcalendar() for ev in q.all()]
    return jsonify(events)


@bp.route('/users')
def users():
    """
    Список пользователей (покупателей).
    URL: /main_admin/users
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    
    query = Buyer.query
    
    if search:
        query = query.filter(
            or_(Buyer.login.ilike(f'%{search}%'),
                Buyer.email.ilike(f'%{search}%'),
                Buyer.phone.ilike(f'%{search}%'))
        )
    
    pagination = query.order_by(Buyer.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('admin/users.html',
                         title='Покупатели',
                         buyers=pagination.items,
                         buyers_count=Buyer.query.count(),
                         pagination=pagination,
                         search=search)


@bp.route('/sellers')
def sellers():
    """
    Список продавцов.
    URL: /main_admin/sellers
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    
    query = Seller.query
    
    if search:
        query = query.filter(
            or_(Seller.login.ilike(f'%{search}%'),
                Seller.email.ilike(f'%{search}%'),
                Seller.store_name.ilike(f'%{search}%'))
        )
    
    pagination = query.order_by(Seller.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('admin/sellers.html',
                         title='Продавцы',
                         sellers=pagination.items,
                         sellers_count=Seller.query.count(),
                         pagination=pagination,
                         search=search)


@bp.route('/buyer/<int:buyer_id>')
def buyer_profile(buyer_id):
    """
    Профиль покупателя для админа.
    URL: /main_admin/buyer/{id}
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    buyer = db.session.get(Buyer, buyer_id)
    if not buyer:
        abort(404)
    
    # Получаем заказы покупателя
    from app.models.orders import Order
    orders = Order.query.filter_by(buyer_id=buyer_id).order_by(Order.created_at.desc()).limit(10).all()
    orders_count = Order.query.filter_by(buyer_id=buyer_id).count()
    
    return render_template('admin/buyer_profile.html',
                         title=f'Профиль {buyer.login}',
                         buyer=buyer,
                         orders=orders,
                         orders_count=orders_count)


@bp.route('/users/<int:user_id>/ban', methods=['POST'])
def user_ban(user_id):
    """
    Блокировка пользователя.
    URL: /main_admin/users/{id}/ban
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    user = db.session.get(Buyer, user_id)
    if not user:
        abort(404)
    
    user.is_active = not user.is_active
    db.session.commit()
    
    status = 'заблокирован' if not user.is_active else 'разблокирован'
    flash(f'Пользователь {status}.', 'success')
    return redirect(url_for('admin.users'))


@bp.route('/sellers/<int:seller_id>/ban', methods=['POST'])
def seller_ban(seller_id):
    """
    Блокировка продавца.
    URL: /main_admin/sellers/{id}/ban
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    seller = db.session.get(Seller, seller_id)
    if not seller:
        abort(404)
    
    seller.is_active = not seller.is_active
    db.session.commit()
    
    status = 'заблокирован' if not seller.is_active else 'разблокирован'
    flash(f'Продавец {status}.', 'success')
    return redirect(url_for('admin.sellers'))


# =============================================================================
# Тарифы (вкладки: Клиенты, Расчёты)
# =============================================================================
#
# Сайдбар «Тариф» ведёт на /main_admin/tariffs. Страница одна, переключение
# между вкладками — query-параметр ?tab=clients|settlements. На вкладке
# «Клиенты» — таблица селлеров, у которых есть оплаченные и действующие
# подписки на тарифы (`is_paid=True` и `expires_at > now()`). Селлеры без
# таких подписок в таблице не показываются. На вкладке «Расчёты» — журнал
# транзакций `tariff_transactions` (оплаты селлеров за тарифы).
# -----------------------------------------------------------------------------

@bp.route('/tariffs')
def tariffs():
    """
    Раздел «Тариф» в админке.
    URL: /main_admin/tariffs?tab=clients|settlements

    Вкладки:
        clients     — селлеры с активными подписками (Клиенты)
        settlements — журнал транзакций (Расчёты)

    Каталог тарифов (то, что селлер видит в «Магазине тарифов») живёт
    в /main_admin/info/tariffs — это информационная страница, где строки
    TariffRow с заполненной ценой/сроком становятся покупаемыми тарифами.
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    tab = request.args.get('tab', 'clients')
    if tab not in ('clients', 'settlements'):
        tab = 'clients'

    now = datetime.utcnow()

    if tab == 'settlements':
        # Журнал расчётов — все транзакции по тарифам.
        transactions = (
            TariffTransaction.query
            .order_by(TariffTransaction.paid_at.desc())
            .all()
        )
        return render_template(
            'admin/tariffs.html',
            title='Тариф',
            tab=tab,
            transactions=transactions,
            seller_rows=[],
        )

    # tab == 'clients'
    # Показываем ВСЕХ seller'ов с сегментацией по состоянию тарифа.
    # Сегменты:
    #   • «Оплатили» — state in ('paid', 'grace') — есть индивидуальная
    #     подписка (is_paid=True).
    #   • «На глобальном» — state == 'global' — селлер явно активировал
    #     глобальный процент через /tariffs.
    #   • «Без тарифа» — state in ('none', 'locked') — нет подписки,
    #     или подписка истекла (и грейс тоже).
    from app.blueprints.seller import _resolve_tariff_state as _resolve_state
    all_sellers = Seller.query.order_by(Seller.id).all()

    # Группируем seller'ов по сегменту. Для каждого считаем state,
    # тянем список его подписок (любых, не только активных — для
    # раскрывающихся строк).
    paid_sellers = []      # state in ('paid', 'grace')
    global_sellers = []    # state == 'global'
    no_tariff_sellers = [] # state in ('none', 'locked')

    all_subs_by_seller: dict = {}
    all_seller_ids = [s.id for s in all_sellers]
    if all_seller_ids:
        all_subs = (
            SellerTariffSubscription.query
            .join(TariffRow, TariffRow.id == SellerTariffSubscription.row_id)
            .filter(SellerTariffSubscription.seller_id.in_(all_seller_ids))
            .order_by(
                SellerTariffSubscription.seller_id,
                SellerTariffSubscription.expires_at.desc(),
            )
            .all()
        )
        for sub in all_subs:
            all_subs_by_seller.setdefault(sub.seller_id, []).append(sub)

    for seller in all_sellers:
        state = _resolve_state(seller)
        sub = state.get('subscription')
        info = {
            'seller': seller,
            'state': state['state'],
            'days_to_expire': state.get('days_to_expire', 0),
            'days_to_grace_end': state.get('days_to_grace_end', 0),
            'billed_amount': state.get('billed_amount', 0.0),
            'current_subscription': sub,
            'subscriptions': all_subs_by_seller.get(seller.id, []),
        }
        if state['state'] in ('paid', 'grace'):
            paid_sellers.append(info)
        elif state['state'] == 'global':
            global_sellers.append(info)
        else:
            # state in ('none', 'locked')
            no_tariff_sellers.append(info)

    return render_template(
        'admin/tariffs.html',
        title='Тариф',
        tab=tab,
        paid_sellers=paid_sellers,
        global_sellers=global_sellers,
        no_tariff_sellers=no_tariff_sellers,
        transactions=[],
    )


@bp.route('/tariffs/subscriptions/<int:subscription_id>/pause', methods=['POST'])
def tariff_subscription_pause(subscription_id):
    """Приостановить действие тарифа у селлера."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub:
        abort(404)
    sub.pause()
    db.session.commit()
    flash('Тариф приостановлен.', 'success')
    return redirect(url_for('admin.tariffs', tab='clients'))


@bp.route('/tariffs/subscriptions/<int:subscription_id>/resume', methods=['POST'])
def tariff_subscription_resume(subscription_id):
    """Возобновить ранее приостановленный тариф."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub:
        abort(404)
    sub.resume()
    db.session.commit()
    flash('Тариф возобновлён.', 'success')
    return redirect(url_for('admin.tariffs', tab='clients'))


@bp.route('/tariffs/subscriptions/<int:subscription_id>/disable', methods=['POST'])
def tariff_subscription_disable(subscription_id):
    """Отключить (аннулировать) тариф у селлера."""
    if not is_admin():
        return redirect(url_for('admin.login'))

    sub = db.session.get(SellerTariffSubscription, subscription_id)
    if not sub:
        abort(404)
    sub.disable()
    db.session.commit()
    flash('Тариф отключён.', 'success')
    return redirect(url_for('admin.tariffs', tab='clients'))


@bp.route('/products/moderation')
def products_moderation():
    """
    Модерация товаров.
    URL: /main_admin/products/moderation
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    status = request.args.get('status', 'pending')
    
    # Подсчет товаров по статусам
    pending_count = Product.query.filter_by(status='on_moderation').count()
    approved_count = Product.query.filter_by(status='approved').count()
    rejected_count = Product.query.filter_by(status='rejected').count()
    
    # Получение товаров по статусу
    if status == 'pending':
        products = Product.query.filter_by(status='on_moderation').order_by(Product.created_at.desc()).all()
    elif status == 'approved':
        products = Product.query.filter_by(status='approved').order_by(Product.moderated_at.desc()).all()
    elif status == 'rejected':
        products = Product.query.filter_by(status='rejected').order_by(Product.moderated_at.desc()).all()
    else:
        products = Product.query.filter_by(status='on_moderation').order_by(Product.created_at.desc()).all()
    
    return render_template('admin/products_moderation.html',
                         title='Модерация товаров',
                         products=products,
                         status=status,
                         pending_count=pending_count,
                         approved_count=approved_count,
                         rejected_count=rejected_count)


@bp.route('/products/<int:product_id>/approve', methods=['POST'])
@csrf.exempt
def product_approve(product_id):
    """
    Одобрение товара.
    URL: /main_admin/products/{id}/approve
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    product = db.session.get(Product, product_id)
    if not product:
        abort(404)
    
    product.approve()
    
    flash('Товар одобрен.', 'success')
    return redirect(url_for('admin.products_moderation'))


@bp.route('/products/<int:product_id>/reject', methods=['POST'])
@csrf.exempt
def product_reject(product_id):
    """
    Отклонение товара.
    URL: /main_admin/products/{id}/reject
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    product = db.session.get(Product, product_id)
    if not product:
        abort(404)
    
    remark = request.form.get('remark', 'Товар отклонён.')
    product.reject(remark)
    
    flash('Товар отклонён.', 'success')
    return redirect(url_for('admin.products_moderation'))


@bp.route('/products/<int:product_id>/moderate')
def product_moderate(product_id):
    """
    Страница модерации отдельного товара.
    URL: /main_admin/products/{id}/moderate
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    product = db.session.get(Product, product_id)
    if not product:
        abort(404)
    
    return render_template('admin/product_moderate.html',
                         title='Модерация товара',
                         product=product)


@bp.route('/categories')
def categories():
    """
    Управление категориями.
    URL: /main_admin/categories
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    # Дерево категорий
    categories = Category.query.filter_by(parent_id=None).all()
    # Плоский список — для select-а «перенести в категорию» при удалении
    all_categories = Category.query.order_by(Category.name).all()

    return render_template('admin/categories.html',
                         title='Категории',
                         categories=categories,
                         all_categories=all_categories)


@bp.route('/categories/new')
def category_new():
    """
    Страница создания категории.
    URL: /main_admin/categories/new
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    parent_id = request.args.get('parent_id', type=int)
    
    # Получаем все категории для выбора родителя
    all_categories = Category.query.order_by(Category.name).all()
    
    # Если указан parent_id, получаем родительскую категорию
    parent_category = None
    if parent_id:
        parent_category = db.session.get(Category, parent_id)
    
    # Строим путь к родительской категории для breadcrumb
    breadcrumb = []
    current = parent_category
    while current:
        breadcrumb.insert(0, current)
        current = current.parent if current.parent else None
    
    # Получаем все доступные параметры
    all_parameters = Parameter.query.order_by(Parameter.name).all()
    
    # Получаем унаследованные параметры от родителей
    inherited_params = []
    if parent_category:
        inherited_params = parent_category.get_all_parameters()
    
    return render_template('admin/category_new.html',
                         title='Новая категория',
                         all_categories=all_categories,
                         all_parameters=all_parameters,
                         inherited_params=inherited_params,
                         parent_category=parent_category,
                         parent_id=parent_id,
                         breadcrumb=breadcrumb)


@bp.route('/categories/new', methods=['POST'])
def category_create():
    """
    Создание категории.
    URL: /main_admin/categories/new
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    name = request.form.get('name')
    parent_id = request.form.get('parent_id', type=int)
    description = request.form.get('description')
    # Получаем выбранные параметры (новые, не унаследованные)
    selected_params_raw = request.form.getlist('parameters')
    
    # Обрабатываем: может прийти как список или как строка с запятыми
    selected_params = []
    for val in selected_params_raw:
        if ',' in val:
            selected_params.extend([p.strip() for p in val.split(',') if p.strip()])
        elif val.strip():
            selected_params.append(val.strip())
    
    if not name:
        flash('Название обязательно.', 'error')
        return redirect(url_for('admin.categories'))
    
    # Генерация slug
    from app.utils.helpers import slugify
    slug = slugify(name)
    
    # Уникализация
    base_slug = slug
    counter = 1
    while Category.query.filter_by(slug=slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1
    
    category = Category(
        name=name,
        slug=slug,
        description=description,
        parent_id=parent_id
    )
    
    db.session.add(category)
    db.session.commit()
    
    # Добавляем выбранные параметры
    for param_id in selected_params:
        if param_id.strip():  # Проверяем, что значение не пустое
            param = db.session.get(Parameter, int(param_id))
            if param:
                cat_param = CategoryParameter(
                    category_id=category.id,
                    parameter_id=param.id,
                    is_inherited=False
                )
                db.session.add(cat_param)
    
    db.session.commit()
    
    flash('Категория создана.', 'success')
    return redirect(url_for('admin.categories'))


@bp.route('/categories/<int:category_id>/edit')
def category_edit(category_id):
    """
    Страница редактирования категории.
    URL: /main_admin/categories/{id}/edit
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    category = db.session.get(Category, category_id)
    if not category:
        abort(404)
    
    categories = Category.query.order_by(Category.name).all()
    
    # Получаем все доступные параметры
    all_parameters = Parameter.query.order_by(Parameter.name).all()
    
    # Получаем унаследованные параметры от родителей
    inherited_params = []
    if category.parent:
        inherited_params = category.parent.get_all_parameters()
    
    # Получаем ТОЛЬКО собственные параметры категории (не унаследованные)
    own_params = [cp.parameter for cp in category.parameters.filter_by(is_inherited=False).all()]
    
    return render_template('admin/category_edit.html',
                         title='Редактирование категории',
                         category=category,
                         categories=categories,
                         all_parameters=all_parameters,
                         inherited_params=inherited_params,
                         own_params=own_params)


@bp.route('/categories/<int:category_id>/edit', methods=['POST'])
def category_update(category_id):
    """
    Обновление категории.
    URL: /main_admin/categories/{id}/edit
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    category = db.session.get(Category, category_id)
    if not category:
        abort(404)
    
    category.name = request.form.get('name')
    category.description = request.form.get('description')
    
    # Получаем новые выбранные параметры
    selected_params_raw = request.form.getlist('parameters')
    
    # Обрабатываем: может прийти как список или как строка с запятыми
    selected_params = []
    for val in selected_params_raw:
        if ',' in val:
            # строка с запятыми (например "2,1")
            selected_params.extend([p.strip() for p in val.split(',') if p.strip()])
        elif val.strip():
            selected_params.append(val.strip())
    
    # Фильтруем пустые значения и преобразуем в int
    new_params = set(int(p) for p in selected_params if p) if selected_params else set()
    
    # Получаем текущие собственные параметры категории (только не унаследованные)
    current_own_params = set(cp.parameter_id for cp in category.parameters.filter_by(is_inherited=False).all())
    
    # Удаляем параметры, которые были убраны
    params_to_remove = current_own_params - new_params
    for param_id in params_to_remove:
        CategoryParameter.query.filter_by(
            category_id=category.id,
            parameter_id=param_id,
            is_inherited=False
        ).delete()
    
    # Добавляем новые параметры
    params_to_add = new_params - current_own_params
    for param_id in params_to_add:
        param = db.session.get(Parameter, param_id)
        if param:
            # Проверяем, не является ли параметр унаследованным
            if param_id not in [cp.parameter_id for cp in category.parameters.filter_by(is_inherited=False).all()]:
                cat_param = CategoryParameter(
                    category_id=category.id,
                    parameter_id=param_id,
                    is_inherited=False
                )
                db.session.add(cat_param)
    
    db.session.commit()
    
    flash('Категория обновлена.', 'success')
    return redirect(url_for('admin.categories'))


@bp.route('/categories/<int:category_id>/delete', methods=['POST'])
def category_delete(category_id):
    """
    Удаление категории.
    URL: /main_admin/categories/{id}/delete

    Нельзя удалить категорию, в которой есть товары или витрины (ProductCard).
    Сначала нужно перенести их в другую категорию (параметр ?move_to=<id>
    в POST) — тогда перенос и удаление пройдут атомарно.
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    category = db.session.get(Category, category_id)
    if not category:
        abort(404)

    # Считаем, что мешает удалению
    products_count = Product.query.filter_by(category_id=category.id).count()
    cards_count = ProductCard.query.filter_by(category_id=category.id).count()

    # Если в форме передали move_to — переносим и удаляем
    move_to = request.form.get('move_to', type=int)
    if (products_count or cards_count) and move_to:
        target = db.session.get(Category, move_to)
        if not target or target.id == category.id:
            flash('Выберите другую категорию для переноса.', 'error')
            return redirect(url_for('admin.categories'))

        Product.query.filter_by(category_id=category.id).update(
            {Product.category_id: target.id}, synchronize_session=False
        )
        ProductCard.query.filter_by(category_id=category.id).update(
            {ProductCard.category_id: target.id}, synchronize_session=False
        )
        db.session.flush()
        products_count = 0
        cards_count = 0

    if products_count or cards_count:
        flash(
            f'Невозможно удалить категорию «{category.name}»: '
            f'в ней {products_count} товаров и {cards_count} витрин. '
            f'Сначала перенесите их в другую категорию.',
            'error',
        )
        return redirect(url_for('admin.categories'))

    db.session.delete(category)
    db.session.commit()

    flash('Категория удалена.', 'success')
    return redirect(url_for('admin.categories'))


@bp.route('/parameters')
def parameters():
    """
    Управление параметрами.
    URL: /main_admin/parameters
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    from collections import defaultdict
    
    parameters = Parameter.query.order_by(Parameter.sort_order).all()
    
    # Группировка по категориям
    parameters_by_category = defaultdict(list)
    for param in parameters:
        if param.categories and param.categories.count() > 0:
            for cat_param in param.categories:
                category = cat_param.category
                if category:
                    cat_name = category.name
                else:
                    cat_name = 'Без категории'
                parameters_by_category[cat_name].append(param)
        else:
            parameters_by_category['Без категории'].append(param)
    
    return render_template('admin/parameters.html',
                         title='Параметры товаров',
                         parameters=parameters,
                         parameters_by_category=dict(parameters_by_category))


@bp.route('/parameters/new')
def parameter_new():
    """
    Страница создания параметра.
    URL: /main_admin/parameters/new
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    # Получаем категории для связывания
    from app.models.products import Category
    categories = Category.query.order_by(Category.name).all()
    
    return render_template('admin/parameter_new.html',
                         title='Новый параметр',
                         categories=categories)


@bp.route('/parameters/new', methods=['POST'])
def parameter_create():
    """
    Создание параметра.
    URL: /main_admin/parameters/new
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    name = request.form.get('name')
    code = request.form.get('code')
    param_type = request.form.get('type')
    is_composite = 'is_composite' in request.form
    is_multiple = 'is_multiple' in request.form
    is_input = 'is_input' in request.form
    predefined_values = request.form.get('predefined_values')
    
    if not all([name, code, param_type]):
        flash('Заполните обязательные поля.', 'error')
        return redirect(url_for('admin.parameters'))
    
    # Проверка на уникальность кода
    existing = Parameter.query.filter_by(code=code).first()
    if existing:
        flash(f'Параметр с кодом "{code}" уже существует.', 'error')
        return redirect(url_for('admin.parameters'))
    
    param = Parameter(
        name=name,
        code=code,
        type=param_type,
        is_composite=is_composite,
        is_multiple=is_multiple,
        is_input=is_input,
        predefined_values=predefined_values.split(',') if predefined_values else None
    )
    
    db.session.add(param)
    db.session.commit()
    
    flash('Параметр создан.', 'success')
    return redirect(url_for('admin.parameters'))


@bp.route('/parameters/<int:param_id>/delete', methods=['POST'])
def parameter_delete(param_id):
    """
    Удаление параметра.
    URL: /main_admin/parameters/{id}/delete
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    param = db.session.get(Parameter, param_id)
    if not param:
        abort(404)
    
    # Сначала удаляем все связи с категориями
    CategoryParameter.query.filter_by(parameter_id=param_id).delete()
    
    # Также удаляем связи с товарами
    from app.models.products import ProductParameter
    ProductParameter.query.filter_by(parameter_id=param_id).delete()
    
    db.session.delete(param)
    db.session.commit()
    
    flash('Параметр удалён.', 'success')
    return redirect(url_for('admin.parameters'))


@bp.route('/parameters/<int:param_id>/edit')
def parameter_edit(param_id):
    """
    Страница редактирования параметра.
    URL: /main_admin/parameters/{id}/edit
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    param = db.session.get(Parameter, param_id)
    if not param:
        abort(404)
    
    return render_template('admin/parameter_edit.html',
                         title='Редактирование параметра',
                         parameter=param)


@bp.route('/parameters/<int:param_id>/edit', methods=['POST'])
def parameter_update(param_id):
    """
    Обновление параметра.
    URL: /main_admin/parameters/{id}/edit
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    param = db.session.get(Parameter, param_id)
    if not param:
        abort(404)
    
    param.name = request.form.get('name')
    param.type = request.form.get('type')
    param.is_composite = 'is_composite' in request.form
    param.is_multiple = 'is_multiple' in request.form
    param.is_input = 'is_input' in request.form
    
    predefined_values = request.form.get('predefined_values')
    if predefined_values:
        param.predefined_values = [v.strip() for v in predefined_values.split(',') if v.strip()]
    else:
        param.predefined_values = None
    
    db.session.commit()
    
    flash('Параметр обновлён.', 'success')
    return redirect(url_for('admin.parameters'))


@bp.route('/orders')
def orders():
    """
    Список заказов.
    URL: /main_admin/orders
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    status = request.args.get('status')
    page = request.args.get('page', 1, type=int)
    
    query = Order.query
    
    if status:
        query = query.filter_by(status=status)
    
    pagination = query.order_by(Order.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False
    )
    
    return render_template('admin/orders.html',
                         title='Заказы',
                         orders=pagination.items,
                         pagination=pagination,
                         current_status=status)


@bp.route('/orders/<int:order_id>')
def order_detail(order_id):
    """
    Детали заказа.
    URL: /main_admin/orders/{id}
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    order = db.session.get(Order, order_id)
    if not order:
        abort(404)
    
    return render_template('admin/order_detail.html',
                         title=f'Заказ {order.order_number}',
                         order=order)


@bp.route('/orders/<int:order_id>/status', methods=['POST'])
def update_order_status(order_id):
    """
    Обновление статуса заказа.
    URL: /main_admin/orders/{id}/status
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    order = db.session.get(Order, order_id)
    if not order:
        abort(404)
    
    new_status = request.form.get('status')
    if new_status:
        order.status = new_status
        db.session.commit()
        flash('Статус заказа обновлён.', 'success')
    
    return redirect(url_for('admin.order_detail', order_id=order_id))


@bp.route('/orders/<int:order_id>/delete', methods=['POST'])
@csrf.exempt
def order_delete(order_id):
    """
    Полное удаление заказа админом.
    URL: /main_admin/orders/{id}/delete

    Сносит запись Order вместе с OrderItem (cascade), а также
    связанные Bonus-транзакции и Return-заявки, чтобы не упереться
    в FK-ограничения. Возвращает товары на склад.
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    order = db.session.get(Order, order_id)
    if not order:
        abort(404)

    from app.models.orders import Bonus, Return

    order_number = order.order_number

    # Возвращаем товары на склад (на случай, если заказ был оплачен/собран)
    for item in order.items.all():
        if item.product:
            item.product.stock_quantity = (item.product.stock_quantity or 0) + item.quantity

    # Чистим связи, у которых нет cascade от Order
    Bonus.query.filter(Bonus.order_id == order_id).delete(synchronize_session=False)
    Return.query.filter(Return.order_id == order_id).delete(synchronize_session=False)

    db.session.delete(order)
    db.session.commit()

    flash(f'Заказ {order_number} удалён.', 'success')
    return redirect(url_for('admin.orders'))


@bp.route('/promotions')
def promotions():
    """
    Управление акциями.
    URL: /main_admin/promotions

    Поддерживает фильтрацию по вкладкам: ?status=active|scheduled|paused|ended
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    status_filter = request.args.get('status')

    # Сопоставление пользовательских вкладок с реальными статусами в БД
    # В БД: draft / forming / active / completed
    status_map = {
        'active':   ['active'],
        'scheduled':['draft'],
        'paused':   ['forming'],
        'ended':    ['completed'],
    }

    query = Promotion.query
    if status_filter and status_filter in status_map:
        query = query.filter(Promotion.status.in_(status_map[status_filter]))

    promotions = query.order_by(Promotion.created_at.desc()).all()

    return render_template('admin/promotions.html',
                         title='Акции',
                         promotions=promotions,
                         status=status_filter)


@bp.route('/promotions/new')
def promotion_new():
    """
    Страница создания акции.
    URL: /main_admin/promotions/new
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    return render_template('admin/promotion_new.html',
                         title='Новая акция')


@bp.route('/promotions/new', methods=['POST'])
def promotion_create():
    """
    Создание акции.
    URL: /main_admin/promotions/new

    Поля start_date и end_date необязательные: если не указаны, акция
    считается бессрочной (неограниченный срок действия).
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    name = request.form.get('name')
    scheme = request.form.get('scheme')
    discount_percent_raw = request.form.get('discount_percent', '').strip()
    min_product_price_raw = request.form.get('min_product_price', '').strip()
    is_template = request.form.get('is_template') == '1'
    apply_same_discount = request.form.get('apply_same_discount') == '1'
    start_date_raw = request.form.get('start_date', '').strip()
    end_date_raw = request.form.get('end_date', '').strip()

    valid_schemes = {'second_with_discount', 'one_plus_one', 'two_plus_one', 'three_plus_one', 'discount', 'gift'}
    if not name or not scheme or scheme not in valid_schemes:
        flash('Заполните обязательные поля и выберите корректную схему.', 'error')
        return redirect(url_for('admin.promotions'))

    # apply_same_discount имеет смысл только для scheme='discount'.
    # Если схема другая — по умолчанию True (не влияет на поведение).
    if scheme != 'discount':
        apply_same_discount = True

    # Для «Процентной скидки» шаблон обязателен — продавцы должны иметь
    # возможность подключить акцию и выбрать свои товары.
    if scheme == 'discount':
        is_template = True

    # 1+1 / 2+1 / 3+1: шаблон обязателен (продавцы подключают и выбирают товары)
    if scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one'):
        is_template = True

    # 1+1 / 2+1 / 3+1: если админ не задал процент — автоматически 99%
    discount_percent = None
    if discount_percent_raw:
        try:
            discount_percent = int(discount_percent_raw)
            if discount_percent < 0 or discount_percent > 100:
                raise ValueError
        except ValueError:
            flash('Скидка должна быть целым числом от 0 до 100.', 'error')
            return redirect(url_for('admin.promotions'))
    if scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one') and (discount_percent is None or discount_percent == 0):
        discount_percent = 99

    min_product_price = None
    if min_product_price_raw:
        try:
            min_product_price = float(min_product_price_raw.replace(',', '.'))
            if min_product_price < 0:
                raise ValueError
        except ValueError:
            flash('Минимальная цена товара должна быть неотрицательным числом.', 'error')
            return redirect(url_for('admin.promotions'))

    from datetime import datetime as _dt
    start_date = None
    end_date = None
    try:
        if start_date_raw:
            start_date = _dt.strptime(start_date_raw, '%Y-%m-%d')
        if end_date_raw:
            end_date = _dt.strptime(end_date_raw, '%Y-%m-%d')
    except ValueError:
        flash('Некорректный формат даты. Используйте ГГГГ-ММ-ДД.', 'error')
        return redirect(url_for('admin.promotions'))

    if start_date and end_date and end_date < start_date:
        flash('Дата окончания не может быть раньше даты начала.', 'error')
        return redirect(url_for('admin.promotions'))

    promotion = Promotion(
        name=name,
        scheme=scheme,
        discount_percent=discount_percent,
        min_product_price=min_product_price,
        is_template=is_template,
        apply_same_discount=apply_same_discount,
        start_date=start_date,
        end_date=end_date,
        status='active',
    )

    db.session.add(promotion)
    db.session.commit()

    flash('Акция создана.', 'success')
    return redirect(url_for('admin.promotions'))


@bp.route('/promotions/<int:promotion_id>/activate', methods=['POST'])
def promotion_activate(promotion_id):
    """
    Активация акции.
    URL: /main_admin/promotions/{id}/activate
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    promotion = db.session.get(Promotion, promotion_id)
    if not promotion:
        abort(404)

    promotion.activate()
    flash('Активация акции.', 'success')
    return redirect(url_for('admin.promotions'))


@bp.route('/promotions/<int:promotion_id>/pause', methods=['POST'])
def promotion_pause(promotion_id):
    """
    Приостановка акции.
    URL: /main_admin/promotions/{id}/pause
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    promotion = db.session.get(Promotion, promotion_id)
    if not promotion:
        abort(404)

    promotion.pause()
    flash('Акция приостановлена.', 'success')
    return redirect(url_for('admin.promotions'))


@bp.route('/promotions/<int:promotion_id>/edit')
def promotion_edit(promotion_id):
    """
    Страница редактирования акции.
    URL: /main_admin/promotions/{id}/edit
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    promotion = db.session.get(Promotion, promotion_id)
    if not promotion:
        abort(404)

    return render_template('admin/promotion_edit.html',
                         title=f'Редактирование: {promotion.name}',
                         promotion=promotion)


@bp.route('/promotions/<int:promotion_id>/edit', methods=['POST'])
def promotion_update(promotion_id):
    """
    Сохранение изменений акции.
    URL: /main_admin/promotions/{id}/edit
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    promotion = db.session.get(Promotion, promotion_id)
    if not promotion:
        abort(404)

    name = request.form.get('name')
    scheme = request.form.get('scheme')
    discount_percent_raw = request.form.get('discount_percent', '').strip()
    min_product_price_raw = request.form.get('min_product_price', '').strip()
    is_template = request.form.get('is_template') == '1'
    apply_same_discount = request.form.get('apply_same_discount') == '1'
    start_date_raw = request.form.get('start_date', '').strip()
    end_date_raw = request.form.get('end_date', '').strip()
    status = request.form.get('status')

    valid_schemes = {'second_with_discount', 'one_plus_one', 'two_plus_one', 'three_plus_one', 'discount', 'gift'}
    valid_statuses = {'draft', 'forming', 'active', 'completed'}

    if not name or not scheme or scheme not in valid_schemes:
        flash('Заполните обязательные поля и выберите корректную схему.', 'error')
        return redirect(url_for('admin.promotion_edit', promotion_id=promotion_id))

    if status and status not in valid_statuses:
        flash('Некорректный статус акции.', 'error')
        return redirect(url_for('admin.promotion_edit', promotion_id=promotion_id))

    if scheme != 'discount':
        apply_same_discount = True

    if scheme == 'discount':
        is_template = True

    if scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one'):
        is_template = True

    discount_percent = None
    if discount_percent_raw:
        try:
            discount_percent = int(discount_percent_raw)
            if discount_percent < 0 or discount_percent > 100:
                raise ValueError
        except ValueError:
            flash('Скидка должна быть целым числом от 0 до 100.', 'error')
            return redirect(url_for('admin.promotion_edit', promotion_id=promotion_id))
    if scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one') and (discount_percent is None or discount_percent == 0):
        discount_percent = 99

    min_product_price = None
    if min_product_price_raw:
        try:
            min_product_price = float(min_product_price_raw.replace(',', '.'))
            if min_product_price < 0:
                raise ValueError
        except ValueError:
            flash('Минимальная цена товара должна быть неотрицательным числом.', 'error')
            return redirect(url_for('admin.promotion_edit', promotion_id=promotion_id))

    from datetime import datetime as _dt
    start_date = None
    end_date = None
    try:
        if start_date_raw:
            start_date = _dt.strptime(start_date_raw, '%Y-%m-%d')
        if end_date_raw:
            end_date = _dt.strptime(end_date_raw, '%Y-%m-%d')
    except ValueError:
        flash('Некорректный формат даты. Используйте ГГГГ-ММ-ДД.', 'error')
        return redirect(url_for('admin.promotion_edit', promotion_id=promotion_id))

    if start_date and end_date and end_date < start_date:
        flash('Дата окончания не может быть раньше даты начала.', 'error')
        return redirect(url_for('admin.promotion_edit', promotion_id=promotion_id))

    promotion.name = name
    promotion.scheme = scheme
    promotion.discount_percent = discount_percent
    promotion.min_product_price = min_product_price
    promotion.is_template = is_template
    promotion.apply_same_discount = apply_same_discount
    promotion.start_date = start_date
    promotion.end_date = end_date
    if status:
        promotion.status = status

    db.session.commit()

    flash(f'Акция «{promotion.name}» обновлена.', 'success')
    return redirect(url_for('admin.promotions'))


@bp.route('/promotions/<int:promotion_id>/delete', methods=['POST'])
def promotion_delete(promotion_id):
    """
    Удаление акции админом.
    URL: /main_admin/promotions/{id}/delete

    Удаляет саму Promotion вместе с её подключениями у продавцов
    (SellerPromotion, cascade) и связями с товарами (PromotionProduct,
    cascade). После удаления шаблонная акция перестаёт действовать у всех
    продавцов, которые её использовали.
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    promotion = db.session.get(Promotion, promotion_id)
    if not promotion:
        abort(404)

    name = promotion.name

    # Считаем, сколько продавцов её использовали — для информирования
    from app.models.orders import SellerPromotion
    sellers_count = SellerPromotion.query.filter_by(promotion_id=promotion_id).count()

    db.session.delete(promotion)
    db.session.commit()

    if sellers_count:
        flash(f'Акция «{name}» удалена. Снята у {sellers_count} продавц(ов).', 'success')
    else:
        flash(f'Акция «{name}» удалена.', 'success')
    return redirect(url_for('admin.promotions'))


@bp.route('/loyalty')
def loyalty():
    """
    Управление программой лояльности.
    URL: /main_admin/loyalty
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    from app.models.loyalty import LoyaltyRate
    from app.models.communications import Settings

    rates = LoyaltyRate.query.order_by(
        LoyaltyRate.sort_order.asc(), LoyaltyRate.id.asc()
    ).all()
    enabled = bool(Settings.get('loyalty_enabled', False))
    promo_enabled = bool(Settings.get('promo_enabled', False))

    return render_template(
        'admin/loyalty.html',
        title='Лояльность',
        rates=rates,
        loyalty_enabled=enabled,
        promo_enabled=promo_enabled,
    )


@bp.route('/loyalty/toggle', methods=['POST'])
@csrf.exempt
def loyalty_toggle():
    """
    Глобальный тумблер «программа лояльности для продавцов».
    URL: /main_admin/loyalty/toggle
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401

    from app.models.communications import Settings
    enabled = request.form.get('enabled') in ('1', 'true', 'on')
    Settings.set('loyalty_enabled', enabled, 'bool')
    return jsonify({'success': True, 'enabled': enabled})


@bp.route('/loyalty/promo-toggle', methods=['POST'])
@csrf.exempt
def loyalty_promo_toggle():
    """
    Глобальный тумблер «промокоды для продавцов».
    URL: /main_admin/loyalty/promo-toggle
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401

    from app.models.communications import Settings
    enabled = request.form.get('enabled') in ('1', 'true', 'on')
    Settings.set('promo_enabled', enabled, 'bool')
    return jsonify({'success': True, 'enabled': enabled})


@bp.route('/loyalty/rates/new', methods=['POST'])
@csrf.exempt
def loyalty_rate_create():
    """
    Создание нового курса начисления.
    URL: /main_admin/loyalty/rates/new
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401

    from app.models.loyalty import LoyaltyRate
    from decimal import Decimal, InvalidOperation

    title = (request.form.get('title') or '').strip()
    raw = (request.form.get('points_per_ruble') or '').strip().replace(',', '.')
    description = (request.form.get('description') or '').strip()
    is_active = request.form.get('is_active') in ('1', 'true', 'on')

    if not title:
        return jsonify({'error': 'Укажите название курса'}), 400
    try:
        ppr = float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return jsonify({'error': 'Укажите корректный курс (баллов за 1 ₽)'}), 400
    if ppr <= 0:
        return jsonify({'error': 'Курс должен быть больше нуля'}), 400

    # sort_order = max + 1
    max_order = db.session.query(db.func.max(LoyaltyRate.sort_order)).scalar() or 0
    rate = LoyaltyRate(
        title=title,
        points_per_ruble=ppr,
        description=description or None,
        is_active=is_active,
        sort_order=int(max_order) + 1,
    )
    db.session.add(rate)
    db.session.commit()
    return jsonify({'success': True, 'id': rate.id})


@bp.route('/loyalty/rates/<int:rate_id>/update', methods=['POST'])
@csrf.exempt
def loyalty_rate_update(rate_id):
    """
    Обновление курса.
    URL: /main_admin/loyalty/rates/{id}/update
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401

    from app.models.loyalty import LoyaltyRate
    from decimal import Decimal, InvalidOperation

    rate = db.session.get(LoyaltyRate, rate_id)
    if not rate:
        return jsonify({'error': 'Курс не найден'}), 404

    title = (request.form.get('title') or '').strip()
    raw = (request.form.get('points_per_ruble') or '').strip().replace(',', '.')
    description = (request.form.get('description') or '').strip()
    is_active = request.form.get('is_active') in ('1', 'true', 'on')

    if not title:
        return jsonify({'error': 'Укажите название курса'}), 400
    try:
        ppr = float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return jsonify({'error': 'Укажите корректный курс (баллов за 1 ₽)'}), 400
    if ppr <= 0:
        return jsonify({'error': 'Курс должен быть больше нуля'}), 400

    rate.title = title
    rate.points_per_ruble = ppr
    rate.description = description or None
    rate.is_active = is_active
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/loyalty/rates/<int:rate_id>/toggle', methods=['POST'])
@csrf.exempt
def loyalty_rate_toggle(rate_id):
    """
    Показать/скрыть курс от селлеров.
    URL: /main_admin/loyalty/rates/{id}/toggle
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401

    from app.models.loyalty import LoyaltyRate
    rate = db.session.get(LoyaltyRate, rate_id)
    if not rate:
        return jsonify({'error': 'Курс не найден'}), 404
    rate.is_active = not rate.is_active
    db.session.commit()
    return jsonify({'success': True, 'is_active': rate.is_active})


@bp.route('/loyalty/rates/<int:rate_id>/delete', methods=['POST'])
@csrf.exempt
def loyalty_rate_delete(rate_id):
    """
    Удаление курса. Если есть подключённые селлеры — ошибка.
    URL: /main_admin/loyalty/rates/{id}/delete
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401

    from app.models.loyalty import LoyaltyRate, SellerLoyalty
    rate = db.session.get(LoyaltyRate, rate_id)
    if not rate:
        return jsonify({'error': 'Курс не найден'}), 404

    linked = SellerLoyalty.query.filter_by(rate_id=rate_id).count()
    if linked:
        return jsonify({
            'error': (
                f'Курс используется {linked} продавц(ами). '
                'Сначала смените им курс или отключите программу.'
            )
        }), 400

    db.session.delete(rate)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/deliveries')
def deliveries():
    """
    Управление службами доставки.
    URL: /main_admin/deliveries
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    services = DeliveryService.query.order_by(DeliveryService.name).all()
    
    return render_template('admin/deliveries.html',
                         title='Службы доставки',
                         deliveries=services)


@bp.route('/deliveries/new')
def delivery_new():
    """
    Страница добавления службы доставки.
    URL: /main_admin/deliveries/new
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    return render_template('admin/delivery_new.html',
                         title='Новая служба доставки')


@bp.route('/deliveries/new', methods=['POST'])
def delivery_create():
    """
    Добавление службы доставки.
    URL: /main_admin/deliveries/new
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    name = request.form.get('name')
    code = request.form.get('code')
    api_module = request.form.get('api_module')
    
    if not all([name, code]):
        flash('Заполните обязательные поля.', 'error')
        return redirect(url_for('admin.deliveries'))
    
    service = DeliveryService(
        name=name,
        code=code,
        api_module=api_module
    )
    
    db.session.add(service)
    db.session.commit()
    
    # Обработка загрузки логотипа
    if 'logo' in request.files:
        file = request.files['logo']
        if file and file.filename:
            from werkzeug.utils import secure_filename
            import os
            from flask import current_app
            
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'delivery_logos')
            os.makedirs(upload_dir, exist_ok=True)
            
            ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
            filename = f"delivery_{service.id}_logo.{ext}"
            filepath = os.path.join(upload_dir, filename)
            
            try:
                from PIL import Image
                img = Image.open(file)
                img.thumbnail((200, 100), Image.Resampling.LANCZOS)
                img.save(filepath, optimize=True)
                service.logo_path = f"uploads/delivery_logos/{filename}"
                db.session.commit()
            except Exception as e:
                print(f"Logo upload error: {e}")
    
    flash('Служба доставки добавлена.', 'success')
    return redirect(url_for('admin.deliveries'))


@bp.route('/deliveries/<int:service_id>/edit', methods=['GET', 'POST'])
def delivery_edit(service_id):
    """
    Редактирование службы доставки.
    URL: /main_admin/deliveries/{id}/edit
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    service = db.session.get(DeliveryService, service_id)
    if not service:
        abort(404)
    
    if request.method == 'POST':
        service.name = request.form.get('name')
        service.code = request.form.get('code')
        service.api_module = request.form.get('api_module')
        service.is_active = 'is_active' in request.form
        
        # Обработка загрузки логотипа
        if 'logo' in request.files:
            file = request.files['logo']
            if file and file.filename:
                from werkzeug.utils import secure_filename
                import os
                from flask import current_app
                
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'delivery_logos')
                os.makedirs(upload_dir, exist_ok=True)
                
                ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
                filename = f"delivery_{service.id}_logo.{ext}"
                filepath = os.path.join(upload_dir, filename)
                
                try:
                    from PIL import Image
                    img = Image.open(file)
                    img.thumbnail((200, 100), Image.Resampling.LANCZOS)
                    img.save(filepath, optimize=True)
                    service.logo_path = f"uploads/delivery_logos/{filename}"
                except Exception as e:
                    print(f"Logo upload error: {e}")
        
        # Удаление логотипа
        if request.form.get('remove_logo') == '1':
            import os
            from flask import current_app
            if service.logo_path:
                old_path = os.path.join(current_app.root_path, 'static', service.logo_path)
                if os.path.exists(old_path):
                    os.remove(old_path)
                service.logo_path = None
        
        db.session.commit()
        flash('Служба доставки обновлена.', 'success')
        return redirect(url_for('admin.deliveries'))
    
    return render_template('admin/delivery_edit.html',
                         title='Редактирование службы доставки',
                         service=service)


@bp.route('/deliveries/<int:service_id>/toggle', methods=['POST'])
def delivery_toggle(service_id):
    """
    Переключение статуса службы доставки.
    URL: /main_admin/deliveries/{id}/toggle
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    service = db.session.get(DeliveryService, service_id)
    if not service:
        abort(404)
    
    service.is_active = not service.is_active
    db.session.commit()
    
    status = 'активна' if service.is_active else 'отключена'
    flash(f'Служба доставки теперь {status}.', 'success')
    return redirect(url_for('admin.deliveries'))


@bp.route('/deliveries/<int:service_id>/delete')
def delivery_delete(service_id):
    """
    Удаление службы доставки.
    URL: /main_admin/deliveries/{id}/delete
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    service = db.session.get(DeliveryService, service_id)
    if not service:
        abort(404)
    
    db.session.delete(service)
    db.session.commit()
    
    flash('Служба доставки удалена.', 'success')
    return redirect(url_for('admin.deliveries'))


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    """
    Настройки системы.
    URL: /main_admin/settings
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    # Загрузка всех настроек
    settings_data = {
        'site_name': Settings.get('site_name', 'Маркетплейс'),
        'site_description': Settings.get('site_description', ''),
        'contact_email': Settings.get('contact_email', ''),
        'contact_phone': Settings.get('contact_phone', ''),
        'min_order_amount': Settings.get('min_order_amount', 0),
        'free_delivery_threshold': Settings.get('free_delivery_threshold', 0),
        'require_phone': Settings.get('require_phone', False),
        'bonus_percent': Settings.get('bonus_percent', 1),
        'bonus_expiration_days': Settings.get('bonus_expiration_days', 365),
        'meta_title': Settings.get('meta_title', ''),
        'meta_description': Settings.get('meta_description', ''),
    }
    
    # Создаём объект для совместимости с шаблоном
    class SettingsObj:
        pass
    settings = SettingsObj()
    for key, value in settings_data.items():
        setattr(settings, key, value)
    
    if request.method == 'POST':
        # Социальные сети
        social_links = []
        for i in range(4):
            name = request.form.get(f'social_name_{i}')
            url = request.form.get(f'social_url_{i}')
            if name and url:
                social_links.append({'name': name, 'url': url})
        Settings.set('social_links', social_links, 'json')
        
        # Сохранение настроек из формы
        Settings.set('site_name', request.form.get('site_name', 'Маркетплейс'))
        Settings.set('site_description', request.form.get('site_description', ''))
        Settings.set('contact_email', request.form.get('contact_email', ''))
        Settings.set('contact_phone', request.form.get('contact_phone', ''))
        Settings.set('min_order_amount', request.form.get('min_order_amount', 0, type=int))
        Settings.set('free_delivery_threshold', request.form.get('free_delivery_threshold', 0, type=int))
        Settings.set('require_phone', 'require_phone' in request.form)
        Settings.set('bonus_percent', request.form.get('bonus_percent', 1, type=float))
        Settings.set('bonus_expiration_days', request.form.get('bonus_expiration_days', 365, type=int))
        Settings.set('meta_title', request.form.get('meta_title', ''))
        Settings.set('meta_description', request.form.get('meta_description', ''))
        
        flash('Настройки сохранены.', 'success')
        return redirect(url_for('admin.settings'))
    
    return render_template('admin/settings.html',
                         title='Настройки',
                         settings=settings)


@bp.route('/messages')
def messages():
    """
    Сообщения админки.
    URL: /main_admin/messages
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    tab = request.args.get('tab', 'buyers')
    dialog_id = request.args.get('dialog_id', type=int)
    
    # Получаем все сообщения для админа (входящие и исходящие)
    all_messages = Message.query.filter(
        ((Message.receiver_type == 'admin') & (Message.receiver_id == 0)) |
        ((Message.sender_type == 'admin') & (Message.sender_id == 0))
    ).order_by(Message.timestamp.desc()).all()
    
    # Группируем по собеседникам
    conversations = {}
    for msg in all_messages:
        # Определяем ключ собеседника - это тот, кто НЕ админ
        if msg.sender_type == 'admin':
            # Админ отправил сообщение -> собеседник это получатель
            partner_type = msg.receiver_type
            partner_id = msg.receiver_id
        elif msg.receiver_type == 'admin':
            # Админ получил сообщение -> собеседник это отправитель
            partner_type = msg.sender_type
            partner_id = msg.sender_id
        else:
            continue
        
        # Нормализуем partner_type к единственному числу
        if partner_type == 'sellers':
            partner_type = 'seller'
        elif partner_type == 'buyers':
            partner_type = 'buyer'
        
        key = f"{partner_type}:{partner_id}"
        
        if key not in conversations:
            # Получаем имя собеседника
            if partner_type == 'buyer':
                partner = Buyer.query.get(partner_id)
                partner_name = partner.full_name if partner else f'Покупатель #{partner_id}'
            elif partner_type == 'seller':
                partner = Seller.query.get(partner_id)
                partner_name = partner.store_name if partner else f'Продавец #{partner_id}'
            else:
                partner_name = 'Неизвестный'
            
            # Считаем непрочитанные (входящие сообщения от этого пользователя)
            # Учитываем обе формы: 'seller'/'sellers' и 'buyer'/'buyers'
            sender_types = [partner_type]
            if partner_type == 'seller':
                sender_types.append('sellers')
            elif partner_type == 'buyer':
                sender_types.append('buyers')
            
            unread = Message.query.filter(
                Message.sender_type.in_(sender_types),
                Message.sender_id == partner_id,
                Message.receiver_type == 'admin',
                Message.receiver_id == 0,
                Message.is_read == False
            ).count()
            
            conversations[key] = {
                'id': partner_id,
                'name': partner_name,
                'partner_type': partner_type,
                'partner_id': partner_id,
                'last_message': msg.text[:50] if msg.text else '',
                'unread_count': unread,
                'avatar': partner.store_logo if partner_type == 'seller' and partner else None
            }
        else:
            # Обновляем last_message если текущее сообщение новее
            if msg.timestamp > (conversations[key].get('_timestamp') or datetime.min):
                conversations[key]['last_message'] = msg.text[:50] if msg.text else ''
                conversations[key]['_timestamp'] = msg.timestamp
    
    # Фильтруем по типу (покупатели или продавцы)
    if tab == 'sellers':
        filtered_conversations = {k: v for k, v in conversations.items() if v['partner_type'] == 'seller'}
    else:
        filtered_conversations = {k: v for k, v in conversations.items() if v['partner_type'] == 'buyer'}
    
    # Получаем текущий диалог и его сообщения
    current_dialog = None
    dialog_messages = []
    
    if dialog_id:
        # Нормализуем tab к singular форме для поиска в БД
        partner_type = tab
        if partner_type == 'sellers':
            partner_type = 'seller'
        elif partner_type == 'buyers':
            partner_type = 'buyer'
        
        dialog_messages = Message.get_conversation('admin', 0, partner_type, dialog_id)
        
        # Создаем объект текущего диалога
        if partner_type == 'buyer':
            partner = Buyer.query.get(dialog_id)
            partner_name = partner.full_name if partner else f'Покупатель #{dialog_id}'
        else:
            partner = Seller.query.get(dialog_id)
            partner_name = partner.store_name if partner else f'Продавец #{dialog_id}'
        
        current_dialog = type('obj', (object,), {
            'id': dialog_id,
            'name': partner_name,
            'avatar': partner.store_logo if partner and partner_type == 'seller' else None,
            'partner_type': partner_type,
        })()
        
        # Добавляем текущий диалог в список, если его там нет
        # Нормализуем partner_type к singular форме для ключа
        key_partner_type = partner_type
        if key_partner_type == 'sellers':
            key_partner_type = 'seller'
        elif key_partner_type == 'buyers':
            key_partner_type = 'buyer'
        
        key = f"{key_partner_type}:{dialog_id}"
        if key not in filtered_conversations:
            # Нормализуем partner_type для запроса в БД
            db_partner_type = key_partner_type
            
            # Считаем непрочитанные сообщения
            unread = Message.query.filter(
                Message.sender_type == db_partner_type,
                Message.sender_id == dialog_id,
                Message.receiver_type == 'admin',
                Message.receiver_id == 0,
                Message.is_read == False
            ).count()
            
            filtered_conversations[key] = {
                'id': dialog_id,
                'name': partner_name,
                'partner_type': key_partner_type,
                'partner_id': dialog_id,
                'last_message': dialog_messages[0].text[:50] if dialog_messages and dialog_messages[0].text else '',
                'unread_count': unread,
                'avatar': None
            }
        
        # Отмечаем сообщения как прочитанные
        for msg in dialog_messages:
            if msg.receiver_type == 'admin' and msg.receiver_id == 0:
                msg.mark_as_read()
    
    # Статистика непрочитанных
    unread_buyers = sum(1 for c in conversations.values() if c['partner_type'] == 'buyer' and c['unread_count'] > 0)
    unread_sellers = sum(1 for c in conversations.values() if c['partner_type'] == 'seller' and c['unread_count'] > 0)
    
    return render_template('admin/messages.html',
                         title='Сообщения',
                         tab=tab,
                         dialogs=list(filtered_conversations.values()),
                         messages=dialog_messages,
                         current_dialog=current_dialog,
                         unread_buyers=unread_buyers,
                         unread_sellers=unread_sellers)


@bp.route('/messages/send', methods=['POST'])
def send_message():
    """
    Отправка сообщения от имени админа.
    URL: /main_admin/messages/send
    """
    if not is_admin():
        return redirect(url_for('admin.login'))
    
    partner_type = request.form.get('partner_type')
    partner_id = request.form.get('partner_id', type=int)
    text = request.form.get('text', '').strip()
    tab = request.form.get('tab', 'buyers')
    image_path = request.form.get('image_path', '').strip()
    file_path = request.form.get('file_path', '').strip()
    
    if not text and not image_path and not file_path:
        flash('Добавьте текст или вложение.', 'error')
        return redirect(url_for('admin.messages', tab=tab, dialog_id=partner_id))
    
    # Нормализуем partner_type: 'sellers' -> 'seller', 'buyers' -> 'buyer'
    if partner_type == 'sellers':
        partner_type = 'seller'
    elif partner_type == 'buyers':
        partner_type = 'buyer'
    
    # Создаем сообщение
    message = Message(
        sender_type='admin',
        sender_id=0,
        receiver_type=partner_type,
        receiver_id=partner_id,
        text=text or None,
        image_path=image_path if image_path else None,
        file_path=file_path if file_path else None,
        is_system=False
    )
    
    db.session.add(message)
    db.session.commit()
    
    flash('Сообщение отправлено.', 'success')
    return redirect(url_for('admin.messages', tab=tab, dialog_id=partner_id))


@bp.route('/api/upload-message-file', methods=['POST'])
@csrf.exempt
def upload_message_file():
    """
    API для загрузки файлов (изображений и PDF) в сообщениях.
    URL: /main_admin/api/upload-message-file
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Проверяем тип файла
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf'}
    file_ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    
    if file_ext not in allowed_extensions:
        return jsonify({'error': 'File type not allowed'}), 400
    
    # Проверяем размер (10MB max)
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    
    if file_size > 10 * 1024 * 1024:
        return jsonify({'error': 'File too large (max 10MB)'}), 400
    
    # Создаём директорию для сообщений если её нет
    import os
    from flask import current_app
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'messages')
    os.makedirs(upload_dir, exist_ok=True)
    
    # Генерируем уникальное имя файла
    from werkzeug.utils import secure_filename
    from datetime import datetime
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    original_name = secure_filename(file.filename)
    filename = f"{timestamp}_{original_name}"
    
    # Сохраняем файл
    file_path = os.path.join(upload_dir, filename)
    file.save(file_path)
    
    # Возвращаем путь относительно static
    static_path = f"uploads/messages/{filename}"
    
    return jsonify({
        'success': True,
        'path': static_path,
        'filename': original_name,
        'is_image': file_ext in {'png', 'jpg', 'jpeg', 'gif', 'webp'},
        'is_pdf': file_ext == 'pdf'
    })


@bp.route('/api/search-users')
def search_users():
    """
    API для поиска пользователей (покупателей или продавцов).
    URL: /main_admin/api/search-users?query=...&type=buyers|sellers
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    
    query = request.args.get('query', '').strip()
    user_type = request.args.get('type', 'buyers')
    
    if not query or len(query) < 2:
        return jsonify({'users': []})
    
    limit = request.args.get('limit', 10, type=int)
    
    users = []
    
    if user_type == 'sellers':
        # Поиск продавцов
        from sqlalchemy import or_
        sellers = Seller.query.filter(
            or_(
                Seller.email.ilike(f'%{query}%'),
                Seller.login.ilike(f'%{query}%'),
                Seller.store_name.ilike(f'%{query}%'),
                Seller.phone.ilike(f'%{query}%')
            ),
            Seller.is_active == True
        ).limit(limit).all()
        
        for seller in sellers:
            users.append({
                'id': seller.id,
                'type': 'seller',
                'name': seller.store_name,
                'email': seller.email,
                'login': seller.login,
                'phone': seller.phone or ''
            })
    else:
        # Поиск покупателей
        from sqlalchemy import or_
        buyers = Buyer.query.filter(
            or_(
                Buyer.email.ilike(f'%{query}%'),
                Buyer.login.ilike(f'%{query}%'),
                Buyer.first_name.ilike(f'%{query}%'),
                Buyer.last_name.ilike(f'%{query}%'),
                Buyer.phone.ilike(f'%{query}%')
            ),
            Buyer.is_active == True
        ).limit(limit).all()
        
        for buyer in buyers:
            users.append({
                'id': buyer.id,
                'type': 'buyer',
                'name': buyer.full_name,
                'email': buyer.email,
                'login': buyer.login,
                'phone': buyer.phone or ''
            })
    
    return jsonify({'users': users})


@bp.route('/api/messages')
def get_messages():
    """
    API для получения сообщений диалога (AJAX).
    URL: /main_admin/api/messages?partner_type=buyer&partner_id=1&last_timestamp=...
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    
    partner_type = request.args.get('partner_type')
    partner_id = request.args.get('partner_id', type=int)
    last_timestamp = request.args.get('last_timestamp')
    
    if not partner_type or not partner_id:
        return jsonify({'error': 'Missing parameters'}), 400
    
    # Нормализуем partner_type
    if partner_type == 'sellers':
        partner_type = 'seller'
    elif partner_type == 'buyers':
        partner_type = 'buyer'
    
    # Получаем сообщения, отсортированные по возрастанию времени
    messages = Message.get_conversation('admin', 0, partner_type, partner_id)
    
    # Фильтруем по timestamp если передан (>= чтобы исключить текущие)
    if last_timestamp:
        from datetime import datetime, timezone
        try:
            # Парсим ISO строку с timezone (Z = UTC)
            last_ts = datetime.fromisoformat(last_timestamp.replace('Z', '+00:00'))
            # Конвертируем в naive datetime для сравнения с БД (БД хранит UTC naive)
            last_ts = last_ts.replace(tzinfo=None)
            # Фильтруем сообщения с timestamp > last_ts (только новые)
            messages = [m for m in messages if m.timestamp and m.timestamp > last_ts]
        except Exception as e:
            print(f"Timestamp parse error: {e}")
            pass
    
    # Форматируем сообщения
    messages_data = []
    for msg in messages:
        messages_data.append({
            'id': msg.id,
            'text': msg.text,
            'sender_type': msg.sender_type,
            'sender_id': msg.sender_id,
            'timestamp': msg.timestamp.isoformat() if msg.timestamp else None,
            'is_read': msg.is_read,
            'image_path': msg.image_path,
            'file_path': msg.file_path
        })
    
    return jsonify({'messages': messages_data})


@bp.route('/api/conversations')
def get_conversations():
    """
    API для получения списка диалогов (AJAX).
    URL: /main_admin/api/conversations?tab=buyers
    """
    if not is_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    
    tab = request.args.get('tab', 'buyers')
    
    # Получаем все сообщения для админа
    all_messages = Message.query.filter(
        ((Message.receiver_type == 'admin') & (Message.receiver_id == 0)) |
        ((Message.sender_type == 'admin') & (Message.sender_id == 0))
    ).order_by(Message.timestamp.desc()).all()
    
    # Группируем по собеседникам
    conversations = {}
    for msg in all_messages:
        if msg.sender_type == 'admin':
            partner_type = msg.receiver_type
            partner_id = msg.receiver_id
        elif msg.receiver_type == 'admin':
            partner_type = msg.sender_type
            partner_id = msg.sender_id
        else:
            continue
        
        # Нормализуем partner_type
        if partner_type == 'sellers':
            partner_type = 'seller'
        elif partner_type == 'buyers':
            partner_type = 'buyer'
        
        key = f"{partner_type}:{partner_id}"
        
        if key not in conversations:
            if partner_type == 'buyer':
                partner = Buyer.query.get(partner_id)
                partner_name = partner.full_name if partner else f'Покупатель #{partner_id}'
            elif partner_type == 'seller':
                partner = Seller.query.get(partner_id)
                partner_name = partner.store_name if partner else f'Продавец #{partner_id}'
            else:
                partner_name = 'Неизвестный'
            
            # Считаем непрочитанные
            sender_types = [partner_type]
            if partner_type == 'seller':
                sender_types.append('sellers')
            elif partner_type == 'buyer':
                sender_types.append('buyers')
            
            unread = Message.query.filter(
                Message.sender_type.in_(sender_types),
                Message.sender_id == partner_id,
                Message.receiver_type == 'admin',
                Message.receiver_id == 0,
                Message.is_read == False
            ).count()
            
            conversations[key] = {
                'id': partner_id,
                'name': partner_name,
                'partner_type': partner_type,
                'partner_id': partner_id,
                'last_message': msg.text[:50] if msg.text else '',
                'unread_count': unread,
                'avatar': partner.store_logo if partner_type == 'seller' and partner else None
            }

    # Фильтруем по типу
    if tab == 'sellers':
        filtered = {k: v for k, v in conversations.items() if v['partner_type'] == 'seller'}
    else:
        filtered = {k: v for k, v in conversations.items() if v['partner_type'] == 'buyer'}
    
    return jsonify({'conversations': list(filtered.values())})


@bp.route('/reviews')
def reviews():
    """
    Список отзывов, ожидающих модерации.
    URL: /main_admin/reviews
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    pending_reviews = (
        Review.query
        .filter(Review.status == 'pending')
        .order_by(Review.created_at.desc())
        .all()
    )

    return render_template(
        'admin/reviews.html',
        title='Модерация отзывов',
        reviews=pending_reviews
    )


@bp.route('/reviews/<int:review_id>/approve', methods=['POST'])
def review_approve(review_id):
    """
    Одобрить отзыв (админ).
    URL: /main_admin/reviews/{id}/approve
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    review = db.session.get(Review, review_id)
    if not review:
        flash('Отзыв не найден.', 'error')
        return redirect(url_for('admin.reviews'))

    review.approve()
    flash('Отзыв одобрен и опубликован.', 'success')
    return redirect(url_for('admin.reviews'))


@bp.route('/reviews/<int:review_id>/reject', methods=['POST'])
def review_reject(review_id):
    """
    Отклонить отзыв (админ) — удаляет запись, чтобы покупатель мог оставить новый.
    URL: /main_admin/reviews/{id}/reject
    """
    if not is_admin():
        return redirect(url_for('admin.login'))

    review = db.session.get(Review, review_id)
    if not review:
        flash('Отзыв не найден.', 'error')
        return redirect(url_for('admin.reviews'))

    review.reject()
    flash('Отзыв отклонён и удалён.', 'success')
    return redirect(url_for('admin.reviews'))


@bp.route('/auth/logout')
def logout():
    """
    Выход из админ-панели.
    URL: /main_admin/auth/logout
    """
    from flask import session, logout_user
    from flask_login import current_user
    
    logout_user()
    session.clear()
    flash('Вы вышли из системы.', 'info')
    return redirect(url_for('main.index'))


def is_admin():
    """
    Проверка прав администратора.
    """
    from flask import session, request
    
    # Проверка сессии абсолютного админа
    if session.get('main_admin_authenticated'):
        return True
    
    # Проверка модели админа
    if current_user.is_authenticated and isinstance(current_user, Admin):
        return True
    
    # Проверка по IP для локальной разработки
    if request.remote_addr in ('127.0.0.1', '::1'):
        return True
    
    return False


# Импорт Admin для проверки
from app.models.users import Admin
