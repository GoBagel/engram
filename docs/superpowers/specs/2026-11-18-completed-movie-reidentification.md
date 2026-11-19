# Completed-job movie re-identification

**Status:** proposed design (implementation not started)  
**Repository:** `GoBagel/engram`  
**Branch:** `feat/completed-movie-reidentification`  
**Date:** 2026-11-18

## Summary

Allow a user to correct a **completed MOVIE job** that was matched to the wrong TMDB title **without re-ripping, re-encoding, or manually editing the database**. Introduce a safe, previewable *Change Movie* action in job history. Correct the movie identity, relocate already-organized main feature and extras, and synchronize persisted job/title output paths. Track identification disagreement as a separate follow-on problem; do not combine the two into an unsafe large change.

## Reproduction / motivation

A physical **Titan A.E. (2000)** DVD was automatically processed, ejected and marked completed. History displayed:
- Disc label: `TITAN`, classified by `tmdb+heuristic` as **Titan (2021)** with **95%** confidence.
- TheDiscDB panel also showed `clash-of-the-titans`, disc type `blu-ray` despite the physical disc being a DVD. This is a *reported inconsistency*; investigate its provenance before assuming TheDiscDB's data is wrong.
- 8 titles were processed; primary title #0 was **94:30**, **5.2 GB**.
- Output included `/mnt/Barracuda/engram/movies/Titan (2021)/Titan (2021).mkv` and several files under its `Extras/` folder.
- History's **REASSIGN** action changes individual track disposition (episode/extra/discard), **not** the movie identity. No completed-job movie re-identification control was found in the UI.

**Do not delete or edit the user's real Titan files or `backend/engram.db` during development.** Reproduce via fixtures and simulation against an isolated test database and temporary directories.

## Goals

1. From a completed **movie** job's History detail, select **Change Movie** and search TMDB for an alternate identity; allow entering a known TMDB movie ID.
2. Display old/new movie identity and a **dry-run preview** of exactly which files/folders will move, including main feature and extras, and any naming convention implications.
3. Require explicit confirmation before writes.
4. Update job metadata and all relevant DiscTitle persisted output paths consistently with the filesystem.
5. Refuse destructive collisions (existing files, ambiguous shared folders, unsafe paths) and show clear actionable errors.
6. Be robust against failure or interruption; preserve a recoverable operation journal and support retry/reconciliation without duplicate destructive moves.
7. Record who/what/when in an audit entry, retaining previous TMDB identity, title, year, and relevant source/confidence fields.
8. Update History UI, emit state/event notification as applicable, and verify corrected files are playable without reripping.

## Non-goals / separate issue

- Rebuilding the ripping/encoding pipeline.
- Rewriting the existing track assignment semantics.
- Re-identifying completed TV jobs or changing episode numbering.
- Automatically correcting catalog disagreements.
- Silently uploading a corrected mapping to fingerprint or DiscDB services.
- Moving items into remote Jellyfin libraries.
- Editing the real Titan job in tests.

## Relevant existing code (verify before coding)

- `backend/app/services/identification_coordinator.py` — disc identity/metadata lookup.
- `backend/app/services/finalization_coordinator.py` — finalization and persisted title state.
- `backend/app/core/organizer.py` — movie naming, safe filesystem organization and output extension.
- `backend/app/models/disc_job.py` — `DiscJob`, `DiscTitle`, `JobState`.
- `backend/app/services/job_manager.py` — orchestration, history, state transitions.
- Locate existing history routes, movie search routes, frontend History job drawer, and existing `REASSIGN` handlers before choosing new filenames. **Reuse services and UI patterns rather than duplicating them.**
- `CLAUDE.md` is authoritative for repo conventions, tests, dev sessions and safety; dated specs belong in `docs/superpowers/specs/`.

These are candidate integration points, **not assertions that an existing endpoint supports completed-job correction**.

## Proposed API contract (subject to existing route conventions)

- `GET /api/jobs/{job_id}/movie-identity/preview?tmdb_id=<id>`
  - Read-only; validates completed/movie job; fetches verified TMDB movie details; returns old/new identities, planned source/destination paths, collision checks, list of affected tracks, and a revision token / job version.
- `POST /api/jobs/{job_id}/movie-identity/change`
  - Body includes TMDB ID, validated preview revision token, and explicit `confirm: true`.
  - Returns operation ID and resulting identity/path mapping; use conflict status for stale preview or collisions; reject wrong content type/state.
- Reuse existing TMDB search endpoint if possible. Do **not** accept arbitrary client-provided destination paths.

Use authentication/authorization patterns already enforced by job modification endpoints. Add CSRF/request-origin protection where applicable; do not introduce an unauthenticated mutation endpoint.

## Data model and transaction approach

Design an idempotent **re-identification operation** persisted before file mutation (using existing migrations/ORM patterns):
- job ID, operation ID, status (`planned`, `moving`, `committing`, `done`, `needs_recovery`), previous and proposed identities, planned source/destination map, timestamps, error details.
- Unique active operation per job; job-level lock/concurrency guard.
- Audit trail recorded on success; failed operations retain recoverable state.

**The filesystem and SQLite cannot be updated atomically together.** Proposed safe protocol:
1. Validate job is completed movie; gather all tracked files; verify each source is within the configured library and exists; disallow symlink escapes and unexpected path traversal.
2. Compute destination using current organizer naming helpers, preserving source extensions and extras structure; show preview. Detect collisions, case-folding issues, conflicting tracked items and mixed/shared directories.
3. Save durable operation plan; acquire job-level lock; revalidate sources and destinations.
4. Prefer same-filesystem atomic rename where safe; if crossing filesystems, use copy+checksum+rename with rollback-aware staging, **never delete original before verification**.
5. Persist updated identity and per-track paths in a transaction after safe filesystem operations; emit event. If the DB commit fails, attempt rollback of moves; on any rollback failure, mark `needs_recovery`, retain journal and do not claim success.
6. Reconcile pending operations on startup or through an explicit repair tool, checking actual file placement and never overwriting data.
7. Only remove an empty old directory if confidently owned by the affected job; leave unrelated files untouched.

No automatic deletion of a title marked discarded during re-identification. Treat missing historical files or shared folders as blockers with a manual-recovery message.

## UI proposal

History → completed movie job → **Change Movie** (separate from per-track **REASSIGN**). Modal:
1. Existing identification, source/confidence and current output folder.
2. TMDB title search + year, details and explicit TMDB ID entry.
3. Preview grid: affected main title, extras, original → destination; warnings.
4. Confirmation button `Apply movie correction` enabled only when preflight passes.
5. Progress/errors/rollback reporting; refresh completed-job detail and displayed paths on success.

Avoid making this available for completed TV or non-completed jobs in v1.

## Acceptance criteria / tests

- Given fixture job classified `Titan (2021)`, correcting to TMDB **Titan A.E. (2000)** updates job identity, file and parent folder, relevant extras, and stored file paths **without changing file bytes**.
- Preview performs **no writes** and rejects invalid TMDB IDs, wrong type, wrong state and absent files.
- A conflicting target file stops the operation, preserving both trees and metadata.
- A second identical request is harmless/idempotent; an out-of-date preview is rejected.
- Simulated rename failure and DB transaction failure leave recoverable state, not silent partial completion.
- Test cross-device move branch with mocked filesystem or temporary mounts where feasible.
- Test symlink/path traversal defenses; files outside configured library cannot be affected.
- Validate no existing movie/TV review, track reassignment, or rip state-machine regression.
- Backend unit/integration tests, frontend component/e2e tests, `uv run ruff check .`, `uv run pytest`, `npm run build`, relevant Playwright run.

## Development guidance for Claude / Grok

1. Start with **read-only code survey**: find concrete models, persistence fields, routes, frontend components and organizer helpers; identify any existing identity-change code reusable for completed movies.
2. Report a short implementation plan with exact file paths and migration needs **before editing**.
3. Implement on this feature branch; keep commits scoped (service/API, frontend, tests). No work on `main`.
4. Use an isolated worktree, test database, and simulation; **do not** start a second backend polling the physical optical drive. `CLAUDE.md` says multiple backends can race on the same drive.
5. Never delete `backend/engram.db`, never run against the real Titan job, never delete original media in automated tests.
6. Stop only the servers/processes your dev session started.
7. Open a draft PR against the fork's `main` once checks pass; do not open an upstream PR before review.

## Follow-on issue: false-positive classification

Investigate the reported `tmdb+heuristic` confidence **95%** for `TITAN` → `Titan (2021)`, despite `Titan A.E. (2000)` being the intended film, plus the mismatched TheDiscDB panel. Trace exact confidence computation, disc fingerprint/matching provenance, and how conflicting signals influence auto-organization. Propose confidence caps and mandatory review for unresolved identity conflicts. Keep this separate from completed-job correction.
