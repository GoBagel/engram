"""Journal and audit trail for correcting a completed movie's TMDB identity.

The library files and SQLite cannot change atomically together, so every
correction is persisted here BEFORE a file moves. The row is the recovery
journal while it is active and the audit entry once it is ``done``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel

# Statuses in which an operation still owns its job (at most one per job).
ACTIVE_STATUSES = ("planned", "moving", "committing", "needs_recovery")


class MovieIdentityOperation(SQLModel, table=True):
    """One completed-movie re-identification (journal while active, audit after)."""

    __tablename__ = "movie_identity_operations"
    __table_args__ = (
        # One active operation per job, enforced by the database.
        Index(
            "uq_movie_identity_active_job",
            "job_id",
            unique=True,
            sqlite_where=text("status IN ('planned','moving','committing','needs_recovery')"),
        ),
    )

    id: str = Field(primary_key=True)  # uuid4 hex
    job_id: int = Field(index=True)
    # planned | moving | committing | done | rolled_back | needs_recovery
    status: str = Field(default="planned")
    # JSON {"tmdb_id","title","year","detected_title","classification_source",
    # "classification_confidence","final_path"} before / after.
    previous_json: str
    proposed_json: str
    # JSON [{"title_id", "src", "dst", "state": "pending"|"moved"|"reverted"}]
    moves_json: str
    # Old directories the operation may remove if (and only if) they end up empty.
    old_dirs_json: str = Field(default="[]")
    error: str | None = None
    actor: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
