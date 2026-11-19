# Database isolation under pytest

How the backend test suite is kept away from a real `engram.db`, what that does and does not
guarantee, how to check it before running tests, and what to do when it refuses something.

## Why it exists

The default database URL is `sqlite+aiosqlite:///./engram.db`, relative to the working
directory, and `app.database` builds its engine at import. Running `uv run pytest` from `backend/`
therefore opened the developer's real database. Integration tests that call `init_db()` and
`async_session()` directly wrote fixture rows into it, replaced a configured `app_config` row (the
setup wizard reappeared and the credentials were gone) and deleted job history.

## What is guaranteed, and what is not

This is a guard against accidents. It is not a security boundary: code written to defeat it can.

| Guarantee | Mechanism | Verified by |
| --- | --- | --- |
| A pytest run uses a throwaway database, whatever `DATABASE_URL` the shell exports. | `tests/conftest.py` bootstrap, before any `app` import. | `test_conftest_overrides_a_hostile_ambient_database_url`, `test_the_default_url_is_replaced_even_when_nothing_is_exported` |
| The session aborts if the engine is not isolated. | `pytest_sessionstart` in `tests/conftest.py`. | `test_sessionstart_hook_aborts_the_run_when_the_engine_is_not_isolated` |
| A configured URL outside a registered directory is refused at import, in `init_db()` and in the sync engine factory. | `enforce_test_database_isolation`. | `test_importing_the_app_under_pytest_with_an_unregistered_url_is_refused`, `test_init_db_refuses_a_replaced_database_url_before_touching_anything`, `test_sync_engine_factory_refuses_a_real_database_before_any_io` |
| Any `sqlite3.connect` to a file outside a registered directory is refused before the file exists: engines, the backup helper, the TMDB and transcript caches, Alembic helpers, scripts. | Wrapper on `sqlite3.connect` and `sqlite3.dbapi2.connect`. | `TestSqliteConnectGuard`, `TestOpenersThatUsedToBypassTheUrlChecks` |
| The target is judged the way SQLite reads it: the `uri` flag is honoured, URIs are parsed with SQLite's rules, control characters, repeated parameters and non-local authorities are refused. | `_parse_sqlite_uri`, `_file_from_sqlite_target`. | `TestUriSemantics`, `TestUriParserAgreesWithSqlite` (includes a differential test against real SQLite) |
| A connector captured before the guard armed, `sqlite3.Connection(...)` and a `creator=` callable are checked too. | `sys.addaudithook` on the `sqlite3.connect` event. | `TestCapturedConnectors`, `TestMarkerCannotBeReusedOrErased` |
| `ATTACH DATABASE` and `VACUUM INTO` fail on connections opened through the wrapper, including from a factory's own `__init__`. | Attach limit 0, set inside `_GuardedConnection.__init__` through the C method; `setlimit` refuses to raise it. | `TestAttachBanCannotBeUndone`, `TestAttachPinHoldsFromTheFirstInstruction` |
| The guard refuses to arm if the attach ban cannot be enforced on this Python/SQLite. | `_attached_limit_category`. | `test_it_fails_closed_when_the_ban_cannot_be_enforced` |
| `pytest --cov` (CI) keeps working. | Coverage's own `.coverage` / `.coverage.*` files are exempt from the location rule and the attach pin. | `TestCoverageDataFiles` (includes a real nested `pytest --cov` run) |
| Nothing is patched, and no audit hook is added, in a process that never imports pytest. | Everything is gated on `"pytest" in sys.modules`. | `test_guard_and_wrapper_are_inert_in_a_process_that_never_imported_pytest` |

### Limits

These are known and deliberate. Do not rely on the guard for anything in this list.

- **Activation.** The guard arms when `tests/conftest.py` or `app.config` runs under pytest. Importing
  pytest alone installs nothing. A connection opened before arming is neither re-checked nor pinned.
- **Connections that bypass the wrapper** (a captured connector, `sqlite3.Connection(...)`) have
  their target checked by the audit hook but do not get the attach pin. `ATTACH` from such a
  connection, which is itself bound to an allowed database, is not blocked.
- **The attach pin can be undone on purpose.** Calling the base method directly,
  `sqlite3.Connection.setlimit(conn, sqlite3.SQLITE_LIMIT_ATTACHED, 10)`, raises the limit; so does
  ctypes or a patched `sqlite3`.
- **Paths, not file identity.** A path is resolved and then opened later, so a symlink swapped in
  between is not detected. A hard link to a real database placed inside an allowed directory is
  accepted. Allowed directories are freshly created, so this needs someone actively working against
  the guard.
- **Pytest leaving `sys.modules`.** The audit hook cannot be removed. If pytest is removed from
  `sys.modules` the hook and wrapper stop checking; already-created guarded connections keep their
  pin.
- **Reads are refused too.** A `mode=ro` open of a real database is refused like any other open.
- **Native code.** A native extension or a replacement SQLite library that does not go through
  Python's `sqlite3` is outside every layer. This was not tested.
- **Cleanup on a crash.** The database directory is removed at normal interpreter exit. A killed run
  leaves an empty `engram-pytest-db-*` directory in the system temp dir; it is safe to delete.

## How it works

The layers, outermost first. Each is a separate check; the later ones exist because the earlier ones
only see URLs.

1. **URL check** (`enforce_test_database_isolation`). `app/config.py` calls it right after the
   settings object is created, `init_db()` calls it again (a test may have replaced the setting) and
   so does the sync engine factory in `app/services/config_service.py`. It accepts an in-memory
   SQLite URL or a file inside a registered directory, and rejects everything else, including
   non-SQLite URLs and `file:` URIs. It opens nothing.
2. **`sqlite3.connect` wrapper** (`_guarded_connect`, installed by `install_sqlite_guard`). It
   replaces `sqlite3.connect` and `sqlite3.dbapi2.connect` (SQLAlchemy calls the latter; aiosqlite
   calls the former). It judges the target with `_file_from_sqlite_target`, which follows SQLite: with
   `uri=True` a `file:` string is parsed by `_parse_sqlite_uri` (not `urllib`, which strips tabs and
   newlines), otherwise the string is a literal filename. It then opens the connection with a
   `Connection` subclass, `_GuardedConnection`, whose attach limit is set to 0 in `__init__`.
3. **Audit hook** (`_audit_hook`). CPython raises a `sqlite3.connect` audit event from below the
   replaceable module attributes, so this covers openers the wrapper never sees. The event carries the
   target but not the `uri` flag, so for a `file:` string both readings (a literal filename in the
   current directory, and a URI) must be safe. A call the wrapper already judged precisely passes
   through a thread-local mark: the hook consumes it on first use and the wrapper restores it
   afterwards, so a connect nested inside a factory cannot reuse or erase it.
4. **Connection check** (`enforce_connection_isolation`). The engines' connect hook
   (`set_sqlite_pragma`) asks SQLite for `PRAGMA database_list` and checks every row, before any
   pragma that writes.

The first thing `tests/conftest.py` does, before any `app` import except the dependency-free
`app.db_guard`, is create a fresh directory, set `DATABASE_URL` to a file in it, register the
directory and install the guard. A session-scoped fixture registers pytest's basetemp (which
contains `tmp_path`).

## Registered roots and `tmp_path`

Only registered directories may hold SQLite files: the bootstrap directory and pytest's basetemp.
The system temp directory is not trusted as a whole, because `TMPDIR` can point anywhere.

- A test that needs a database file uses `tmp_path`: `sqlite3.connect(tmp_path / "case.sqlite")`.
- A test that needs no persistence uses `:memory:`.
- A harness that must write elsewhere creates its own fresh directory and registers it with
  `db_guard.allow_test_root`. Never register a home directory, a checkout or the system temp
  directory.

Coverage data (`.coverage`, `.coverage.<suffix>`) is exempt by file name, in any directory. A coverage
file given a different name through `COVERAGE_FILE` is not exempt and is refused unless it is in a
registered directory.

## Production is untouched

All of it is gated on `"pytest" in sys.modules`, which no served or packaged Engram process
imports and nothing in `app/` imports. The audit hook is only ever added under pytest. The inertness
test runs a fresh interpreter that imports the app without pytest and checks that `sqlite3.connect`
is the original function, that explicit arming is a no-op, and that arbitrary paths, `ATTACH` and the
attach limit all behave normally.

An application started inside a pytest process (an in-process server for an end-to-end test, say) is
subject to the guard like everything else in that process.

## Verifying isolation before the first test run

Do this before the first `pytest` run in a checkout, and after changing anything in the guard.

1. Check the import-time refusal. It runs no tests and must fail with
   `ProductionDatabaseAccessError`:

   ```bash
   cd backend
   DATABASE_URL=sqlite+aiosqlite:///./engram.db uv run python -c "import pytest, app.config"
   ```

   Any other failure (a missing dependency, a different exception) is not a successful check.

2. In a scratch checkout with no database of its own, also confirm nothing was created:

   ```bash
   test ! -e engram.db && echo "no engram.db created"
   ```

3. In a checkout whose `backend/engram.db` matters, record it before and after a run and compare.
   This reads the file without opening it as a database:

   ```bash
   sha256sum engram.db
   ls -la --time-style=full-iso engram.db*
   ```

   An unchanged hash and unchanged modification times, with no new `-wal` or `-shm` file, is what you
   expect. A matching hash of the main file alone says nothing about those two files.

4. Run only the isolation tests first: `uv run pytest tests/unit/test_db_isolation.py`.

## Troubleshooting

Every cause below is a bug in the test or its setup. Fix that; do not widen the registered
directories or weaken the guard.

- **`ProductionDatabaseAccessError` at import or at session start.** `tests/conftest.py` did not run
  before the app was imported: a custom `--rootdir`, a script that imports `app` under pytest, a
  plugin that imports it early, or a test that replaced `settings.database_url`. Run pytest from
  `backend/` against `tests/`.
- **A test opening its own SQLite file.** Move it into `tmp_path`, or use `:memory:`. This includes
  third-party caches opened from inside a test.
- **"unreadable or ambiguous sqlite target".** The URI has a control character, a repeated query
  parameter or a non-local authority. Use a plain filename.
- **A captured connector refuses a `file:` URI that the wrapped one accepts.** The audit event has no
  `uri` flag, so the hook needs both readings to be safe. Call `sqlite3.connect`, or use a plain
  filename.
- **`ATTACH` or `VACUUM INTO` fails.** It is disabled on purpose. Use separate connections to test
  databases and copy the fixture data explicitly.
- **The guard refuses to arm: "Cannot disable ATTACH DATABASE".** This Python or SQLite lacks
  `Connection.setlimit` or `SQLITE_LIMIT_ATTACHED`. Use a runtime that has them; the guard
  intentionally does not fall back to allowing `ATTACH`.
- **Coverage fails to write its data file.** Use the default file name, or point `COVERAGE_FILE` at a
  registered directory.

## Adding or changing the guard

- Run `uv run pytest tests/unit/test_db_isolation.py` from `backend/` after every change.
- Every test that proves a refusal must also prove the forbidden file was not created. The module's
  autouse fixture removes a canary file in `backend/` (deliberately not named `engram.db`) and fails
  the test if something created it. New refusal tests aim at that canary, never at a real database.
- When a change touches the URI parser, extend `TestUriParserAgreesWithSqlite`, which compares the
  parser with what SQLite really opens (`PRAGMA database_list`); do not reason about SQLite's URI
  rules from memory.
- Mutation-check the change: break one decision in a throwaway copy (accept the home directory,
  stop consuming the audit mark, drop the attach pin in `__init__`, and so on), run the file and
  expect failures, then restore the file. A mutant that survives means a missing test.
- Keep the module docstring in `app/db_guard.py` and the limits list above in step with the code.
