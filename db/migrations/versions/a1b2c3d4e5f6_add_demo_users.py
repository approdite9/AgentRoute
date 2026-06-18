"""add demo_users table

Revision ID: a1b2c3d4e5f6
Revises: cf903902dd2d
Create Date: 2026-06-18 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a1b2c3d4e5f6"
down_revision = "cf903902dd2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("email", sa.String(256), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("quota", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_demo_users_token", "demo_users", ["token"], unique=True)
    op.create_index("ix_demo_users_email", "demo_users", ["email"])


def downgrade() -> None:
    op.drop_index("ix_demo_users_email", table_name="demo_users")
    op.drop_index("ix_demo_users_token", table_name="demo_users")
    op.drop_table("demo_users")
