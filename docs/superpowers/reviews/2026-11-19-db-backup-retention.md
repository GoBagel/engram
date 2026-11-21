# Database backup retention: investigation

Date: 2026-11-19. Status: findings only, nothing in `database_backup.py` is changed by this PR.
Prompted by the test-suite contamination of a real `engram.db` (see the isolation changes in
this PR): the only clean copy of the user's configuration was an automatic pre-migration backup.

## What the code does today (`backend/app/database_backup.py`)

- **When a backup is taken.** Only from `init_db()`, only when startup is about to change the
  database: the app version differs from the version recorded in `<db>.version`, or the live schema
  has drifted from the models, or an Alembic revision is pending. A startup with the same version and
  no schema change takes no backup.
- **What it captures.** The database as it was *before that startup's migration*, via SQLite's
  online backup API (WAL-consistent), named `<db>.bak-<previous-version>-<timestamp>`.
- **How many are kept.** `BACKUP_KEEP = 3`. After every new backup `_prune` deletes the oldest by
  timestamp beyond three. Pruning does not look at content.
- **Crash-loop guard.** A startup that dies mid-migration keeps its original backup and does not
  take another (`pending_backup` in the marker), so a failing upgrade cannot rotate the good copy out.
- Backups are written next to the database, in the same directory.

## Findings

1. **A restore can spend a backup slot, so a clean copy can be rotated out by routine use.** This
   is the main risk. The incident's clean backup was a pre-migration copy from 23:01. After the user
   restored it, the next start backed up again (a second `.bak-0.45.2-...` appeared at 01:25). That
   is inferred, not verified: the restored copy predates the `movie_identity_operations` table, so
   the schema-drift trigger fires and takes a backup, which is correct behaviour. But each
   restore-then-start, each release with a migration, and each dev schema change takes a slot. Three
   of those and the original is deleted, and the newer backups are copies of whatever state the
   database was in, damaged or not.
2. **The backup trigger does not cover this class of damage at all.** The contamination happened
   while no startup was migrating anything. The clean copy existed only because a new-version backend
   had started at 23:01. Had the suite run an hour earlier, before that start, no copy would have
   existed. Backups protect against bad migrations, not against content being overwritten later.
3. **Pruning is content-blind.** A backup of an already-damaged database counts the same as a good
   one. Nothing marks "last copy that held a configured `app_config`".
4. **`app_config` is not enforced as a singleton.** `get_config()` selects `limit(1)` with no
   ordering, so 26 rows left the app silently using row 1. A guard against more than one row would
   have turned silent corruption into a visible startup warning.
5. **Backups share the database's directory.** Losing the directory (or a `git clean`, a Docker
   volume reset) loses both. Acceptable for a local tool, worth saying in the restore docs.

## Recommendations (not implemented here; ordered by value for effort)

1. **Docs (free, do first).** In `docs/troubleshooting.md`, tell users to copy the backup they are
   restoring from to a safe place before the next start, since that start may take a new backup and
   count against the limit (finding 1).
2. **Snapshot the config on change, not only on migration.** Write a small JSON or SQLite export of
   `app_config` (the credentials and paths users must re-enter by hand) to a rotating
   `<db>.config-snapshots/` folder whenever the config is saved, keeping the last ~10 distinct
   versions. This is the one that covers finding 2 directly, is tiny, and does not depend on
   judging which database copy is good.
3. **Detect config regression at startup.** If `app_config` has more than one row, or `setup_complete`
   went from 1 to 0 since the last recorded start, log a loud warning and take a backup before
   continuing. Covers finding 4 and gives a trigger for finding 2.
4. **Pin a last-known-good backup, as a heuristic.** When pruning, also keep the newest backup whose
   `app_config` has `setup_complete = 1` and a non-empty credential. This is a heuristic, not proof
   of a good copy: a newer, damaged database can satisfy both tests and displace the genuinely good
   one. It narrows finding 1 and 3 but does not close them, so prefer an explicit pin (a marker the
   user or a successful config save sets, which only another explicit action replaces) if this is
   built, and treat 2 as the primary protection.
5. **Raise `BACKUP_KEEP` modestly (5) only if 4 is not adopted.** More slots delay finding 1 but do
   not fix it.

Each of 2 to 4 is small enough for its own PR and would need tests in
`backend/tests/unit/test_database_backup.py`.

## Follow-up status

- **Recommendation 1 (docs): done**, in `docs/troubleshooting.md` (restore steps now say to keep a copy of the backup).
- **Recommendation 2 (snapshot the config on change): done.** `backend/app/services/config_snapshots.py` writes a settings file (the Settings -> Export format, secrets included) when settings are saved and at startup, keeps the newest 10 beside the database, and restores through the existing Settings -> Import. It reuses `config_transfer.export_from_config`; there is no second format. Tests: `backend/tests/unit/test_config_snapshots.py`.
- **Recommendation 3 (detect a config regression at startup): not started.** Detection only; the snapshot is what makes recovery possible.
- **Recommendation 4 (pin a last-known-good backup) and 5 (`BACKUP_KEEP`): not planned.** The snapshot removes the need for the heuristic pin.
