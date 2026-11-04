"""cart and quotes: a seller's cart, kept on the server, and proformas

Revision ID: c8f2d6a4e317
Revises: b4e7a1c3d905
Create Date: 2026-11-05 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8f2d6a4e317'
down_revision = 'b4e7a1c3d905'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cart_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('unit_id', sa.Integer(), nullable=False),
        sa.Column('price', sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['unit_id'], ['units.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'unit_id', name='uq_cart_item'))
    with op.batch_alter_table('cart_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cart_items_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_cart_items_unit_id'), ['unit_id'], unique=False)
    op.create_table('quotes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('number', sa.String(length=20), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('issued_on', sa.Date(), nullable=False),
        sa.Column('valid_until', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('total', sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column('deposit_pct', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('customer_name', sa.String(length=120), nullable=False),
        sa.Column('customer_address', sa.String(length=255), nullable=True),
        sa.Column('customer_phone', sa.String(length=60), nullable=True),
        sa.Column('lines', sa.JSON(), nullable=False),
        sa.Column('terms', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('number'))
    with op.batch_alter_table('quotes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_quotes_customer_id'), ['customer_id'], unique=False)


def downgrade():
    op.drop_table('quotes')
    op.drop_table('cart_items')
