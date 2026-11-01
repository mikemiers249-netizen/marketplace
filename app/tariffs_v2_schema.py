"""Additive schema preparation; no cutover, resets, old-table writes or test-data purge."""
from sqlalchemy import inspect, text

from app import db

REVISION = '20261101_tariffs_v2_initial'


class SchemaPreparationError(ValueError):
    pass


def models():
    from app.models import tariffs_v2, tariffs_v2_commerce, tariffs_v2_finance, tariffs_v2_lifecycle  # noqa: F401
    return [table for table in db.metadata.sorted_tables if table.name.startswith('tariff_v2_')]


def prepare_schema(engine):
    """One transaction on PostgreSQL; additive/idempotent on local SQLite."""
    if engine.dialect.name not in ('sqlite', 'postgresql'):
        raise SchemaPreparationError('Поддерживается только PostgreSQL или изолированная SQLite.')
    tables = models()
    with engine.begin() as connection:
        if engine.dialect.name == 'postgresql':
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('wimli_tariffs_v2_schema'))"))
        existing = set(inspect(connection).get_table_names())
        required = {'products', 'categories', 'sellers', 'buyers', 'orders'}
        if not required <= existing:
            raise SchemaPreparationError('Не найдена базовая схема marketplace. Сначала подготовьте отдельную копию базы.')
        db.metadata.create_all(bind=connection, tables=tables)
        columns = {row['name'] for row in inspect(connection).get_columns('tariff_v2_coverage')}
        if 'is_test' not in columns:
            connection.execute(text('ALTER TABLE tariff_v2_coverage ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT FALSE'))
        columns = {row['name'] for row in inspect(connection).get_columns('tariff_v2_storage_invoices')}
        if 'gateway_id' not in columns:
            connection.execute(text('ALTER TABLE tariff_v2_storage_invoices ADD COLUMN gateway_id INTEGER REFERENCES tariff_v2_gateway_numbers(id)'))
        invoices = connection.execute(text('SELECT id FROM tariff_v2_storage_invoices WHERE gateway_id IS NULL')).all()
        for row in invoices:
            number = connection.execute(text('SELECT purpose, reference_id FROM tariff_v2_gateway_numbers WHERE id=:id'), {'id': row.id}).first()
            if number and (number.purpose != 'storage' or number.reference_id != row.id):
                raise SchemaPreparationError('Конфликт прежних платежных номеров. Нужна сверка, автоматическая перенумерация запрещена.')
            if not number:
                connection.execute(text("INSERT INTO tariff_v2_gateway_numbers(id,purpose,reference_id) VALUES (:id,'storage',:id)"), {'id': row.id})
            connection.execute(text('UPDATE tariff_v2_storage_invoices SET gateway_id=:id WHERE id=:id'), {'id': row.id})
        connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS v2_storage_gateway_unique ON tariff_v2_storage_invoices(gateway_id)'))
        if engine.dialect.name == 'postgresql' and invoices:
            connection.execute(text("SELECT setval(pg_get_serial_sequence('tariff_v2_gateway_numbers','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM tariff_v2_gateway_numbers"))
        inspector = inspect(connection)
        for table in tables:
            actual = {row['name'] for row in inspector.get_columns(table.name)}
            if not set(table.columns.keys()) <= actual:
                raise SchemaPreparationError('Обнаружена несовместимая промежуточная схема v2. Подготовка остановлена.')
    return {'revision': REVISION, 'tables': len(tables), 'activation_changed': False}
