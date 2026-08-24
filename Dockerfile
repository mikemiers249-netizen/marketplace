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

# Запуск: создаём таблицы (db-init), потом идемпотентный DDL для новых
# колонок (ADD COLUMN IF NOT EXISTS), потом stamp на текущий head
# (s1y2s3t4u5m6 — add system_sku), потом gunicorn.
#
# ПОЧЕМУ ТАК, А НЕ 'flask db upgrade':
#  - 'db upgrade' зависит от alembic_version. Если он уже стоит на
#    s1y2s3t4u5m6 (после прошлого stamp'а), upgrade() НЕ выполнится,
#    а колонки может не быть → 500 UndefinedColumn.
#  - 'db stamp' сам по себе не создаёт колонки.
#  - Решение: руками выполнить идемпотентный DDL (ADD COLUMN IF NOT EXISTS
#    работает в PostgreSQL 9.6+), потом stamp — Alembic считает, что
#    миграция применена, и в следующий раз не накатывает.
#
# ВАЖНО: 'flask db stamp heads' / 'flask db upgrade head' НЕ использовать —
# в истории миграций есть baseline 000000000000 (висит как head, но его
# удалять нельзя по AGENTS.md), и 'heads' становится неоднозначным,
# Alembic падает с "Multiple head revisions". Указываем конкретную
# ревизию — самую новую, не baseline.
#
# При добавлении новой миграции: (1) создать файл с down_revision =
# 's1y2s3t4u5m6'; (2) добавить идемпотентный DDL в строку python -c ниже;
# (3) обновить s1y2s3t4u5m6 в строке flask db stamp на новую ревизию.
# Если задан RESET_DB=1 в env — сначала сбрасывает public schema (только
# для свежей БД). PORT, GUNICORN_WORKERS, APP_CONFIG — из env Coolify.
CMD ["sh", "-c", "if [ \"$RESET_DB\" = \"1\" ]; then FLASK_APP=wsgi.py flask reset-public-schema --yes; fi && FLASK_APP=wsgi.py flask db-init && FLASK_APP=wsgi.py flask fix-password-length && FLASK_APP=wsgi.py python -c 'from app import db; from sqlalchemy import text; db.session.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS system_sku VARCHAR(64)")); db.session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_products_system_sku ON products(system_sku)")); db.session.commit()' 2>/dev/null; FLASK_APP=wsgi.py flask db stamp s1y2s3t4u5m6 && if [ \"$PURGE_SELLER_SUBS_ON_BOOT\" = \"1\" ]; then FLASK_APP=wsgi.py flask clear-seller-subs --seller-id \"${PURGE_SELLER_ID:-1}\" --yes; fi && if [ -n \"$GRANT_TEST_TARIFF_SELLER_ID\" ]; then FLASK_APP=wsgi.py flask grant-test-tariff \"$GRANT_TEST_TARIFF_SELLER_ID\" --days \"${GRANT_TEST_TARIFF_DAYS:-30}\"; fi && gunicorn wsgi:app --bind 0.0.0.0:${PORT:-5000} --workers ${GUNICORN_WORKERS:-2} --timeout 60 --access-logfile - --error-logfile -"]
