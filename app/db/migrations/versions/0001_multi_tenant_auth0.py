"""allow same auth0 user across tenants

Revision ID: fix_multi_tenant_auth0
Revises: SET_YOUR_CURRENT_HEAD_HERE
Create Date: 2026-03-16

Drops the global unique constraint on staff_users.auth0_user_id
and replaces it with a composite unique on (tenant_id, auth0_user_id).
This allows the same Auth0 user to be staff in multiple agencies.
"""
from alembic import op

revision = "fix_multi_tenant_auth0"
down_revision = "SET_YOUR_CURRENT_HEAD_HERE"  # ← CHANGE THIS before pushing
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the global unique constraint on auth0_user_id
    # If your constraint has a different name, update it here.
    # Check with: SELECT conname FROM pg_constraint
    #             WHERE conrelid = 'staff_users'::regclass AND contype = 'u';
    op.drop_constraint(
        "staff_users_auth0_user_id_key",
        "staff_users",
        type_="unique",
    )

    # Add composite unique: same Auth0 user per-tenant is unique,
    # but allowed across different tenants
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
