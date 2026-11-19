"""Core pytest fixtures for UMA subtitle workflow tests."""

# --- Database isolation: MUST stay above every ``app`` import -------------------------
# ``app.config`` reads DATABASE_URL and ``app.database`` builds its engine at import. The
# default is the relative ``./engram.db``, i.e. the developer's REAL database when pytest
# runs from backend/. Point it at a throwaway file first, unconditionally (an ambient
# DATABASE_URL from a developer's shell is overridden too), register that directory as the
# only place a test may keep SQLite files, and guard sqlite3.connect itself. app/db_guard.py
# then refuses to run at all if anything resolves outside the registered directories.
# Regression tests: tests/unit/test_db_isolation.py.
import atexit
import os
import shutil
import tempfile
from pathlib import Path

from app import db_guard  # light: imports nothing else from the app

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="engram-pytest-db-")).resolve()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB_DIR / 'engram-test.db'}"
db_guard.allow_test_root(_TEST_DB_DIR)
db_guard.install_sqlite_guard()
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)
# ---------------------------------------------------------------------------------------

from unittest.mock import Mock  # noqa: E402

import pytest  # noqa: E402


def pytest_sessionstart(session):
    """Prove isolation before the first test runs; abort the whole session if it fails."""
    from app.config import settings
    from app.database import engine

    problems = db_guard.session_isolation_problems(
        **{"settings.database_url": settings.database_url, "engine.url": str(engine.url)}
    )
    if problems:
        pytest.exit(
            "Refusing to run: tests would touch a database outside the registered test "
            "directories (" + "; ".join(problems) + ").",
            returncode=3,
        )


@pytest.fixture(autouse=True, scope="session")
def _register_pytest_basetemp(tmp_path_factory):
    """Let tests keep SQLite files under pytest's own basetemp, and nowhere else."""
    db_guard.allow_test_root(tmp_path_factory.getbasetemp())


@pytest.fixture(autouse=True)
def _isolate_tmdb_persistent_cache(tmp_path, monkeypatch):
    """Redirect the on-disk TMDB cache to a tmp_path-scoped SQLite file.

    Without this, every test inherits the developer's real
    ``~/.engram/cache/tmdb_cache.sqlite`` — a populated row for "Breaking
    Bad" can silently satisfy ``fetch_show_id`` and consume zero of the
    ``mock_requests.side_effect`` items the test queued, leaving later
    ``requests.get`` calls without a mock and breaking the test for
    reasons unrelated to the change under test.
    """
    from app.matcher import tmdb_persistent_cache

    tmdb_persistent_cache.close()
    monkeypatch.setattr(tmdb_persistent_cache, "CACHE_DB_PATH", tmp_path / "tmdb_cache.sqlite")
    yield
    tmdb_persistent_cache.close()


@pytest.fixture(autouse=True)
def _isolate_transcript_store(tmp_path, monkeypatch):
    """Redirect the on-disk ASR transcript cache to a tmp_path-scoped SQLite file.

    Without this, every test inherits the developer's real
    ``~/.engram/cache/transcripts.sqlite`` — a populated row for a previously
    matched file could silently satisfy ``transcribe_chunk_cached`` lookups in
    unrelated matcher tests (zero ``model.transcribe`` calls where the test
    expects N), and tests would write their fake transcripts into the real
    cache.
    """
    from app.matcher import transcript_store

    transcript_store.reset_module_state_for_tests()
    monkeypatch.setattr(transcript_store, "CACHE_DB_PATH", tmp_path / "transcripts.sqlite")
    yield
    transcript_store.reset_module_state_for_tests()


@pytest.fixture
def temp_cache_dir(tmp_path):
    """Isolated cache directory for each test."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "data").mkdir()
    return cache_dir


@pytest.fixture
def populated_cache_dir(temp_cache_dir):
    """Cache with sample subtitle files for cache hit tests."""
    show_dir = temp_cache_dir / "data" / "Breaking_Bad"
    show_dir.mkdir(parents=True)
    for ep in range(1, 4):
        subtitle_file = show_dir / f"Breaking_Bad - S01E{ep:02d}.srt"
        subtitle_file.write_text(
            f"1\n00:00:00,000 --> 00:00:02,000\nSubtitle content for episode {ep}\n"
        )
    return temp_cache_dir


@pytest.fixture
def mock_config(temp_cache_dir):
    """Mock Config object with test paths."""
    from app.matcher.models import Config

    return Config(
        tmdb_api_key="test_key_12345678901234567890123456789012",
        cache_dir=temp_cache_dir,
        min_confidence=0.7,
    )


@pytest.fixture
def mock_tmdb_responses():
    """Pre-built TMDB API response data."""
    return {
        "arrested_development": {"results": [{"id": 4589, "name": "Arrested Development"}]},
        "the_office": {"results": [{"id": 2316, "name": "The Office"}]},
        "season_details": {
            "season_number": 1,
            "episodes": [
                {"episode_number": 1, "name": "Pilot"},
                {"episode_number": 2, "name": "Top Banana"},
                {"episode_number": 3, "name": "Bringing Up Buster"},
            ],
        },
        "empty": {"results": []},
    }


@pytest.fixture
def mock_subtitle():
    """Mock subtitle object for testing."""
    subtitle = Mock()
    subtitle.language = "English"
    subtitle.version = "WEB"
    subtitle.download_url = "http://example.com/subtitle.srt"
    subtitle.downloads = 1000
    return subtitle


@pytest.fixture
def mock_addic7ed_html():
    """Sample Addic7ed HTML page for parsing tests."""
    return """
    <html>
    <body>
        <table class="tabel95">
            <tr>
                <td class="language">English</td>
                <td>WEB</td>
                <td><a href="/subtitle/123">Download</a></td>
                <td>1000 Downloads</td>
            </tr>
        </table>
    </body>
    </html>
    """
