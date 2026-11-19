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
2. ``install_sqlite_guard()`` wraps ``sqlite3.connect`` and ``sqlite3.dbapi2.connect`` (the
   one SQLAlchemy calls; aiosqlite calls ``sqlite3.connect``). It judges the target with
   SQLite's own rules, BEFORE the file exists: the ``uri`` flag is honoured; a ``file:``
   URI is parsed the way SQLite parses it (not with ``urllib``, which strips tabs and
   newlines); control characters, repeated parameters and non-local authorities are
   refused. Connections it opens are an instance of a ``Connection`` subclass whose attach
   limit is set to 0 inside ``__init__`` through the C method (so an overriding ``setlimit``
   cannot neutralise it, and an ``ATTACH`` in a cooperative factory's ``__init__``, one that
   reaches ``super().__init__``, already finds it in force) and whose ``setlimit`` refuses to
   raise it. ``ATTACH DATABASE`` and ``VACUUM INTO`` then fail. If the ban cannot be enforced
   on this Python/SQLite it refuses to open anything.
3. A ``sys.addaudithook`` hook on the ``sqlite3.connect`` audit event, which CPython raises
   from below the replaceable module attributes. It covers openers the wrapper never sees:
   a connector captured before the wrapper was installed, ``sqlite3.Connection(...)``
   built directly, a ``creator=`` callable. The event carries the target but not the
   ``uri`` flag, so for a ``file:`` string it requires BOTH readings (literal filename and
   URI) to be safe. A call the wrapper already judged precisely passes via a thread-local
   mark that the hook consumes on first use and the wrapper restores afterwards, so a
   connect nested inside a factory cannot reuse or erase it.
4. ``enforce_connection_isolation(conn)``: the engines' connect hook asks SQLite which
   files the connection is bound to (``PRAGMA database_list``, every entry).

Exemption: coverage.py's own data files (``.coverage`` and ``.coverage.<suffix>``, in any
directory) are neither location-checked nor ATTACH-pinned. ``pytest --cov`` (CI uses it)
writes them in the working directory, and ``coverage combine`` needs ATTACH. A coverage
file given a custom name via ``COVERAGE_FILE`` is not exempt and is refused unless it is
in a registered directory.

Production is untouched: all of this is inert unless ``pytest`` is in ``sys.modules``,
which no packaged or served Engram process ever imports. The audit hook cannot be removed
once added, so it is only ever added under pytest, and it does nothing if pytest is gone.

Known limits, stated rather than hidden:

- Activation is "pytest has been imported". Importing pytest alone installs nothing; the
  guard arms when ``tests/conftest.py`` or ``app.config`` runs (every pytest run for this
  project does one of them before test code). A connection opened before then is not
  retro-checked.
- Connections that bypass the wrapper (a captured connector, ``sqlite3.Connection(...)``)
  have their TARGET checked by the audit hook but are not given the ATTACH ban: ATTACH from
  such a connection, which is itself bound to a registered database, is not blocked.
- The ATTACH ban is a limit on the connection, kept at 0 by ``setlimit`` refusing to raise
  it. Code that calls the base method directly (``sqlite3.Connection.setlimit(conn, ...)``),
  or goes around the Python API (ctypes, a patched ``sqlite3``), can undo it. A connection
  whose class a caller built without going through ``sqlite3.connect`` is not covered.
  This guards against accidents, not against code written to defeat it.
- A factory that calls ``sqlite3.Connection.__init__`` directly (skipping ``super()``), or a
  plain callable factory, can run ``ATTACH`` in its own body before the pin is applied; the
  pin is applied as soon as it returns.
- Reading an existing real database ``mode=ro`` is refused like any other open; there is no
  read-only exception.
- A relative path is judged against the current directory when the check runs; another thread
  changing the working directory between the check and the open is not detected (one case of
  the check-then-open gap above).
- The coverage exemption applies to a name that is coverage data both as written and after
  symlinks are followed; it is still name-based, so a real database that someone renames to
  ``.coverage`` is exempt.
- Paths are checked by resolving symlinks and ``..`` and then opened later; a symlink
  swapped in between, or a hard link to a real database placed inside a registered
  directory, is not detected. The registered directories are private, freshly created
  directories, so this needs a hostile actor on the same machine.

This module imports nothing from the rest of the app, so ``app.config`` can call it while
the settings object is being created.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

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


# --------------------------------------------------------------------------- #
# Reading a sqlite3.connect target the way SQLite does
# --------------------------------------------------------------------------- #


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_PERCENT_ESCAPE = re.compile(rb"%([0-9A-Fa-f]{2})")
# coverage.py's own data files: ``.coverage`` and ``.coverage.<suffix>`` (parallel mode,
# which pytest-cov always uses). They are tooling output, not application databases, and
# ``coverage combine`` needs ATTACH, so they are exempt from both the roots rule and the
# ATTACH ban. CI runs ``pytest --cov=app``; without this exemption it would crash.
_COVERAGE_DATA_FILE = re.compile(r"\.coverage(\..*)?")


def _as_str(database) -> str:
    """The target as an exact ``str`` (never a subclass, whose ``__eq__`` could lie)."""
    if isinstance(database, (bytes, bytearray)):
        text = os.fsdecode(database)
    else:
        text = os.fsdecode(os.fspath(database))  # a PathLike may hand back bytes
    if type(text) is not str:
        # str.encode is called on the base class so an override in a subclass is not used.
        text = str.encode(text, "utf-8", "surrogateescape").decode("utf-8", "surrogateescape")
    return text


def _percent_decode(text: str) -> str:
    """Decode ``%HH`` escapes exactly as SQLite does: hex pairs only, everything else literal."""
    raw = text.encode("utf-8", "surrogateescape")
    raw = _PERCENT_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), raw)
    return raw.decode("utf-8", "surrogateescape")


def _parse_sqlite_uri(name: str) -> tuple[str, dict[str, str]]:
    """Split a ``file:`` URI the way SQLite's own parser does; returns (path, options).

    Not ``urllib``: that silently strips tabs and newlines and splits queries differently, so
    its reading of a URI can name a different file than SQLite opens. A control character
    anywhere in the URI, raw or percent-decoded, is refused outright (SQLite truncates a
    component at ``%00``, so a decoded NUL changes which parameters and which path count).
    Repeated parameters are refused too: SQLite lets the last one win.
    """
    if _CONTROL_CHARS.search(name):
        raise ValueError("control character in sqlite URI")
    rest = name[len("file:") :].split("#", 1)[0]  # a fragment ends the URI
    location, _, query = rest.partition("?")
    if location.startswith("//"):
        authority, slash, tail = location[2:].partition("/")
        if authority not in ("", "localhost"):  # SQLite compares the raw text
            raise ValueError("invalid sqlite URI authority")
        location = slash + tail
    path = _percent_decode(location)
    if _CONTROL_CHARS.search(path):
        raise ValueError("control character in sqlite URI path")
    options: dict[str, str] = {}
    for item in filter(None, query.split("&")):
        key, _, value = item.partition("=")
        key, value = _percent_decode(key), _percent_decode(value)
        if _CONTROL_CHARS.search(key) or _CONTROL_CHARS.search(value):
            raise ValueError("control character in sqlite URI parameter")
        if key in options:
            raise ValueError("repeated sqlite URI parameter")
        options[key] = value
    return path, options


def _file_from_sqlite_target(database, uri: bool) -> str | None:
    """The filesystem path a ``sqlite3.connect`` target names; None for in-memory.

    Mirrors SQLite: without ``uri=True`` the string is a literal filename even if it starts
    with ``file:``; with it, only a ``file:`` string is parsed as a URI. Anything whose
    meaning could differ between readers raises ValueError and is refused.
    """
    name = _as_str(database)
    if not name or name == ":memory:":
        return None
    if not uri or not name.startswith("file:"):
        return name
    path, options = _parse_sqlite_uri(name)
    mode = options.get("mode")
    if mode == "memory" or path in ("", ":memory:"):
        if mode not in (None, "memory"):
            raise ValueError("ambiguous sqlite URI")
        return None
    return path


def _is_coverage_data_file(file: str) -> bool:
    """True if ``file`` is named like coverage data AND still is after following symlinks.

    The resolved name matters: a link called ``.coverage`` that points at a database must
    not borrow the exemption.
    """
    if not _COVERAGE_DATA_FILE.fullmatch(os.path.basename(file)):
        return False
    try:
        resolved = os.path.realpath(file)
    except (OSError, ValueError):
        return False
    return bool(_COVERAGE_DATA_FILE.fullmatch(os.path.basename(resolved)))


def check_sqlite_target(database, *, uri: bool = False) -> None:
    """Refuse a ``sqlite3.connect`` target that is not in-memory or in a test root."""
    try:
        file = _file_from_sqlite_target(database, uri)
    except (TypeError, ValueError):
        raise _refusal("<unreadable or ambiguous sqlite target>") from None
    if file is not None and not is_isolated_path(file) and not _is_coverage_data_file(file):
        raise _refusal(file)


def _check_target_either_reading(database) -> None:
    """For callers that cannot see ``uri``: a ``file:`` string must be safe both ways."""
    check_sqlite_target(database, uri=False)
    try:
        is_uri_form = _as_str(database).startswith("file:")
    except (TypeError, ValueError):
        raise _refusal("<unreadable sqlite target>") from None
    if is_uri_form:
        check_sqlite_target(database, uri=True)


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
# sqlite3.connect wrapper, ATTACH ban, and the audit hook
# --------------------------------------------------------------------------- #

_REAL_CONNECT = sqlite3.connect
_state = threading.local()  # the target the wrapper has already judged on this thread
_GUARDED_CLASSES: dict[type, type] = {}
_hook_installed = False


def _attached_limit_category() -> int:
    category = getattr(sqlite3, "SQLITE_LIMIT_ATTACHED", None)
    if category is None or not hasattr(sqlite3.Connection, "setlimit"):
        raise ProductionDatabaseAccessError(
            "Cannot disable ATTACH DATABASE on this Python/SQLite (sqlite3.Connection.setlimit "
            "or SQLITE_LIMIT_ATTACHED is missing), so no database may be opened under pytest."
        )
    return category


def _pin_attach_limit(conn) -> None:
    """Set the attach limit to 0 through the C method itself.

    ``sqlite3.Connection.setlimit`` is called on the base class on purpose: a subclass that
    overrides ``setlimit`` (a caller's factory, say) cannot turn the pin into a no-op.
    """
    sqlite3.Connection.setlimit(conn, _attached_limit_category(), 0)


class _GuardedConnection(sqlite3.Connection):
    """A Connection whose attach limit is 0 from the moment it exists, and stays there.

    The pin is applied inside ``__init__``, so a caller's factory that runs ``ATTACH`` in its
    own ``__init__`` (after ``super().__init__``) already finds it in force.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _pin_attach_limit(self)

    def setlimit(self, category, limit):
        # A negative limit is SQLite's "query, do not change"; only raising is refused.
        if category == _attached_limit_category() and limit > 0:
            raise ProductionDatabaseAccessError(
                "ATTACH DATABASE is disabled under pytest; the attach limit cannot be raised."
            )
        return super().setlimit(category, limit)


def _guarded_class(base: type) -> type:
    """``base`` combined with the pinning behaviour; MRO is base, _GuardedConnection, Connection."""
    if not issubclass(base, sqlite3.Connection):
        return base  # not ours to wrap; sqlite3 raises its own error for a non-Connection class
    if issubclass(base, _GuardedConnection):
        return base
    if base is sqlite3.Connection:
        return _GuardedConnection
    cls = _GUARDED_CLASSES.get(base)
    if cls is None:
        cls = _GUARDED_CLASSES[base] = type(
            f"Guarded{base.__name__}", (base, _GuardedConnection), {}
        )
    return cls


def _guarded_factory(factory):
    """The ``factory=`` to hand the real connect: pinned whatever the caller supplied."""
    if factory is None:
        return _GuardedConnection
    if isinstance(factory, type):
        return _guarded_class(factory)

    def pinned(*args, **kwargs):  # a plain callable that builds a Connection some other way
        conn = factory(*args, **kwargs)
        if isinstance(conn, sqlite3.Connection):
            _pin_attach_limit(conn)
        return conn

    return pinned


def _guarded_connect(database, *args, **kwargs):
    if not running_under_pytest():
        return _REAL_CONNECT(database, *args, **kwargs)

    category = _attached_limit_category()  # fail closed before opening anything
    uri = bool(kwargs.get("uri", args[6] if len(args) > 6 else False))
    check_sqlite_target(database, uri=uri)

    file = _file_from_sqlite_target(database, uri)
    exempt = file is not None and _is_coverage_data_file(file)  # coverage's own file: needs ATTACH

    args = list(args)
    if exempt:
        pass  # no ATTACH pin; still marked as judged below so the audit hook does not redo it
    elif len(args) > 4:  # factory was passed positionally
        args[4] = _guarded_factory(args[4])
    else:
        kwargs["factory"] = _guarded_factory(kwargs.get("factory"))

    # Tell the audit hook "this exact call was judged with its real uri flag". The mark is
    # consumed by the hook on first use and restored afterwards, so a connect nested inside
    # a caller's factory can neither reuse it nor erase the outer call's.
    outer = getattr(_state, "judged", None)
    _state.judged = _as_str(database)
    try:
        conn = _REAL_CONNECT(database, *args, **kwargs)
    finally:
        _state.judged = outer
    if not exempt:
        sqlite3.Connection.setlimit(conn, category, 0)
    return conn


_guarded_connect._engram_guard = True  # type: ignore[attr-defined]


def _clear_mark() -> None:
    _state.judged = None


def _audit_hook(event, args):
    if event != "sqlite3.connect" or not args or not running_under_pytest():
        return
    database = args[0]
    try:
        target = _as_str(database)
    except (TypeError, ValueError):
        target = None
    if target is not None and getattr(_state, "judged", None) == target:
        _state.judged = None  # consumed: a second open of the same text is judged afresh
        return
    _check_target_either_reading(database)


def install_sqlite_guard() -> bool:
    """Arm the guard: wrap ``sqlite3.connect`` and add the audit hook. Idempotent; pytest only.

    Returns True when armed. Outside pytest it does nothing, so a served or packaged
    Engram never has its sqlite3 module touched and no audit hook is ever added.
    """
    global _hook_installed
    if not running_under_pytest():
        return False
    _attached_limit_category()  # refuse to arm a guard that cannot ban ATTACH
    for module in (sqlite3, sqlite3.dbapi2):
        if not getattr(module.connect, "_engram_guard", False):
            module.connect = _guarded_connect
    if not _hook_installed:
        sys.addaudithook(_audit_hook)
        if hasattr(os, "register_at_fork"):
            # A forked child must not inherit the parent's "this call was judged" mark.
            os.register_at_fork(after_in_child=_clear_mark)
        _hook_installed = True
    return True
