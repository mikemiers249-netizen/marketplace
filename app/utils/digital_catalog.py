"""Category attributes and additive schema changes for digital goods."""
import math
from sqlalchemy import inspect, text


def ensure_schema(connection):
    additions = {
        'products': {'digital_book_id': 'INTEGER REFERENCES digital_books(id)'},
        'categories': {'is_digital_book': 'BOOLEAN NOT NULL DEFAULT FALSE'},
        'digital_books': {
            'category_id': 'INTEGER REFERENCES categories(id)',
            'article': 'VARCHAR(50)', 'system_sku': 'VARCHAR(64)',
            'parameter_values': "JSON NOT NULL DEFAULT '{}'",
            'cover_name': 'VARCHAR(80)',
            'moderation_status': "VARCHAR(20) NOT NULL DEFAULT 'draft'",
            'moderation_comment': "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in additions.items():
        existing = {c['name'] for c in inspect(connection).get_columns(table)}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
                if table == 'digital_books' and name == 'moderation_status':
                    connection.execute(text("UPDATE digital_books SET moderation_status='pending', published=FALSE WHERE published=TRUE"))
    for column in ('article', 'system_sku', 'category_id'):
        unique = 'UNIQUE ' if column != 'category_id' else ''
        connection.execute(text(f'CREATE {unique}INDEX IF NOT EXISTS ix_digital_books_{column} ON digital_books ({column})'))
    # Deterministic, separate legacy namespace; preserve all existing books/files.
    connection.execute(text("UPDATE digital_books SET system_sku = 'WML-' || seller_id || '-LEGACY-D' || id WHERE system_sku IS NULL"))
    connection.execute(text('UPDATE digital_books SET article = system_sku WHERE article IS NULL'))
    connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_products_digital_book_id ON products (digital_book_id)'))
    if not inspect(connection).has_table('digital_purchases'):
        return
    if connection.dialect.name == 'postgresql':
        for constraint in inspect(connection).get_unique_constraints('digital_purchases'):
            if constraint['column_names'] == ['order_id']:
                name = connection.dialect.identifier_preparer.quote(constraint['name'])
                connection.execute(text(f'ALTER TABLE digital_purchases DROP CONSTRAINT {name}'))
    connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_digital_purchase_order_book ON digital_purchases (order_id, book_id)'))
    connection.execute(text("""UPDATE digital_purchases SET
        file_name = (SELECT file_name FROM digital_books WHERE digital_books.id = digital_purchases.book_id),
        format = (SELECT format FROM digital_books WHERE digital_books.id = digital_purchases.book_id),
        title = (SELECT title FROM digital_books WHERE digital_books.id = digital_purchases.book_id)
        WHERE book_id IN (SELECT id FROM digital_books WHERE moderation_status <> 'deleted')
        AND (file_name <> (SELECT file_name FROM digital_books WHERE digital_books.id = digital_purchases.book_id)
             OR format <> (SELECT format FROM digital_books WHERE digital_books.id = digital_purchases.book_id)
             OR title <> (SELECT title FROM digital_books WHERE digital_books.id = digital_purchases.book_id))"""))


def category_parameters(category):
    """Include required/default settings from the category and all ancestors."""
    from app.models.products import CategoryParameter
    settings = {}
    for node in category.all_parents + [category]:
        for binding in CategoryParameter.query.filter_by(category_id=node.id).order_by(CategoryParameter.sort_order, CategoryParameter.id):
            p = binding.parameter
            if p and p.is_active:
                old = settings.get(p.id, {})
                settings[p.id] = {'parameter': p,
                    'required': bool(p.is_required or binding.is_required or old.get('required')),
                    'default': binding.default_value if binding.default_value is not None else old.get('default')}
    return list(settings.values())


def read_parameters(category, form):
    values = {}
    for setting in category_parameters(category):
        p = setting['parameter']
        key = f'param_{p.id}'
        raw = form.getlist(key) if p.is_multiple or p.is_composite else form.get(key, '')
        if isinstance(raw, list) and not p.is_composite:
            raw = [v for v in raw if str(v).strip()]
        if isinstance(raw, str):
            raw = raw.strip()
        missing = not raw or (isinstance(raw, list) and not any(str(v).strip() for v in raw))
        if missing and setting['default'] is not None:
            raw = setting['default']
            missing = False
        if missing:
            if setting['required']:
                raise ValueError(f'Заполните параметр «{p.name}»')
            continue
        def convert(value):
            value = str(value).strip()
            if p.type in ('numeric', 'integer', 'float'):
                try:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError()
                    if p.type == 'integer' and not number.is_integer():
                        raise ValueError()
                    return int(number) if p.type == 'integer' else number
                except ValueError:
                    raise ValueError(f'Параметр «{p.name}» должен быть числом')
            if p.type == 'boolean':
                if value not in ('true', 'false'):
                    raise ValueError(f'Выберите значение параметра «{p.name}»')
                return value == 'true'
            return value
        if p.is_composite and (not isinstance(raw, list) or len(raw) != p.composite_count or any(not str(v).strip() for v in raw)):
            raise ValueError(f'Заполните все части параметра «{p.name}»')
        if (not p.is_input or p.type == 'select') and p.predefined_values:
            choices = {str(v) for v in p.predefined_values}
            submitted = raw if isinstance(raw, list) else [raw]
            if any(str(v) not in choices for v in submitted):
                raise ValueError(f'Выберите допустимое значение параметра «{p.name}»')
            valid, error = True, None
        else:
            valid, error = p.validate_value(raw)
        if not valid:
            raise ValueError(f'{p.name}: {error}')
        values[str(p.id)] = [convert(v) for v in raw] if isinstance(raw, list) else convert(raw)
    return values
