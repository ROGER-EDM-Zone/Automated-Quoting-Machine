"""live market data

Revision ID: 8c1d24b5f0a3
Revises: 17aef839394e
Create Date: 2026-09-04 16:02:11.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '8c1d24b5f0a3'
down_revision = '17aef839394e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'market_source',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('series_key', sa.String(length=120), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('unit', sa.String(length=20), nullable=False),
        sa.Column('basis', sa.String(length=30), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=True),
        sa.Column('target', sa.Text(), nullable=True),
        sa.Column('spec', sa.String(length=200), nullable=True),
        sa.Column('stock_form', sa.String(length=40), nullable=True),
        sa.Column('max_age_hours', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_market_source_series_key', 'market_source', ['series_key'])
    op.create_index('ix_market_source_kind', 'market_source', ['kind'])
    op.create_index('ix_market_source_spec', 'market_source', ['spec'])
    op.create_index('ix_market_source_series_active', 'market_source', ['series_key', 'active'])

    op.create_table(
        'market_observation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('series_key', sa.String(length=120), nullable=False),
        sa.Column('value', sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column('unit', sa.String(length=20), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('method', sa.String(length=20), nullable=False),
        sa.Column('basis', sa.String(length=30), nullable=False),
        sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column('evidence', sa.Text(), nullable=True),
        sa.Column('sizes_mm', sa.JSON(), nullable=True),
        sa.Column('source_url', sa.String(length=500), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['source_id'], ['market_source.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_market_observation_source_id', 'market_observation', ['source_id'])
    op.create_index('ix_market_observation_series_key', 'market_observation', ['series_key'])
    op.create_index('ix_market_observation_observed_at', 'market_observation', ['observed_at'])
    op.create_index('ix_market_obs_series_time', 'market_observation', ['series_key', 'observed_at'])

    with op.batch_alter_table('part', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_rotational', sa.Boolean(), nullable=True))

    with op.batch_alter_table('material_requirement', schema=None) as batch_op:
        batch_op.add_column(sa.Column('required_section_mm', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('required_length_mm', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('section_oversize_mm', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('price_source_name', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('price_source_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('price_observed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('price_is_stale', sa.Boolean(), nullable=False, server_default=sa.false()))

    with op.batch_alter_table('stock_size', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('origin', sa.String(length=20), nullable=False, server_default='manual')
        )
        batch_op.add_column(sa.Column('market_series_key', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('density_kg_m3', sa.Numeric(precision=10, scale=2), nullable=True))
        batch_op.add_column(sa.Column('listed', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('priced_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('source_name', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('source_url', sa.String(length=500), nullable=True))
        batch_op.create_index('ix_stock_size_market_series_key', ['market_series_key'])


def downgrade() -> None:
    # SQLite rebuilds the table for every drop, and batching several at once
    # confuses the dependency sort, so they go one at a time.
    for column in (
        'price_is_stale',
        'price_observed_at',
        'price_source_url',
        'price_source_name',
        'section_oversize_mm',
        'required_length_mm',
        'required_section_mm',
    ):
        with op.batch_alter_table('material_requirement', schema=None) as batch_op:
            batch_op.drop_column(column)

    with op.batch_alter_table('stock_size', schema=None) as batch_op:
        batch_op.drop_index('ix_stock_size_market_series_key')
    for column in (
        'source_url',
        'source_name',
        'priced_at',
        'listed',
        'density_kg_m3',
        'market_series_key',
        'origin',
    ):
        with op.batch_alter_table('stock_size', schema=None) as batch_op:
            batch_op.drop_column(column)

    with op.batch_alter_table('part', schema=None) as batch_op:
        batch_op.drop_column('is_rotational')

    op.drop_table('market_observation')
    op.drop_table('market_source')
