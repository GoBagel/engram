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

1. **Pin a last-known-good copy.** When pruning, always keep the newest backup whose `app_config`
   has `setup_complete = 1` and a non-empty credential, in addition to the newest N. A read-only
   open of each candidate at prune time is enough. This alone closes finding 1 and 3.
2. **Snapshot the config on change, not only on migration.** Write a small JSON or SQLite export of
   `app_config` (the credentials and paths users must re-enter by hand) to a rotating
   `<db>.config-snapshots/` folder whenever the config is saved, keeping the last ~10 distinct
   versions. This covers finding 2 and is tiny.
3. **Detect config regression at startup.** If `app_config` has more than one row, or `setup_complete`
   went from 1 to 0 since the last recorded start, log a loud warning and take a backup before
   continuing. Covers finding 4 and gives a trigger for finding 2.
4. **Raise `BACKUP_KEEP` modestly (5) only if 1 is not adopted.** More slots delay finding 1 but do
   not fix it.
5. **Docs.** In `docs/troubleshooting.md`, tell users to copy the backup they are restoring from to
   a safe place before the next start, since that start may take a new backup and count against the
   limit.

Each of 1 to 3 is small enough for its own PR and would need tests in
`backend/tests/unit/test_database_backup.py`.
