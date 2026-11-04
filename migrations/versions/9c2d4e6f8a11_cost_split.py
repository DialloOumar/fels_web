"""cost split: how each cost is spread over the purchase's vehicles

Revision ID: 9c2d4e6f8a11
Revises: 7a3e5b2c9d10
Create Date: 2026-11-04 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9c2d4e6f8a11'
down_revision = '7a3e5b2c9d10'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('purchase_costs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('split', sa.String(length=10), nullable=False, server_default='value'))
    op.create_table('purchase_cost_allocations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cost_id', sa.Integer(), nullable=False),
        sa.Column('unit_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.ForeignKeyConstraint(['cost_id'], ['purchase_costs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['unit_id'], ['units.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cost_id', 'unit_id', name='uq_cost_allocation'))
    with op.batch_alter_table('purchase_cost_allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_purchase_cost_allocations_cost_id'), ['cost_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_cost_allocations_unit_id'), ['unit_id'], unique=False)


def downgrade():
    op.drop_table('purchase_cost_allocations')
    with op.batch_alter_table('purchase_costs', schema=None) as batch_op:
        batch_op.drop_column('split')
