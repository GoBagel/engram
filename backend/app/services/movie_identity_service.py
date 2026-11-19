"""Correct the TMDB identity of a COMPLETED movie job, without re-ripping.

A wrong auto-identification (``TITAN`` filed as *Titan (2021)* instead of *Titan
A.E. (2000)*) leaves the main feature and its extras in the wrong library folder.
This service relocates the already-organized files and rewrites the persisted
identity and paths to match.

The filesystem and SQLite cannot be updated atomically together, so the protocol
is journal-first (see ``MovieIdentityOperation``):

1. ``build_preview`` is read-only: validate, plan every move, detect collisions.
2. ``apply`` persists the plan as ``planned``, re-validates, then ``moving``:
   each file is moved with a no-clobber move and journaled. Then ``committing``:
   one DB transaction rewrites job + title paths and marks the operation ``done``.
   A failed move or commit is rolled back; a rollback that cannot finish leaves
   ``needs_recovery`` and never claims success.
3. ``reconcile`` (startup) / ``recover`` (explicit) inspect where the files
   really are and finish or undo the operation, never overwriting anything.

Only files already tracked by this job (``DiscTitle.organized_to``) are touched,
only inside the configured movies library, and never by a client-supplied path.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.core.media_formats import library_suffix
from app.core.organizer import _existing_media, plan_movie_names
from app.core.security import is_within_configured_roots, sanitize_log_value
from app.database import async_session
from app.models.disc_job import ContentType, DiscJob, DiscTitle, JobState
from app.models.movie_identity_operation import ACTIVE_STATUSES, MovieIdentityOperation

# Errno values meaning "this filesystem cannot hard-link", not "the move failed".
_NO_LINK_ERRNOS = {
    errno.EPERM,
    errno.ENOTSUP,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    errno.EINVAL,
    errno.ENOSYS,
}


class MovieIdentityError(Exception):
    """A refusal with an HTTP-shaped status the route layer reports verbatim."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class PlannedMove:
    title_id: int
    role: str  # "main" | "extra"
    src: str
    dst: str
    state: str = "pending"  # pending | moved | reverted

    def to_json(self) -> dict:
        return {
            "title_id": self.title_id,
            "role": self.role,
            "src": self.src,
            "dst": self.dst,
            "state": self.state,
        }


@dataclass
class Preview:
    job_id: int
    previous: dict
    proposed: dict
    moves: list[PlannedMove]
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    old_dirs: list[str] = field(default_factory=list)
    token: str = ""

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "ok": self.ok,
            "previous": self.previous,
            "proposed": self.proposed,
            "moves": [m.to_json() for m in self.moves],
            "blockers": self.blockers,
            "warnings": self.warnings,
            "token": self.token,
        }


# --------------------------------------------------------------------------- #
# Filesystem primitives
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class MoveLeftBothNames(OSError):
    """A move failed and the file is present under both names (needs hand recovery)."""


def _publish_no_clobber(src: Path, dst: Path) -> bool:
    """Make ``dst`` appear atomically, failing if it already exists.

    ``os.link`` fails with EEXIST rather than replacing, which closes the
    check-then-rename race that ``os.replace`` leaves open. Filesystems without
    hard links fall back to an existence check plus rename (the one weaker
    case, taken only when linking is impossible). The source is left in place;
    the caller unlinks it once the new name is safely there. Returns True when
    the source name was consumed (a plain rename), so there is nothing to unlink.
    """
    try:
        os.link(src, dst)
        return False
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise
        if e.errno not in _NO_LINK_ERRNOS:
            raise
    if dst.exists():
        raise FileExistsError(errno.EEXIST, "destination exists", str(dst))
    os.rename(src, dst)
    return True


def move_no_clobber(src: Path, dst: Path, op_id: str = "x") -> None:
    """Move ``src`` to ``dst`` without ever replacing an existing file.

    Same filesystem: hard-link then unlink (atomic, no-clobber). Across
    filesystems: copy to a ``.partial`` beside the destination, verify a sha256
    against the source, publish it no-clobber, and only then delete the
    original. The original is never removed before the copy is verified.
    """
    if dst.exists() or os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "destination exists", str(dst))
    try:
        consumed = _publish_no_clobber(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        _copy_verify_publish(src, dst, op_id)
        consumed = False
    if consumed:
        return
    try:
        os.unlink(src)
    except OSError as e:
        if not os.path.lexists(src):
            # The source name is already gone and ``dst`` is the only name left:
            # that is a completed move, and removing ``dst`` would destroy the file.
            return
        # The source survived, so ``dst`` is a second name (hard link) or a verified
        # copy of it. Drop ``dst`` to restore the pre-move state, never the reverse.
        try:
            os.unlink(dst)
        except OSError as e2:
            logger.error(f"Could not remove {src} after moving it to {dst}; both exist: {e2}")
            raise MoveLeftBothNames(f"{src} and {dst} both exist after a failed move: {e2}") from e
        raise


def _copy_verify_publish(src: Path, dst: Path, op_id: str) -> None:
    partial = dst.with_name(f".{dst.name}.{op_id}.partial")
    try:
        shutil.copy2(src, partial)
        if _sha256(src) != _sha256(partial):
            raise OSError(errno.EIO, f"checksum mismatch copying {src.name}")
        if partial.stat().st_size != src.stat().st_size:
            raise OSError(errno.EIO, f"size mismatch copying {src.name}")
        _publish_no_clobber(partial, dst)
    finally:
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
        except OSError:
            logger.error(f"Could not remove partial copy {partial}; delete it by hand")


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

_job_locks: dict[int, asyncio.Lock] = {}


def _lock_for(job_id: int) -> asyncio.Lock:
    lock = _job_locks.get(job_id)
    if lock is None:
        lock = _job_locks[job_id] = asyncio.Lock()
    return lock


def _release_year(details: dict) -> int | None:
    raw = (details.get("release_date") or "")[:4]
    return int(raw) if raw.isdigit() else None


async def _fetch_tmdb_movie(tmdb_id: int) -> dict:
    from app.matcher import tmdb_client

    details = await asyncio.to_thread(tmdb_client.fetch_movie_details, tmdb_id)
    if not details or not details.get("title"):
        raise MovieIdentityError(
            404, f"TMDB movie {tmdb_id} was not found (or TMDB is unreachable)"
        )
    return details


def _identity_snapshot(job: DiscJob) -> dict:
    return {
        "tmdb_id": job.tmdb_id,
        "title": job.tmdb_name or job.detected_title,
        "year": job.tmdb_year,
        "detected_title": job.detected_title,
        "tmdb_name": job.tmdb_name,
        "classification_source": job.classification_source,
        "classification_confidence": job.classification_confidence,
        "is_ambiguous_movie": job.is_ambiguous_movie,
        "final_path": job.final_path,
    }


def _token(job: DiscJob, tmdb_id: int, proposed: dict, moves: list[PlannedMove]) -> str:
    """A fingerprint of everything the preview depended on.

    It changes if the job row, any tracked file (size/mtime) or the plan changes,
    so a preview that has gone stale is rejected rather than applied.
    """
    files = []
    for m in moves:
        try:
            st = os.stat(m.src)
            files.append([m.src, m.dst, st.st_size, st.st_mtime_ns])
        except OSError:
            files.append([m.src, m.dst, None, None])
    blob = json.dumps(
        [
            job.id,
            job.updated_at.isoformat() if job.updated_at else None,
            job.tmdb_id,
            tmdb_id,
            proposed,
            files,
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


async def build_preview(job_id: int, tmdb_id: int) -> Preview:
    """Plan a correction. Read-only: nothing on disk or in the DB changes."""
    from app.services.config_service import get_config

    cfg = await get_config()
    details = await _fetch_tmdb_movie(tmdb_id)

    async with async_session() as session:
        job = await session.get(DiscJob, job_id)
        if job is None:
            raise MovieIdentityError(404, "Job not found")
        if job.state != JobState.COMPLETED:
            raise MovieIdentityError(
                409, f"Only completed jobs can be corrected (state: {job.state.value})"
            )
        if job.content_type != ContentType.MOVIE:
            raise MovieIdentityError(
                409, f"Only movie jobs can be corrected (type: {job.content_type.value})"
            )
        if job.destination_mode != "library" or job.drive_id == "import":
            raise MovieIdentityError(
                409,
                "Imported and in-place jobs keep their files outside the movie library "
                "and cannot be corrected here",
            )
        titles = (
            (await session.execute(select(DiscTitle).where(DiscTitle.job_id == job_id)))
            .scalars()
            .all()
        )
        active = (
            await session.execute(
                select(MovieIdentityOperation).where(
                    MovieIdentityOperation.job_id == job_id,
                    MovieIdentityOperation.status.in_(ACTIVE_STATUSES),  # type: ignore[attr-defined]
                )
            )
        ).scalar_one_or_none()
        others = await _other_tracked_paths(session, job_id)

    return _plan(job, list(titles), cfg, tmdb_id, details, active, others)


async def _other_tracked_paths(session, job_id: int) -> list[str]:
    rows = await session.execute(
        select(DiscTitle.organized_to).where(
            DiscTitle.job_id != job_id, DiscTitle.organized_to.isnot(None)
        )
    )
    return [r for (r,) in rows.all() if r]


def _plan(job, titles, cfg, tmdb_id, details, active_op, other_paths) -> Preview:
    root_str = (cfg.library_movies_path or "").strip()
    new_title = details["title"]
    new_year = _release_year(details)
    previous = _identity_snapshot(job)
    proposed = {
        "tmdb_id": tmdb_id,
        "title": new_title,
        "year": new_year,
        "final_path": None,
    }
    pv = Preview(job_id=job.id, previous=previous, proposed=proposed, moves=[])

    def block(msg: str) -> Preview:
        pv.blockers.append(msg)
        return pv

    if active_op is not None:
        pv.blockers.append(
            f"A correction is already {active_op.status} for this job "
            f"(operation {active_op.id}); finish or recover it first"
        )
    if job.tmdb_id == tmdb_id:
        pv.blockers.append("The job is already identified as this TMDB movie")
    if not root_str:
        return block("The movies library path is not configured")
    root = Path(root_str)
    if not job.final_path:
        return block("The job has no recorded library file (final_path)")

    placed = [t for t in titles if t.organized_to]
    main = [t for t in placed if t.organized_to == job.final_path]
    if len(main) != 1:
        return block("Could not identify the single main feature among the job's files")
    main_title = main[0]

    old_main = Path(main_title.organized_to)
    old_folder = old_main.parent
    try:
        resolved_root = root.resolve(strict=False)
        folder_ok = old_folder.resolve(strict=False).parent == resolved_root
    except (OSError, ValueError):
        folder_ok = False
    if not folder_ok:
        return block(
            "The movie's folder is not directly inside the movies library; "
            "refusing to move it automatically"
        )

    extras_dir = old_folder / "Extras"
    sources: list[tuple[DiscTitle, str]] = [(main_title, "main")]
    for t in placed:
        if t is main_title:
            continue
        p = Path(t.organized_to)
        if p.parent != extras_dir:
            pv.blockers.append(
                f"Tracked file is outside the movie's Extras folder: {p.name} "
                "(manual recovery needed)"
            )
            continue
        sources.append((t, "extra"))

    edition = main_title.edition
    if edition and edition.lower() in new_title.lower():
        edition = None
    new_folder_name, new_main_name = plan_movie_names(
        cfg, new_title, new_year, tmdb_id, edition, library_suffix(old_main)
    )
    new_folder = root / new_folder_name
    new_extras = new_folder / "Extras"
    if new_folder == old_folder:
        pv.blockers.append("The corrected name maps to the same library folder")
    elif new_folder_name.lower() == old_folder.name.lower():
        pv.blockers.append(
            "The corrected folder differs from the current one only by letter case, "
            "which is unsafe on case-insensitive filesystems"
        )

    if root.is_dir():
        clash = [
            c.name
            for c in root.iterdir()
            if c.name != new_folder_name and c.name.lower() == new_folder_name.lower()
        ]
        if clash:
            pv.blockers.append(
                f"A library folder differing only by letter case already exists: {clash[0]}"
            )

    for t, role in sources:
        src = Path(t.organized_to)
        if role == "main":
            dst = new_folder / new_main_name
        else:
            dst = new_extras / src.name
        pv.moves.append(PlannedMove(t.id, role, str(src), str(dst)))
        _check_source(pv, src, resolved_root, root)

    pv.proposed["final_path"] = str(new_folder / new_main_name)
    for d in (new_folder, new_extras):
        link = _symlinked_component(d, root, resolved_root)
        if link is not None:
            pv.blockers.append(f"Refusing to move into a symlinked folder: {link}")
    _check_destinations(pv, new_folder, other_paths, old_folder)

    # Directories the operation may tidy up after, if (and only if) empty.
    pv.old_dirs = [str(extras_dir), str(old_folder)]

    if old_folder.is_dir():
        tracked = {Path(m.src).name for m in pv.moves}
        leftovers = [c.name for c in old_folder.rglob("*") if c.is_file() and c.name not in tracked]
        if leftovers:
            pv.warnings.append(
                f"{len(leftovers)} untracked file(s) in the old folder will stay there "
                "(the folder is not removed)"
            )
    if getattr(job, "submitted_at", None) or getattr(job, "exported_at", None):
        pv.warnings.append(
            "This disc was already contributed/exported under the old identity; "
            "that contribution is not changed or retracted"
        )
    pv.token = _token(job, tmdb_id, proposed, pv.moves)
    return pv


def _symlinked_component(path: Path, root: Path, resolved_root: Path) -> Path | None:
    """The first symlink between the library root and ``path`` (inclusive), if any.

    Only the final name was checked before; a symlinked folder in the middle
    sends the bytes somewhere other than the planned path.
    """
    for base in (root, resolved_root):
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        cur = base
        for part in rel.parts:
            cur = cur / part
            if os.path.islink(cur):
                return cur
        return None
    return None


def _check_source(pv: Preview, src: Path, resolved_root: Path, root: Path | None = None) -> None:
    link = _symlinked_component(src, root or resolved_root, resolved_root)
    if link is not None and link != src:
        pv.blockers.append(f"Refusing to move through a symlinked folder: {link}")
    if os.path.islink(src):
        pv.blockers.append(f"Refusing to move a symlink: {src.name}")
    elif not src.is_file():
        pv.blockers.append(f"File is missing: {src}")
    elif not is_within_configured_roots(src, [str(resolved_root)]):
        pv.blockers.append(f"File is outside the movies library: {src}")


def _check_destinations(pv: Preview, new_folder: Path, other_paths, old_folder: Path) -> None:
    seen: set[str] = set()
    for m in pv.moves:
        dst = Path(m.dst)
        key = str(dst).lower()
        if key in seen:
            pv.blockers.append(f"Two files would be moved to the same name: {dst.name}")
        seen.add(key)
        if os.path.lexists(dst) or _existing_media(dst) is not None:
            pv.blockers.append(f"A file already exists at the destination: {dst}")
        elif dst.parent.exists() and any(
            c.name.lower() == dst.name.lower() for c in dst.parent.iterdir()
        ):
            pv.blockers.append(f"A file differing only by letter case exists at: {dst}")
    for folder in (old_folder, new_folder):
        prefix = str(folder) + os.sep
        if any(p.startswith(prefix) for p in other_paths):
            pv.blockers.append(
                f"Another job's files are tracked inside {folder.name}; "
                "the folder is shared and cannot be moved automatically"
            )


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

# Test seam: raised/consulted at the commit boundary and between moves.
_FAULTS: dict[str, object] = {}


def _op_snapshot(op: MovieIdentityOperation) -> dict:
    return {
        "operation_id": op.id,
        "job_id": op.job_id,
        "status": op.status,
        "previous": json.loads(op.previous_json),
        "proposed": json.loads(op.proposed_json),
        "moves": json.loads(op.moves_json),
        "error": op.error,
        "actor": op.actor,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "completed_at": op.completed_at.isoformat() if op.completed_at else None,
    }


async def _save_op(op_id: str, **changes) -> None:
    async with async_session() as session:
        op = await session.get(MovieIdentityOperation, op_id)
        for k, v in changes.items():
            setattr(op, k, v)
        op.updated_at = datetime.now(UTC)
        session.add(op)
        await session.commit()


async def apply(job_id: int, tmdb_id: int, token: str, confirm: bool, actor: str | None) -> dict:
    """Run a confirmed correction. Raises MovieIdentityError on any refusal."""
    if confirm is not True:
        raise MovieIdentityError(400, "Explicit confirmation is required")
    async with _lock_for(job_id):
        done = await _already_applied(job_id, tmdb_id)
        if done is not None:
            return {**done, "already_applied": True}

        pv = await build_preview(job_id, tmdb_id)
        if pv.token != token:
            raise MovieIdentityError(409, "The preview is out of date; reload it and confirm again")
        if not pv.ok:
            raise MovieIdentityError(409, "; ".join(pv.blockers))

        op_id = uuid.uuid4().hex
        try:
            async with async_session() as session:
                session.add(
                    MovieIdentityOperation(
                        id=op_id,
                        job_id=job_id,
                        status="planned",
                        previous_json=json.dumps(pv.previous),
                        proposed_json=json.dumps(pv.proposed),
                        moves_json=json.dumps([m.to_json() for m in pv.moves]),
                        old_dirs_json=json.dumps(pv.old_dirs),
                        actor=actor,
                    )
                )
                await session.commit()
        except IntegrityError:
            raise MovieIdentityError(
                409, "Another correction is already in progress for this job"
            ) from None

        # Re-validate against the durable plan: anything that changed between the
        # preview and now (a file appeared, a job edit) aborts before any move.
        again = await build_preview_ignoring(job_id, tmdb_id, op_id)
        if again.token != pv.token or not again.ok:
            await _save_op(op_id, status="rolled_back", error="State changed before moving")
            raise MovieIdentityError(409, "State changed while starting; nothing was moved")

        moves = pv.moves
        await _save_op(op_id, status="moving")
        try:
            await _run_moves(op_id, moves)
        except Exception as e:  # noqa: BLE001 - every failure must roll back
            logger.error(f"Movie correction {op_id}: move failed: {e}", exc_info=True)
            settled = await _roll_back_or_flag(
                op_id, moves, f"Move failed: {e}", force_flag=isinstance(e, MoveLeftBothNames)
            )
            if settled == "needs_recovery":
                raise MovieIdentityError(
                    409,
                    f"The correction failed and some files could not be restored: {e}. "
                    "Use Recover on the Change Movie dialog to finish or undo it.",
                ) from e
            raise MovieIdentityError(
                409, f"The correction failed and every file was put back: {e}"
            ) from e

        await _save_op(op_id, status="committing")
        try:
            await _commit(op_id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Movie correction {op_id}: DB commit failed: {e}", exc_info=True)
            settled = await _roll_back_or_flag(op_id, moves, f"Database commit failed: {e}")
            if settled == "needs_recovery":
                raise MovieIdentityError(
                    409,
                    f"Saving the correction failed and the files could not all be moved "
                    f"back: {e}. Use Recover on the Change Movie dialog to finish or undo it.",
                ) from e
            raise MovieIdentityError(
                409, f"Saving the correction failed and the files were moved back: {e}"
            ) from e

        await _tidy_old_dirs(op_id)
        return await _finish(op_id)


async def build_preview_ignoring(job_id: int, tmdb_id: int, op_id: str) -> Preview:
    """Preview again with this operation's own ``planned`` row not counted as a blocker."""
    pv = await build_preview(job_id, tmdb_id)
    own = f"(operation {op_id})"
    pv.blockers = [b for b in pv.blockers if own not in b]
    return pv


async def _already_applied(job_id: int, tmdb_id: int) -> dict | None:
    async with async_session() as session:
        job = await session.get(DiscJob, job_id)
        if job is None or job.tmdb_id != tmdb_id:
            return None
        rows = await session.execute(
            select(MovieIdentityOperation)
            .where(
                MovieIdentityOperation.job_id == job_id,
                MovieIdentityOperation.status == "done",
            )
            .order_by(MovieIdentityOperation.completed_at.desc())  # type: ignore[union-attr]
        )
        for op in rows.scalars():
            if json.loads(op.proposed_json).get("tmdb_id") == tmdb_id:
                return _op_snapshot(op)
    return None


async def _run_moves(op_id: str, moves: list[PlannedMove]) -> None:
    for i, m in enumerate(moves):
        if _FAULTS.get("fail_move_at") == i:
            raise OSError(errno.EIO, "injected move failure")
        dst = Path(m.dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(move_no_clobber, Path(m.src), dst, op_id[:8])
        m.state = "moved"
        await _save_op(op_id, moves_json=json.dumps([x.to_json() for x in moves]))


async def _undo_moves(op_id: str, moves: list[PlannedMove]) -> list[str]:
    """Move already-moved files back, newest first. Returns failure messages."""
    failures: list[str] = []
    for m in reversed(moves):
        if m.state != "moved":
            continue
        try:
            await asyncio.to_thread(move_no_clobber, Path(m.dst), Path(m.src), op_id[:8])
            m.state = "reverted"
        except OSError as e:
            failures.append(f"{m.dst} -> {m.src}: {e}")
    # Remove destination directories, deepest first. rmdir only succeeds on an
    # empty directory, so nothing with content in it can be lost.
    dirs = {Path(m.dst).parent for m in moves}
    dirs |= {d.parent for d in dirs if d.name == "Extras"}
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            os.rmdir(d)
        except OSError:
            pass
    return failures


async def _roll_back_or_flag(
    op_id: str, moves: list[PlannedMove], reason: str, *, force_flag: bool = False
) -> str:
    """Undo what was moved; returns the journal status it settled on."""
    failures = await _undo_moves(op_id, moves)
    if force_flag and not failures:
        failures = ["a file is present under both its old and new name"]
    if failures:
        await _save_op(
            op_id,
            status="needs_recovery",
            error=f"{reason}. Rollback incomplete: " + "; ".join(failures),
            moves_json=json.dumps([x.to_json() for x in moves]),
        )
        return "needs_recovery"
    await _save_op(
        op_id,
        status="rolled_back",
        error=reason,
        moves_json=json.dumps([x.to_json() for x in moves]),
    )
    return "rolled_back"


async def _commit(op_id: str) -> None:
    """One transaction: identity + every title path + the operation's ``done``."""
    if _FAULTS.get("fail_commit"):
        raise RuntimeError("injected commit failure")
    async with async_session() as session:
        op = await session.get(MovieIdentityOperation, op_id)
        proposed = json.loads(op.proposed_json)
        moves = json.loads(op.moves_json)
        job = await session.get(DiscJob, op.job_id)
        job.detected_title = proposed["title"]
        job.tmdb_name = proposed["title"]
        job.tmdb_id = proposed["tmdb_id"]
        job.tmdb_year = proposed["year"]
        job.classification_source = "user"
        job.classification_confidence = 1.0
        job.is_ambiguous_movie = False
        job.final_path = proposed["final_path"]
        job.updated_at = datetime.now(UTC)
        session.add(job)
        for m in moves:
            title = await session.get(DiscTitle, m["title_id"])
            title.organized_to = m["dst"]
            session.add(title)
        op.status = "done"
        op.error = None
        op.completed_at = datetime.now(UTC)
        op.updated_at = op.completed_at
        session.add(op)
        await session.commit()


async def _tidy_old_dirs(op_id: str) -> None:
    """rmdir (never rmtree) the old Extras/ and movie folder, only if empty."""
    async with async_session() as session:
        op = await session.get(MovieIdentityOperation, op_id)
        dirs = json.loads(op.old_dirs_json)
    for d in dirs:
        try:
            os.rmdir(d)
        except OSError:
            pass  # not empty or already gone: leave it


async def _finish(op_id: str) -> dict:
    async with async_session() as session:
        op = await session.get(MovieIdentityOperation, op_id)
        snap = _op_snapshot(op)
        job = await session.get(DiscJob, op.job_id)
        state = job.state.value
    await _broadcast(snap, state)
    logger.info(
        f"Job {sanitize_log_value(op.job_id)}: movie identity corrected "
        f"to TMDB {sanitize_log_value(snap['proposed']['tmdb_id'])}"
    )
    return {**snap, "already_applied": False}


async def _broadcast(snap: dict, state: str) -> None:
    from app.api.websocket import manager as ws_manager

    try:
        await ws_manager.broadcast_job_update(
            snap["job_id"], state, detected_title=snap["proposed"]["title"]
        )
        for m in snap["moves"]:
            await ws_manager.broadcast_title_update(
                snap["job_id"], m["title_id"], "completed", organized_to=m["dst"]
            )
    except Exception:  # noqa: BLE001 - a missed push must not undo a good correction
        logger.warning("Could not broadcast the movie identity change", exc_info=True)


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


def _placement(moves: list[dict]) -> list[str]:
    out = []
    for m in moves:
        s, d = os.path.lexists(m["src"]), os.path.lexists(m["dst"])
        out.append("src" if s and not d else "dst" if d and not s else "ambiguous")
    return out


async def recover(job_id: int, action: str | None = None) -> dict | None:
    """Finish or undo an interrupted operation for ``job_id``.

    ``action`` None means automatic: roll forward when every file is at its
    destination, roll back when each is at one end, otherwise ``needs_recovery``
    (never guess, never overwrite). ``roll_forward`` / ``roll_back`` are the
    explicit overrides and still refuse an ambiguous layout.
    """
    if action not in (None, "roll_forward", "roll_back"):
        raise MovieIdentityError(400, "action must be roll_forward or roll_back")
    async with _lock_for(job_id):
        async with async_session() as session:
            op = (
                await session.execute(
                    select(MovieIdentityOperation).where(
                        MovieIdentityOperation.job_id == job_id,
                        MovieIdentityOperation.status.in_(ACTIVE_STATUSES),  # type: ignore[attr-defined]
                    )
                )
            ).scalar_one_or_none()
        if op is None:
            if action is not None:
                raise MovieIdentityError(404, "No operation needs recovery for this job")
            return None

        moves = [PlannedMove(**m) for m in json.loads(op.moves_json)]
        places = _placement([m.to_json() for m in moves])
        if "ambiguous" in places:
            await _save_op(
                op.id,
                status="needs_recovery",
                error="A file is missing or present at both locations; manual recovery needed",
            )
            if action is not None:
                raise MovieIdentityError(409, "File placement is ambiguous; fix it by hand")
            return None

        # Trust the filesystem over the journal: a crash can land between a move
        # and its journal write.
        for m, place in zip(moves, places, strict=True):
            m.state = "moved" if place == "dst" else "pending"
        all_dst = all(p == "dst" for p in places)
        if (
            op.status == "planned"
            and action in (None, "roll_back")
            and all(p == "src" for p in places)
        ):
            await _save_op(op.id, status="rolled_back", error="Interrupted before moving")
        elif action == "roll_forward" or (action is None and all_dst):
            if not all_dst:
                raise MovieIdentityError(409, "Not every file is at its destination")
            await _commit(op.id)
            await _tidy_old_dirs(op.id)
        elif action == "roll_back" or action is None:
            await _roll_back_or_flag(op.id, moves, "Interrupted; rolled back by recovery")
        async with async_session() as session:
            return _op_snapshot(await session.get(MovieIdentityOperation, op.id))


async def reconcile_pending() -> None:
    """Startup hook: settle every operation a crash left half-done."""
    async with async_session() as session:
        job_ids = (
            (
                await session.execute(
                    select(MovieIdentityOperation.job_id).where(
                        MovieIdentityOperation.status.in_(ACTIVE_STATUSES)  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
    for job_id in job_ids:
        try:
            await recover(job_id)
        except Exception:  # noqa: BLE001 - one bad row must not block startup
            logger.error(f"Movie identity recovery failed for job {job_id}", exc_info=True)


async def list_operations(job_id: int) -> list[dict]:
    async with async_session() as session:
        rows = await session.execute(
            select(MovieIdentityOperation)
            .where(MovieIdentityOperation.job_id == job_id)
            .order_by(MovieIdentityOperation.created_at.desc())  # type: ignore[union-attr]
        )
        return [_op_snapshot(op) for op in rows.scalars()]
