"""references for models and vehicles

Every model gets TRC-001, REM-001… by category, in the order it was created;
every vehicle gets its model's reference and its number within the model,
in the order it was created: TRC-001-01, TRC-001-02…

Revision ID: 5951d9614059
Revises: b94044bd4439
Create Date: 2026-10-31 11:07:42.412328

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5951d9614059'
down_revision = 'b94044bd4439'
branch_labels = None
depends_on = None

# As in models.CATEGORY_PREFIX, copied so this migration never changes with it.
PREFIX = {"tractor": "TRC", "trailer": "REM", "truck": "CAM", "pickup": "PKP", "other": "DIV"}


def upgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ref', sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column('unit_seq', sa.Integer(), nullable=False, server_default='0'))
        batch_op.create_unique_constraint('uq_products_ref', ['ref'])

    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seq', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('ref', sa.String(length=20), nullable=True))
        batch_op.create_unique_constraint('uq_units_ref', ['ref'])

    conn = op.get_bind()
    counters, refs = {}, {}
    for pid, category in conn.execute(sa.text("SELECT id, category FROM products ORDER BY id")):
        prefix = PREFIX.get(category, PREFIX["other"])
        counters[prefix] = counters.get(prefix, 0) + 1
        refs[pid] = "%s-%03d" % (prefix, counters[prefix])
        conn.execute(sa.text("UPDATE products SET ref = :r WHERE id = :i"), {"r": refs[pid], "i": pid})
    seqs = {}
    for uid, pid in conn.execute(sa.text("SELECT id, product_id FROM units ORDER BY id")):
        seqs[pid] = seqs.get(pid, 0) + 1
        conn.execute(sa.text("UPDATE units SET seq = :s, ref = :r WHERE id = :i"),
                     {"s": seqs[pid], "r": "%s-%02d" % (refs[pid], seqs[pid]), "i": uid})
    for pid, n in seqs.items():
        conn.execute(sa.text("UPDATE products SET unit_seq = :n WHERE id = :i"), {"n": n, "i": pid})


def downgrade():
    with op.batch_alter_table('units', schema=None) as batch_op:
        batch_op.drop_constraint('uq_units_ref', type_='unique')
        batch_op.drop_column('ref')
        batch_op.drop_column('seq')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_constraint('uq_products_ref', type_='unique')
        batch_op.drop_column('unit_seq')
        batch_op.drop_column('ref')
