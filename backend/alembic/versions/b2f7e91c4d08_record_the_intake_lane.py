"""record the intake lane

Revision ID: b2f7e91c4d08
Revises: 8c1d24b5f0a3
Create Date: 2026-09-07 21:40:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2f7e91c4d08'
down_revision = '8c1d24b5f0a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('enquiry', schema=None) as batch_op:
        batch_op.add_column(sa.Column('intake_lane', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('enquiry', schema=None) as batch_op:
        batch_op.drop_column('intake_lane')
