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

# Запуск: создаём таблицы (db-init), потом приводим Alembic в актуальное
# состояние и догоняем дельту миграциями, потом gunicorn.
#
# Логика согласована с тем, что раньше в Dockerfile было 'flask db stamp heads':
#  - db.create_all() (db-init) уже создал все таблицы со всеми колонками
#    из текущих моделей.
#  - На проде в таблице alembic_version уже стоит head='d2e3f4a5b6c7',
#    потому что так исторически работало: 'db stamp heads' записывал head
#    без фактического выполнения миграций.
#  - Наша новая миграция 's1y2s3t4e5m6' (add system_sku) ещё не в stamp'е.
#  - Если сразу вызвать 'flask db upgrade head' — Alembic попытается
#    выполнить ВСЕ миграции, начиная с baseline, и упрётся в DuplicateColumn
#    на старых миграциях (apply_same_discount, order_id, status и т.п.),
#    потому что эти колонки уже созданы через create_all().
#
# Решение: 'flask db stamp d2e3f4a5b6c7' идемпотентно проставит эту ревизию
# (если уже она — no-op), а 'flask db upgrade head' накатит только нашу
# новую миграцию s1y2s3t4e5m6, которая как раз отсутствует в БД.
# После этого alembic_version = s1y2s3t4e5m6, и при следующих деплоях
# upgrade head будет no-op.
#
# Если задан RESET_DB=1 в env — сначала сбрасывает public schema (только для
# свежей БД). PORT, GUNICORN_WORKERS, APP_CONFIG — из env.
CMD ["sh", "-c", "if [ \"$RESET_DB\" = \"1\" ]; then FLASK_APP=wsgi.py flask reset-public-schema --yes; fi && FLASK_APP=wsgi.py flask db-init && FLASK_APP=wsgi.py flask fix-password-length && FLASK_APP=wsgi.py flask db stamp d2e3f4a5b6c7 && FLASK_APP=wsgi.py flask db upgrade head && if [ \"$PURGE_SELLER_SUBS_ON_BOOT\" = \"1\" ]; then FLASK_APP=wsgi.py flask clear-seller-subs --seller-id \"${PURGE_SELLER_ID:-1}\" --yes; fi && if [ -n \"$GRANT_TEST_TARIFF_SELLER_ID\" ]; then FLASK_APP=wsgi.py flask grant-test-tariff \"$GRANT_TEST_TARIFF_SELLER_ID\" --days \"${GRANT_TEST_TARIFF_DAYS:-30}\"; fi && gunicorn wsgi:app --bind 0.0.0.0:${PORT:-5000} --workers ${GUNICORN_WORKERS:-2} --timeout 60 --access-logfile - --error-logfile -"]
