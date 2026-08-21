"""
Фабрика приложений Flask для маркетплейса.
Инициализация расширений и регистрация blueprints.
"""

import logging
import os
from datetime import datetime
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_migrate import Migrate
from flask_mail import Mail
from flask_caching import Cache
from sqlalchemy import func, event

# Инициализация расширений
db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
migrate = Migrate()
mail = Mail()
cache = Cache()


def create_app(config_class=None):
    """
    Создание и конфигурация Flask-приложения.
    
    Args:
        config_class: Класс конфигурации (по умолчанию из config.py)
    
    Returns:
        Настроенное приложение Flask
    """
    app = Flask(__name__)

    # Загрузка конфигурации
    if config_class is None:
        config_class = config_by_name[os.getenv('FLASK_ENV') or os.getenv('APP_CONFIG', 'dev')]
    elif isinstance(config_class, str):
        config_class = config_by_name.get(config_class, config_by_name['dev'])
    app.config.from_object(config_class)

    # Принудительно включаем авто-перечитку шаблонов Jinja по mtime.
    # По умолчанию это зависит от app.debug, но если debug по какой-то
    # причине не сработал — Jinja всё равно будет читать .html с диска
    # при каждом запросе, без кеширования скомпилированного шаблона.
    # Цена — небольшое замедление рендера; для dev-окружения это ок.
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True
    # На всякий случай — обнуляем размер кеша шаблонов Jinja,
    # чтобы он не держал старые скомпилированные шаблоны.
    try:
        app.jinja_env.cache = None
    except Exception:
        pass

    # Инициализация расширений с приложением
    init_extensions(app)
    
    # Регистрация blueprints
    register_blueprints(app)
    
    # Настройка логирования
    setup_logging(app)
    
    # Создание директорий для загрузок
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    
    # Регистрация контекстных процессоров
    register_context_processors(app)
    
    # Установка model_globals для декораторов
    from app.models.users import Buyer, Seller
    app.model_globals = {
        'Buyer': Buyer,
        'Seller': Seller
    }
    
    # Регистрация обработчиков ошибок
    register_error_handlers(app)

    # На проде таблицы создаются миграциями Alembic (flask db upgrade),
    # которые вызываются через release-фазу в Procfile. db.create_all()
    # здесь НЕ вызываем: он конфликтует с миграциями и падает на hstore
    # в PostgreSQL.
    # Для локальной разработки миграции можно прогнать вручную через
    # `flask db upgrade` или `db.create_all()` в shell.

    # Регистрация кастомных CLI-команд
    from app.commands import (
        reset_public_schema_command, db_init_command,
        fix_password_length_command, grant_test_tariff_command,
        clean_test_subs_command, clear_seller_subs_command,
        clean_missing_photos_command,
        seed_footer_links_command,
    )
    app.cli.add_command(reset_public_schema_command)
    app.cli.add_command(db_init_command)
    app.cli.add_command(fix_password_length_command)
    app.cli.add_command(grant_test_tariff_command)
    app.cli.add_command(clean_test_subs_command)
    app.cli.add_command(clear_seller_subs_command)
    app.cli.add_command(clean_missing_photos_command)
    app.cli.add_command(seed_footer_links_command)

    # Санитизация коннектов к PostgreSQL: если PgBouncer (Coolify)
    # отдаёт коннект в состоянии "transaction aborted", любой первый
    # SQL падает. Перехватываем on_connect и сбрасываем состояние,
    # переключая в AUTOCOMMIT. Это заставляет каждую команду быть
    # отдельной транзакцией — сломанная не висит на всю сессию.
    from sqlalchemy import event as _sa_event
    from sqlalchemy.engine import Engine as _SAEngine

    @_sa_event.listens_for(_SAEngine, "connect")
    def _pgbouncer_sanitizer(dbapi_connection, connection_record):
        try:
            # Если есть открытая битая транзакция — откатываем
            dbapi_connection.rollback()
        except Exception:
            pass
        try:
            # Переключаем psycopg2 в autocommit
            dbapi_connection.set_isolation_level(0)  # 0 = AUTOCOMMIT
        except Exception:
            pass

    return app


def init_extensions(app):
    """Инициализация Flask-расширений."""

    # База данных
    db.init_app(app)
    migrate.init_app(app, db)

    # Включаем ON DELETE CASCADE на стороне SQLite для каждого нового
    # соединения. По умолчанию PRAGMA foreign_keys = OFF в SQLite, и без
    # этого event listener'а ON DELETE CASCADE из миграций
    # (например, h1u2c3a4s5c6_tariff_row_fk_cascade.py) не работает —
    # ORM будет пытаться выставить FK в NULL, что падает на NOT NULL
    # колонках (row_id, plan_id и т.п.).
    from sqlalchemy import event as _sa_event
    from sqlalchemy.engine import Engine as _Engine

    @_sa_event.listens_for(_Engine, 'connect')
    def _sqlite_enable_fk(dbapi_connection, connection_record):
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA foreign_keys = ON')
            cursor.close()
        except Exception:
            # Не ломаем приложение, если движок не SQLite (например, тесты
            # с in-memory или PostgreSQL).
            pass
    
    # CSRF защита
    csrf.init_app(app)
    
    # Явно добавляем функцию csrf_token в Jinja2 globals для шаблонов
    @app.template_global()
    def csrf_token():
        """Получение CSRF токена для форм."""
        return csrf.generate_csrf()

    # Хелперы для раздела «Информация»: слаг тега и индекс цвета плашки.
    @app.template_filter('tag_slug')
    def tag_slug_filter(tag):
        if not tag:
            return ''
        from app.models.communications import InfoPost
        return InfoPost.tag_slug(tag)

    @app.template_global()
    def tag_color_index(tag):
        """Стабильный индекс 0..7 по тегу для выбора цвета плашки."""
        if not tag:
            return 0
        return sum(ord(c) for c in str(tag)) % 8
    
    # Настройка CSRF для работы с поддоменами
    # CSRF токен должен быть привязан к конкретному домену, а не к поддомену
    app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1 час
    app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken', 'X-CSRF-Token']
    
    # Логин менеджер
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Пожалуйста, войдите для доступа к этой странице.'
    login_manager.login_message_category = 'info'
    
    @login_manager.user_loader
    def load_user(user_id):
        """Загрузка пользователя для Flask-Login.
        
        Проверяет все типы пользователей в системе.
        """
        from app.models.users import Buyer, Seller
        
        try:
            # Разбор user_id формата "ClassName:id"
            user_type, user_id = user_id.split(':')
            user_id = int(user_id)
            
            if user_type == 'Buyer':
                user = Buyer.query.get(user_id)
                logging.getLogger(__name__).info(f"load_user: Buyer id={user_id} -> {user}")
                return user
            elif user_type == 'Seller':
                user = Seller.query.get(user_id)
                logging.getLogger(__name__).info(f"load_user: Seller id={user_id} -> {user}")
                return user
        except (ValueError, AttributeError) as e:
            logging.getLogger(__name__).warning(f"load_user error for user_id={user_id!r}: {e}")
        
        return None
    
    # Почта
    mail.init_app(app)
    
    # Кэширование
    cache.init_app(app)


def register_blueprints(app):
    """Регистрация blueprints для модульной архитектуры."""

    # Режим работы: subdomain или path.
    # По умолчанию используем path-based (/seller/*) — он не требует SSL
    # на поддомене, что критично для shared hosting'а типа Coolify.
    # Чтобы вернуть subdomain-режим, поставь USE_SELLER_SUBDOMAIN=1 в env.
    use_subdomain = bool(os.environ.get("USE_SELLER_SUBDOMAIN"))

    # Импорты blueprint'ов внутри функции для избежания циклических зависимостей
    from app.blueprints.main import bp as main_bp
    from app.blueprints.seller import bp as seller_bp
    from app.blueprints.admin import bp as admin_bp
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.api import bp as api_bp
    from app.blueprints.messages import bp as messages_bp

    # Основной домен (покупатели)
    app.register_blueprint(main_bp)

    if use_subdomain:
        # Поддомен продавца (старый режим)
        app.register_blueprint(seller_bp, subdomain='seller')
    else:
        # Path-based режим: seller доступен на /seller/* основного домена.
        # Пересоздаём blueprint БЕЗ subdomain, иначе Flask наследует
        # subdomain='seller' из исходного Blueprint-объявления в seller.py
        # и url_for генерирует URL с поддоменом даже в path-режиме.
        from flask import Blueprint as _Blueprint
        seller_bp_path = _Blueprint('seller', __name__)
        # Копируем deferred_functions — это все view-функции, зарегистрированные
        # через @bp.route(...) в seller.py. Flask их добавит при register_blueprint.
        for _df in seller_bp.deferred_functions:
            seller_bp_path.record(_df)
        app.register_blueprint(seller_bp_path, url_prefix='/seller')

    # Админ-панель
    app.register_blueprint(admin_bp, url_prefix='/main_admin')

    # Авторизация — основной домен
    app.register_blueprint(auth_bp)

    if use_subdomain:
        # Тот же auth-блюпринт, но на поддомене seller.
        from flask import Blueprint
        from app.blueprints.auth import login, seller_login, seller_logout, signup, seller_signup
        auth_seller_bp = Blueprint('auth_seller', __name__, url_prefix='/auth')
        auth_seller_bp.add_url_rule('/login', view_func=login, methods=['GET', 'POST'])
        auth_seller_bp.add_url_rule('/seller/login', view_func=seller_login, methods=['GET', 'POST'])
        auth_seller_bp.add_url_rule('/seller/logout', view_func=seller_logout, methods=['GET'])
        auth_seller_bp.add_url_rule('/signup', view_func=signup, methods=['GET', 'POST'])
        auth_seller_bp.add_url_rule('/seller/signup', view_func=seller_signup, methods=['GET', 'POST'])
        app.register_blueprint(auth_seller_bp, subdomain='seller')
    else:
        # Path-режим: тот же набор url-rules, но в основном домене
        # (нужно, чтобы url_for('auth_seller.seller_login') работал в seller.py)
        from flask import Blueprint
        from app.blueprints.auth import login, seller_login, seller_logout, signup, seller_signup
        auth_seller_bp = Blueprint('auth_seller', __name__, url_prefix='/auth')
        auth_seller_bp.add_url_rule('/login', view_func=login, methods=['GET', 'POST'])
        auth_seller_bp.add_url_rule('/seller/login', view_func=seller_login, methods=['GET', 'POST'])
        auth_seller_bp.add_url_rule('/seller/logout', view_func=seller_logout, methods=['GET'])
        auth_seller_bp.add_url_rule('/signup', view_func=signup, methods=['GET', 'POST'])
        auth_seller_bp.add_url_rule('/seller/signup', view_func=seller_signup, methods=['GET', 'POST'])
        app.register_blueprint(auth_seller_bp)
    
    # API для AJAX-запросов
    app.register_blueprint(api_bp, url_prefix='/api')
    
    # Унифицированный модуль сообщений
    app.register_blueprint(messages_bp)


def register_context_processors(app):
    """Регистрация контекстных процессоров."""

    # ВАЖНО: tariff_state должен рассчитываться для Seller на ЛЮБОМ пути
    # (path-режим /seller/* и main_admin/*). Раньше это было в
    # @bp.before_request внутри blueprint `seller`, но в path-режиме
    # мы пересоздаём blueprint без копирования before_request/
    # context_processor — и tariff_state терялся, sidebar уходил
    # в «can_work=False», кнопка «Активировать тариф» висела всегда.
    # Решение: вешаем before_request и context_processor на уровне app,
    # а внутри делаем проверку isinstance(current_user, Seller).

    @app.before_request
    def _app_seller_tariff_state():
        from flask import g
        from flask_login import current_user
        from app.models.users import Seller
        from app.blueprints.seller import (
            _resolve_tariff_state,
        )

        # Всегда инициализируем дефолты, чтобы context_processor
        # не ловил AttributeError.
        g.all_delivery_services = []
        g.active_delivery_ids = []
        g.tariff_state = None
        g.footer_links_by_column = {}

        # Ссылки подвала: грузим один раз на запрос, группируем по колонкам.
        # Если таблицы ещё нет (миграция не применена) — не валим рендер.
        try:
            from app.models.footer import FooterLink
            from collections import defaultdict
            grouped = defaultdict(list)
            for link in (
                FooterLink.query
                .filter_by(is_active=True)
                .order_by(FooterLink.column, FooterLink.sort_order, FooterLink.id)
                .all()
            ):
                grouped[link.column].append(link)
            g.footer_links_by_column = dict(grouped)
        except Exception:
            # Если таблица не создана (до первого db-init) — пусто.
            g.footer_links_by_column = {}

        if not current_user.is_authenticated or not isinstance(current_user, Seller):
            return

        # Эта логика раньше жила в @bp.before_request blueprint `seller`.
        # Состояние тарифа — критично для layout (sidebar, баннеры).
        try:
            from app.models.users import DeliveryService, SellerDelivery
            all_services = (
                DeliveryService.query
                .filter_by(is_active=True)
                .order_by(DeliveryService.name)
                .all()
            )
            active_ids = [
                sd.delivery_service_id
                for sd in SellerDelivery.query.filter_by(
                    seller_id=current_user.id, is_active=True
                ).all()
            ]
            g.all_delivery_services = all_services
            g.active_delivery_ids = active_ids
            g.tariff_state = _resolve_tariff_state(current_user)
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).exception(
                "tariff_state before_request failed for seller=%s: %s",
                getattr(current_user, 'id', None), _e,
            )
            g.tariff_state = None
    
    @app.context_processor
    def inject_global_variables():
        """Добавление глобальных переменных в шаблоны."""
        from flask import g
        from app.models.users import Buyer, Seller, Admin
        from app.models.communications import Message
        from app.utils.helpers import (
            get_main_admin_config,
            format_price,
            compute_product_promotion_info,
        )
        from app.utils.loyalty import is_loyalty_enabled
        from builtins import isinstance as isinstance_func
        from flask import session
        from flask_login import current_user
        from app.models.orders import CartItem, Favorite
        import os as _os
        import time as _time

        # mtime ключевых layout'ов — чтобы шаблон мог пометить в HTML,
        # какая версия файла реально отрендерена. Помогает при дебаге
        # «кеш сломался и сервер не видит мои правки».
        _templates_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'templates')
        _tpl_mtimes = {}
        for _name in ('admin/layout.html', 'seller/layout.html', 'main/profile.html'):
            _p = _os.path.join(_templates_dir, _name)
            try:
                _tpl_mtimes[_name] = int(_os.path.getmtime(_p))
            except OSError:
                _tpl_mtimes[_name] = 0
        _build_marker = f"{int(_time.time())}-" + ",".join(
            f"{k}={v}" for k, v in _tpl_mtimes.items()
        )

        # Получение количества товаров в корзине
        cart_count = 0
        if current_user.is_authenticated and isinstance(current_user, Buyer):
            cart_count = db.session.query(func.sum(CartItem.quantity)).filter(
                CartItem.buyer_id == current_user.id
            ).scalar() or 0

        # Получение ID избранных товаров
        favorite_ids = []
        if current_user.is_authenticated and isinstance(current_user, Buyer):
            favorite_ids = [f.product_id for f in Favorite.query.filter_by(buyer_id=current_user.id).all()]

        # Количество непрочитанных сообщений для текущего пользователя.
        # Используется в layout'ах/меню для красной подсветки пункта «Сообщения».
        #  - покупатель: получатель 'buyer' / current_user.id
        #  - продавец:   получатель 'seller' / current_user.id
        #  - main_admin: сессия 'main_admin_authenticated' — это «админ с id=0»,
        #                так в Message хранятся входящие к абсолютному админу
        #  - доп. Admin: получатель 'admin' / current_user.id
        unread_messages_count = 0
        try:
            if current_user.is_authenticated and isinstance(current_user, Buyer):
                unread_messages_count = Message.get_unread_count('buyer', current_user.id)
            elif current_user.is_authenticated and isinstance(current_user, Seller):
                unread_messages_count = Message.get_unread_count('seller', current_user.id)
            elif current_user.is_authenticated and isinstance(current_user, Admin):
                unread_messages_count = Message.get_unread_count('admin', current_user.id)
            elif session.get('main_admin_authenticated'):
                unread_messages_count = Message.get_unread_count('admin', 0)
        except Exception:
            # Никогда не валим рендер страницы из-за бейджика.
            unread_messages_count = 0

        return {
            'main_admin_config': get_main_admin_config(),
            'format_price': format_price,
            'promo_info': compute_product_promotion_info,
            'Buyer': Buyer,
            'Seller': Seller,
            'isinstance': isinstance_func,
            'cart_count': cart_count,
            'favorite_ids': favorite_ids,
            'loyalty_enabled': is_loyalty_enabled(),
            'unread_messages_count': unread_messages_count,
            # build_marker — отметка «сборки» страницы: время рендера + mtime
            # ключевых шаблонов. Помогает глазами увидеть в HTML, что именно
            # сейчас отдаёт сервер (и не кешируется ли у браузера).
            'build_marker': _build_marker,
            # Ссылки в подвале: сгруппированные по колонкам. Чтобы не дёргать
            # БД на каждом запросе, кешируем на уровне `g` в before_request выше.
            'footer_links': getattr(g, 'footer_links_by_column', {}),
        }

    @app.context_processor
    def _app_seller_tariff_state_processor():
        """Пробрасывает tariff_state / tariff_locked / tariff_warning_banner
        в шаблоны на уровне app (а не blueprint'а seller). Это нужно для
        path-режима, где blueprint пересоздаётся без context_processor'а.
        """
        from flask import g
        state = getattr(g, 'tariff_state', None)
        if state is None:
            return {
                'tariff_state': None,
                'tariff_warning_banner': False,
                'tariff_locked': False,
            }
        return {
            'tariff_state': state,
            'tariff_warning_banner': bool(state.get('show_warning_banner')),
            'tariff_locked': state.get('state') == 'locked',
        }

    @app.context_processor
    def _app_seller_delivery_processor():
        """delivery context processor — на уровне app, чтобы был в шаблонах
        в path-режиме.
        """
        from flask import g
        from app.utils.loyalty import is_loyalty_enabled
        return {
            'all_delivery_services': getattr(g, 'all_delivery_services', []),
            'active_delivery_ids': getattr(g, 'active_delivery_ids', []),
            'loyalty_enabled': is_loyalty_enabled(),
        }
    
    @app.context_processor
    def year_processor():
        """Добавление текущего года в футер."""
        return {'current_year': datetime.now().year}


def register_error_handlers(app):
    """Регистрация обработчиков ошибок."""
    
    @app.errorhandler(400)
    def bad_request(error):
        return {'error': 'Неверный запрос'}, 400
    
    @app.errorhandler(403)
    def forbidden(error):
        return {'error': 'Доступ запрещён'}, 403
    
    @app.errorhandler(404)
    def not_found(error):
        return {'error': 'Страница не найдена'}, 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return {'error': 'Внутренняя ошибка сервера'}, 500


def setup_logging(app):
    """Настройка логирования."""
    
    logs_dir = os.path.dirname(app.config['LOG_FILE'])
    os.makedirs(logs_dir, exist_ok=True)
    
    logging.basicConfig(
        level=getattr(logging, app.config['LOG_LEVEL']),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(app.config['LOG_FILE']),
            logging.StreamHandler()
        ]
    )


# Импорт конфигурации в конце файла
from config import config_by_name
