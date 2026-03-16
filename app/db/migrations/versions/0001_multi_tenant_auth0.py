"""allow same auth0 user across tenants

Revision ID: 0001_multi_tenant_auth0
Revises: None (first migration — no prior alembic history)
Create Date: 2026-03-16

Drops the global unique constraint on staff_users.auth0_user_id
and replaces it with a composite unique on (tenant_id, auth0_user_id).
This allows the same Auth0 user to be staff in multiple agencies.
"""
from alembic import op


revision = "0001_multi_tenant_auth0"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the global unique constraint on auth0_user_id
    op.drop_constraint(
        "staff_users_auth0_user_id_key",
        "staff_users",
        type_="unique",
    )

    # Add composite unique: same Auth0 user per-tenant is fine,
    # but not twice in the SAME tenant
    op.create_unique_constraint(
        "uq_staff_tenant_auth0",
        "staff_users",
        ["tenant_id", "auth0_user_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_staff_tenant_auth0", "staff_users", type_="unique")
    op.create_unique_constraint(
        "staff_users_auth0_user_id_key",
        "staff_users",
        ["auth0_user_id"],
    )