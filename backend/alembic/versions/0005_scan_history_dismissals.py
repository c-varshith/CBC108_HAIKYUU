"""per-user hidden scan history

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_history_dismissals",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.String(length=64), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scan_id"], ["scans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "scan_id"),
    )


def downgrade() -> None:
    op.drop_table("scan_history_dismissals")
