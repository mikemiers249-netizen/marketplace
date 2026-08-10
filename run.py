#!/usr/bin/env python3
"""
Точка входа для запуска приложения маркетплейса.
"""

import os
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла
load_dotenv()

from app import create_app

# Создание приложения
app = create_app()

if __name__ == '__main__':
    # В режиме разработки Flask dev-сервер не поддерживает виртуальные
    # сабдомены на 127.0.0.1/IP-адресах: при заданном SERVER_NAME он
    # сравнивает его с заголовком Host и отдаёт 404 на любое несовпадение.
    # Без SERVER_NAME subdomain matching просто отключается, и сабдомен
    # seller.* перестаёт работать. Решаем это так: выделяем host из
    # SERVER_NAME (если он задан) и слушаем только на нём, сохраняя
    # subdomain matching.
    server_name = app.config.get('SERVER_NAME')
    listen_host = '0.0.0.0'
    listen_port = int(os.getenv('PORT', 5000))

    if server_name and app.config.get('DEBUG'):
        # Берём хост из SERVER_NAME (отрезаем :port, если есть)
        configured_host = server_name.split(':')[0] or '0.0.0.0'
        # Если это реальный DNS-имя (не IP), слушаем на 0.0.0.0, но
        # Werkzeug всё равно будет матчить host по SERVER_NAME.
        listen_host = '0.0.0.0' if configured_host not in ('127.0.0.1', 'localhost') else configured_host

    app.run(
        host=listen_host,
        port=listen_port,
        debug=False,
        use_reloader=False,
    )
