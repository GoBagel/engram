"""Fail-closed guard: a pytest run must never open a real Engram database.

Why this exists. ``settings.database_url`` defaults to ``sqlite+aiosqlite:///./engram.db``,
relative to the working directory, and ``app.database`` builds its engine at import. Run
pytest from ``backend/`` and anything that reaches the real session (integration tests
call ``init_db()`` and ``async_session()`` directly; one runs ``DELETE FROM app_config``)
operates on the developer's real database. That happened: a full suite run replaced a
configured ``app_config`` row with fixture rows and deleted the user's job history.

The rule enforced here: while pytest is imported, SQLite may only be opened in memory or
inside a directory that the test run explicitly registered with ``allow_test_root``
(``tests/conftest.py`` registers its throwaway database directory and pytest's basetemp).
The system temp directory is deliberately NOT trusted as a whole: ``TMPDIR`` is
environment-controlled and can name any directory.

Layers, outermost first:

1. ``enforce_test_database_isolation(url)``: the configured URL, at ``app.config`` import,
   ``init_db()`` and the sync engine factory. Gives a clear error early.
2. ``install_sqlite_guard()``: wraps ``sqlite3.connect`` (and ``sqlite3.dbapi2.connect``,
   which SQLAlchemy uses; aiosqlite calls ``sqlite3.connect``). The refusal happens BEFORE
   the DBAPI connection exists, so no file is created, and it covers every opener
   regardless of module: engines, the backup helper, the TMDB and transcript caches,
   Alembic helpers and scripts. It also sets ``SQLITE_LIMIT_ATTACHED`` to 0 on each
   connection, so ``ATTACH DATABASE`` cannot reach a second file.
3. ``enforce_connection_isolation(conn)``: the engines' connect hook asks SQLite which
   files the connection is bound to (``PRAGMA database_list``, every entry).

Production is untouched: all of this is inert unless ``pytest`` is in ``sys.modules``,
which no packaged or served Engram process ever imports.

Known limits, stated rather than hidden:

- Activation is "pytest has been imported". Every pytest run imports it before test code,
  but a connection that was opened before ``pytest`` was imported is not retro-checked.
- ``sqlite3.Connection(...)`` constructed directly bypasses the ``connect`` wrapper.
- Paths are checked by resolving symlinks and ``..`` and then opened later; a symlink
  swapped in between, or a hard link to a real database placed inside a registered
  directory, is not detected. The registered directories are private, freshly created
  temp directories, so this needs a hostile actor on the same machine.

This module imports nothing from the rest of the app, so ``app.config`` can call it while
the settings object is being created.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ProductionDatabaseAccessError(RuntimeError):
    """Raised when a pytest run would open a database outside its registered test dirs."""


_ALLOWED_ROOTS: list[Path] = []


def running_under_pytest() -> bool:
    """True when pytest has been imported into this interpreter.

    ``pytest`` is only ever in ``sys.modules`` in a test process (it is a dev
    dependency and nothing in ``app/`` imports it), so this cannot trip in production.
    """
    return "pytest" in sys.modules


def allow_test_root(path: str | Path) -> Path:
    """Register a directory in which a test run may create or open SQLite files.

    Only call this with a directory the run itself created (a fresh ``mkdtemp`` or
    pytest's basetemp), never with an environment-derived location.
    """
    root = Path(path).resolve(strict=False)
    if root not in _ALLOWED_ROOTS:
        _ALLOWED_ROOTS.append(root)
    return root


def allowed_test_roots() -> tuple[Path, ...]:
    return tuple(_ALLOWED_ROOTS)


def is_isolated_path(path: str | Path) -> bool:
    """True if ``path`` (resolved through symlinks and ``..``) is inside a registered root.

    Relative paths resolve against the current directory, so the default
    ``./engram.db`` is only accepted when the process is running inside a registered root.
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return any(resolved.is_relative_to(root) for root in _ALLOWED_ROOTS)


def is_isolated_database_url(database_url: str) -> bool:
    """True for an in-memory SQLite URL or a SQLite file inside a registered test root.

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


def _describe(database_url: str) -> str:
    """The URL without user info, host or query: enough to debug, nothing secret."""
    try:
        url = make_url(database_url)
    except ArgumentError:
        return "<unparseable database url>"
    return f"{url.drivername}:///{url.database or ''}"


def _refusal(target: str) -> ProductionDatabaseAccessError:
    return ProductionDatabaseAccessError(
        f"Refusing to open database {target!r} while running under pytest.\n"
        "Tests may only use an in-memory SQLite database or a file inside a directory the "
        "test run registered (tests/conftest.py registers its throwaway database directory "
        "and pytest's basetemp). Reaching this means the test setup was bypassed: pytest was "
        "started with a rootdir/conftest that does not load tests/conftest.py, or code "
        "replaced settings.database_url or opened SQLite on its own."
    )


def enforce_test_database_isolation(database_url: str, *, under_pytest: bool | None = None) -> None:
    """Raise if pytest is running and ``database_url`` is not in-memory or in a test root.

    ``under_pytest`` exists so tests can exercise both branches; production code
    leaves it unset.
    """
    active = running_under_pytest() if under_pytest is None else under_pytest
    if active and not is_isolated_database_url(database_url):
        raise _refusal(_describe(database_url))


def _file_from_sqlite_target(database, uri: bool) -> str | None:
    """The filesystem path a ``sqlite3.connect`` target names; None for in-memory."""
    if isinstance(database, (bytes, bytearray)):
        database = os.fsdecode(database)
    database = os.fspath(database)
    if not database or database == ":memory:":
        return None
    if uri or database.startswith("file:"):
        if not database.startswith("file:"):
            return database
        parsed = urlparse(database)
        query = parse_qs(parsed.query)
        if "memory" in query.get("mode", []) or parsed.path in ("", ":memory:"):
            return None
        return unquote(parsed.path)
    return database


def check_sqlite_target(database, *, uri: bool = False) -> None:
    """Refuse a ``sqlite3.connect`` target that is not in-memory or in a test root."""
    try:
        file = _file_from_sqlite_target(database, uri)
    except (TypeError, ValueError):
        raise _refusal("<unreadable sqlite target>") from None
    if file is not None and not is_isolated_path(file):
        raise _refusal(file)


def enforce_connection_isolation(dbapi_connection, *, under_pytest: bool | None = None) -> None:
    """Check every file a live SQLite connection is bound to (``PRAGMA database_list``).

    Runs from the engines' ``connect`` hook, before any pragma that writes. It is a second
    opinion behind the ``sqlite3.connect`` wrapper: it asks SQLite itself, so it still holds
    for an attached or aliased file. Every entry is inspected, not only ``main``.
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
        file = row[2]  # (seq, name, file); '' for in-memory and temp databases
        if file and not is_isolated_path(file):
            raise _refusal(file)


def session_isolation_problems(**urls: str) -> list[str]:
    """Describe each named URL that is not isolated (empty list means the session is safe)."""
    return [
        f"{name} = {_describe(url)!r}"
        for name, url in urls.items()
        if not is_isolated_database_url(url)
    ]


# --------------------------------------------------------------------------- #
# sqlite3.connect wrapper
# --------------------------------------------------------------------------- #

_REAL_CONNECT = sqlite3.connect


def _guarded_connect(database, *args, **kwargs):
    if running_under_pytest():
        uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
        check_sqlite_target(database, uri=bool(uri))
    conn = _REAL_CONNECT(database, *args, **kwargs)
    if running_under_pytest():
        # No ATTACH in tests: it could open a second, unchecked file on a checked connection.
        limit = getattr(sqlite3, "SQLITE_LIMIT_ATTACHED", None)
        if limit is not None and hasattr(conn, "setlimit"):
            conn.setlimit(limit, 0)
    return conn


_guarded_connect._engram_guard = True  # type: ignore[attr-defined]


def install_sqlite_guard() -> bool:
    """Wrap ``sqlite3.connect`` and ``sqlite3.dbapi2.connect``. Idempotent; pytest only.

    Returns True when the wrapper is in place. Outside pytest it does nothing, so a served
    or packaged Engram never has its sqlite3 module touched.
    """
    if not running_under_pytest():
        return False
    for module in (sqlite3, sqlite3.dbapi2):
        if not getattr(module.connect, "_engram_guard", False):
            module.connect = _guarded_connect
    return True
