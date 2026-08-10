"""Запускает миграции напрямую через alembic API, чтобы избежать глюков flask cli."""
import os

from app import create_app, db
from flask_migrate import upgrade

app = create_app()
with app.app_context():
    print('FLASK_ENV:', os.environ.get('FLASK_ENV'))
    print('DATABASE_URI env:', os.environ.get('DATABASE_URI'))
    print('DB URI:', app.config['SQLALCHEMY_DATABASE_URI'])
    from sqlalchemy import text
    rows = db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")).fetchall()
    print('Tables in db BEFORE:')
    for r in rows:
        print(' ', r[0])
    try:
        ver = db.session.execute(text("SELECT * FROM alembic_version")).fetchall()
        print('alembic version:', ver)
    except Exception as e:
        print('alembic_version: <no table>', e)
    upgrade()
    rows = db.session.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")).fetchall()
    print('Tables in db AFTER:')
    for r in rows:
        print(' ', r[0])
    ver = db.session.execute(text("SELECT * FROM alembic_version")).fetchall()
    print('alembic version:', ver)
