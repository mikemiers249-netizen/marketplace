"""
Конфигурация приложения маркетплейса.
Все секреты и адреса внешних сервисов читаются из переменных окружения.
"""

import os
from datetime import timedelta
from werkzeug.security import generate_password_hash


def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    """Базовый класс конфигурации."""

    SECRET_KEY = _env("SECRET_KEY") or "dev-secret-key-change-in-prod"

    # ===== База данных =====
    SQLALCHEMY_DATABASE_URI = (
        _env("DATABASE_URI")
        or _env("DATABASE_URL")  # стандартное имя для PaaS
        or "sqlite:///marketplace.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        # Не пытаться определять тип hstore при подключении.
        # В Coolify за PgBouncer бывают сломанные коннекты
        # (transaction aborted), из-за которых on_connect-хук
        # для hstore падает с InFailedSqlTransaction. Нам hstore
        # в моделях не нужен.
        "use_native_hstore": False,
    }

    # ===== Поддомены =====
    SERVER_NAME = _env("SERVER_NAME")
    SESSION_COOKIE_DOMAIN = _env("SESSION_COOKIE_DOMAIN")

    # ===== Сессия =====
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    SESSION_REFRESH_EACH_REQUEST = True
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # ===== Загрузка файлов =====
    UPLOAD_FOLDER = _env("UPLOAD_FOLDER") or os.path.join(
        os.getcwd(), "app", "static", "uploads"
    )
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "pdf"}

    # ===== Почта =====
    MAIL_SERVER = _env("MAIL_SERVER", "smtp.mail.ru")
    MAIL_PORT = _env_int("MAIL_PORT", 465)
    MAIL_USE_TLS = _env_bool("MAIL_USE_TLS", False)
    MAIL_USE_SSL = _env_bool("MAIL_USE_SSL", True)
    MAIL_USERNAME = _env("MAIL_USERNAME")
    MAIL_PASSWORD = _env("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = _env("MAIL_DEFAULT_SENDER") or _env("MAIL_USERNAME")

    # ===== Админ =====
    MAIN_ADMIN_LOGIN = _env("MAIN_ADMIN_LOGIN", "admin")
    MAIN_ADMIN_PASSWORD_HASH = _env("MAIN_ADMIN_PASSWORD_HASH")

    # ===== Кэш =====
    # На проде подключаем Redis (если есть REDIS_URL), иначе SimpleCache
    CACHE_TYPE = _env("CACHE_TYPE", "SimpleCache")
    CACHE_DEFAULT_TIMEOUT = _env_int("CACHE_DEFAULT_TIMEOUT", 300)
    CACHE_REDIS_URL = _env("REDIS_URL") or _env("CACHE_REDIS_URL")

    # ===== Логирование =====
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")
    LOG_FILE = _env("LOG_FILE") or os.path.join(os.getcwd(), "logs", "marketplace.log")

    # ===== СДЭК =====
    CDEK_ACCOUNT = _env("CDEK_ACCOUNT")
    CDEK_SECURE = _env("CDEK_SECURE")
    CDEK_TEST_MODE = _env_bool("CDEK_TEST_MODE", False)


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = _env("DATABASE_URI", "sqlite:///marketplace_dev.db")
    # Тестовый хэш для логина "admin" (только для локальной разработки)
    MAIN_ADMIN_PASSWORD_HASH = _env(
        "MAIN_ADMIN_PASSWORD_HASH",
        generate_password_hash("admin"),
    )


class ProductionConfig(Config):
    DEBUG = False

    # В проде БД обязательна
    SQLALCHEMY_DATABASE_URI = (
        _env("DATABASE_URI")
        or _env("DATABASE_URL")
        or "postgresql+psycopg2://postgres:postgres@localhost:5432/marketplace"
    )
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)

    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        if not cls.MAIN_ADMIN_PASSWORD_HASH:
            raise RuntimeError(
                "MAIN_ADMIN_PASSWORD_HASH must be set in production environment"
            )


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///marketplace_test.db"
    WTF_CSRF_ENABLED = False


config_by_name = {
    "dev": DevelopmentConfig,
    "prod": ProductionConfig,
    "test": TestingConfig,
}

# По умолчанию в проде — production, иначе development
config = config_by_name[_env("APP_CONFIG", "dev")]
