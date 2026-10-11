"""idempotency key for scan uploads

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scans") as batch:
        batch.add_column(sa.Column("client_request_id", sa.String(length=64), nullable=True))
        batch.create_unique_constraint("uq_scans_created_by_client_request_id", ["created_by", "client_request_id"])


def downgrade() -> None:
    with op.batch_alter_table("scans") as batch:
        batch.drop_constraint("uq_scans_created_by_client_request_id", type_="unique")
        batch.drop_column("client_request_id")
