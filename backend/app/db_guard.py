"""Fail-closed guard: a pytest run must never open a real Engram database.

Why this exists. ``settings.database_url`` defaults to ``sqlite+aiosqlite:///./engram.db``,
relative to the working directory, and ``app.database`` builds its engine at import. Run
pytest from ``backend/`` and anything that reaches the real session (integration tests
call ``init_db()`` and ``async_session()`` directly; one runs ``DELETE FROM app_config``)
operates on the developer's real database. That happened: a full suite run replaced a
configured ``app_config`` row with fixture rows and deleted the user's job history.

The rule enforced here: while pytest is imported, every database the app opens must be
an in-memory SQLite database or a file inside the system temp directory. Anything else
raises ``ProductionDatabaseAccessError``, whose message says what to fix.

Production is untouched: the guard is inert unless ``pytest`` is in ``sys.modules``,
which no packaged or served Engram process ever imports.

This module imports nothing from the rest of the app, so ``app.config`` can call it while
the settings object is being created.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ProductionDatabaseAccessError(RuntimeError):
    """Raised when a pytest run would open a database outside temp/in-memory storage."""


def running_under_pytest() -> bool:
    """True when pytest has been imported into this interpreter.

    ``pytest`` is only ever in ``sys.modules`` in a test process (it is a dev
    dependency and nothing in ``app/`` imports it), so this cannot trip in production.
    """
    return "pytest" in sys.modules


def _temp_roots() -> list[Path]:
    roots = {Path(tempfile.gettempdir()).resolve()}
    return sorted(roots)


def is_isolated_path(path: str | Path) -> bool:
    """True if ``path`` (resolved through symlinks and ``..``) lies inside the temp dir.

    Relative paths resolve against the current directory, so the default
    ``./engram.db`` is only accepted when the process is itself running in temp.
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return any(resolved.is_relative_to(root) for root in _temp_roots())


def is_isolated_database_url(database_url: str) -> bool:
    """True for an in-memory SQLite URL or a SQLite file inside the temp directory.

    Anything unparseable, non-SQLite, or using SQLite URI filenames (``file:...``)
    is rejected: when in doubt the answer is no.
    """
    try:
        url = make_url(database_url)
    except ArgumentError:
        return False
    if not url.get_backend_name().startswith("sqlite"):
        return False
    database = url.database
    if not database or database == ":memory:":
        return True
    if database.startswith("file:") or url.query:
        # URI filenames and query options can redirect to a different file.
        return False
    return is_isolated_path(database)


def _refusal(target: str) -> ProductionDatabaseAccessError:
    return ProductionDatabaseAccessError(
        f"Refusing to open database {target!r} while running under pytest.\n"
        "Tests may only use an in-memory SQLite database or a file under "
        f"{tempfile.gettempdir()!r}. tests/conftest.py points DATABASE_URL at a throwaway "
        "file before the app is imported, so reaching this means that file was bypassed: "
        "pytest was started with a rootdir/conftest that does not load tests/conftest.py, "
        "or code replaced settings.database_url or built its own engine. "
        "Your real engram.db was NOT touched."
    )


def enforce_test_database_isolation(database_url: str, *, under_pytest: bool | None = None) -> None:
    """Raise if pytest is running and ``database_url`` is not temp/in-memory.

    ``under_pytest`` exists so tests can exercise both branches; production code
    leaves it unset.
    """
    active = running_under_pytest() if under_pytest is None else under_pytest
    if active and not is_isolated_database_url(database_url):
        raise _refusal(_redact(database_url))


def enforce_connection_isolation(dbapi_connection, *, under_pytest: bool | None = None) -> None:
    """Last line of defence: check the file a live SQLite connection really opened.

    Runs from the engines' ``connect`` hook, before any pragma that writes. It asks
    SQLite itself (``PRAGMA database_list``) which file the connection is bound to,
    so it still holds if a URL was rewritten, an engine was built by hand, or a
    relative path resolved somewhere unexpected.
    """
    active = running_under_pytest() if under_pytest is None else under_pytest
    if not active:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA database_list")
        rows = cursor.fetchall()
    finally:
        cursor.close()
    for row in rows:
        # (seq, name, file); file is '' for in-memory and temp databases.
        name, file = row[1], row[2]
        if name in ("main", "temp") and file and not is_isolated_path(file):
            raise _refusal(file)


def _redact(database_url: str) -> str:
    try:
        return make_url(database_url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<unparseable database url>"
