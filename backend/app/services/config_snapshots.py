"""Rolling snapshots of the user's settings, so a lost configuration can be restored.

Why. ``engram.db`` holds credentials and settings a user enters by hand (MakeMKV key, TMDB
token, AI and OpenSubtitles keys, the local AI server). The pre-migration database backups
only exist when a startup is about to migrate, so a configuration that is overwritten or
wiped *between* migrations has no copy at all. A snapshot is written whenever the settings
are saved, and once at startup, so there is always a recent one.

What a snapshot is. An ordinary settings file, byte-for-byte what Settings -> Export writes
with keys and passwords included (``config_transfer.export_from_config``). Restoring one is
the existing Settings -> Import: the file is previewed field by field and applied through the
same validators as ``PUT /api/config``. There is no second format and no second restore path.

Where. ``<database>.config-snapshots/settings-<UTC timestamp>-<hash>.json`` beside the
database (``~/.engram`` for the packaged app). The files hold the same plaintext secrets the
database does, so they get the same trust boundary: the directory is created ``0700`` and the
files ``0600`` (POSIX; Windows has no equivalent, the user's profile folder is the boundary).

Rules that keep it useful:

- Only a configuration with something the user supplied is snapshotted (a key, a password,
  the local AI server, or a finished wizard). A default row left by a wipe is never
  written, so it cannot push good snapshots out of the rotation.
- Only when the settings actually changed since the newest snapshot (a hash of the settings,
  not of the export time), so saving the same values twice writes nothing.
- The newest ``SNAPSHOT_KEEP`` are kept. Every snapshot has content by construction, so the
  rotation is never filled with blanks.
- It never blocks a save: any problem writing is logged as a warning (key names only, never
  values) and the save carries on. The one exception is the pytest guard
  (``app/db_guard.py``): a snapshot directory outside the test run's registered directories
  raises, so a test can never write next to a real database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from app.database_backup import sqlite_file_path
from app.db_guard import ProductionDatabaseAccessError, enforce_test_database_isolation
from app.services.config_service import SECRET_FIELDS, get_config_if_exists
from app.services.config_transfer import export_from_config

__all__ = [
    "SNAPSHOT_DIR_SUFFIX",
    "SNAPSHOT_KEEP",
    "has_user_content",
    "list_snapshots",
    "snapshot_config",
    "snapshot_current_config",
    "snapshot_dir",
    "write_snapshot",
]

SNAPSHOT_KEEP = 10
SNAPSHOT_DIR_SUFFIX = ".config-snapshots"

# Settings a user types in (beyond the credentials): a configuration holding any of these is
# worth keeping even if no key has been entered, e.g. a local AI server and nothing else.
_USER_SUPPLIED = SECRET_FIELDS | {"opensubtitles_username", "ai_local_base_url", "ai_model"}
_NAME = re.compile(r"settings-(\d{8}T\d{6}\.\d{6}Z)-([0-9a-f]{12})\.json")


def snapshot_dir(database_url: str) -> Path | None:
    """The snapshot folder for this database, or None for an in-memory/non-SQLite URL."""
    db_path = sqlite_file_path(database_url)
    if db_path is None:
        return None
    return db_path.with_name(db_path.name + SNAPSHOT_DIR_SUFFIX)


def has_user_content(config) -> bool:
    """True if ``config`` holds anything the user supplied (so it is worth a snapshot)."""
    if getattr(config, "setup_complete", False):
        return True
    for key in _USER_SUPPLIED:
        value = getattr(config, key, "")
        if isinstance(value, str) and value.strip():
            return True
    return False


def _settings_hash(export: dict) -> str:
    """A fingerprint of the settings only: the export time must not make two saves differ."""
    canonical = json.dumps(export.get("settings", {}), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def list_snapshots(directory: Path) -> list[Path]:
    """Snapshot files in ``directory``, oldest first. Anything not named like one is ignored."""
    try:
        files = [p for p in directory.iterdir() if p.is_file() and _NAME.fullmatch(p.name)]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name)


def _write_private(path: Path, text: str) -> None:
    """Write ``path`` atomically with mode 0600 (no moment where it is world-readable)."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_snapshot(database_url: str, export: dict, *, keep: int = SNAPSHOT_KEEP) -> Path | None:
    """Write ``export`` as the newest snapshot, then prune. Returns the path, or None.

    None means nothing was written: in-memory database, settings unchanged since the newest
    snapshot, or a problem (logged). Never raises, except the pytest guard's error.
    """
    enforce_test_database_isolation(database_url)
    directory = snapshot_dir(database_url)
    if directory is None:
        return None
    try:
        digest = _settings_hash(export)
        existing = list_snapshots(directory)
        if existing:
            newest = _NAME.fullmatch(existing[-1].name)
            if newest and newest.group(2) == digest:
                return None  # the settings are what the newest snapshot already holds
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        path = directory / f"settings-{stamp}-{digest}.json"
        _write_private(path, json.dumps(export, indent=2, ensure_ascii=False, default=str) + "\n")
        for old in list_snapshots(directory)[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        return path
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"Could not write a settings snapshot (the save is not affected): {e}")
        return None


async def snapshot_config(config, reason: str = "") -> Path | None:
    """Snapshot ``config`` if it holds user-supplied settings. Never raises (see module doc)."""
    try:
        if config is None or not has_user_content(config):
            return None
        from app.config import settings

        export = export_from_config(config, include_secrets=True)
        path = await asyncio.to_thread(write_snapshot, settings.database_url, export)
    except ProductionDatabaseAccessError:
        raise
    except Exception as e:  # noqa: BLE001 - a snapshot must never break a save or a startup
        logger.warning(f"Settings snapshot skipped ({e.__class__.__name__}): {e}")
        return None
    if path is not None:
        suffix = f" ({reason})" if reason else ""
        logger.info(f"Saved a settings snapshot{suffix}: {path.name}")
    return path


async def snapshot_current_config(reason: str = "") -> Path | None:
    """Snapshot the stored configuration without creating one if there is none."""
    try:
        config = await get_config_if_exists()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Settings snapshot skipped, could not read the configuration: {e}")
        return None
    return await snapshot_config(config, reason)
