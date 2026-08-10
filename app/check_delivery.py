from app import create_app, db
from app.models.users import DeliveryService

app = create_app()
with app.app_context():
    services = DeliveryService.query.all()
    for s in services:
        print(f"{s.id}: {s.name} ({s.code})")