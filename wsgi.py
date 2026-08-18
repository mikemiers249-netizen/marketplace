"""
WSGI entrypoint для продакшн-серверов (gunicorn, uwsgi) и PaaS-платформ.

Использование:
  gunicorn wsgi:app
  gunicorn -b 0.0.0.0:$PORT wsgi:app
"""

import os
import sys

# Маркер версии кода — должен быть в каждом релизе, чтобы при дебаге
# сразу видеть, какой код реально запущен.
print(f"[wsgi] BOOT marker={os.environ.get('WSGI_BOOT_MARKER', 'NO-MARKER')} pid={os.getpid()}", flush=True)

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

# Краткий лог: сколько маршрутов seller и auth_seller зарегистрировано.
import logging as _logging
_log = _logging.getLogger(__name__)
_seller_rules = [r for r in app.url_map.iter_rules() if r.endpoint.startswith("seller.")]
_auth_seller_rules = [r for r in app.url_map.iter_rules() if r.endpoint.startswith("auth_seller.")]
_log.info(f"=== seller blueprint: {len(_seller_rules)} rules, sample: {str(_seller_rules[0].rule) if _seller_rules else 'none'} ===")
_log.info(f"=== auth_seller blueprint: {len(_auth_seller_rules)} rules ===")
for _r in _auth_seller_rules:
    _log.info(f"    auth_seller route: {_r.rule} -> {_r.endpoint}")

# Главное: печатаем URL, который url_for генерирует для seller.dashboard.
# Если тут seller.marketplace.* — значит subdomain-режим всё ещё активен.
try:
    with app.test_request_context("/", base_url="https://marketplace.apps.hostim.app"):
        _dashboard_url = app.url_for("seller.dashboard")
        _log.info(f"=== url_for('seller.dashboard') = {_dashboard_url} ===")
except Exception as _e:
    _log.error(f"=== url_for test failed: {_e} ===")

_log.info(f"=== SERVER_NAME={app.config.get('SERVER_NAME')}, USE_SELLER_SUBDOMAIN={os.environ.get('USE_SELLER_SUBDOMAIN')}")
