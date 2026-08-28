"""users.token_version — stateless session revocation

Access tokens are JWTs, so nothing about a suspension, password reset, role change or member
removal reached a token that was already issued: access continued for the rest of
``access_token_ttl_min``. This counter is stamped into the token as ``tv``; bumping it
invalidates every token minted before the bump.

Additive and backfill-free. The column defaults to 0 at the database, so existing rows get a
value without an UPDATE, and a token carrying no ``tv`` claim (issued by the previous release) is
accepted by ``get_principal`` until it expires — which is what makes this safe to deploy while
users are signed in.

Revision ID: 0053_user_token_version
Revises: 0052_signal_preferences
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_user_token_version"
down_revision = "0052_signal_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
