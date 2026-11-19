"""add movie_identity_operations (completed-movie re-identification journal)

Revision ID: d7a1f4c93b28
Revises: c6e3a8d2f5b1
Create Date: 2026-11-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migration_guards import table_exists

# revision identifiers, used by Alembic.
revision: str = "d7a1f4c93b28"
down_revision: str | Sequence[str] | None = "c6e3a8d2f5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # create_all() in init_db() normally makes the table (and its index) from
    # the model first; only build it on a DB that lacks it.
    if table_exists("movie_identity_operations"):
        return
    op.create_table(
        "movie_identity_operations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="planned"),
        sa.Column("previous_json", sa.String(), nullable=False),
        sa.Column("proposed_json", sa.String(), nullable=False),
        sa.Column("moves_json", sa.String(), nullable=False),
        sa.Column("old_dirs_json", sa.String(), nullable=False, server_default="[]"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_movie_identity_operations_job_id", "movie_identity_operations", ["job_id"])
    op.create_index(
        "uq_movie_identity_active_job",
        "movie_identity_operations",
        ["job_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('planned','moving','committing','needs_recovery')"),
    )


def downgrade() -> None:
    op.drop_table("movie_identity_operations")
