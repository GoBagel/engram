"""Rolling settings snapshots: a lost configuration can be restored.

The configuration a user types in has no copy between database migrations. A snapshot, an
ordinary settings file with secrets included, is written when settings are saved and at
startup. These tests pin when one is written, when it is not, what it contains, how it is
protected, that it can never break a save, and that it restores through the Settings import.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.db_guard import ProductionDatabaseAccessError
from app.models.app_config import AppConfig
from app.services import config_snapshots as snaps
from app.services import config_transfer as ct
from app.services.config_service import get_config, update_config
from tests.unit.conftest import _unit_session_factory

TOKEN = "eyJsecret-token-value-1234567890"
NAME = r"settings-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{12}\.json"


@pytest.fixture
def snap_dir(tmp_path, monkeypatch) -> Path:
    """Point the database URL at a throwaway file; snapshots land beside it."""
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'engram.db'}")
    return tmp_path / "engram.db.config-snapshots"


async def _seed(**fields) -> None:
    async with _unit_session_factory() as session:
        session.add(AppConfig(**fields))
        await session.commit()


async def _wipe_to_defaults() -> None:
    from sqlalchemy import delete

    async with _unit_session_factory() as session:
        await session.execute(delete(AppConfig))
        session.add(AppConfig())
        await session.commit()


def _files(directory: Path) -> list[Path]:
    return snaps.list_snapshots(directory) if directory.exists() else []


def _export(**settings_) -> dict:
    return {"format": "engram-settings", "schema_version": 1, "settings": settings_}


# --------------------------------------------------------------------------- #
# When a snapshot is written
# --------------------------------------------------------------------------- #


async def test_a_configuration_with_a_secret_is_snapshotted(snap_dir):
    await _seed(tmdb_api_key=TOKEN)
    path = await snaps.snapshot_current_config("test")
    assert path is not None and path.parent == snap_dir
    assert snaps._NAME.fullmatch(path.name)
    assert [p.name for p in _files(snap_dir)] == [path.name]


async def test_a_default_configuration_is_never_snapshotted(snap_dir):
    """A wipe leaves a default row; snapshotting it would push good copies out of the rotation."""
    await _seed()  # nothing the user supplied
    assert await snaps.snapshot_current_config("test") is None
    assert not snap_dir.exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"tmdb_api_key": TOKEN},
        {"makemkv_key": "T-abc"},
        {"opensubtitles_password": "pw"},
        {"discord_webhook_url": "https://discord.com/api/webhooks/1/x"},
        {"opensubtitles_username": "someone"},
        {"ai_local_base_url": "http://localhost:11434/v1"},  # a local AI server and nothing else
        {"ai_model": "qwen2.5-coder:7b"},
        {"setup_complete": True},
    ],
)
async def test_each_kind_of_user_supplied_setting_counts(fields, snap_dir):
    await _seed(**fields)
    assert await snaps.snapshot_current_config("test") is not None


def test_whitespace_alone_is_not_content():
    assert not snaps.has_user_content(AppConfig(tmdb_api_key="   ", opensubtitles_username="\t"))
    assert snaps.has_user_content(AppConfig(tmdb_api_key=" x "))


async def test_reading_the_configuration_never_creates_one(snap_dir):
    async with _unit_session_factory() as session:
        before = (await session.execute(select(func.count()).select_from(AppConfig))).scalar_one()
    assert before == 0
    assert await snaps.snapshot_current_config("startup") is None
    async with _unit_session_factory() as session:
        after = (await session.execute(select(func.count()).select_from(AppConfig))).scalar_one()
    assert after == 0


async def test_saving_settings_writes_a_snapshot(snap_dir):
    await _seed()
    await update_config(tmdb_api_key=TOKEN)
    assert len(_files(snap_dir)) == 1


# --------------------------------------------------------------------------- #
# When one is not
# --------------------------------------------------------------------------- #


async def test_saving_the_same_values_again_writes_nothing(snap_dir):
    await _seed(tmdb_api_key=TOKEN)
    assert await snaps.snapshot_current_config("a") is not None
    assert await snaps.snapshot_current_config("b") is None
    await update_config(tmdb_api_key=TOKEN)  # a save that changes nothing
    assert len(_files(snap_dir)) == 1


async def test_a_changed_setting_writes_a_new_snapshot(snap_dir):
    await _seed(tmdb_api_key=TOKEN)
    await snaps.snapshot_current_config("a")
    await update_config(always_review=True)
    assert len(_files(snap_dir)) == 2


async def test_returning_to_an_earlier_value_is_a_new_snapshot(snap_dir):
    """A -> B -> A: comparing only with the newest snapshot (B) means A is kept again."""
    await _seed(tmdb_api_key=TOKEN, always_review=False)
    await snaps.snapshot_current_config("a")
    await update_config(always_review=True)
    await update_config(always_review=False)
    assert len(_files(snap_dir)) == 3
    hashes = [snaps._NAME.fullmatch(p.name).group(2) for p in _files(snap_dir)]
    assert hashes[0] == hashes[2] != hashes[1]


async def test_the_export_time_does_not_make_two_saves_differ(snap_dir):
    a = snaps._settings_hash({"settings": {"k": 1}, "exported_at": "2026-01-01T00:00:00Z"})
    b = snaps._settings_hash({"settings": {"k": 1}, "exported_at": "2027-06-06T06:06:06Z"})
    assert a == b


async def test_an_in_memory_database_has_no_snapshots():
    assert snaps.snapshot_dir("sqlite+aiosqlite:///:memory:") is None
    assert snaps.write_snapshot("sqlite+aiosqlite:///:memory:", _export(k=1)) is None


# --------------------------------------------------------------------------- #
# What is in one, and how it is protected
# --------------------------------------------------------------------------- #


async def test_a_snapshot_is_a_settings_file_with_secrets_included(snap_dir):
    await _seed(tmdb_api_key=TOKEN, opensubtitles_username="someone", always_review=True)
    path = await snaps.snapshot_current_config("test")
    data = json.loads(path.read_text(encoding="utf-8"))
    parsed, warnings = ct.parse_settings_file(data)  # the Settings import accepts it as it is
    assert warnings == []
    assert data["format"] == ct.FILE_FORMAT and data["includes_secrets"] is True
    assert parsed["tmdb_api_key"] == TOKEN
    assert parsed["opensubtitles_username"] == "someone"
    assert parsed["always_review"] is True
    assert "setup_complete" not in parsed  # per-install state is never in a settings file


async def test_the_file_is_identical_to_what_the_user_export_writes(snap_dir):
    await _seed(tmdb_api_key=TOKEN)
    path = await snaps.snapshot_current_config("test")
    from_snapshot = json.loads(path.read_text(encoding="utf-8"))["settings"]
    from_export = (await ct.build_export(include_secrets=True))["settings"]
    assert from_snapshot == from_export


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
async def test_secrets_are_not_readable_by_other_users(snap_dir):
    await _seed(tmdb_api_key=TOKEN)
    path = await snaps.snapshot_current_config("test")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(snap_dir.stat().st_mode) == 0o700


async def test_the_file_is_never_world_readable_even_for_a_moment(snap_dir, monkeypatch):
    """The temp file is created 0600 (not chmod-ed afterwards), so there is no open window."""
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **k):
        if str(path).endswith(".tmp"):
            modes.append(mode)
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(snaps.os, "open", spy)
    snaps.write_snapshot(settings.database_url, _export(k=1))
    assert modes == [0o600]


async def test_no_temporary_files_are_left_behind(snap_dir):
    await _seed(tmdb_api_key=TOKEN)
    await snaps.snapshot_current_config("test")
    assert [p.name for p in snap_dir.iterdir() if p.name.endswith(".tmp")] == []


async def test_secret_values_never_reach_the_log(snap_dir):
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        await _seed()
        await update_config(tmdb_api_key=TOKEN, opensubtitles_password="hunter2-pw")
    finally:
        logger.remove(sink)
    text = "\n".join(messages)
    assert "Saved a settings snapshot" in text
    assert TOKEN not in text and "hunter2-pw" not in text


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #


def test_only_the_newest_snapshots_are_kept(snap_dir):
    paths = [snaps.write_snapshot(settings.database_url, _export(n=i), keep=3) for i in range(7)]
    kept = _files(snap_dir)
    assert len(kept) == 3
    assert [p.name for p in kept] == [p.name for p in paths[-3:]]


def test_files_that_are_not_snapshots_are_never_pruned(snap_dir):
    snap_dir.mkdir()
    (snap_dir / "notes.txt").write_text("mine")
    (snap_dir / "settings-by-hand.json").write_text("{}")
    for i in range(5):
        snaps.write_snapshot(settings.database_url, _export(n=i), keep=2)
    assert (snap_dir / "notes.txt").exists() and (snap_dir / "settings-by-hand.json").exists()
    assert len(_files(snap_dir)) == 2


def test_the_default_keeps_ten(snap_dir):
    for i in range(13):
        snaps.write_snapshot(settings.database_url, _export(n=i))
    assert snaps.SNAPSHOT_KEEP == 10 and len(_files(snap_dir)) == 10


# --------------------------------------------------------------------------- #
# It never breaks a save
# --------------------------------------------------------------------------- #


async def test_a_failing_write_does_not_stop_the_save(snap_dir, monkeypatch):
    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(snaps.os, "replace", boom)
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        await _seed()
        saved = await update_config(tmdb_api_key=TOKEN)
    finally:
        logger.remove(sink)
    assert saved.tmdb_api_key == TOKEN  # the settings were saved
    assert (await get_config()).tmdb_api_key == TOKEN
    assert _files(snap_dir) == []
    assert any("Could not write a settings snapshot" in m for m in messages)
    assert not [p for p in snap_dir.iterdir() if p.name.endswith(".tmp")]


async def test_an_unexpected_error_does_not_stop_the_save(snap_dir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(snaps, "export_from_config", boom)
    await _seed()
    saved = await update_config(tmdb_api_key=TOKEN)
    assert saved.tmdb_api_key == TOKEN


async def test_an_unreadable_configuration_does_not_stop_startup(snap_dir, monkeypatch):
    async def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(snaps, "get_config_if_exists", boom)
    assert await snaps.snapshot_current_config("startup") is None


# --------------------------------------------------------------------------- #
# The pytest guard: a test can never write next to a real database
# --------------------------------------------------------------------------- #


async def test_a_snapshot_folder_outside_the_test_directories_is_refused(monkeypatch):
    # A canary inside backend/ (an unregistered location, so the guard must refuse it), never
    # in a real home directory: if the guard were broken, this test must not be able to write
    # next to anyone's real data. It fails the test and is removed.
    canary = Path(__file__).resolve().parents[2] / "isolation-canary-snapshots"
    shutil.rmtree(canary, ignore_errors=True)
    try:
        monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{canary / 'engram.db'}")
        with pytest.raises(ProductionDatabaseAccessError):
            snaps.write_snapshot(settings.database_url, _export(k=1))
        await _seed(tmdb_api_key=TOKEN)
        with pytest.raises(ProductionDatabaseAccessError):
            await snaps.snapshot_current_config("test")
        with pytest.raises(ProductionDatabaseAccessError):
            await update_config(always_review=True)
        assert not canary.exists(), "a snapshot folder was created outside the test directories"
    finally:
        shutil.rmtree(canary, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Restoring: the existing Settings import
# --------------------------------------------------------------------------- #


async def test_a_snapshot_restores_a_wiped_configuration_through_the_import(snap_dir):
    """The incident: the configured row is replaced by defaults. The snapshot brings it back."""
    seeded = {
        "tmdb_api_key": TOKEN,
        "opensubtitles_username": "someone",
        "opensubtitles_password": "pw-secret",
        "ai_provider": "ollama",
        "ai_model": "qwen2.5-coder:7b",
        "always_review": True,
    }
    await _seed(**seeded)
    path = await snaps.snapshot_current_config("before the wipe")
    assert path is not None

    await _wipe_to_defaults()
    wiped = await get_config()
    assert wiped.tmdb_api_key == "" and wiped.ai_provider != "ollama"

    data = json.loads(path.read_text(encoding="utf-8"))
    applied = await ct.apply_import(data, list(seeded))
    assert set(applied) == set(seeded)
    restored = await get_config()
    for key, value in seeded.items():
        assert getattr(restored, key) == value, key


async def test_the_import_preview_reads_a_snapshot(snap_dir):
    await _seed(tmdb_api_key=TOKEN, ai_provider="ollama")
    path = await snaps.snapshot_current_config("test")
    preview = await ct.preview_import(json.loads(path.read_text(encoding="utf-8")))
    keys = {row["key"] for row in preview["fields"]}
    assert {"tmdb_api_key", "ai_provider"} <= keys
    assert preview["unknown"] == []
