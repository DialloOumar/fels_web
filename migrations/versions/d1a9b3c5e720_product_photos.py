"""product photos: pictures of each model

Revision ID: d1a9b3c5e720
Revises: c8f2d6a4e317
Create Date: 2026-11-05 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1a9b3c5e720'
down_revision = 'c8f2d6a4e317'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('product_photos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=False),
        sa.Column('thumb_key', sa.String(length=255), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('uploaded_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key'),
        sa.UniqueConstraint('thumb_key'))
    with op.batch_alter_table('product_photos', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_product_photos_product_id'), ['product_id'], unique=False)


def downgrade():
    op.drop_table('product_photos')
