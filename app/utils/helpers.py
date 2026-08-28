"""
Вспомогательные функции и утилиты.
"""

import os
import json
import uuid
from datetime import datetime
from flask import url_for, flash
from werkzeug.utils import secure_filename
from app import db, cache
from app.models.communications import Settings


def allowed_file(filename, allowed_extensions=None):
    """
    Проверка допустимого расширения файла.
    
    Args:
        filename: Имя файла
        allowed_extensions: Множество допустимых расширений (опционально)
    
    Returns:
        bool: True если файл допустим
    """
    if allowed_extensions is None:
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    return ext in allowed_extensions


def upload_file(file, folder, allowed_extensions=None, custom_filename=None):
    """
    Загрузка файла на сервер.
    
    Args:
        file: Объект файла из Flask
        folder: Папка для сохранения (относительно UPLOAD_FOLDER)
        allowed_extensions: Допустимые расширения
        custom_filename: Кастомное имя файла
    
    Returns:
        str: Путь к сохранённому файлу или None при ошибке
    """
    from flask import current_app
    
    if not file or not allowed_file(file.filename, allowed_extensions):
        return None
    
    # Создание директории
    upload_path = os.path.join(current_app.config['UPLOAD_FOLDER'], folder)
    os.makedirs(upload_path, exist_ok=True)
    
    # Генерация имени файла
    if custom_filename:
        filename = f"{custom_filename}.{file.filename.rsplit('.', 1)[-1].lower()}"
    else:
        filename = f"{uuid.uuid4().hex}.{file.filename.rsplit('.', 1)[-1].lower()}"
    
    # Безопасное имя файла
    filename = secure_filename(filename)
    
    # Сохранение
    file_path = os.path.join(upload_path, filename)
    file.save(file_path)
    
    # Относительный путь для хранения в БД
    return os.path.join('uploads', folder, filename)


def delete_file(file_path):
    """
    Удаление файла.
    
    Args:
        file_path: Относительный путь к файлу
    
    Returns:
        bool: True при успехе
    """
    from flask import current_app
    
    full_path = os.path.join(current_app.config['UPLOAD_FOLDER'], '..', '..', file_path)
    full_path = os.path.normpath(full_path)
    
    try:
        if os.path.exists(full_path):
            os.remove(full_path)
            return True
    except Exception:
        pass
    
    return False


def format_price(price, currency='₽'):
    """
    Форматирование цены.
    
    Args:
        price: Числовое значение цены
        currency: Символ валюты
    
    Returns:
        str: Отформатированная цена
    """
    try:
        price = float(price)
        return f"{price:,.2f} {currency}".replace(',', ' ')
    except (ValueError, TypeError):
        return f"0.00 {currency}"


def compute_product_promotion_info(product):
    """
    Единый источник правды по акциям для карточки/плитки товара.

    Возвращает dict:
        {
            'has_promotion': bool,
            'percent': int|None,         # эффективный % скидки
            'old_price': float|None,     # цена ДО скидки
            'final_price': float|None,   # цена ПОСЛЕ скидки (= product.price если нет)
            'scheme': str|None,          # 'discount' / 'second_with_discount' / '1+1' / 'gift' / 'product_discount'
            'scheme_label': str|None,    # человекочитаемое название схемы
            'name': str|None,            # название акции
        }

    Приоритет источников скидки (по убыванию «информативности» для покупателя):
      1) current_discount (если > 0) — продавец выставил скидку на товар.
      2) Классическая акция (PromotionProduct) со схемой discount.
      3) Seller-уровневая акция (SellerPromotion) со схемой discount /
         second_with_discount (по подключённому шаблону).
      4) N+1 и gift — отдельная метка без процента (показывается как «2 по цене 1» и т.п.).
    """
    if product is None:
        return {
            'has_promotion': False, 'percent': None, 'old_price': None,
            'final_price': None, 'scheme': None, 'scheme_label': None, 'name': None,
        }

    final_price = round(float(product.price or 0), 2)
    info = {
        'has_promotion': False,
        'percent': None,
        'old_price': None,
        'final_price': final_price,
        'scheme': None,
        'scheme_label': None,
        'name': None,
    }

    # 1) current_discount товара — самый частый и самый дешёвый случай.
    try:
        cd = int(product.current_discount or 0)
    except (TypeError, ValueError):
        cd = 0
    if cd > 0:
        if cd < 100:
            old_price = final_price
            final_price = round(final_price * (1 - cd / 100), 2)
        else:
            old_price = final_price
            final_price = 0.0
        info.update({
            'has_promotion': True,
            'percent': cd,
            'old_price': old_price,
            'final_price': final_price,
            'scheme': 'product_discount',
            'scheme_label': 'Скидка на товар',
            'name': None,
        })
        return info

    # 2) Классическая акция (PromotionProduct) со схемой discount.
    try:
        from app.models.orders import Promotion, PromotionProduct, SellerPromotion
        from datetime import datetime
        now = datetime.utcnow()
        link = (
            PromotionProduct.query
            .filter_by(product_id=product.id)
            .join(Promotion, Promotion.id == PromotionProduct.promotion_id)
            .filter(
                Promotion.status == 'active',
                Promotion.scheme == 'discount',
            )
            .first()
        )
        if link is not None:
            promo = link.promotion
            # Если у этого продавца для данной акции есть SellerPromotion
            # и она отключена (is_active=False) — игнорируем связь.
            # Без этой проверки после «отключения» шаблонной акции
            # кнопкой «Отключить» плашка «Акция» остаётся на карточках,
            # потому что запись в promotion_products никуда не девается.
            seller_link = SellerPromotion.query.filter_by(
                promotion_id=promo.id, seller_id=product.seller_id
            ).first()
            if seller_link is not None and not seller_link.is_active:
                link = None
        if link is not None:
            promo = link.promotion
            # Проверяем даты вручную (Promotion.is_active это property, тут дёргать ORM-объект).
            if (not promo.start_date or promo.start_date <= now) and (not promo.end_date or promo.end_date >= now):
                # Процент: per-item override -> общий discount_percent
                percent = link.discount_percent if link.discount_percent is not None else (promo.discount_percent or 0)
                if percent and percent > 0 and percent < 100:
                    old_price = final_price
                    final_price = round(final_price * (1 - percent / 100), 2)
                    info.update({
                        'has_promotion': True,
                        'percent': int(percent),
                        'old_price': old_price,
                        'final_price': final_price,
                        'scheme': 'discount',
                        'scheme_label': 'Скидка',
                        'name': promo.name,
                    })
                    return info
                # Если у этой акции нет числового процента, но есть название — покажем как «Акция».
                if promo.name:
                    info.update({
                        'has_promotion': True,
                        'percent': None,
                        'old_price': None,
                        'final_price': final_price,
                        'scheme': 'discount',
                        'scheme_label': 'Скидка',
                        'name': promo.name,
                    })
                    return info
    except Exception:
        # Не валим рендер страницы, если что-то с ORM пошло не так.
        pass

    # 3) Seller-уровневая акция (подключённый шаблон).
    try:
        from app.models.orders import Promotion, SellerPromotion
        from datetime import datetime
        now = datetime.utcnow()
        link = (
            SellerPromotion.query
            .filter_by(seller_id=product.seller_id, is_active=True)
            .join(Promotion, Promotion.id == SellerPromotion.promotion_id)
            .filter(
                Promotion.status == 'active',
                Promotion.scheme.in_(['discount', 'second_with_discount']),
            )
            .first()
        )
        if link is not None:
            promo = link.promotion
            if (not promo.start_date or promo.start_date <= now) and (not promo.end_date or promo.end_date >= now):
                if promo.scheme == 'discount':
                    percent = link.override_discount_percent if link.override_discount_percent is not None else (promo.discount_percent or 0)
                    if percent and percent > 0 and percent < 100:
                        old_price = final_price
                        final_price = round(final_price * (1 - percent / 100), 2)
                        info.update({
                            'has_promotion': True,
                            'percent': int(percent),
                            'old_price': old_price,
                            'final_price': final_price,
                            'scheme': 'discount',
                            'scheme_label': 'Скидка',
                            'name': promo.name,
                        })
                        return info
                elif promo.scheme == 'second_with_discount':
                    percent = link.override_discount_percent if link.override_discount_percent is not None else (promo.discount_percent or 0)
                    if percent and percent > 0:
                        # «каждый второй по минимальной цене» — для одной штуки
                        # цена не меняется, поэтому числовой old_price не показываем.
                        # Просто помечаем, что акция действует.
                        info.update({
                            'has_promotion': True,
                            'percent': None,
                            'old_price': None,
                            'final_price': final_price,
                            'scheme': 'second_with_discount',
                            'scheme_label': 'Каждый 2-й со скидкой',
                            'name': promo.name,
                        })
                        return info
    except Exception:
        pass

    # 4) N+1 / gift как «Акция» без числовой скидки.
    try:
        from app.models.orders import Promotion, PromotionProduct, SellerPromotion
        from datetime import datetime
        now = datetime.utcnow()
        link = (
            PromotionProduct.query
            .filter_by(product_id=product.id)
            .join(Promotion, Promotion.id == PromotionProduct.promotion_id)
            .filter(
                Promotion.status == 'active',
                Promotion.scheme.in_(['1+1', '2+1', '3+1', 'one_plus_one', 'two_plus_one', 'three_plus_one', 'gift']),
            )
            .first()
        )
        if link is not None:
            promo = link.promotion
            # Симметрично блоку #2: если у продавца товара есть
            # SellerPromotion для этой акции и она отключена — не показываем.
            seller_link = SellerPromotion.query.filter_by(
                promotion_id=promo.id, seller_id=product.seller_id
            ).first()
            if seller_link is not None and not seller_link.is_active:
                link = None
        if link is not None:
            promo = link.promotion
            if (not promo.start_date or promo.start_date <= now) and (not promo.end_date or promo.end_date >= now):
                scheme = promo.scheme
                scheme_labels = {
                    '1+1': '1+1',
                    '2+1': '2+1',
                    '3+1': '3+1',
                    'one_plus_one': '1+1',
                    'two_plus_one': '2+1',
                    'three_plus_one': '3+1',
                    'gift': 'Подарок',
                }
                info.update({
                    'has_promotion': True,
                    'percent': None,
                    'old_price': None,
                    'final_price': final_price,
                    'scheme': scheme,
                    'scheme_label': scheme_labels.get(scheme, 'Акция'),
                    'name': promo.name,
                })
                return info
    except Exception:
        pass

    return info


def format_date(date, format='%d.%m.%Y'):
    """
    Форматирование даты.
    
    Args:
        date: Объект datetime или строка
        format: Формат вывода
    
    Returns:
        str: Отформатированная дата
    """
    if isinstance(date, str):
        try:
            date = datetime.fromisoformat(date.replace('Z', '+00:00'))
        except ValueError:
            return date
    
    if not date:
        return ''
    
    return date.strftime(format)


def format_datetime(date, format='%d.%m.%Y %H:%M'):
    """
    Форматирование даты и времени.
    
    Args:
        date: Объект datetime или строка
        format: Формат вывода
    
    Returns:
        str: Отформатированная дата и время
    """
    return format_date(date, format)


def time_ago(date):
    """
    Относительное время (например, "5 минут назад").
    
    Args:
        date: Объект datetime
    
    Returns:
        str: Относительное время
    """
    if not date:
        return ''
    
    now = datetime.utcnow()
    delta = now - date
    
    seconds = delta.total_seconds()
    
    if seconds < 60:
        return 'только что'
    elif seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} мин. назад"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} ч. назад"
    elif seconds < 604800:
        days = int(seconds / 86400)
        return f"{days} дн. назад"
    else:
        return format_date(date)


def pluralize(count, forms):
    """
    Склонение существительных по числительному.
    
    Args:
        count: Число
        forms: Кортеж из трёх форм (1, 2, 5)
            Например: ('товар', 'товара', 'товаров')
    
    Returns:
        str: Правильная форма слова
    """
    if isinstance(count, float):
        count = int(count)
    
    mod10 = count % 10
    mod100 = count % 100
    
    if 11 <= mod100 <= 19:
        return forms[2]
    elif mod10 == 1:
        return forms[0]
    elif 2 <= mod10 <= 4:
        return forms[1]
    else:
        return forms[2]


def get_breadcrumbs(category, product=None):
    """
    Генерация хлебных крошек.
    
    Args:
        category: Объект Category
        product: Опционально объект Product
    
    Returns:
        list: Список словарей с name и url
    """
    breadcrumbs = []
    
    # Родительские категории
    if category.parent:
        breadcrumbs.extend(get_breadcrumbs(category.parent))
    
    # Текущая категория
    breadcrumbs.append({
        'name': category.name,
        'url': url_for('main.catalog', category_id=category.id)
    })
    
    # Товар
    if product:
        breadcrumbs.append({
            'name': product.name,
            'url': url_for('main.product', product_id=product.id)
        })
    
    return breadcrumbs


def generate_order_number():
    """
    Генерация уникального номера заказа.
    
    Returns:
        str: Номер заказа в формате ГГММДДNNNNNN
    """
    import random
    import string
    
    prefix = datetime.utcnow().strftime('%y%m%d')
    random_part = ''.join(random.choices(string.digits, k=6))
    return f"{prefix}{random_part}"


def cleanhtml(value):
    """
    «Стерилизация» HTML-описания товара/магазина.

    Применяется:
      - при сохранении (в _sanitize_html в seller.py),
      - при выводе в публичной части (Jinja-фильтр |cleanhtml).

    Что вырезается:
      1) Опасные блок-теги целиком с содержимым:
         <script>, <iframe>, <object>, <embed>, <style>, <link>,
         <meta>, <form>, <applet>, <frame>, <frameset>, <noframes>,
         <noscript>, <base>, <svg>*, <math>*.
      2) Все атрибуты style="..." и class="..." (визуальные стили
         и имена CSS-классов) — продавцу не нужны, а злоумышленнику
         позволяют встроить визуально-обманчивое оформление.
      3) Все атрибуты id="..." — нам не нужны, иначе можно
         переопределить элемент страницы (id=header, id=footer, ...).
      4) on*=* атрибуты (onclick, onerror, onload, onmouseover, ...).
      5) href="javascript:..." / src="javascript:..." / data: URL →
         заменяем на "#".
      6) Все ссылки на внешние ресурсы:
         href="http(s)://...", src="http(s)://..." (а также
         href="//cdn...", href="ftp://...") — вырезаем целиком атрибут.
         Продавцу можно оставлять ТОЛЬКО относительные ссылки
         (href="/page/...") и якоря (href="#section").
         Внешние картинки в описании товара запрещены — фото
         загружаются через форму товара в /static/uploads/.
      7) PHP/Python/SQL/Shell/JavaScript-блоки (даже если они
         проскочили в тексте, не как теги):
         - <?php ... ?> (PHP)
         - <% ... %> (ASP/JSP/ERB)
         - {{ ... }} и {% ... %} (Jinja)
         - SELECT/INSERT/UPDATE/DELETE/DROP/CREATE/ALTER с ; (SQL)
         - import os / from ... import / def ...(  (Python)
         - eval( / exec( / system( (выполнение команд)
         Эти шаблоны редко нужны в описании товара; режем превентивно.
      8) <p class> / <p class=""> → <p>  (битые теги из копипаста).
      9) </br>, <br/> → <br> ; </hr> → "".
     10) Гolый '&' (без валидного entity) → &amp;.

    Возвращает безопасный HTML, который гарантированно не сдвинет
    DOM-структуру страницы и не даст возможности для XSS/инъекций.
    """
    import re
    if not value:
        return ''
    text = str(value)

    # 1) Опасные блок-теги целиком (содержимое тоже вырезаем)
    DANGEROUS = (
        'script|iframe|object|embed|style|link|meta|form|applet|'
        'frame|frameset|noframes|noscript|base|svg|math|template|slot'
    )
    text = re.sub(
        rf'<\s*({DANGEROUS})\b[^>]*>.*?<\s*/\s*\1\s*>',
        '',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # 1a) Самозакрывающиеся/без содержимого
    text = re.sub(
        rf'<\s*({DANGEROUS})\b[^>]*/?\s*>',
        '',
        text,
        flags=re.IGNORECASE,
    )

    # 2) style="..." и class="..."  (любые кавычки)
    text = re.sub(r'\s+style\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+style\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+class\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+class\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)

    # 2a) style/class без значения (style=  /  class=)
    text = re.sub(r'\s+style\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+style\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)

    # 3) id="..."  (чужой id может переопределить элементы страницы)
    text = re.sub(r'\s+id\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+id\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)

    # 4) on*=* (любые кавычки)
    text = re.sub(r'\s+on[a-z]+\s*=\s*"[^"]*"', '', text, flags=re.IGNORECASE)
    text = re.sub(r"\s+on[a-z]+\s*=\s*'[^']*'", '', text, flags=re.IGNORECASE)

    # 5) javascript:/vbscript:/data: → "#"
    text = re.sub(
        r'(href|src)\s*=\s*"(?:\s*)(?:javascript|vbscript|data)\s*:[^"]*"',
        r'\1="#"',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(href|src)\s*=\s*'(?:\s*)(?:javascript|vbscript|data)\s*:[^']*'",
        r'\1="#"',
        text,
        flags=re.IGNORECASE,
    )

    # 6) Внешние ссылки и любые src/href с абсолютным URL → вырезаем
    #    атрибут целиком. Оставляем только относительные ("/...", "#...")
    #    и пустые значения.
    def _strip_external(match):
        attr = match.group(1)
        quote = match.group(2)
        value = match.group(3)
        v = (value or '').strip().lower()
        # Разрешаем: относительные пути, якоря, пусто, mailto:
        if (not v
                or v.startswith('/')
                or v.startswith('#')
                or v.startswith('mailto:')
                or v.startswith('tel:')):
            return match.group(0)  # оставляем как есть
        return f' {attr}={quote}#{quote}'  # заменяем внешний URL на #

    text = re.sub(
        r'\s+(href|src)\s*=\s*(["\'])([^"\']*)\2',
        _strip_external,
        text,
        flags=re.IGNORECASE,
    )

    # 7) Блоки кода (даже в тексте, не в тегах) — вырезаем целиком.
    # 7a) <?php ... ?> (PHP)
    text = re.sub(
        r'<\?(?:php)?\b.*?\?>',
        '',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # 7b) <% ... %> (ASP / JSP / ERB)
    text = re.sub(r'<%.*?%>', '', text, flags=re.DOTALL)
    # 7c) Jinja {{ ... }} / {% ... %}  — несколько проходов, потому что
    #     ленивый квантификатор .*? в одном проходе оставляет хвост
    #     "1{% endif %}" если встретил первый "{% if x %}".
    for _ in range(5):
        new_text = re.sub(r'\{\{[\s\S]*?\}\}', '', text)
        new_text = re.sub(r'\{%[\s\S]*?%\}', '', new_text)
        if new_text == text:
            break
        text = new_text
    # 7d) SQL-инструкции (только если есть ';' в конце — простой
    #     детектор, чтобы не резать обычные слова вроде "delete file")
    text = re.sub(
        r'\b(?:select|insert|update|delete|drop|create|alter|truncate|'
        r'grant|revoke)\b[^;{<>}]*;',
        '',
        text,
        flags=re.IGNORECASE,
    )
    # 7e) Python: import / from / def в начале строки
    text = re.sub(
        r'^\s*(?:import\s+\S+|from\s+\S+\s+import\s+\S+|def\s+\w+\s*\()',
        '',
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    # 7f) eval( / exec( / system( / passthru(  (выполнение команд)
    text = re.sub(
        r'\b(?:eval|exec|system|passthru|shell_exec|popen|proc_open)\s*\(',
        '(',
        text,
        flags=re.IGNORECASE,
    )

    # 8) Битые теги <tag class> / <tag class="">  /  <tag class=foo>
    #    Любой class=… вырезаем (мы уже стёрли class="…" в п.2, но
    #    на всякий случай — class без кавычек / с пробелом в значении).
    text = re.sub(
        r'<\s*(p|div|span|li|ul|ol|h[1-6])\s+class\s*=\s*"\s*"\s*>',
        r'<\1>',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"<\s*(p|div|span|li|ul|ol|h[1-6])\s+class\s*=\s*'\s*'\s*>",
        r'<\1>',
        text,
        flags=re.IGNORECASE,
    )
    # <tag class> / <tag class=...>  →  <tag>
    text = re.sub(
        r'<\s*(p|div|span|li|ul|ol|h[1-6])\s+class(?:=[^\s>]*)?\s*>',
        r'<\1>',
        text,
        flags=re.IGNORECASE,
    )

    # 9) Нормализация br/hr
    text = re.sub(r'<\s*br\s*/?\s*>', '<br>', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*/\s*br\s*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*hr\s*/?\s*>', '<hr>', text, flags=re.IGNORECASE)
    text = re.sub(r'<\s*/\s*hr\s*>', '', text, flags=re.IGNORECASE)

    # 10) Голый &  →  &amp;  (если не валидный entity)
    text = re.sub(
        r'&(?!(?:amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-fA-F]+);)',
        '&amp;',
        text,
    )

    return text


def slugify(text):
    """
    Транслитерация и создание URL-слага.
    
    Args:
        text: Текст для преобразования
    
    Returns:
        str: Slug
    """
    from slugify import slugify as _slugify
    return _slugify(text)


@cache.memoize(timeout=3600)
def get_main_admin_config():
    """
    Получение конфигурации главного админа (кешировано).
    
    Returns:
        dict: Конфигурация из БД или настройки по умолчанию
    """
    config = {
        'social_links': [],
        'marketplace_links': [],
        'footer_info_pages': [],
        'footer_support_pages': [],
        'footer_extra_pages': [],
        'penalty_percent': 10,
        'penalty_grace_days': 3,
        'max_penalty_per_day': 500,
        'min_penalty_per_day': 50,
    }
    
    # Загрузка из БД
    for key in config.keys():
        value = Settings.get(key)
        if value is not None:
            config[key] = value
    
    return config


def calculate_delivery_cost(weight, volume, delivery_service_id, address):
    """
    Расчёт стоимости доставки через API службы доставки.
    
    Args:
        weight: Вес в кг
        volume: Объём в м³
        delivery_service_id: ID службы доставки
        address: Адрес доставки или код ПВЗ
    
    Returns:
        float: Стоимость доставки
    """
    from app.models.users import DeliveryService
    
    service = db.session.get(DeliveryService, delivery_service_id)
    if not service:
        return 0
    
    # Вызов API модуля
    api_module = service.api_module
    if api_module:
        try:
            module = __import__(f'app.integrations.{api_module}', fromlist=['calculate'])
            return module.calculate(weight, volume, address)
        except (ImportError, AttributeError):
            pass
    
    # Заглушка - базовый расчёт
    base_price = 300  # Минимальная цена
    weight_price = max(0, (weight - 1) * 50)  # +50₽ за каждый кг > 1
    volume_price = max(0, (volume - 0.01) * 100)  # +100₽ за каждые 0.01 м³
    
    return round(base_price + weight_price + volume_price, 2)


def apply_promotion_to_cart(cart_items, promotion):
    """
    Применение акции к товарам в корзине.

    Поддерживает схемы:
      • seller-scope (second_with_discount) — считаем по корзине продавца целиком
        (Promotion.calculate_cart_discount), без записей в PromotionProduct.
      • N+1 (one_plus_one / two_plus_one / three_plus_one) — шаблонная
        с выбором товаров; считаем по корзине продавца, берём только
        участвующие товары (PromotionProduct).
      • классическая (per-item через PromotionProduct) — старая логика.

    Args:
        cart_items: Список объектов CartItem
        promotion: Объект Promotion

    Returns:
        dict: {'discount': сумма скидки, 'items': товары со скидкой}
    """
    if not promotion or not promotion.is_active:
        return {'discount': 0.0, 'items': []}

    # Seller-scope или N+1: считаем по корзине продавца
    if (getattr(promotion, 'is_seller_scope', False)
            or promotion.scheme in ('one_plus_one', 'two_plus_one', 'three_plus_one')):
        # Группируем по продавцам и суммируем скидки
        from collections import defaultdict
        by_seller = defaultdict(list)
        for it in cart_items:
            if it.product and it.product.seller_id:
                by_seller[it.product.seller_id].append(it)

        total_discount = 0.0
        applicable_items = []
        for seller_id, items in by_seller.items():
            result = promotion.calculate_cart_discount(items, seller_id)
            total_discount += result['discount']
            percent = promotion.get_effective_discount_percent(seller_id) or 0
            for it in result['discounted_items']:
                applicable_items.append({
                    'item': it,
                    'discount': round(it.product.price * percent / 100, 2)
                })
        return {
            'discount': round(total_discount, 2),
            'items': applicable_items,
        }

    # Классическая схема: по записям PromotionProduct
    total_discount = 0
    applicable_items = []

    for item in cart_items:
        is_promotional = item.product.promotion_items.filter(
            PromotionProduct.promotion_id == promotion.id
        ).first()

        if is_promotional:
            discount, _ = promotion.calculate_discount(item.product.price, item.quantity)
            total_discount += discount
            applicable_items.append({
                'item': item,
                'discount': discount
            })

    return {
        'discount': round(total_discount, 2),
        'items': applicable_items
    }


def get_cart_discount_breakdown(cart_total):
    """
    Возвращает dict {cart_item_id: discount_amount} из результата get_cart_total.

    Используется при оформлении заказа, чтобы проставить per-item скидку
    в OrderItem.price_at_order.
    """
    if not cart_total or not isinstance(cart_total, dict):
        return {}
    breakdown = {}
    for entry in cart_total.get('items', []):
        item = entry.get('item')
        if not item:
            continue
        # Идентифицируем cart_item по (buyer_id, product_id) — у CartItem composite PK
        key = (item.buyer_id, item.product_id)
        breakdown[key] = breakdown.get(key, 0.0) + float(entry.get('discount', 0.0))
    return breakdown


def get_cart_total(buyer_id, use_promotion=True, promotion_id=None):
    """
    Расчёт общей суммы корзины.

    Правила применения акций:
      • Скидки НЕ суммируются. На каждую позицию корзины применяется ровно
        одна — та, что даёт максимальную скидку в рублях на эту позицию.
      • Источники скидки:
          1. current_discount самого товара (если выставлен продавцом);
          2. классические discount-акции, в которые товар добавлен через
             PromotionProduct (с учётом per-item override или общего процента
             в зависимости от apply_same_discount);
          3. seller-scope акции (second_with_discount), подключённые
             продавцом корзины.
      • Явно переданный promotion_id форсирует применение именно этой акции
        (используется, например, при оформлении заказа с конкретной акцией);
        в этом режиме остальные источники игнорируются.

    Args:
        buyer_id: ID покупателя
        use_promotion: Применять ли акции
        promotion_id: Если задан — применить только эту акцию (для UI
                      оформления заказа с конкретной выбранной акцией)

    Returns:
        dict: {
            'subtotal': сумма товаров,
            'discount': скидка,
            'total': итого,
            'items': [{'item': cart_item, 'discount': per_item_discount}, ...],
            'applied_promotions': [{'promotion_id', 'name', 'discount'}, ...],
        }
    """
    from app.models.orders import (
        CartItem, Promotion, PromotionProduct, SellerPromotion,
    )
    from collections import defaultdict
    from datetime import datetime as _dt

    cart_items = CartItem.query.filter_by(buyer_id=buyer_id).all()

    subtotal = sum(item.total_price for item in cart_items)

    if not use_promotion or not cart_items:
        return {
            'subtotal': subtotal,
            'discount': 0.0,
            'total': round(subtotal, 2),
            'items': [],
            'applied_promotions': [],
        }

    # Режим форсированной акции (например, конкретный промокод на checkout).
    if promotion_id:
        promotion = db.session.get(Promotion, promotion_id)
        if promotion and promotion.is_active:
            result = apply_promotion_to_cart(cart_items, promotion)
            return {
                'subtotal': subtotal,
                'discount': round(result['discount'], 2),
                'total': round(subtotal - result['discount'], 2),
                'items': result['items'],
                'applied_promotions': [{
                    'promotion_id': promotion.id,
                    'name': promotion.name,
                    'discount': round(result['discount'], 2),
                }],
            }
        return {
            'subtotal': subtotal,
            'discount': 0.0,
            'total': round(subtotal, 2),
            'items': [],
            'applied_promotions': [],
        }

    # Авто-режим: для каждой позиции выбираем максимальную скидку из всех
    # доступных источников.
    now = _dt.utcnow()

    # 1) Классические discount-акции (не шаблоны), активные на текущий момент.
    classic_links = (
        db.session.query(PromotionProduct, Promotion)
        .join(Promotion, PromotionProduct.promotion_id == Promotion.id)
        .filter(
            Promotion.scheme == 'discount',
            Promotion.is_template == False,
            Promotion.status == 'active',
            (Promotion.start_date.is_(None)) | (Promotion.start_date <= now),
            (Promotion.end_date.is_(None)) | (Promotion.end_date >= now),
        )
        .all()
    )
    # Группируем по product_id, чтобы быстро доставать список акций товара.
    classic_by_product = defaultdict(list)
    for pp, promo in classic_links:
        classic_by_product[pp.product_id].append((pp, promo))

    # 1b) Шаблонные discount-акции, подключённые продавцами. Берём все
    # PromotionProduct для шаблонных discount-акций, активных на текущий момент;
    # для каждого товара смотрим, есть ли у его продавца активный
    # SellerPromotion на эту акцию.
    template_promos = (
        Promotion.query.filter(
            Promotion.scheme == 'discount',
            Promotion.is_template == True,
            Promotion.status == 'active',
            (Promotion.start_date.is_(None)) | (Promotion.start_date <= now),
            (Promotion.end_date.is_(None)) | (Promotion.end_date >= now),
        ).all()
    )
    template_promos_by_id = {p.id: p for p in template_promos}

    # Активные SellerPromotion для этих шаблонов
    template_seller_links = (
        SellerPromotion.query.filter(
            SellerPromotion.is_active == True,
            SellerPromotion.promotion_id.in_(list(template_promos_by_id.keys())),
        ).all()
    )
    # seller_id -> {promo_id: link}
    template_link_by_seller_promo = defaultdict(dict)
    for link in template_seller_links:
        template_link_by_seller_promo[link.seller_id][link.promotion_id] = link

    template_products_q = (
        db.session.query(PromotionProduct)
        .filter(PromotionProduct.promotion_id.in_(list(template_promos_by_id.keys())))
        .all()
    )
    template_by_product = defaultdict(list)  # product_id -> [(pp, promo)]
    for pp in template_products_q:
        promo = template_promos_by_id.get(pp.promotion_id)
        if promo:
            template_by_product[pp.product_id].append((pp, promo))

    # 2) seller-scope акции (second_with_discount), подключённые продавцами.
    by_seller = defaultdict(list)
    for it in cart_items:
        if it.product and it.product.seller_id:
            by_seller[it.product.seller_id].append(it)

    seller_links = {}  # seller_id -> (link, promo)
    seller_links_q = (
        SellerPromotion.query
        .join(Promotion, SellerPromotion.promotion_id == Promotion.id)
        .filter(
            SellerPromotion.is_active == True,
            Promotion.is_template == True,
            Promotion.scheme.in_(Promotion.SELLER_SCOPE_SCHEMES),
            Promotion.status == 'active',
        )
        .all()
    )
    for link in seller_links_q:
        promo = link.promotion
        if promo and promo.is_active:
            seller_links.setdefault(link.seller_id, (link, promo))

    # 2b) N+1 (one_plus_one / two_plus_one / three_plus_one) — шаблонные,
    # подключённые продавцами, выбор товаров.
    # Один продавец — один активный N+1 (логическая уникальность, проверяется
    # на этапе link_template_promotion). Для каждого продавца берём
    # участвующие PromotionProduct-ы, считаем floor(N / (required + 1))
    # самых дешёвых.
    n_plus_one_links = {}  # seller_id -> (link, promo)
    npo_q = (
        SellerPromotion.query
        .join(Promotion, SellerPromotion.promotion_id == Promotion.id)
        .filter(
            SellerPromotion.is_active == True,
            Promotion.is_template == True,
            Promotion.scheme.in_(['one_plus_one', 'two_plus_one', 'three_plus_one']),
            Promotion.status == 'active',
        )
        .all()
    )
    for link in npo_q:
        promo = link.promotion
        if promo and promo.is_active:
            n_plus_one_links.setdefault(link.seller_id, (link, promo))

    # Товары, участвующие в N+1 (promotion_id -> set(product_id))
    npo_promo_ids = list({p.id for _, p in n_plus_one_links.values()})
    npo_products_by_promo = defaultdict(set)  # promo_id -> {product_id}
    if npo_promo_ids:
        for pp in PromotionProduct.query.filter(
            PromotionProduct.promotion_id.in_(npo_promo_ids)
        ).all():
            npo_products_by_promo[pp.promotion_id].add(pp.product_id)

    # 2c) Выбор cart-floor-схемы на уровне продавца.
    #
    # У одного продавца теоретически могут одновременно быть подключены
    # second_with_discount И N+1. Это взаимоисключающие сценарии скидки
    # на корзину: каждая из них раздаёт скидку на floor(N / divisor) самых
    # дешёвых штук продавца, и если считать их независимо — на одни и те
    # же штуки может сработать двойная скидка, а на разные штуки — две
    # скидки от разных акций, что завышает сумму.
    #
    # Правило: для каждого продавца выбираем ОДНУ cart-floor-схему —
    # ту, что даёт максимальную суммарную скидку в рублях на всю корзину
    # продавца. Результат выбора кладём в cart_floor_by_seller в виде
    # (promo, divisor, eligible_set), где eligible_set — это множество
    # (item_id, unit_index) штук, попавших в floor-выборку выбранной
    # схемы. Штуки идентифицируются по (id(cart_item), unit_index) —
    # так корректно обрабатываются позиции с quantity > 1.
    cart_floor_by_seller = {}  # seller_id -> {'promo', 'divisor', 'percent', 'units'}
    for seller_id, seller_items in by_seller.items():
        candidates_for_seller = []  # list of (total_rub, promo, divisor, percent, units)

        # Кандидат A: second_with_discount
        if seller_id in seller_links:
            link_sw, promo_sw = seller_links[seller_id]
            percent_sw = promo_sw.get_effective_discount_percent(seller_id) or 0
            if percent_sw > 0:
                flat_sw = []
                for si in seller_items:
                    if si.product:
                        for u in range(si.quantity):
                            flat_sw.append((si.product.price, id(si), u))
                flat_sw.sort(key=lambda t: t[0])
                n_disc_sw = len(flat_sw) // 2
                if n_disc_sw > 0:
                    cheapest_sw = flat_sw[:n_disc_sw]
                    total_sw = sum(round(price * percent_sw / 100, 2) for price, _, _ in cheapest_sw)
                    units_sw = {(src, u) for _, src, u in cheapest_sw}
                    candidates_for_seller.append(
                        (round(total_sw, 2), promo_sw, 2, percent_sw, units_sw)
                    )

        # Кандидат B: N+1
        if seller_id in n_plus_one_links:
            link_np, promo_np = n_plus_one_links[seller_id]
            in_promo = npo_products_by_promo.get(promo_np.id, set())
            eligible_items = [
                si for si in seller_items
                if si.product and si.product.id in in_promo
            ]
            if eligible_items:
                percent_np = promo_np.get_effective_discount_percent(seller_id) or 0
                if percent_np > 0:
                    required = Promotion.N_PLUS_ONE_REQUIRED.get(promo_np.scheme, 1)
                    divisor = required + 1
                    flat_np = []
                    for si in eligible_items:
                        for u in range(si.quantity):
                            flat_np.append((si.product.price, id(si), u))
                    flat_np.sort(key=lambda t: t[0])
                    n_disc_np = len(flat_np) // divisor
                    if n_disc_np > 0:
                        cheapest_np = flat_np[:n_disc_np]
                        total_np = sum(
                            round(price * percent_np / 100, 2)
                            for price, _, _ in cheapest_np
                        )
                        units_np = {(src, u) for _, src, u in cheapest_np}
                        candidates_for_seller.append(
                            (round(total_np, 2), promo_np, divisor, percent_np, units_np)
                        )

        if candidates_for_seller:
            candidates_for_seller.sort(key=lambda t: t[0], reverse=True)
            total, promo, divisor, percent, units = candidates_for_seller[0]
            cart_floor_by_seller[seller_id] = {
                'promo': promo,
                'divisor': divisor,
                'percent': percent,
                'units': units,  # set of (id(cart_item), unit_index)
            }

    # 3) Собственно перебор позиций.
    total_discount = 0.0
    per_item = []              # list of (cart_item, discount_rub, source_label)
    applied_promotion_ids = set()
    promotion_contrib = defaultdict(float)  # promo_id -> rub contribution

    def _add_source(cart_item, amount, label, promo_id=None):
        nonlocal total_discount
        if amount <= 0:
            return
        per_item.append((cart_item, amount, label))
        total_discount += amount
        if promo_id is not None:
            applied_promotion_ids.add(promo_id)
            promotion_contrib[promo_id] += amount

    for it in cart_items:
        product = it.product
        if not product:
            continue

        candidates = []  # list of (amount, label, promo_id)

        # a) current_discount самого товара
        if product.current_discount and product.current_discount > 0:
            amt = round(product.price * product.current_discount / 100, 2) * it.quantity
            candidates.append((amt, f'-{product.current_discount}% на товар', None))

        # b) классические discount-акции для этого товара
        if product.id in classic_by_product:
            for pp, promo in classic_by_product[product.id]:
                percent = promo.get_product_discount_percent(product.id)
                if not percent or percent <= 0:
                    continue
                amt = round(product.price * percent / 100, 2) * it.quantity
                if promo.max_discount_amount:
                    amt = min(amt, promo.max_discount_amount)
                if amt > 0:
                    candidates.append((amt, f'{promo.name}: -{percent}%', promo.id))

        # b2) шаблонные discount-акции, подключённые продавцом этого товара
        if product.id in template_by_product and product.seller_id:
            seller_promo_links = template_link_by_seller_promo.get(product.seller_id, {})
            for pp, promo in template_by_product[product.id]:
                link = seller_promo_links.get(promo.id)
                if not link:
                    continue
                percent = promo.get_product_discount_percent(product.id, seller_id=product.seller_id)
                if not percent or percent <= 0:
                    continue
                amt = round(product.price * percent / 100, 2) * it.quantity
                if promo.max_discount_amount:
                    amt = min(amt, promo.max_discount_amount)
                if amt > 0:
                    candidates.append((amt, f'{promo.name}: -{percent}%', promo.id))

        # c/c2) cart-floor-схема (second_with_discount ИЛИ N+1) для продавца
        # этого товара. Схема выбрана заранее на уровне продавца в
        # cart_floor_by_seller — для каждого seller_id не более одной
        # (та, что даёт максимальную суммарную скидку на корзину продавца).
        cf = cart_floor_by_seller.get(product.seller_id)
        if cf and product.id:
            # Сколько штук именно этой позиции попало в floor-выборку
            # выбранной cart-floor-схемы. quantity > 1 корректно: каждая штука
            # имеет уникальный unit_index в eligible_set.
            units_in_floor = sum(
                1 for (src_id, u_idx) in cf['units']
                if src_id == id(it) and u_idx < it.quantity
            )
            if units_in_floor > 0:
                promo = cf['promo']
                percent = cf['percent']
                amt = round(product.price * percent / 100, 2) * units_in_floor
                divisor = cf['divisor']
                if promo.scheme == 'second_with_discount':
                    label = f'{promo.name}: -{percent}% (2-й по цене)'
                else:
                    required = divisor - 1
                    tag = '1+1' if required == 1 else f'{required}+1'
                    label = f'{promo.name}: -{percent}% ({tag})'
                candidates.append((amt, label, promo.id))

        if not candidates:
            continue

        # Выбираем максимальную скидку для этой позиции
        candidates.sort(key=lambda t: t[0], reverse=True)
        amount, label, promo_id = candidates[0]
        _add_source(it, amount, label, promo_id)

    # Сборка applied_promotions для UI
    applied_promotions = []
    for promo_id, amount in promotion_contrib.items():
        if amount <= 0:
            continue
        promo = db.session.get(Promotion, promo_id)
        if not promo:
            continue
        applied_promotions.append({
            'promotion_id': promo.id,
            'name': promo.name,
            'discount': round(amount, 2),
        })

    # Сборка items: {item, discount}
    items_for_breakdown = []
    for it, amt, label in per_item:
        items_for_breakdown.append({'item': it, 'discount': round(amt, 2)})

    return {
        'subtotal': subtotal,
        'discount': round(total_discount, 2),
        'total': round(subtotal - total_discount, 2),
        'items': items_for_breakdown,
        'applied_promotions': applied_promotions,
    }


def flash_form_errors(form):
    """
    Вывод ошибок формы во flash-сообщения.
    
    Args:
        form: WTForm объект
    """
    for field, errors in form.errors.items():
        for error in errors:
            flash(f"{getattr(form, field).label.text}: {error}", 'error')
def _collect_item_discount_candidates(cart_item, seller_items_in_cart):
    """
    Собирает все возможные скидки (в рублях, за всё количество позиции)
    для одной cart_item, не делая выбор «максимум». Возвращает список
    кортежей (amount_rub, label, promo_id) — promo_id=None означает
    «от базовой скидки товара (current_discount)», иначе это ID промо.

    Используется и в compute_best_discount_for_item (для совместимости),
    и в compute_item_discount_breakdown (для разложения «товар vs промо»
    при записи в OrderItem).
    """
    from app.models.orders import Promotion, PromotionProduct, SellerPromotion
    from datetime import datetime as _dt

    product = cart_item.product
    if not product:
        return []

    now = _dt.utcnow()
    candidates = []  # list of (amount_rub, label, promo_id)

    # 1) current_discount товара
    if product.current_discount and product.current_discount > 0:
        amt = round(product.price * product.current_discount / 100, 2) * cart_item.quantity
        candidates.append((amt, f'current_discount {product.current_discount}%', None))

    # 2) классические discount-акции для этого товара
    classic_q = (
        db.session.query(PromotionProduct, Promotion)
        .join(Promotion, PromotionProduct.promotion_id == Promotion.id)
        .filter(
            PromotionProduct.product_id == product.id,
            Promotion.scheme == 'discount',
            Promotion.is_template == False,
            Promotion.status == 'active',
            (Promotion.start_date.is_(None)) | (Promotion.start_date <= now),
            (Promotion.end_date.is_(None)) | (Promotion.end_date >= now),
        )
        .all()
    )
    for pp, promo in classic_q:
        percent = promo.get_product_discount_percent(product.id)
        if not percent or percent <= 0:
            continue
        amt = round(product.price * percent / 100, 2) * cart_item.quantity
        if promo.max_discount_amount:
            amt = min(amt, promo.max_discount_amount)
        if amt > 0:
            candidates.append((amt, f'{promo.name}: -{percent}%', promo.id))

    # 2b) шаблонные discount-акции, подключённые продавцом этого товара
    seller_id = product.seller_id
    if seller_id:
        template_q = (
            db.session.query(PromotionProduct, Promotion, SellerPromotion)
            .join(Promotion, PromotionProduct.promotion_id == Promotion.id)
            .join(SellerPromotion, (SellerPromotion.promotion_id == Promotion.id) &
                                       (SellerPromotion.seller_id == seller_id))
            .filter(
                PromotionProduct.product_id == product.id,
                Promotion.scheme == 'discount',
                Promotion.is_template == True,
                Promotion.status == 'active',
                SellerPromotion.is_active == True,
                (Promotion.start_date.is_(None)) | (Promotion.start_date <= now),
                (Promotion.end_date.is_(None)) | (Promotion.end_date >= now),
            )
            .all()
        )
        for pp, promo, _link in template_q:
            percent = promo.get_product_discount_percent(product.id, seller_id=seller_id)
            if not percent or percent <= 0:
                continue
            amt = round(product.price * percent / 100, 2) * cart_item.quantity
            if promo.max_discount_amount:
                amt = min(amt, promo.max_discount_amount)
            if amt > 0:
                candidates.append((amt, f'{promo.name}: -{percent}%', promo.id))

    # 3) cart-floor-схемы (second_with_discount ИЛИ N+1) для продавца.
    #
    # У одного продавца теоретически могут быть одновременно подключены и
    # second_with_discount, и N+1. Это взаимоисключающие сценарии floor-схемы
    # на корзину: каждая из них раздаёт скидку на floor(N / divisor) самых
    # дешёвых штук продавца, и если считать их независимо — на одни и те же
    # штуки может сработать двойная скидка, а на разные штуки — две скидки от
    # разных акций, что завышает сумму.
    #
    # Правило (единое с get_cart_total, секция 2c): трактуем seller-scope и
    # N+1 как ОБЩИЙ floor-пул для продавца и выбираем ровно одну схему —
    # ту, что даёт максимальную суммарную скидку в рублях на всю корзину
    # продавца. Штуки идентифицируются по (id(cart_item), unit_index),
    # так что quantity > 1 обрабатываются корректно.
    seller_id = product.seller_id
    if seller_id and seller_items_in_cart:
        # Кандидат A: second_with_discount
        link_sw = (
            SellerPromotion.query
            .join(Promotion, SellerPromotion.promotion_id == Promotion.id)
            .filter(
                SellerPromotion.seller_id == seller_id,
                SellerPromotion.is_active == True,
                Promotion.is_template == True,
                Promotion.scheme.in_(Promotion.SELLER_SCOPE_SCHEMES),
                Promotion.status == 'active',
            )
            .first()
        )
        candidates_for_seller = []  # (total_rub, promo, divisor, percent, units)

        if link_sw:
            promo_sw = link_sw.promotion
            if promo_sw and promo_sw.is_active:
                percent_sw = promo_sw.get_effective_discount_percent(seller_id) or 0
                if percent_sw > 0:
                    flat_sw = []
                    for si in seller_items_in_cart:
                        if si.product:
                            for u in range(si.quantity):
                                flat_sw.append((si.product.price, id(si), u))
                    flat_sw.sort(key=lambda t: t[0])
                    n_disc_sw = len(flat_sw) // 2
                    if n_disc_sw > 0:
                        cheapest_sw = flat_sw[:n_disc_sw]
                        total_sw = sum(
                            round(price * percent_sw / 100, 2)
                            for price, _, _ in cheapest_sw
                        )
                        units_sw = {(src, u) for _, src, u in cheapest_sw}
                        candidates_for_seller.append(
                            (round(total_sw, 2), promo_sw, 2, percent_sw, units_sw)
                        )

        # Кандидат B: N+1
        link_np = (
            SellerPromotion.query
            .join(Promotion, SellerPromotion.promotion_id == Promotion.id)
            .filter(
                SellerPromotion.seller_id == seller_id,
                SellerPromotion.is_active == True,
                Promotion.is_template == True,
                Promotion.scheme.in_(['one_plus_one', 'two_plus_one', 'three_plus_one']),
                Promotion.status == 'active',
            )
            .first()
        )
        if link_np:
            promo_np = link_np.promotion
            if promo_np and promo_np.is_active:
                in_promo_ids = {
                    pp.product_id for pp in
                    PromotionProduct.query.filter_by(promotion_id=promo_np.id).all()
                }
                if in_promo_ids and product.id in in_promo_ids:
                    percent_np = promo_np.get_effective_discount_percent(seller_id) or 0
                    if percent_np > 0:
                        # Только участвующие в этой N+1 — иначе эта схема
                        # для текущего товара неприменима и не должна
                        # «отбирать» floor-пул у second_with_discount.
                        in_items = [si for si in seller_items_in_cart
                                    if si.product and si.product.id in in_promo_ids]
                        if in_items:
                            required = Promotion.N_PLUS_ONE_REQUIRED.get(promo_np.scheme, 1)
                            divisor = required + 1
                            flat_np = []
                            for si in in_items:
                                for u in range(si.quantity):
                                    flat_np.append((si.product.price, id(si), u))
                            flat_np.sort(key=lambda t: t[0])
                            n_disc_np = len(flat_np) // divisor
                            if n_disc_np > 0:
                                cheapest_np = flat_np[:n_disc_np]
                                total_np = sum(
                                    round(price * percent_np / 100, 2)
                                    for price, _, _ in cheapest_np
                                )
                                units_np = {(src, u) for _, src, u in cheapest_np}
                                candidates_for_seller.append(
                                    (round(total_np, 2), promo_np, divisor, percent_np, units_np)
                                )

        if candidates_for_seller:
            candidates_for_seller.sort(key=lambda t: t[0], reverse=True)
            _total, promo, divisor, percent, units = candidates_for_seller[0]
            # Сколько штук именно этой позиции попало в floor-выборку
            # выбранной cart-floor-схемы. quantity > 1 корректно: каждая штука
            # имеет уникальный unit_index в units.
            units_in_floor = sum(
                1 for (src_id, u_idx) in units
                if src_id == id(cart_item) and u_idx < cart_item.quantity
            )
            if units_in_floor > 0:
                amt = round(product.price * percent / 100, 2) * units_in_floor
                if promo.scheme == 'second_with_discount':
                    label = f'{promo.name}: -{percent}% (2-й по цене)'
                else:
                    required = divisor - 1
                    tag = '1+1' if required == 1 else f'{required}+1'
                    label = f'{promo.name}: -{percent}% ({tag})'
                candidates.append((amt, label, promo.id))

    if not candidates:
        return []
    return candidates


def compute_best_discount_for_item(cart_item, seller_items_in_cart):
    """
    Возвращает лучшую (максимальную) скидку в рублях для одной позиции
    корзины с учётом правила «не суммируется — берём максимум».

    Используется при оформлении заказа, чтобы корректно посчитать
    price_at_order для OrderItem, без риска сложить скидки.

    Args:
        cart_item: CartItem
        seller_items_in_cart: все CartItem того же продавца в корзине
                              (нужно для second_with_discount)

    Returns:
        float: скидка в рублях на эту позицию (за всё количество)
    """
    candidates = _collect_item_discount_candidates(cart_item, seller_items_in_cart)
    if not candidates:
        return 0.0
    candidates.sort(key=lambda t: t[0], reverse=True)
    return round(candidates[0][0], 2)


def compute_item_discount_breakdown(cart_item, seller_items_in_cart):
    """
    Возвращает разложение «лучшей скидки» для одной позиции корзины.

    Правило «не суммируется — берём максимум» сохраняется: в каждом
    результате либо product_discount, либо promo_discount больше нуля,
    но не оба (если у товара и у промо одинаковая скидка в рублях —
    приоритет у промо, чтобы в заказе сохранилась ссылка на акцию).

    Args:
        cart_item: CartItem
        seller_items_in_cart: все CartItem того же продавца в корзине
                              (нужно для second_with_discount)

    Returns:
        dict: {
            'product_discount': float,  # скидка от current_discount (руб, на позицию)
            'promo_discount':   float,  # скидка от промо-акции (руб, на позицию)
            'total_discount':   float,  # сумма (== max из кандидатов)
            'source_label':     str,    # человекочитаемое имя источника
            'promo_id':         int|None,
        }
    """
    candidates = _collect_item_discount_candidates(cart_item, seller_items_in_cart)
    if not candidates:
        return {
            'product_discount': 0.0,
            'promo_discount': 0.0,
            'total_discount': 0.0,
            'source_label': '',
            'promo_id': None,
        }
    candidates.sort(key=lambda t: t[0], reverse=True)
    amount, label, promo_id = candidates[0]

    if promo_id is None:
        # Победила скидка от current_discount товара.
        return {
            'product_discount': round(amount, 2),
            'promo_discount': 0.0,
            'total_discount': round(amount, 2),
            'source_label': label,
            'promo_id': None,
        }
    # Победила скидка от промо-акции.
    return {
        'product_discount': 0.0,
        'promo_discount': round(amount, 2),
        'total_discount': round(amount, 2),
        'source_label': label,
        'promo_id': promo_id,
    }


class PaginationHelper:
    """
    Вспомогательный класс для пагинации.
    """
    
    def __init__(self, query, page=1, per_page=20, endpoint=None):
        """
        Инициализация пагинатора.
        
        Args:
            query: SQLAlchemy query
            page: Текущая страница
            per_page: Элементов на страницу
            endpoint: Endpoint для генерации URL
        """
        self.query = query
        self.page = page
        self.per_page = per_page
        self.endpoint = endpoint
        self.pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    @property
    def items(self):
        return self.pagination.items
    
    @property
    def has_prev(self):
        return self.pagination.has_prev
    
    @property
    def has_next(self):
        return self.pagination.has_next
    
    @property
    def total(self):
        return self.pagination.total
    
    @property
    def pages(self):
        return self.pagination.pages
    
    def iter_pages(self, left_edge=2, right_edge=2, left_current=2, right_current=5):
        """
        Генерация списка страниц для отображения.
        """
        return self.pagination.iter_pages(
            left_edge=left_edge,
            right_edge=right_edge,
            left_current=left_current,
            right_current=right_current
        )
