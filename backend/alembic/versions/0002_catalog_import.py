"""catalog import: product provenance, reference hashes, import_batches

Backfill: before this revision a product only got reference rows AFTER the AI worker acknowledged them, so every existing
product that has reference photos is marked indexed (at the time of its newest photo). indexed_with stays NULL (unknown).

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('import_batches',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('org_id', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('counts', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.Column('error_log', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_import_batches_org_id'), ['org_id'], unique=False)

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('barcode', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('brand', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='manual', nullable=False))
        batch_op.add_column(sa.Column('source_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('indexed_with', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('image_license', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('image_attribution', sa.String(length=500), nullable=True))
        batch_op.create_index(batch_op.f('ix_products_barcode'), ['barcode'], unique=False)

    with op.batch_alter_table('reference_images', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='upload', nullable=False))
        batch_op.add_column(sa.Column('content_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('phash', sa.String(length=16), nullable=True))
        batch_op.create_index(batch_op.f('ix_reference_images_content_hash'), ['content_hash'], unique=False)

    op.execute(
        "UPDATE products SET indexed_at = (SELECT MAX(r.created_at) FROM reference_images r WHERE r.product_id = products.id) "
        "WHERE EXISTS (SELECT 1 FROM reference_images r WHERE r.product_id = products.id)")


def downgrade() -> None:
    with op.batch_alter_table('reference_images', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reference_images_content_hash'))
        batch_op.drop_column('phash')
        batch_op.drop_column('content_hash')
        batch_op.drop_column('source')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_products_barcode'))
        batch_op.drop_column('image_attribution')
        batch_op.drop_column('image_license')
        batch_op.drop_column('indexed_with')
        batch_op.drop_column('indexed_at')
        batch_op.drop_column('source_url')
        batch_op.drop_column('source')
        batch_op.drop_column('brand')
        batch_op.drop_column('barcode')

    with op.batch_alter_table('import_batches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_import_batches_org_id'))

    op.drop_table('import_batches')
