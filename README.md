# Flask Marketplace (Маркетплейс)

Полнофункциональный маркетплейс на Python с использованием Flask.

## Структура проекта

```
marketplace/
├── app/
│   ├── __init__.py           # Фабрика приложений
│   ├── models/               # Модели базы данных
│   │   ├── __init__.py
│   │   ├── users.py          # Покупатели, продавцы, админы
│   │   ├── products.py       # Товары, категории, параметры
│   │   ├── orders.py         # Заказы, корзина, акции
│   │   └── communications.py # Сообщения, отзывы
│   ├── blueprints/           # Blueprints
│   │   ├── __init__.py
│   │   ├── main.py           # Публичная часть (покупатели)
│   │   ├── seller.py         # Панель продавца (поддомен)
│   │   ├── admin.py          # Админ-панель
│   │   ├── auth.py           # Авторизация
│   │   └── api.py            # API endpoints
│   ├── static/               # Статические файлы
│   │   ├── css/
│   │   └── js/
│   ├── templates/            # Jinja2 шаблоны
│   │   ├── base.html
│   │   ├── main/
│   │   ├── auth/
│   │   ├── seller/
│   │   ├── admin/
│   │   └── components/
│   └── utils/                # Утилиты
│       ├── __init__.py
│       ├── decorators.py     # Декораторы доступа
│       └── helpers.py        # Вспомогательные функции
├── config.py                 # Конфигурация
├── run.py                    # Точка входа
└── requirements.txt          # Зависимости
```

## Установка

1. Клонируйте репозиторий:
```bash
git clone <repository_url>
cd marketplace
```

2. Создайте виртуальное окружение:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows
```

3. Установите зависимости:
```bash
pip install -r requirements.txt
```

4. Настройте окружение (опционально):
```bash
cp .env.example .env
# Отредактируйте .env файл
```

5. Запустите приложение:
```bash
python run.py
```

## URL структура

### Главный домен (покупатели)
- `/` - Главная страница
- `/login` - Вход
- `/signup` - Регистрация
- `/catalogue/` - Каталог товаров
- `/catalogue/{id}` - Категория
- `/product/{id}` - Карточка товара
- `/search` - Поиск
- `/cart` - Корзина
- `/profile` - Личный кабинет
- `/profile/orders` - Заказы
- `/profile/favorite` - Избранное
- `/profile/messages` - Сообщения
- `/seller/{slug}` - Магазин продавца

### Поддомен продавца (seller.localhost)
- `seller.localhost/` - Панель продавца
- `seller.localhost/products` - Товары
- `seller.localhost/orders` - Заказы
- `seller.localhost/messages` - Сообщения
- `seller.localhost/analytics` - Аналитика
- `seller.localhost/delivery` - Доставка
- `seller.localhost/reviews` - Отзывы

### Админ-панель
- `/main_admin` - Главная страница
- `/main_admin/users` - Покупатели
- `/main_admin/sellers` - Продавцы
- `/main_admin/products/moderation` - Модерация товаров
- `/main_admin/orders` - Заказы
- `/main_admin/categories` - Категории
- `/main_admin/parameters` - Параметры
- `/main_admin/promotions` - Акции
- `/main_admin/deliveries` - Службы доставки
- `/main_admin/settings` - Настройки

## Тестовые аккаунты

### Абсолютный админ
- URL: `/main_admin/auth/login`
- Логин: `admin`
- Пароль: задаётся в `config.py` или переменной окружения

## Технологии

- **Backend**: Flask 3.0
- **База данных**: SQLite (dev) / PostgreSQL (prod)
- **ORM**: SQLAlchemy
- **Миграции**: Alembic
- **Шаблонизатор**: Jinja2
- **Аутентификация**: Flask-Login
- **Формы**: Flask-WTF

## Разработка

### Создание миграций
```bash
flask db migrate -m "description"
```

### Применение миграций
```bash
flask db upgrade
```

### Создание суперпользователя
```bash
flask shell
from app.models.users import Admin
admin = Admin(login='admin', email='admin@example.com')
admin.set_password('password')
db.session.add(admin)
db.session.commit()
```

## Лицензия

MIT
