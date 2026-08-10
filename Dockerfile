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

# Запуск gunicorn
# PORT, GUNICORN_WORKERS, APP_CONFIG — берутся из env Coolify
CMD ["sh", "-c", "gunicorn wsgi:app --bind 0.0.0.0:${PORT:-5000} --workers ${GUNICORN_WORKERS:-2} --timeout 60 --access-logfile - --error-logfile -"]
