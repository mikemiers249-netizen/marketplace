"""Single release entry point. Never equate sandbox readiness with live activation."""
import json

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app import db

# These are code integrations, not configurable acknowledgements. They must be
# replaced with verified implementations before production activation is possible.
PENDING_INTEGRATIONS = {
    'checkout_cutover': 'Переключение рабочего каталога, корзины и кабинетов на v2.',
    'live_payments': 'Рабочие платежи и фискальные чеки вместо test-only счетов.',
    'refund_dispatch': 'Подключение сохраненной очереди возвратов к рабочим платежам и исполнителю API.',
    'bill_payments': 'Рабочий режим оплаты месячных счетов и общий обработчик уведомлений со старой оплатой.',
    'digital_access': 'Выдача электронных покупок из новых заказов в рабочую библиотеку.',
    'storage_cleanup': 'Удаление рабочих карточек и вложений; сейчас удаляются только копии.',
    'scheduler': 'Единственный исполнитель периодических заданий с защитой от повторов.',
}


def readiness():
    config = current_app.config
    blockers = [{'code': code, 'message': value} for code, value in PENDING_INTEGRATIONS.items()]
    if db.engine.dialect.name != 'postgresql':
        blockers.append({'code': 'database', 'message': 'Рабочий выпуск требует PostgreSQL.'})
    if config.get('TARIFFS_V2_SANDBOX') or config.get('TARIFFS_V2_PAYMENT_TEST_ENABLED'):
        blockers.append({'code': 'sandbox_mode', 'message': 'Тестовый режим нельзя считать рабочим включением.'})
    for name in ('TARIFFS_V2_ROBOKASSA_LIVE_LOGIN', 'TARIFFS_V2_ROBOKASSA_LIVE_PASSWORD1',
                 'TARIFFS_V2_ROBOKASSA_LIVE_PASSWORD2', 'TARIFFS_V2_ROBOKASSA_PASSWORD3'):
        if not config.get(name):
            blockers.append({'code': 'missing_' + name.lower(), 'message': f'Не настроена защищенная переменная {name}.'})
    if not config.get('TARIFFS_V2_REFUND_API_ENABLED'):
        blockers.append({'code': 'refund_api', 'message': 'Не подключен API возвратов Robokassa.'})
    table_names = set(inspect(db.engine).get_table_names())
    from app.tariffs_v2_schema import models, REVISION
    tables = models()
    missing = sorted(table.name for table in tables if table.name not in table_names)
    if missing:
        blockers.append({'code': 'schema', 'message': 'Схема v2 еще не подготовлена.', 'tables': missing})
    # A copied sandbox must not turn simulated payments into real entitlements.
    test_rows = {}
    for name in ('tariff_v2_coverage', 'tariff_v2_storage_invoices', 'tariff_v2_checkouts', 'tariff_v2_checkout_refunds'):
        if name not in table_names:
            continue
        columns = {row['name'] for row in inspect(db.engine).get_columns(name)}
        if 'is_test' in columns:
            count = db.session.execute(text(f'SELECT COUNT(*) FROM {name} WHERE is_test = TRUE')).scalar()
            if count:
                test_rows[name] = count
    if test_rows:
        blockers.append({'code': 'test_data', 'message': 'В базе есть тестовые финансовые записи. Их нельзя активировать как рабочие.', 'counts': test_rows})
    return {'target': 'production', 'revision': REVISION, 'ready': not blockers,
            'activation_changed': False, 'blockers': blockers}


@click.command('tariffs-v2-release')
@click.option('--check', 'action', flag_value='check', default=True, help='Read-only readiness report (default).')
@click.option('--prepare', 'action', flag_value='prepare', help='Prepare additive v2 schema without activation.')
@click.option('--activate', 'action', flag_value='activate', help='Activate only when every production integration is ready.')
@with_appcontext
def release(action):
    """Prepare/check the eventual one-command release. Never activates a simulator."""
    from app.tariffs_v2_schema import prepare_schema, SchemaPreparationError
    try:
        if action == 'prepare':
            # Close read-only request/session transactions before the independent DDL transaction.
            db.session.rollback()
            result = prepare_schema(db.engine)
            click.echo(json.dumps(result, ensure_ascii=False, indent=2))
            return
        result = readiness()
        db.session.rollback()
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['ready']:
            raise click.ClickException('Рабочее включение пока заблокировано перечисленными зависимостями. Настройки сайта не изменены.')
        # No currently missing implementation can be bypassed by an env variable.
        if action == 'activate':
            raise click.ClickException('Контроллер рабочего переключения еще не подключен. Настройки сайта не изменены.')
    except SchemaPreparationError as error:
        db.session.rollback()
        raise click.ClickException(str(error)) from error
    except SQLAlchemyError:
        db.session.rollback()
        raise click.ClickException('Не удалось проверить или подготовить схему базы. Реквизиты подключения не выводятся.') from None
