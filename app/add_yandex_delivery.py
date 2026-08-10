import os
os.environ['DATABASE_URI'] = 'sqlite:///instance/marketplace_dev.db'

from app import create_app, db
from app.models.users import DeliveryService

app = create_app()
with app.app_context():
    # Check if already exists
    existing = DeliveryService.query.filter_by(code='yandex').first()
    if existing:
        print('Already exists:', existing.name)
    else:
        service = DeliveryService(
            name='Яндекс Доставка',
            code='yandex',
            api_module='app.delivery.yandex',
            is_active=True
        )
        db.session.add(service)
        db.session.commit()
        print('Added:', service.name)