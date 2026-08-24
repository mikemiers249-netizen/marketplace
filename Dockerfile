# Dockerfile для Flask Marketplace
# Используется вместо Nixpacks в Coolify, потому что Nixpacks-detect
# в текущей версии Coolify падает на нашем репо.

FROM python:3.11-slim

# Системные зависимости для psycopg2 и прочего
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — для кеширования слоя
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY . .

# Директория для загрузок и логов
RUN mkdir -p /app/app/static/uploads /app/logs

# Порт приложения
EXPOSE 5000

# Запуск: создаём таблицы (db-init), потом пытаемся накатить миграции до
# текущей head-ревизии (s1y2s3t4u5m6 — add system_sku); если upgrade
# не сработал (например, из-за исторического «Multiple head revisions»),
# fallback на stamp — ставит ревизию как применённую без выполнения
# upgrade(). Потом gunicorn.
#
# ВАЖНО: 'flask db stamp heads' / 'flask db upgrade head' НЕ использовать —
# в истории миграций есть baseline 000000000000 (висит как head, но его
# удалять нельзя по AGENTS.md), и 'heads' становится неоднозначным,
# Alembic падает с "Multiple head revisions". Указываем конкретную
# ревизию — самую новую, не baseline.
#
# ВАЖНО про порядок: 'db upgrade' ВСЕГДА должен идти РАНЬШЕ 'db stamp'.
# Если сначала stamp — Alembic запишет ревизию как применённую, и upgrade
# решит, что мигрировать нечего → upgrade() НЕ выполнится → колонки
# в БД не появятся, но код будет думать, что они есть (psycopg2
# UndefinedColumn). Именно поэтому fallback через '||' (если upgrade
# упал — выполнить stamp).
#
# При добавлении новой миграции: создать файл с down_revision = 's1y2s3t4u5m6',
# обновить s1y2s3t4u5m6 в двух местах ниже на новую ревизию.
# Если задан RESET_DB=1 в env — сначала сбрасывает public schema (только
# для свежей БД). PORT, GUNICORN_WORKERS, APP_CONFIG — из env Coolify.
CMD ["sh", "-c", "if [ \"$RESET_DB\" = \"1\" ]; then FLASK_APP=wsgi.py flask reset-public-schema --yes; fi && FLASK_APP=wsgi.py flask db-init && FLASK_APP=wsgi.py flask fix-password-length && FLASK_APP=wsgi.py flask db upgrade s1y2s3t4u5m6 || FLASK_APP=wsgi.py flask db stamp s1y2s3t4u5m6 && if [ \"$PURGE_SELLER_SUBS_ON_BOOT\" = \"1\" ]; then FLASK_APP=wsgi.py flask clear-seller-subs --seller-id \"${PURGE_SELLER_ID:-1}\" --yes; fi && if [ -n \"$GRANT_TEST_TARIFF_SELLER_ID\" ]; then FLASK_APP=wsgi.py flask grant-test-tariff \"$GRANT_TEST_TARIFF_SELLER_ID\" --days \"${GRANT_TEST_TARIFF_DAYS:-30}\"; fi && gunicorn wsgi:app --bind 0.0.0.0:${PORT:-5000} --workers ${GUNICORN_WORKERS:-2} --timeout 60 --access-logfile - --error-logfile -"]
