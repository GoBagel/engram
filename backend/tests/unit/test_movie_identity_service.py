"""Completed-movie re-identification (spec 2026-11-18).

Everything runs against the in-memory unit DB and ``tmp_path`` directories; no
real library, no real engram.db, no network.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.database import get_session
from app.main import app
from app.models import AppConfig, DiscJob, DiscTitle, MovieIdentityOperation
from app.models.disc_job import ContentType, JobState, TitleState
from app.services import movie_identity_service as svc
from tests.unit.conftest import _unit_session_factory
from tests.unit.test_api_routes import _seed_job

NEW_ID = 12345
NEW_DETAILS = {"id": NEW_ID, "title": "Titan A.E.", "release_date": "2000-06-16"}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def tmdb(monkeypatch):
    from app.matcher import tmdb_client

    calls = {"details": dict(NEW_DETAILS)}
    monkeypatch.setattr(
        tmdb_client, "fetch_movie_details", lambda mid: calls["details"] if mid == NEW_ID else None
    )
    return calls


async def _make(lib: Path, *, with_extras: bool = True, **job_kwargs):
    """A COMPLETED movie job filed as Titan (2021), with real files on disk."""
    async with _unit_session_factory() as s:
        s.add(AppConfig(library_movies_path=str(lib), tmdb_api_key="eyJ.test"))
        await s.commit()
    old = lib / "Titan (2021)"
    (old / "Extras").mkdir(parents=True)
    main = old / "Titan (2021).mkv"
    main.write_bytes(b"MAIN" * 4096)
    extras = []
    if with_extras:
        for i in (1, 2):
            e = old / "Extras" / f"Extra {i}.mkv"
            e.write_bytes(f"EXTRA{i}".encode() * 2048)
            extras.append(e)
    kwargs = dict(
        drive_id="D:",
        volume_label="TITAN",
        content_type=ContentType.MOVIE,
        state=JobState.COMPLETED,
        detected_title="Titan",
        tmdb_id=111,
        tmdb_name="Titan",
        tmdb_year=2021,
        classification_source="tmdb+heuristic",
        classification_confidence=0.95,
        final_path=str(main),
    )
    kwargs.update(job_kwargs)
    job = await _seed_job(**kwargs)
    async with _unit_session_factory() as s:
        s.add(
            DiscTitle(
                job_id=job.id,
                title_index=0,
                duration_seconds=5670,
                state=TitleState.COMPLETED,
                organized_to=str(main),
                is_extra=False,
            )
        )
        for i, e in enumerate(extras, start=1):
            s.add(
                DiscTitle(
                    job_id=job.id,
                    title_index=i,
                    duration_seconds=300,
                    state=TitleState.COMPLETED,
                    organized_to=str(e),
                    is_extra=True,
                )
            )
        await s.commit()
    return job, old, main, extras


async def _job(job_id):
    async with _unit_session_factory() as s:
        return await s.get(DiscJob, job_id)


async def _titles(job_id):
    async with _unit_session_factory() as s:
        rows = await s.execute(
            select(DiscTitle).where(DiscTitle.job_id == job_id).order_by(DiscTitle.title_index)
        )
        return list(rows.scalars())


async def _ops(job_id):
    async with _unit_session_factory() as s:
        rows = await s.execute(
            select(MovieIdentityOperation).where(MovieIdentityOperation.job_id == job_id)
        )
        return list(rows.scalars())


async def _apply(job_id, *, token=None, confirm=True):
    pv = await svc.build_preview(job_id, NEW_ID)
    return await svc.apply(job_id, NEW_ID, token or pv.token, confirm, "127.0.0.1")


# ------------------------------------------------------------------ preview


async def test_preview_plans_moves_and_writes_nothing(lib):
    job, old, main, extras = await _make(lib)
    before = _tree(lib)
    db_before = (await _job(job.id)).model_dump()

    pv = await svc.build_preview(job.id, NEW_ID)

    assert pv.ok, pv.blockers
    new = lib / "Titan A.E. (2000)"
    dsts = {m.role: m.dst for m in pv.moves if m.role == "main"}
    assert dsts["main"] == str(new / "Titan A.E. (2000).mkv")
    assert sorted(Path(m.dst).name for m in pv.moves if m.role == "extra") == [
        "Extra 1.mkv",
        "Extra 2.mkv",
    ]
    assert pv.previous["tmdb_id"] == 111 and pv.proposed["tmdb_id"] == NEW_ID
    assert pv.token
    assert _tree(lib) == before and not new.exists()
    assert (await _job(job.id)).model_dump() == db_before
    assert await _ops(job.id) == []


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"state": JobState.REVIEW_NEEDED}, 409),
        ({"content_type": ContentType.TV}, 409),
        ({"destination_mode": "in_place"}, 409),
        ({"drive_id": "import"}, 409),
    ],
)
async def test_preview_rejects_ineligible_jobs(lib, kwargs, status):
    job, *_ = await _make(lib, **kwargs)
    with pytest.raises(svc.MovieIdentityError) as ei:
        await svc.build_preview(job.id, NEW_ID)
    assert ei.value.status == status


async def test_preview_rejects_invalid_tmdb_id_and_unknown_job(lib):
    job, *_ = await _make(lib)
    with pytest.raises(svc.MovieIdentityError) as ei:
        await svc.build_preview(job.id, 999)
    assert ei.value.status == 404
    with pytest.raises(svc.MovieIdentityError) as ei:
        await svc.build_preview(424242, NEW_ID)
    assert ei.value.status == 404


async def test_preview_blocks_missing_file(lib):
    job, old, main, extras = await _make(lib)
    extras[0].unlink()
    pv = await svc.build_preview(job.id, NEW_ID)
    assert not pv.ok and any("missing" in b.lower() for b in pv.blockers)


async def test_preview_blocks_same_identity(lib):
    job, *_ = await _make(lib, tmdb_id=NEW_ID)
    pv = await svc.build_preview(job.id, NEW_ID)
    assert any("already identified" in b for b in pv.blockers)


async def test_preview_warns_about_contribution_and_untracked_files(lib):
    from datetime import UTC, datetime

    job, old, *_ = await _make(lib, exported_at=datetime.now(UTC))
    (old / "poster.jpg").write_bytes(b"x")
    pv = await svc.build_preview(job.id, NEW_ID)
    text = " ".join(pv.warnings)
    assert "contributed/exported" in text and "untracked" in text


# -------------------------------------------------------------------- apply


async def test_apply_moves_files_and_updates_db_without_changing_bytes(lib):
    job, old, main, extras = await _make(lib)
    sums = {p.name: _sha(p) for p in [main, *extras]}

    result = await _apply(job.id)

    new = lib / "Titan A.E. (2000)"
    assert result["status"] == "done" and result["already_applied"] is False
    assert sorted(_sha(p) for p in new.rglob("*.mkv")) == sorted(sums.values())
    assert not old.exists()  # empty old tree tidied up

    j = await _job(job.id)
    assert (j.tmdb_id, j.tmdb_name, j.detected_title, j.tmdb_year) == (
        NEW_ID,
        "Titan A.E.",
        "Titan A.E.",
        2000,
    )
    assert j.final_path == str(new / "Titan A.E. (2000).mkv")
    assert (j.classification_source, j.classification_confidence) == ("user", 1.0)
    paths = [t.organized_to for t in await _titles(job.id)]
    assert paths == [
        str(new / "Titan A.E. (2000).mkv"),
        str(new / "Extras" / "Extra 1.mkv"),
        str(new / "Extras" / "Extra 2.mkv"),
    ]
    assert all(Path(p).is_file() for p in paths)

    [op] = await _ops(job.id)
    assert op.status == "done" and op.actor == "127.0.0.1" and op.completed_at
    assert json.loads(op.previous_json)["tmdb_id"] == 111
    assert json.loads(op.previous_json)["classification_confidence"] == 0.95


async def test_apply_requires_confirm(lib):
    job, *_ = await _make(lib)
    with pytest.raises(svc.MovieIdentityError) as ei:
        await _apply(job.id, confirm=False)
    assert ei.value.status == 400
    assert await _ops(job.id) == []


async def test_apply_is_idempotent(lib):
    job, *_ = await _make(lib)
    first = await _apply(job.id)
    tree = _tree(lib)
    again = await svc.apply(job.id, NEW_ID, "whatever-token", True, "127.0.0.1")
    assert again["already_applied"] is True
    assert again["operation_id"] == first["operation_id"]
    assert _tree(lib) == tree and len(await _ops(job.id)) == 1


async def test_stale_preview_is_rejected(lib):
    job, old, main, extras = await _make(lib)
    pv = await svc.build_preview(job.id, NEW_ID)
    main.write_bytes(b"CHANGED AFTER PREVIEW")
    with pytest.raises(svc.MovieIdentityError) as ei:
        await svc.apply(job.id, NEW_ID, pv.token, True, None)
    assert ei.value.status == 409 and "out of date" in ei.value.message
    assert main.read_bytes() == b"CHANGED AFTER PREVIEW" and await _ops(job.id) == []


async def test_edition_is_kept_in_the_new_filename(lib):
    job, old, main, extras = await _make(lib)
    async with _unit_session_factory() as s:
        t = (await _titles(job.id))[0]
        t = await s.get(DiscTitle, t.id)
        t.edition = "Director's Cut"
        s.add(t)
        await s.commit()
    pv = await svc.build_preview(job.id, NEW_ID)
    main_dst = next(m.dst for m in pv.moves if m.role == "main")
    assert "Director's Cut" in Path(main_dst).name


# --------------------------------------------------------------- collisions


async def test_conflicting_target_file_stops_everything(lib):
    job, old, main, extras = await _make(lib)
    new = lib / "Titan A.E. (2000)"
    new.mkdir()
    (new / "Titan A.E. (2000).mkv").write_bytes(b"SOMEONE ELSE")
    before = _tree(lib)
    pv = await svc.build_preview(job.id, NEW_ID)
    assert not pv.ok and any("already exists" in b for b in pv.blockers)
    with pytest.raises(svc.MovieIdentityError) as ei:
        await svc.apply(job.id, NEW_ID, pv.token, True, None)
    assert ei.value.status == 409
    assert _tree(lib) == before
    assert (await _job(job.id)).tmdb_id == 111
    assert await _ops(job.id) == []


async def test_other_container_in_same_slot_is_a_conflict(lib):
    job, *_ = await _make(lib)
    new = lib / "Titan A.E. (2000)"
    new.mkdir()
    (new / "Titan A.E. (2000).mp4").write_bytes(b"x")
    pv = await svc.build_preview(job.id, NEW_ID)
    assert any("already exists" in b for b in pv.blockers)


async def test_case_only_difference_is_blocked(lib):
    job, *_ = await _make(lib)
    (lib / "titan a.e. (2000)").mkdir()
    pv = await svc.build_preview(job.id, NEW_ID)
    assert not pv.ok and any("letter case" in b for b in pv.blockers)


async def test_shared_folder_with_another_job_is_blocked(lib):
    job, old, main, extras = await _make(lib)
    other = await _seed_job(content_type=ContentType.MOVIE, state=JobState.COMPLETED)
    async with _unit_session_factory() as s:
        s.add(
            DiscTitle(
                job_id=other.id,
                title_index=0,
                duration_seconds=5670,
                state=TitleState.COMPLETED,
                organized_to=str(old / "Titan (2021) - Disc 2.mkv"),
            )
        )
        await s.commit()
    pv = await svc.build_preview(job.id, NEW_ID)
    assert any("shared" in b for b in pv.blockers)


# ------------------------------------------------------------- path defenses


async def test_symlinked_file_is_refused(lib, tmp_path):
    job, old, main, extras = await _make(lib)
    outside = tmp_path / "secret.mkv"
    outside.write_bytes(b"SECRET")
    extras[0].unlink()
    os.symlink(outside, extras[0])
    pv = await svc.build_preview(job.id, NEW_ID)
    assert any("symlink" in b for b in pv.blockers)
    with pytest.raises(svc.MovieIdentityError):
        await svc.apply(job.id, NEW_ID, pv.token, True, None)
    assert outside.read_bytes() == b"SECRET" and os.path.islink(extras[0])


async def test_file_outside_library_is_never_touched(lib, tmp_path):
    outside_dir = tmp_path / "elsewhere" / "Titan (2021)"
    outside_dir.mkdir(parents=True)
    outside = outside_dir / "Titan (2021).mkv"
    outside.write_bytes(b"OUTSIDE")
    job, *_ = await _make(lib, final_path=str(outside))
    async with _unit_session_factory() as s:
        t = await s.get(DiscTitle, (await _titles(job.id))[0].id)
        t.organized_to = str(outside)
        s.add(t)
        await s.commit()
    pv = await svc.build_preview(job.id, NEW_ID)
    assert not pv.ok
    with pytest.raises(svc.MovieIdentityError):
        await svc.apply(job.id, NEW_ID, pv.token, True, None)
    assert outside.read_bytes() == b"OUTSIDE"


async def test_dotdot_traversal_cannot_escape(lib, tmp_path):
    job, old, main, extras = await _make(lib)
    sneaky = f"{old}/../../elsewhere/x.mkv"
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.mkv").write_bytes(b"X")
    async with _unit_session_factory() as s:
        t = await s.get(DiscTitle, (await _titles(job.id))[1].id)
        t.organized_to = sneaky
        s.add(t)
        await s.commit()
    pv = await svc.build_preview(job.id, NEW_ID)
    assert not pv.ok
    assert (tmp_path / "elsewhere" / "x.mkv").read_bytes() == b"X"


# ------------------------------------------------------- failure and recovery


async def test_move_failure_is_rolled_back(lib):
    job, old, main, extras = await _make(lib)
    before = _tree(lib)
    svc._FAULTS["fail_move_at"] = 2
    with pytest.raises(svc.MovieIdentityError) as ei:
        await _apply(job.id)
    assert ei.value.status == 409
    assert _tree(lib) == before and not (lib / "Titan A.E. (2000)").exists()
    j = await _job(job.id)
    assert j.tmdb_id == 111 and j.final_path == str(main)
    [op] = await _ops(job.id)
    assert op.status == "rolled_back" and "Move failed" in op.error
    # The job is free for another attempt.
    svc._FAULTS.clear()
    assert (await _apply(job.id))["status"] == "done"


async def test_commit_failure_moves_files_back(lib):
    job, old, main, extras = await _make(lib)
    before = _tree(lib)
    svc._FAULTS["fail_commit"] = True
    with pytest.raises(svc.MovieIdentityError):
        await _apply(job.id)
    assert _tree(lib) == before
    assert (await _job(job.id)).tmdb_id == 111
    [op] = await _ops(job.id)
    assert op.status == "rolled_back" and "commit failed" in op.error


async def _break_commit_and_rollback(monkeypatch):
    """Commit fails AND moving back fails: the only honest outcome is needs_recovery."""
    real = svc.move_no_clobber
    state = {"forward_done": False}

    def flaky(src, dst, op_id="x"):
        if "Titan A.E." in str(src):  # a revert
            raise OSError(errno.EIO, "injected revert failure")
        return real(src, dst, op_id)

    svc._FAULTS["fail_commit"] = True
    monkeypatch.setattr(svc, "move_no_clobber", flaky)
    return real, state


async def test_failed_rollback_leaves_needs_recovery_then_roll_forward(lib, monkeypatch):
    job, old, main, extras = await _make(lib)
    real, _ = await _break_commit_and_rollback(monkeypatch)
    with pytest.raises(svc.MovieIdentityError):
        await _apply(job.id)
    [op] = await _ops(job.id)
    assert op.status == "needs_recovery" and "Rollback incomplete" in op.error
    assert (await _job(job.id)).tmdb_id == 111  # never claims success

    # A new correction is refused while recovery is pending.
    pv = await svc.build_preview(job.id, NEW_ID)
    assert any("already" in b for b in pv.blockers)

    svc._FAULTS.clear()
    monkeypatch.setattr(svc, "move_no_clobber", real)
    snap = await svc.recover(job.id, "roll_forward")
    assert snap["status"] == "done"
    assert (await _job(job.id)).tmdb_id == NEW_ID
    assert all(Path(t.organized_to).is_file() for t in await _titles(job.id))


async def test_failed_rollback_then_explicit_roll_back(lib, monkeypatch):
    job, old, main, extras = await _make(lib)
    before = _tree(lib)
    real, _ = await _break_commit_and_rollback(monkeypatch)
    with pytest.raises(svc.MovieIdentityError):
        await _apply(job.id)
    svc._FAULTS.clear()
    monkeypatch.setattr(svc, "move_no_clobber", real)
    snap = await svc.recover(job.id, "roll_back")
    assert snap["status"] == "rolled_back"
    assert _tree(lib) == before and (await _job(job.id)).tmdb_id == 111


async def _crashed_op(job, moves_state: str, status: str, lib: Path):
    """Fabricate the journal a crash would leave, with files placed per ``moves_state``."""
    pv = await svc.build_preview(job.id, NEW_ID)
    moves = [m.to_json() for m in pv.moves]
    for m in moves:
        if moves_state == "dst":
            Path(m["dst"]).parent.mkdir(parents=True, exist_ok=True)
            os.rename(m["src"], m["dst"])
            m["state"] = "moved"
    async with _unit_session_factory() as s:
        s.add(
            MovieIdentityOperation(
                id="crash1",
                job_id=job.id,
                status=status,
                previous_json=json.dumps(pv.previous),
                proposed_json=json.dumps(pv.proposed),
                moves_json=json.dumps(moves),
                old_dirs_json=json.dumps(pv.old_dirs),
            )
        )
        await s.commit()


async def test_startup_reconcile_rolls_forward_when_all_files_moved(lib):
    job, *_ = await _make(lib)
    await _crashed_op(job, "dst", "committing", lib)
    await svc.reconcile_pending()
    [op] = await _ops(job.id)
    assert op.status == "done" and (await _job(job.id)).tmdb_id == NEW_ID


async def test_startup_reconcile_rolls_back_a_partial_move(lib):
    job, old, main, extras = await _make(lib)
    before = _tree(lib)
    await _crashed_op(job, "src", "moving", lib)
    # Half moved: extras[1] already at its destination.
    pv = await svc.build_preview_ignoring(job.id, NEW_ID, "crash1")
    last = pv.moves[-1]
    Path(last.dst).parent.mkdir(parents=True)
    os.rename(last.src, last.dst)
    await svc.reconcile_pending()
    [op] = await _ops(job.id)
    assert op.status == "rolled_back"
    assert _tree(lib) == before and (await _job(job.id)).tmdb_id == 111


async def test_startup_reconcile_flags_ambiguous_layout_without_touching_files(lib):
    job, old, main, extras = await _make(lib)
    await _crashed_op(job, "src", "moving", lib)
    extras[0].unlink()  # neither source nor destination exists
    snapshot = _tree(lib)
    await svc.reconcile_pending()
    [op] = await _ops(job.id)
    assert op.status == "needs_recovery"
    assert _tree(lib) == snapshot


async def test_reconcile_abandons_a_planned_operation(lib):
    job, *_ = await _make(lib)
    await _crashed_op(job, "src", "planned", lib)
    await svc.reconcile_pending()
    [op] = await _ops(job.id)
    assert op.status == "rolled_back"


async def test_only_one_active_operation_per_job(lib):
    job, *_ = await _make(lib)
    await _crashed_op(job, "src", "planned", lib)
    async with _unit_session_factory() as s:
        s.add(
            MovieIdentityOperation(
                id="second",
                job_id=job.id,
                status="moving",
                previous_json="{}",
                proposed_json="{}",
                moves_json="[]",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


# --------------------------------------------------------- move primitives


def test_move_no_clobber_refuses_existing_destination(tmp_path):
    a, b = tmp_path / "a.mkv", tmp_path / "b.mkv"
    a.write_bytes(b"A")
    b.write_bytes(b"B")
    with pytest.raises(FileExistsError):
        svc.move_no_clobber(a, b)
    assert a.read_bytes() == b"A" and b.read_bytes() == b"B"


def test_cross_device_move_copies_verifies_then_deletes(tmp_path, monkeypatch):
    src, dst = tmp_path / "src.mkv", tmp_path / "out" / "dst.mkv"
    dst.parent.mkdir()
    src.write_bytes(b"PAYLOAD" * 1000)
    real_link = os.link
    seen = {"exdev": 0}

    def link(a, b, *args, **kw):
        if Path(a) == src:  # the direct link attempt crosses a "device"
            seen["exdev"] += 1
            raise OSError(errno.EXDEV, "cross-device")
        return real_link(a, b, *args, **kw)

    monkeypatch.setattr(os, "link", link)
    svc.move_no_clobber(src, dst, "t")
    assert seen["exdev"] == 1
    assert dst.read_bytes() == b"PAYLOAD" * 1000 and not src.exists()
    assert not list(dst.parent.glob("*.partial"))


def test_cross_device_checksum_mismatch_keeps_the_original(tmp_path, monkeypatch):
    src, dst = tmp_path / "src.mkv", tmp_path / "dst.mkv"
    src.write_bytes(b"PAYLOAD")
    real_link = os.link

    def link(a, b, *args, **kw):
        if Path(a) == src:
            raise OSError(errno.EXDEV, "cross-device")
        return real_link(a, b, *args, **kw)

    calls = iter(["aaa", "bbb"])
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(svc, "_sha256", lambda p: next(calls))
    with pytest.raises(OSError, match="checksum"):
        svc.move_no_clobber(src, dst, "t")
    assert src.read_bytes() == b"PAYLOAD" and not dst.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_filesystem_without_hard_links_falls_back_to_rename(tmp_path, monkeypatch):
    src, dst = tmp_path / "src.mkv", tmp_path / "dst.mkv"
    src.write_bytes(b"P")
    monkeypatch.setattr(
        os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EPERM, "x"))
    )
    svc.move_no_clobber(src, dst)
    assert dst.read_bytes() == b"P" and not src.exists()


# ---------------------------------------------------------------------- API


@pytest.fixture
async def client():
    async def override_get_session():
        async with _unit_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_api_preview_change_and_audit(client, lib):
    job, old, main, extras = await _make(lib)
    r = await client.get(f"/api/jobs/{job.id}/movie-identity/preview", params={"tmdb_id": NEW_ID})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["ok"] and pv["token"]

    r = await client.post(
        f"/api/jobs/{job.id}/movie-identity/change",
        json={"tmdb_id": NEW_ID, "token": pv["token"], "confirm": False},
    )
    assert r.status_code == 400
    r = await client.post(
        f"/api/jobs/{job.id}/movie-identity/change",
        json={"tmdb_id": NEW_ID, "token": "stale", "confirm": True},
    )
    assert r.status_code == 409
    r = await client.post(
        f"/api/jobs/{job.id}/movie-identity/change",
        json={"tmdb_id": NEW_ID, "token": pv["token"], "confirm": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"

    r = await client.get(f"/api/jobs/{job.id}/movie-identity/operations")
    [op] = r.json()
    assert op["previous"]["tmdb_id"] == 111 and op["proposed"]["tmdb_id"] == NEW_ID

    r = await client.post(f"/api/jobs/{job.id}/movie-identity/recover", json={})
    assert r.json() == {"status": "nothing_to_recover"}


async def test_api_rejects_bad_input(client, lib):
    job, *_ = await _make(lib)
    r = await client.get(f"/api/jobs/{job.id}/movie-identity/preview", params={"tmdb_id": 0})
    assert r.status_code == 422
    r = await client.get(f"/api/jobs/{job.id}/movie-identity/preview", params={"tmdb_id": 999})
    assert r.status_code == 404
    r = await client.post(
        f"/api/jobs/{job.id}/movie-identity/recover", json={"action": "roll_forward"}
    )
    assert r.status_code == 404


def test_mutation_endpoints_are_lan_guarded():
    from app.api.guards import require_localhost_or_lan

    guarded = set()
    from app.api.routes import router

    for route in router.routes:
        path = getattr(route, "path", "")
        if "movie-identity" in path:
            deps = {d.call for d in route.dependant.dependencies}
            assert require_localhost_or_lan in deps, path
            guarded.add(path)
    assert len(guarded) == 4
