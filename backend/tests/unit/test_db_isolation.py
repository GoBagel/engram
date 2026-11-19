"""Regression tests: pytest must never open a real Engram database.

Background: a full backend suite run from ``backend/`` once wrote fixture rows into
the developer's real ``engram.db`` (the default ``./engram.db`` is relative to the
working directory, and only the unit suite patched the session). ``app/db_guard.py``
now fails closed, and ``tests/conftest.py`` points ``DATABASE_URL`` at a throwaway
file before the app is imported.

Nothing here opens a database outside temp storage. Tests that must prove a
rejection use a stub connection, an in-process refusal that happens before any I/O,
or a subprocess whose working directory is a throwaway directory.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app import db_guard
from app.db_guard import (
    ProductionDatabaseAccessError,
    enforce_connection_isolation,
    enforce_test_database_isolation,
    is_isolated_database_url,
    is_isolated_path,
)

BACKEND = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()

# A non-temp location (so the guard must refuse it) that is deliberately NOT named
# engram.db, so a guard bug could never touch a real database: it would at worst
# create this canary, which the tests then fail on.
CANARY = BACKEND / "isolation-canary.db"
CANARY_URL = f"sqlite+aiosqlite:///{CANARY}"


def _real_home_db_url() -> str:
    """A URL for a database in the user's home. Used only to be REFUSED, never opened."""
    return f"sqlite+aiosqlite:///{Path.home() / '.engram' / 'engram.db'}"


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


class TestIsolatedDatabaseUrl:
    def test_in_memory_urls_are_isolated(self):
        assert is_isolated_database_url("sqlite+aiosqlite:///:memory:")
        assert is_isolated_database_url("sqlite://")
        assert is_isolated_database_url("sqlite+aiosqlite://")

    def test_file_in_temp_is_isolated(self, tmp_path):
        assert is_isolated_database_url(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        assert is_isolated_database_url(f"sqlite:///{tmp_path / 'a' / 'b' / 'x.db'}")

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite+aiosqlite:///./engram.db",  # the default: relative to cwd
            "sqlite+aiosqlite:///engram.db",
            "sqlite+aiosqlite:///../engram.db",
            "postgresql://u:p@localhost/engram",  # not SQLite at all
            "mysql+pymysql://u:p@localhost/engram",
            "not a url",
            "",
            "sqlite+aiosqlite:///file:engram.db?uri=true",  # URI filename
            "sqlite+aiosqlite:///file::memory:?cache=shared",
        ],
    )
    def test_everything_else_is_refused(self, url):
        assert not is_isolated_database_url(url)

    def test_home_and_production_locations_are_refused(self):
        assert not is_isolated_database_url(_real_home_db_url())
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{BACKEND / 'engram.db'}")
        assert not is_isolated_database_url("sqlite+aiosqlite:////var/lib/engram/engram.db")

    def test_dotdot_traversal_out_of_temp_is_refused(self, tmp_path):
        up = "/".join([".."] * (len(tmp_path.parts) + 2))
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{tmp_path}/{up}/home/x/e.db")

    def test_symlink_out_of_temp_is_refused(self, tmp_path):
        link = tmp_path / "link"
        link.symlink_to(Path.home(), target_is_directory=True)
        assert not is_isolated_path(link / "engram.db")
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{link / 'engram.db'}")

    def test_relative_path_is_judged_by_where_it_resolves(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert is_isolated_database_url("sqlite+aiosqlite:///./engram.db")
        monkeypatch.chdir(BACKEND)
        assert not is_isolated_database_url("sqlite+aiosqlite:///./engram.db")


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #


class TestEnforcement:
    def test_refuses_a_real_database_when_under_pytest(self):
        with pytest.raises(ProductionDatabaseAccessError) as exc:
            enforce_test_database_isolation(_real_home_db_url())
        assert "NOT touched" in str(exc.value)

    def test_default_dev_database_is_refused_from_backend_dir(self, monkeypatch):
        monkeypatch.chdir(BACKEND)
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_test_database_isolation("sqlite+aiosqlite:///./engram.db")

    def test_accepts_temp_and_memory(self, tmp_path):
        enforce_test_database_isolation(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        enforce_test_database_isolation("sqlite+aiosqlite:///:memory:")

    def test_is_inert_outside_pytest(self):
        """Production never imports pytest, so the guard must do nothing there."""
        enforce_test_database_isolation(_real_home_db_url(), under_pytest=False)

    def test_pytest_detection_is_true_in_this_process(self):
        assert db_guard.running_under_pytest()

    def test_error_message_hides_credentials(self):
        with pytest.raises(ProductionDatabaseAccessError) as exc:
            enforce_test_database_isolation("postgresql://admin:hunter2@db/engram")
        assert "hunter2" not in str(exc.value)


class _StubCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql):
        assert sql == "PRAGMA database_list", "the guard must only ask, never write"

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _StubConn:
    def __init__(self, file):
        self._rows = [(0, "main", file)]

    def cursor(self):
        return _StubCursor(self._rows)


class TestConnectionLevelGuard:
    """The last line of defence checks the file SQLite actually opened."""

    def test_connection_bound_to_a_real_location_is_refused(self):
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_connection_isolation(_StubConn(str(Path.home() / ".engram" / "engram.db")))
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_connection_isolation(_StubConn(str(BACKEND / "engram.db")))

    def test_temp_and_memory_connections_pass(self, tmp_path):
        enforce_connection_isolation(_StubConn(str(tmp_path / "x.db")))
        enforce_connection_isolation(_StubConn(""))

    def test_inert_outside_pytest(self):
        enforce_connection_isolation(_StubConn(str(BACKEND / "engram.db")), under_pytest=False)

    def test_real_sqlite_connection_in_temp_passes(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "real.db")
        try:
            enforce_connection_isolation(conn)
        finally:
            conn.close()

    def test_every_engine_connection_runs_the_guard(self, tmp_path, monkeypatch):
        """set_sqlite_pragma is the shared connect hook of the async and sync engines."""
        from app import database

        seen = []
        monkeypatch.setattr(database, "enforce_connection_isolation", seen.append)
        conn = sqlite3.connect(tmp_path / "hook.db")
        try:
            database.set_sqlite_pragma(conn, None)
        finally:
            conn.close()
        assert seen == [conn]

    def test_guard_runs_before_any_pragma_that_writes(self, monkeypatch):
        from app import database

        class Boom(Exception):
            pass

        def refuse(_conn):
            raise Boom

        class NoTouch:
            def cursor(self):
                raise AssertionError("a pragma ran before the isolation check")

        monkeypatch.setattr(database, "enforce_connection_isolation", refuse)
        with pytest.raises(Boom):
            database.set_sqlite_pragma(NoTouch(), None)


# --------------------------------------------------------------------------- #
# The live test session
# --------------------------------------------------------------------------- #


def test_session_database_is_isolated():
    """The properties every test relies on, checked on the process pytest is running in."""
    from app.config import settings
    from app.database import engine

    assert is_isolated_database_url(settings.database_url), settings.database_url
    assert is_isolated_database_url(str(engine.url)), str(engine.url)
    db_file = engine.url.database
    assert db_file and is_isolated_path(db_file)
    assert Path(db_file).resolve() != (BACKEND / "engram.db").resolve()
    assert os.environ["DATABASE_URL"] == settings.database_url


def test_sync_engine_is_isolated():
    from app.services import config_service

    sync_url = str(config_service._get_sync_engine().url)
    assert is_isolated_database_url(sync_url), sync_url


def test_building_a_sync_engine_for_a_real_database_is_refused_before_any_io():
    from app.services import config_service

    canary_dir = Path.home() / ".engram-isolation-canary"
    url = f"sqlite:///{canary_dir / 'engram.db'}"
    with pytest.raises(ProductionDatabaseAccessError):
        config_service._build_sync_engine(url)
    assert not canary_dir.exists()


def test_init_db_refuses_a_replaced_database_url_before_touching_anything(monkeypatch):
    """A test that swaps settings.database_url must not get a migration or backup."""
    from app import database

    canary_dir = Path.home() / ".engram-isolation-canary"
    monkeypatch.setattr(database.settings, "database_url", f"sqlite:///{canary_dir / 'engram.db'}")

    def must_not_run(*_a, **_k):
        raise AssertionError("init_db did work before the isolation check")

    monkeypatch.setattr(database, "backup_before_migrate", must_not_run)
    with pytest.raises(ProductionDatabaseAccessError):
        asyncio.run(database.init_db())
    assert not canary_dir.exists()


def test_temp_directory_is_where_pytest_databases_live():
    assert (
        Path(tempfile.gettempdir()).resolve()
        in Path(__import__("app.database", fromlist=["engine"]).engine.url.database)
        .resolve()
        .parents
    )


# --------------------------------------------------------------------------- #
# Subprocess proofs: fresh interpreters, throwaway working directories
# --------------------------------------------------------------------------- #


def _run(args, cwd, **env):
    full_env = {**os.environ, "PYTHONPATH": str(BACKEND), **env}
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def scratch(tmp_path):
    """A working directory for hostile runs; fails the test if a database appears."""
    work = tmp_path / "cwd"
    work.mkdir()
    CANARY.unlink(missing_ok=True)
    yield work
    assert not CANARY.exists(), "a hostile run created a database outside temp storage"
    CANARY.unlink(missing_ok=True)
    stray = sorted(p.name for p in work.iterdir() if p.suffix in {".db", ".sqlite"})
    assert not stray, f"a database was created in the hostile working directory: {stray}"


def test_importing_the_app_under_pytest_with_the_default_url_is_refused(scratch):
    """A pytest process whose DATABASE_URL points outside temp cannot even import the app."""
    proc = _run(["-c", "import pytest, app.config"], cwd=scratch, DATABASE_URL=CANARY_URL)
    assert proc.returncode != 0
    assert "ProductionDatabaseAccessError" in proc.stderr
    # The refusal happened at import; nothing was opened or created.
    assert not CANARY.exists()


def test_guard_is_inert_in_a_process_that_never_imported_pytest(scratch):
    """Production is unaffected: no pytest in sys.modules means no guard."""
    proc = _run(
        [
            "-c",
            "import sys; assert 'pytest' not in sys.modules; "
            "import app.config as c; print(c.settings.database_url)",
        ],
        cwd=scratch,
        DATABASE_URL=CANARY_URL,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == CANARY_URL
    assert not CANARY.exists()  # importing settings opens nothing


def test_conftest_overrides_a_hostile_ambient_database_url(scratch):
    """A developer's shell exporting DATABASE_URL (any real location) cannot reach a test."""
    proc = _run(
        [
            "-m",
            "pytest",
            "-c",
            str(BACKEND / "pytest.ini"),
            "--rootdir",
            str(BACKEND),
            "-p",
            "no:cacheprovider",
            "-q",
            f"{THIS_FILE}::test_session_database_is_isolated",
        ],
        cwd=scratch,
        DATABASE_URL=CANARY_URL,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout
    assert not CANARY.exists()


def test_a_pytest_run_that_bypasses_the_conftest_fails_closed(scratch):
    """Rootdir without tests/conftest.py (the bypass): the guard still stops the run."""
    probe = scratch / "test_probe.py"
    probe.write_text("def test_probe():\n    import app.config\n    import app.database\n")
    proc = _run(
        [
            "-m",
            "pytest",
            "--rootdir",
            str(scratch),
            "-c",
            os.devnull,
            "-p",
            "no:cacheprovider",
            "-q",
            str(probe),
        ],
        cwd=scratch,
        DATABASE_URL=CANARY_URL,
    )
    assert proc.returncode != 0, proc.stdout
    assert "ProductionDatabaseAccessError" in proc.stdout + proc.stderr
    assert not re.search(r"\b\d+ passed\b", proc.stdout)  # "bypassed" is in the message
    assert not CANARY.exists()
