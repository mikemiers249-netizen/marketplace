"""Empty baseline: real schema is created in app/commands.py via db-init
or by an explicit `flask db-init` command. This file only stamps the
revision so that Alembic considers the DB at the baseline state.

Revision ID: 000000000000
Revises:
Create Date: 2026-08-11 22:30:00.000000
"""

# revision identifiers, used by Alembic.
revision = "000000000000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Schema уже создана командой `flask db-init` ДО этой миграции
    # (см. Dockerfile). Здесь Alembic просто пометит, что baseline применён.
    pass


def downgrade():
    pass
