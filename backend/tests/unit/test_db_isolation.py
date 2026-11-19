"""Regression tests: pytest must never open a real Engram database.

Background: a full backend suite run from ``backend/`` once wrote fixture rows into
the developer's real ``engram.db`` (the default ``./engram.db`` is relative to the
working directory, and only the unit suite patched the session). ``app/db_guard.py``
now fails closed, and ``tests/conftest.py`` points ``DATABASE_URL`` at a throwaway
file and registers the only directories a test may keep SQLite files in.

Nothing here opens a database outside registered test directories. "Forbidden" targets
are a canary file that is deliberately NOT named ``engram.db`` and sits in ``backend/``
(an unregistered location), or paths that are only ever parsed. Every test that must prove
a refusal also asserts the canary was never created, so a guard bug cannot touch a real
database: it would at worst create the canary, which fails the test.
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
from sqlalchemy import MetaData, create_engine, event, text

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
CANARY = BACKEND / "isolation-canary.db"
CANARY_URL = f"sqlite+aiosqlite:///{CANARY}"


def _home_db_url() -> str:
    """A URL for a database in the user's home. Only ever parsed or refused, never opened."""
    return f"sqlite+aiosqlite:///{Path.home() / '.engram' / 'engram.db'}"


@pytest.fixture(autouse=True)
def _canary_guard():
    """Any test that leaves the canary behind has let something open an unregistered file."""
    CANARY.unlink(missing_ok=True)
    yield
    existed = CANARY.exists()
    CANARY.unlink(missing_ok=True)
    assert not existed, "a database was created outside the registered test directories"


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


class TestIsolatedDatabaseUrl:
    def test_in_memory_urls_are_isolated(self):
        assert is_isolated_database_url("sqlite+aiosqlite:///:memory:")
        assert is_isolated_database_url("sqlite://")
        assert is_isolated_database_url("sqlite+aiosqlite://")

    def test_file_in_a_registered_root_is_isolated(self, tmp_path):
        # pytest's basetemp is registered by tests/conftest.py.
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
        assert not is_isolated_database_url(_home_db_url())
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{BACKEND / 'engram.db'}")
        assert not is_isolated_database_url("sqlite+aiosqlite:////var/lib/engram/engram.db")

    def test_dotdot_traversal_out_of_a_root_is_refused(self, tmp_path):
        up = "/".join([".."] * (len(tmp_path.parts) + 2))
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{tmp_path}/{up}/home/x/e.db")

    def test_symlink_out_of_a_root_is_refused(self, tmp_path):
        link = tmp_path / "link"
        link.symlink_to(Path.home(), target_is_directory=True)
        assert not is_isolated_path(link / "engram.db")
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{link / 'engram.db'}")

    def test_relative_path_is_judged_by_where_it_resolves(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert is_isolated_database_url("sqlite+aiosqlite:///./engram.db")
        monkeypatch.chdir(BACKEND)
        assert not is_isolated_database_url("sqlite+aiosqlite:///./engram.db")

    def test_the_system_temp_dir_is_not_trusted(self, tmp_path_factory):
        """TMPDIR is environment-controlled, so being under it proves nothing."""
        base = tmp_path_factory.getbasetemp()
        for outside in (
            Path(tempfile.gettempdir()) / "engram-unregistered.db",
            base.parent / "engram-sibling.db",  # beside pytest's basetemp, not inside it
        ):
            assert not is_isolated_database_url(f"sqlite+aiosqlite:///{outside}"), outside

    def test_pointing_the_temp_dir_somewhere_else_does_not_widen_the_rule(self, monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(Path.home()))
        monkeypatch.setenv("TMPDIR", str(BACKEND))
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{Path.home() / 'x.db'}")
        assert not is_isolated_database_url(f"sqlite+aiosqlite:///{BACKEND / 'engram.db'}")

    def test_only_registered_roots_count_wherever_the_checkout_lives(self, tmp_path):
        # tmp_path.parent is pytest's basetemp (registered); its parent is not.
        assert is_isolated_path(tmp_path.parent / "inside-basetemp" / "engram.db")
        assert not is_isolated_path(tmp_path.parent.parent / "never-registered" / "engram.db")


# --------------------------------------------------------------------------- #
# Enforcement and messages
# --------------------------------------------------------------------------- #


class TestEnforcement:
    def test_refuses_a_real_database_when_under_pytest(self):
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_test_database_isolation(_home_db_url())

    def test_default_dev_database_is_refused_from_backend_dir(self, monkeypatch):
        monkeypatch.chdir(BACKEND)
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_test_database_isolation("sqlite+aiosqlite:///./engram.db")

    def test_accepts_registered_and_memory(self, tmp_path):
        enforce_test_database_isolation(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        enforce_test_database_isolation("sqlite+aiosqlite:///:memory:")

    def test_is_inert_outside_pytest(self):
        """Production never imports pytest, so the guard must do nothing there."""
        enforce_test_database_isolation(_home_db_url(), under_pytest=False)

    def test_pytest_detection_is_true_in_this_process(self):
        assert db_guard.running_under_pytest()

    def test_message_does_not_leak_credentials_or_query_secrets(self):
        url = "postgresql://admin:hunter2@db.example/engram?password=QUERY_SECRET&sslkey=K3Y"
        with pytest.raises(ProductionDatabaseAccessError) as exc:
            enforce_test_database_isolation(url)
        message = str(exc.value)
        for secret in ("hunter2", "QUERY_SECRET", "K3Y", "admin", "db.example"):
            assert secret not in message, secret

    def test_message_makes_no_claim_about_what_was_touched(self):
        """The old text promised the real database was 'NOT touched'; that is unprovable."""
        with pytest.raises(ProductionDatabaseAccessError) as exc:
            enforce_test_database_isolation(_home_db_url())
        assert "not touched" not in str(exc.value).lower()

    def test_session_problem_report_names_each_bad_url_without_secrets(self):
        problems = db_guard.session_isolation_problems(
            a="sqlite+aiosqlite:///:memory:", b="postgresql://u:pw@h/db?x=SECRET"
        )
        assert len(problems) == 1 and problems[0].startswith("b = ")
        assert "pw" not in problems[0] and "SECRET" not in problems[0]


# --------------------------------------------------------------------------- #
# The sqlite3.connect guard: every opener, before any file exists
# --------------------------------------------------------------------------- #


class TestSqliteConnectGuard:
    def test_installed_on_both_entry_points(self):
        assert getattr(sqlite3.connect, "_engram_guard", False)
        assert getattr(sqlite3.dbapi2.connect, "_engram_guard", False)  # what SQLAlchemy calls

    def test_install_is_idempotent(self):
        before = sqlite3.connect
        assert db_guard.install_sqlite_guard()
        assert sqlite3.connect is before

    @pytest.mark.parametrize("connect", [sqlite3.connect, sqlite3.dbapi2.connect])
    def test_unregistered_files_are_refused_before_creation(self, connect):
        with pytest.raises(ProductionDatabaseAccessError):
            connect(str(CANARY))
        with pytest.raises(ProductionDatabaseAccessError):
            connect(CANARY)  # os.PathLike
        with pytest.raises(ProductionDatabaseAccessError):
            connect(os.fsencode(CANARY))
        with pytest.raises(ProductionDatabaseAccessError):
            connect(f"file:{CANARY}?mode=rwc", uri=True)
        assert not CANARY.exists()

    def test_uri_given_positionally_is_still_checked(self):
        with pytest.raises(ProductionDatabaseAccessError):
            sqlite3.connect(f"file:{CANARY}?mode=rwc", 5.0, 0, "DEFERRED", True, None, 128, True)

    def test_registered_files_and_memory_are_allowed(self, tmp_path):
        for target, kwargs in [
            (tmp_path / "a.db", {}),
            (str(tmp_path / "b.db"), {}),
            (f"file:{tmp_path / 'c.db'}?mode=rwc", {"uri": True}),
            (":memory:", {}),
            ("", {}),
            ("file::memory:?cache=shared", {"uri": True}),
        ]:
            conn = sqlite3.connect(target, **kwargs)
            conn.execute("select 1")
            conn.close()

    def test_attach_is_disabled_so_a_checked_connection_cannot_open_a_second_file(self, tmp_path):
        conn = sqlite3.connect(":memory:")
        try:
            for target in (CANARY, tmp_path / "second.db"):
                with pytest.raises(sqlite3.Error):
                    conn.execute(f"ATTACH DATABASE '{target}' AS other")
        finally:
            conn.close()
        assert not (tmp_path / "second.db").exists()

    def test_attach_through_an_engine_with_the_shared_hook_is_blocked(self):
        from app.database import set_sqlite_pragma

        engine = create_engine("sqlite://")
        event.listens_for(engine, "connect")(set_sqlite_pragma)
        with engine.connect() as conn, pytest.raises(Exception, match="attached"):
            conn.execute(text(f"ATTACH DATABASE '{CANARY}' AS other"))
        engine.dispose()

    def test_a_hand_built_engine_for_an_unregistered_file_never_creates_it(self):
        """Codex: the connect hook ran after SQLite had already created the file."""
        from app.database import set_sqlite_pragma

        engine = create_engine(f"sqlite:///{CANARY}")
        event.listens_for(engine, "connect")(set_sqlite_pragma)
        with pytest.raises(ProductionDatabaseAccessError):
            engine.connect()
        engine.dispose()

    def test_a_sync_engine_for_an_unregistered_file_never_creates_it(self):
        engine = create_engine(f"sqlite:///{CANARY}")
        with pytest.raises(ProductionDatabaseAccessError), engine.connect() as conn:
            conn.execute(text("create table t (x)"))
        engine.dispose()


class TestOpenersThatUsedToBypassTheUrlChecks:
    """Codex reproduced each of these creating a database outside the allowed root."""

    def test_backup_helper_cannot_open_an_unregistered_database(self):
        from app.database_backup import pending_schema_changes

        with pytest.raises(ProductionDatabaseAccessError):
            pending_schema_changes(CANARY, MetaData())

    def test_backup_copy_cannot_write_to_an_unregistered_destination(self, tmp_path):
        from app.database_backup import _copy_database

        source = tmp_path / "src.db"
        sqlite3.connect(source).close()
        with pytest.raises(ProductionDatabaseAccessError):
            _copy_database(source, CANARY)

    def test_tmdb_cache_cannot_open_an_unregistered_database(self, monkeypatch):
        from app.matcher import tmdb_persistent_cache

        tmdb_persistent_cache.close()
        monkeypatch.setattr(tmdb_persistent_cache, "CACHE_DB_PATH", CANARY)
        with pytest.raises(ProductionDatabaseAccessError):
            tmdb_persistent_cache.get_conn()

    def test_transcript_store_cannot_open_an_unregistered_database(self, monkeypatch):
        from app.matcher import transcript_store

        transcript_store.reset_module_state_for_tests()
        monkeypatch.setattr(transcript_store, "CACHE_DB_PATH", CANARY)
        try:
            transcript_store._get_conn()
        except Exception:  # noqa: BLE001 - the store may degrade; the file must not appear
            pass
        transcript_store.reset_module_state_for_tests()

    def test_alembic_helper_cannot_open_a_replaced_database_url(self, monkeypatch):
        from app import database

        monkeypatch.setattr(database.settings, "database_url", f"sqlite:///{CANARY}")
        try:
            database._run_alembic_upgrade()
        except Exception:  # noqa: BLE001 - raising or logging are both fine; no file may appear
            pass


# --------------------------------------------------------------------------- #
# Connection-level check (second opinion behind the wrapper)
# --------------------------------------------------------------------------- #


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
    def __init__(self, *files):
        self._rows = [(i, name, file) for i, (name, file) in enumerate(files)]

    def cursor(self):
        return _StubCursor(self._rows)


class TestConnectionLevelCheck:
    def test_connection_bound_to_an_unregistered_location_is_refused(self):
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_connection_isolation(_StubConn(("main", str(Path.home() / "e.db"))))
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_connection_isolation(_StubConn(("main", str(BACKEND / "engram.db"))))

    def test_an_attached_database_is_inspected_not_only_main(self, tmp_path):
        """Codex: the check ignored every schema except main and temp."""
        with pytest.raises(ProductionDatabaseAccessError):
            enforce_connection_isolation(
                _StubConn(("main", str(tmp_path / "ok.db")), ("other", str(CANARY)))
            )

    def test_registered_and_memory_connections_pass(self, tmp_path):
        enforce_connection_isolation(_StubConn(("main", str(tmp_path / "x.db"))))
        enforce_connection_isolation(_StubConn(("main", "")))

    def test_inert_outside_pytest(self):
        enforce_connection_isolation(
            _StubConn(("main", str(BACKEND / "engram.db"))), under_pytest=False
        )

    def test_real_sqlite_connection_in_a_registered_root_passes(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "real.db")
        try:
            enforce_connection_isolation(conn)
        finally:
            conn.close()

    def test_every_engine_connection_runs_the_check(self, tmp_path, monkeypatch):
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

    def test_check_runs_before_any_pragma_that_writes(self, monkeypatch):
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
    db_file = Path(engine.url.database)
    assert is_isolated_path(db_file)
    assert db_file.resolve() != (BACKEND / "engram.db").resolve()
    assert os.environ["DATABASE_URL"] == settings.database_url


def test_session_database_lives_in_the_bootstrap_directory():
    """Pins the bootstrap: removing it leaves the default ./engram.db, which is refused."""
    from app.database import engine

    assert Path(engine.url.database).parent.name.startswith("engram-pytest-db-")
    assert Path(engine.url.database).parent in db_guard.allowed_test_roots()


def test_sync_engine_factory_builds_and_uses_a_registered_database(tmp_path):
    """Exercises the real factory (the session's cached sync engine is replaced by a fixture)."""
    from app.services import config_service

    engine = config_service._build_sync_engine(f"sqlite:///{tmp_path / 'factory.db'}")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("select 1")).scalar() == 1
    finally:
        engine.dispose()


def test_sync_engine_factory_refuses_a_real_database_before_any_io():
    from app.services import config_service

    canary_dir = Path.home() / ".engram-isolation-canary"
    with pytest.raises(ProductionDatabaseAccessError):
        config_service._build_sync_engine(f"sqlite:///{canary_dir / 'engram.db'}")
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


def test_sessionstart_hook_aborts_the_run_when_the_engine_is_not_isolated(monkeypatch):
    """The conftest hook turns a failed check into pytest.exit; it must stay wired up."""
    from app import config

    root_conftest = sys.modules["tests.conftest"]
    monkeypatch.setattr(config.settings, "database_url", _home_db_url())
    with pytest.raises(pytest.exit.Exception) as exc:
        root_conftest.pytest_sessionstart(None)
    assert exc.value.returncode == 3
    assert "Refusing to run" in str(exc.value)


# --------------------------------------------------------------------------- #
# Subprocess proofs: fresh interpreters, throwaway working directories
# --------------------------------------------------------------------------- #


def _run(args, cwd, timeout=180, **env):
    full_env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    for key, value in env.items():
        if value is None:
            full_env.pop(key, None)
        else:
            full_env[key] = value
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def scratch(tmp_path):
    work = tmp_path / "cwd"
    work.mkdir()
    return work


def _nested_pytest(target, cwd, **env):
    return _run(
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
            target,
        ],
        cwd=cwd,
        **env,
    )


def test_importing_the_app_under_pytest_with_an_unregistered_url_is_refused(scratch):
    proc = _run(["-c", "import pytest, app.config"], cwd=scratch, DATABASE_URL=CANARY_URL)
    assert proc.returncode != 0
    assert "ProductionDatabaseAccessError" in proc.stderr


def test_guard_and_wrapper_are_inert_in_a_process_that_never_imported_pytest(scratch):
    """Production is unaffected: no pytest in sys.modules means no guard and no wrapper."""
    proc = _run(
        [
            "-c",
            "import sys, sqlite3; assert 'pytest' not in sys.modules\n"
            "import app.config as c, app.database\n"
            "assert not getattr(sqlite3.connect, '_engram_guard', False), 'sqlite3 was wrapped'\n"
            "assert not getattr(sqlite3.dbapi2.connect, '_engram_guard', False)\n"
            "print(c.settings.database_url)",
        ],
        cwd=scratch,
        DATABASE_URL=CANARY_URL,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == CANARY_URL  # importing settings and the engine opens nothing


def test_conftest_overrides_a_hostile_ambient_database_url(scratch):
    """A developer's shell exporting DATABASE_URL (any real location) cannot reach a test."""
    proc = _nested_pytest(
        f"{THIS_FILE}::test_session_database_is_isolated", scratch, DATABASE_URL=CANARY_URL
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"\b1 passed\b", proc.stdout)


def test_the_default_url_is_replaced_even_when_nothing_is_exported(scratch):
    """Pins the bootstrap itself: with no ambient URL the default ./engram.db would be used."""
    proc = _nested_pytest(
        f"{THIS_FILE}::test_session_database_lives_in_the_bootstrap_directory",
        scratch,
        DATABASE_URL=None,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"\b1 passed\b", proc.stdout)
    assert not (scratch / "engram.db").exists()


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


def test_a_refused_async_connection_does_not_hang_the_process(scratch):
    """aiosqlite opens the file on a worker thread; a refusal there must not leave it running."""
    script = (
        "import asyncio, pytest\n"
        "from app import db_guard\n"
        "db_guard.install_sqlite_guard()\n"
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "async def main():\n"
        f"    engine = create_async_engine({CANARY_URL!r})\n"
        "    try:\n"
        "        async with engine.connect():\n"
        "            pass\n"
        "    except db_guard.ProductionDatabaseAccessError:\n"
        "        print('refused')\n"
        "    await engine.dispose()\n"
        "asyncio.run(main())\n"
    )
    proc = _run(["-c", script], cwd=scratch, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "refused" in proc.stdout
