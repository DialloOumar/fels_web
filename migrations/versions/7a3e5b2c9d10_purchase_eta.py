"""purchase eta: the stage and the estimated arrival

Revision ID: 7a3e5b2c9d10
Revises: 3c0195c42002
Create Date: 2026-11-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7a3e5b2c9d10'
down_revision = '3c0195c42002'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('purchases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('eta_on', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('eta_date', sa.Date(), nullable=True))


def downgrade():
    # Purchases at the ETA stage fall back to "shipped", the stage before it.
    op.execute("UPDATE purchases SET stage = 'shipped' WHERE stage = 'eta'")
    with op.batch_alter_table('purchases', schema=None) as batch_op:
        batch_op.drop_column('eta_date')
        batch_op.drop_column('eta_on')
