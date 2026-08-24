# Marketplace — AGENTS.md

Долгосрочный контекст проекта для AI-агентов. Прочитай перед любыми изменениями.

## Stack & deploy

- Flask marketplace (sellers + buyers), Python 3.11, PostgreSQL.
- Deploy: Amvera Cloud (https://amvera.ru), сервис `MP` (тип — Docker, сборка из `Dockerfile`).
- Repo: https://github.com/mikemiers249-netizen/marketplace (branch: `main`).
- Домен (текущий): `https://mp-honest.amvera.io` (бесплатный поддомен Amvera).
- Исторически был `https://marketplace.apps.hostim.app` на Hostim — переехали из-за эфемерной ФС (фото не сохранялись между редеплоями). На Hostim **не возвращаемся**.
- **Seller работает в PATH-режиме `/seller/*`** (без поддомена).
- Workflow: правка в локальной копии → `git commit` → `git push origin main` → ждать редеплой Amvera. Если auto-deploy не сработал — вручную «Развернуть принудительно» в Amvera UI.

## PostgreSQL (Amvera managed cluster)

- Внутреннее DNS-имя: `amvera-honest-cnpg-marketplace-bd-rw` (не резолвится снаружи, только из контейнеров того же проекта `honest`).
- БД: `mp`, пользователь: `mpuser`, пароль задаётся в UI Amvera при создании кластера.
- `DATABASE_URI` в env сервиса `MP` имеет вид:
  `postgresql+psycopg2://mpuser:<пароль>@amvera-honest-cnpg-marketplace-bd-rw:5432/mp`
- Префикс `+psycopg2` обязателен (иначе psycopg2 падает `Can't load plugin: sqlalchemy.dialects:postgres`).
- Persistent storage для `/app/app/static/uploads` подключён через `persistenceMount` в настройках приложения Amvera. **Файлы пользователей переживают редеплой.**

## Env-переменные в Amvera (сервис `MP`)

```
APP_CONFIG=prod
SECRET_KEY=c8f3e2a1d4b5c6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2
DATABASE_URI=postgresql+psycopg2://mpuser:<PWD>@amvera-honest-cnpg-marketplace-bd-rw:5432/mp
MAIN_ADMIN_PASSWORD_HASH=<scrypt-хэш от admin123; см. историю чата или сгенерировать заново>
SESSION_COOKIE_SECURE=True
GUNICORN_WORKERS=2
PORT=3000
```

**Не задавать (специфично для Hostim и опасно для прода):**
- ~~`PURGE_SELLER_SUBS_ON_BOOT`~~ — стартовый hook в `Dockerfile` стирал ВСЕ подписки seller'а на каждом редеплое.
- ~~`RESET_DB=1`~~ — дропнет всю БД.
- ~~`GRANT_TEST_TARIFF_SELLER_ID`~~ — спам подписок на каждом рестарте.

## Учётки

- **Главный админ** (env-auth, не из БД): `https://mp-honest.amvera.io/main_admin/auth/login` — логин `admin`, пароль `admin123`.
  Хэш `MAIN_ADMIN_PASSWORD_HASH` от `admin123` (scrypt):
  `scrypt:32768:8:1$SJaazGSKNDog5nBW$bfac96176130e4b8da27ca8459075e464ca9816b11599cbc0c2fa1cd7d458242fe7c3f259ddbeb5675edb4492cc810c2aa02b64c36a59cc5161dd447efb6fa98`
  Если поменяешь — `python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('admin123'))"`.
- **Продавец Vibli** (id=1): `https://mp-honest.amvera.io/auth/seller/login` — `ronnie83@mail.ru` / `admin123`.
- **Покупатель** (id=1): `https://mp-honest.amvera.io/auth/login` — `ronnie83@mail.ru` / `admin123`.

## Ключевые файлы

| Путь | Что важно |
|------|-----------|
| `app/__init__.py` | factory; `_app_seller_tariff_state` на ~308-355 — обязательно на уровне app (before_request + context_processor), не blueprint |
| `app/blueprints/seller.py:22` | `bp = Blueprint('seller', __name__, subdomain='seller')` — hardcoded, но фактически работает в path-режиме |
| `app/blueprints/seller.py:409` | tariffs list filter `is_paid.is_(True) | (source='global_auto' & status='active')` |
| `app/blueprints/seller.py:643-658` | `_resolve_tariff_state` ветка `state='global'` |
| `app/blueprints/seller.py:829` | `tariff_activate_global` |
| `app/blueprints/seller.py:1231` | `product_new` — `from PIL import Image` под `try/except ImportError` |
| `app/blueprints/seller.py` (~235) | `_sanitize_html(value)` — вырезает `<script>`, `<iframe>`, on*=*, `javascript:` для описаний товара/магазина |
| `app/models/tariffs.py:163-172` | `is_active_now` — возвращает False для `is_paid=False` |
| `app/models/footer.py` | модель `FooterLink` (id, title, slug, content, display_mode, column, is_active, sort_order) |
| `app/templates/seller/layout.html:36` | `can_work = _st in ('paid', 'grace', 'global')` |
| `app/templates/main/product.html:244` | описание: `{{ product.description|safe }}` (HTML рендерится как HTML) |
| `app/templates/seller/product_form.html` | редактор описания с панелью кнопок HTML-форматирования |
| `app/templates/main/footer_page.html` | страница-рендер для FooterLink display_mode=page |
| `app/templates/admin/footer_links.html` + `footer_link_form.html` | CRUD по ссылкам подвала (info/support/additional × modal/page) |
| `app/templates/admin/settings.html` | форма настроек (включая social_links) — обязателен CSRF-токен! |
| `app/templates/base.html` | подвал с тремя колонками FooterLink, соцсети по центру (justify-content: center) |
| `app/static/css/main.css` | `.product-photo aspect-ratio: 1 / 1.5; object-fit: contain; background: #fff;` (плитки) |
| `app/static/css/main.css` (footer) | `.social-links { display:flex; flex-wrap:wrap; justify-content:center; }` |
| `app/static/css/product.css` | `.main-photo aspect-ratio: 1 / 1.5; object-fit: contain; background: #fff;` (страница товара) |
| `Dockerfile` | `CMD` запускает `db-init` + `fix-password-length` + `db stamp heads` + (опц.) `clear-seller-subs` + gunicorn |
| `docker-compose.yml` | для Coolify/локальной разработки; **на Amvera не используется** (там Compose-режим) |
| `amvera.yml` | конфиг Amvera (`build`, `containerPort`, `persistenceMount`) |
| `env.example` | шпаргалка по env-переменным без секретов |
| `migrations/versions/000000000000_baseline_schema.py` | пустой baseline, `down_revision=None` — **НЕ УДАЛЯТЬ** |
| `migrations/versions/d2e3f4a5b6c7_add_footer_links.py` | создаёт таблицу `footer_links` |

## CLI-команды (`app/commands.py`)

- `flask db-init` — `db.create_all()` (создаёт ВСЕ таблицы, включая `footer_links`)
- `flask fix-password-length` — ALTER `password_hash` на 256
- `flask reset-public-schema --yes` — DROP/CREATE public (только на свежей БД)
- `flask grant-test-tariff <seller_id> --days 30` — аварийная выдача global_auto подписки
- `flask clean-test-subs --seller-id N --yes` — удалить «тестовые» global_auto без transactions
- `flask clear-seller-subs --seller-id N --yes` — удалить ВСЕ подписки seller'а (для отладки)
- `flask clean-missing-photos --yes` — удалить ProductPhoto-записи, ссылающиеся на несуществующие файлы
- `flask seed-footer-links` — добавить дефолтные ссылки в подвал (about, delivery, privacy, offer, contacts, return, sitemap)

## ⛔ Hard "do not" list

- **НЕ добавлять** `RESET_DB=1` — дропнет всю БД
- **НЕ добавлять** `GRANT_TEST_TARIFF_SELLER_ID` — плодит мусорные подписки на каждом рестарте
- **НЕ добавлять** `PURGE_SELLER_SUBS_ON_BOOT` — стирает ВСЕ подписки seller'а на каждом редеплое
- **НЕ трогать** `_app_seller_tariff_state` в `app/__init__.py` — сломается sidebar у всех Seller'ов
- **НЕ удалять** `migrations/versions/000000000000_baseline_schema.py` — без неё `flask db stamp heads` не сработает
- **НЕ переключать** seller на subdomain-режим — SSL на поддомен не выдан
- **НЕ переключаться обратно на Hostim** — там эфемерная ФС, фото пропадают
- **НЕ использовать `Add-Content` в PowerShell** для редактирования Python-файлов — ломает UTF-8, в Python вылетает `SyntaxError: 'utf-8' codec can't decode byte 0xce`. Использовать `Edit` tool или `Write` (полная перезапись).

## Tariff UI (уже реализовано, не переделывать)

- Кнопка «Продлить» для глобальных правил ведёт на `/extend` (не `/renew`)
- Sidebar скрыт когда `g.tariff_state` is None — поэтому app-level регистрация критична
- Кнопка «Активировать тариф» видна только при `state='none'`

## Footer (подвал)

- Три колонки: «Информация» / «Служба поддержки» / «Дополнительно» (`FooterLink.COLUMN_*`).
- Способ показа: `display_mode=modal` (Bootstrap modal, fetch `/api/footer-link/<slug>`) или `display_mode=page` (отдельный URL `/page/<slug>`).
- Шаблон `base.html` рендерит колонки из `footer_links` (context processor `inject_global_variables` в `app/__init__.py`).
- Блок «Мы на маркетплейсах» **убран** из подвала (коммит `725bfd8`).
- Блок «Наши соцсети» управляется через `/main_admin/settings` → секция «Социальные сети (до 4 ссылок)», рендерится по центру.

## Catalog 500 — DISTINCT/= на JSON (исторически починено)

`product_parameters.value` имеет тип JSON в PostgreSQL, psycopg2 не умеет DISTINCT/= по json. В `app/blueprints/main.py`:
- `_get_parameter_values` использует `case((display_value.isnot(None), display_value), else_=func.cast(value, db.String))`.
- В `catalog` фильтр `ProductParameter.value == value` заменён на case-выражение.
- Импортируй `from sqlalchemy import case` в `main.py` — он уже там.

## Каталог фото (история, актуальное состояние)

- Плитки (`components/product_card.html`): `aspect-ratio: 1 / 1.5`, `object-fit: contain`, фон `#ffffff`. Фото с любой пропорцией влезает целиком, лишнее место — белое.
- Страница товара (`templates/main/product.html`, `.main-photo`): те же `1/1.5`, `contain`, белый фон.
- Миниатюры и card-gallery: `object-fit: contain; background: #ffffff`.
- Загрузка фото: `app/blueprints/seller.py` ~1295 (product_new) и ~1565 (product_edit). Защита от «висячих» записей: `db.session.add(photo)` только если `saved_ok` и `os.path.getsize > 0`.
- **Persistent storage `/app/app/static/uploads`** подключён в Amvera → настройки → persistenceMount. Файлы переживают редеплой.

## CSRF-токены в формах админки

Все формы, которые делают POST в админку, должны иметь `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>` внутри `<form>`. Иначе Flask-WTF отбивает 400 Bad Request.

Проверенные формы с CSRF: `admin/settings.html`, `admin/footer_link_form.html`, `admin/footer_links.html` (delete-form).

Если добавляешь новую форму — копируй CSRF-инпут.

## Распространённые ошибки

- **«SyntaxError: 'utf-8' codec can't decode byte 0xce»** при старте gunicorn — файл `*.py` в репо имеет битые байты (PowerShell-эффект). Откати файл (`git checkout <commit> -- <file>`) и примени изменения через `Edit` tool.
- **«NameError: name 'g' is not defined»** в `inject_global_variables` — забыт `from flask import g` в `app/__init__.py`.
- **«could not translate host name 'amvera-…-rw'»** — БД ещё не поднята (статус «Создаётся» в Amvera) или в `DATABASE_URI` лежит только хост, без `postgresql+psycopg2://user:pass@…:5432/db`.
- **«password authentication failed for user 'postgres'»** — после пересоздания БД Amvera генерирует новый пароль; в `DATABASE_URI` должен быть **свежий** пароль, не из Hostim.
- **«Can't load plugin: sqlalchemy.dialects:postgres»** — забыл `+psycopg2` в URI.
- **«NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres»** — см. выше.
- **400 Bad Request на /main_admin/settings** (или другой POST в админке) — отсутствует `{{ csrf_token() }}`.

## Редеплой на Amvera

Amvera иногда не подхватывает push автоматически. Если после `git push` через 2-3 минуты на проде всё ещё старая версия:
1. Зайди в Amvera → приложение `MP` → вкладка «Обзор» → «Развернуть принудительно».
2. Если не помогает — в настройках приложения переключи Build Type / очисти кеш (если есть такая опция) или удали и пересоздай приложение.

Если возникают постоянные проблемы с UTF-8 файлами в репо — **удалить приложение и пересоздать**: Amvera при пересоздании делает чистый клон, без локальных артефактов.

## Открытые вопросы (на 2026-08-24)

1. Toolbar форматирования HTML в `product_form.html` — проверить, что кнопки работают после редеплоя `2516f4d` (пока не подтверждено).
2. `display_mode=page` ссылки подвала рендерятся как `/page/<slug>` — выглядят «голыми» без хлебных крошек, добавить если попросят.
3. Проверить, что при `RESET_DB=1` сценарий в Amvera не сработает случайно.
4. **CSRF-проверка**: пройтись по всем формам в `app/templates/admin/*.html` и `app/templates/seller/*.html` и убедиться, что у каждой `<form method="POST">` есть `{{ csrf_token() }}` (выборочно проверил основные, но не все).
5. Долгая задержка редеплоя (~1-2 мин) — норма для Amvera, но иногда кеш образа не обновляется. Добавление no-op в `amvera.yml` или `Dockerfile` форсирует пересборку.
