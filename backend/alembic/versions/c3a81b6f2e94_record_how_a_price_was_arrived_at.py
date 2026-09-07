"""record how a material price was arrived at

Revision ID: c3a81b6f2e94
Revises: b2f7e91c4d08
Create Date: 2026-09-07 22:15:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3a81b6f2e94'
down_revision = 'b2f7e91c4d08'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('material_requirement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('price_method', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('material_requirement', schema=None) as batch_op:
        batch_op.drop_column('price_method')
