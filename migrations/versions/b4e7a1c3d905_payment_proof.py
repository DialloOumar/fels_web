"""payment proof: the photo or PDF of the paper receipt

Revision ID: b4e7a1c3d905
Revises: 9c2d4e6f8a11
Create Date: 2026-11-04 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4e7a1c3d905'
down_revision = '9c2d4e6f8a11'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('proof_key', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('proof_filename', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('proof_type', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('proof_size', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint('uq_payments_proof_key', ['proof_key'])


def downgrade():
    with op.batch_alter_table('payments', schema=None) as batch_op:
        batch_op.drop_constraint('uq_payments_proof_key', type_='unique')
        batch_op.drop_column('proof_size')
        batch_op.drop_column('proof_type')
        batch_op.drop_column('proof_filename')
        batch_op.drop_column('proof_key')
