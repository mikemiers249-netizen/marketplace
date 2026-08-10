"""add ON DELETE CASCADE to tariff_rows foreign keys

Revision ID: h1u2c3a4s5c6
Revises: g1t2a3r4i5f6
Create Date: 2026-08-01 14:15:00.000000

При удалении строки тарифа (TariffRow) подписки селлеров и
транзакции должны удаляться каскадно, иначе SQLite генерирует
UPDATE ... SET row_id=NULL, который валится на NOT NULL constraint
в seller_tariff_subscriptions.row_id.

SQLite не поддерживает ALTER TABLE ... DROP CONSTRAINT для FOREIGN KEY,
поэтому пересоздаём таблицы с правильным ondelete= 'CASCADE':
  1) Создаём _new-копию таблицы с новой схемой FK.
  2) Копируем данные (INSERT INTO ... SELECT).
  3) Дропаем старую, переименовываем _new.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h1u2c3a4s5c6'
down_revision = 'g1t2a3r4i5f6'
branch_labels = None
depends_on = None


def _recreate_with_cascade(table_name: str, fk_column: str, ref_table: str):
    """Пересоздать таблицу table_name, заменив FK на fk_column → ref_table.id
    с ondelete='CASCADE'. Имена индексов и PK сохраняем как у оригинала."""
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1. Схема оригинала
    cols = insp.get_columns(table_name)
    pk = insp.get_pk_constraint(table_name)
    fks = insp.get_foreign_keys(table_name)
    idxs = insp.get_indexes(table_name)

    new_name = '_new_' + table_name

    # 2. Создаём новую таблицу с тем же набором колонок и PK, но
    #    FK c ondelete='CASCADE'. PRIMARY KEY (id) + FOREIGN KEY ...
    col_defs = []
    for c in cols:
        col_type = str(c['type'])
        nullable = 'NULL' if c['nullable'] else 'NOT NULL'
        default = ''
        if c.get('default') is not None:
            dv = c['default']
            if isinstance(dv, str):
                default = " DEFAULT '" + dv.replace("'", "''") + "'"
            else:
                default = ' DEFAULT ' + str(dv)
        col_defs.append('"' + c['name'] + '" ' + col_type + ' ' + nullable + default)

    pk_cols = ', '.join('"' + c + '"' for c in pk['constrained_columns'])
    fk_lines = []
    for fk in fks:
        if fk['constrained_columns'] == [fk_column]:
            # тот самый FK — ставим CASCADE
            fk_lines.append(
                'FOREIGN KEY ("' + fk_column + '") REFERENCES "'
                + ref_table + '"("' + fk['referred_columns'][0] + '") ON DELETE CASCADE'
            )
        else:
            local = ', '.join('"' + c + '"' for c in fk['constrained_columns'])
            referred_tbl = fk['referred_table']
            referred = ', '.join('"' + c + '"' for c in fk['referred_columns'])
            fk_lines.append(
                'FOREIGN KEY (' + local + ') REFERENCES "' + referred_tbl + '"(' + referred + ')'
            )

    parts = ['CREATE TABLE "' + new_name + '" (\n']
    parts.append(',\n'.join(col_defs))
    if pk_cols:
        parts.append(',\nPRIMARY KEY (' + pk_cols + ')')
    if fk_lines:
        parts.append(',\n' + '\n'.join(fk_lines))
    parts.append('\n)')
    op.execute(''.join(parts))

    # 3. Копируем данные
    col_names = ', '.join('"' + c['name'] + '"' for c in cols)
    op.execute(
        'INSERT INTO "' + new_name + '" (' + col_names + ') SELECT ' + col_names + ' FROM "' + table_name + '"'
    )

    # 4. Дропаем старую, переименовываем
    op.execute('DROP TABLE "' + table_name + '"')
    op.execute('ALTER TABLE "' + new_name + '" RENAME TO "' + table_name + '"')

    # 5. Пересоздаём индексы
    for idx in idxs:
        cols_csv = ', '.join('"' + c + '"' for c in idx['column_names'])
        if idx.get('unique'):
            op.execute('CREATE UNIQUE INDEX "' + idx['name'] + '" ON "' + table_name + '" (' + cols_csv + ')')
        else:
            op.execute('CREATE INDEX "' + idx['name'] + '" ON "' + table_name + '" (' + cols_csv + ')')


def upgrade():
    _recreate_with_cascade('seller_tariff_subscriptions', 'row_id', 'tariff_rows')
    _recreate_with_cascade('tariff_transactions', 'row_id', 'tariff_rows')


def downgrade():
    # Обратное пересоздание без CASCADE: штатный путь — пересоздать
    # таблицы заново, убрав ON DELETE CASCADE. Для простоты делаем то же
    # самое (CASCADE безвреден) и оставляем как есть. Если нужен строгий
    # down без CASCADE — замените вызовы на прямую пересоздающую
    # миграцию с явным FOREIGN KEY (...) REFERENCES ... (без CASCADE).
    pass
