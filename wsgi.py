"""
WSGI entrypoint для продакшн-серверов (gunicorn, uwsgi) и PaaS-платформ.

Использование:
  gunicorn wsgi:app
  gunicorn -b 0.0.0.0:$PORT wsgi:app
"""

import os

from app import create_app

# Конфиг берётся из переменной окружения APP_CONFIG (по умолчанию prod)
config_name = os.environ.get("APP_CONFIG", "prod")

# DEBUG: печатаем в лог, какой DATABASE_URI реально используется
# (без пароля), чтобы убедиться, что Coolify подхватил правильную БД.
_db_uri = os.environ.get("DATABASE_URI") or os.environ.get("DATABASE_URL") or "NOT SET"
if "@" in _db_uri:
    _safe = _db_uri.split("@", 1)[1]
else:
    _safe = _db_uri
print(f"[wsgi] APP_CONFIG={config_name} DB host={_safe}", flush=True)

app = create_app(config_name)

# Логируем зарегистрированные routes — помогает при дебаге path/subdomain
import logging as _logging
_log = _logging.getLogger(__name__)
_log.info("=== Registered URL rules ===")
for _rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
    _log.info(f"  {sorted(_rule.subdomain or ['<no-subdomain>'])} {_rule.rule} -> {_rule.endpoint}")
_log.info(f"=== SERVER_NAME={app.config.get('SERVER_NAME')}, NO_SELLER_SUBDOMAIN={os.environ.get('NO_SELLER_SUBDOMAIN')}")
