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
app = create_app(config_name)
