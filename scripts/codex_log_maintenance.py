#!/usr/bin/env python3
"""Keep a workspace-scoped Codex diagnostic log database bounded.

The Codex app-server creates ``logs_2.sqlite`` with incremental auto-vacuum
enabled.  It deletes old diagnostic rows itself, but SQLite does not return
their pages to the filesystem until incremental_vacuum is requested.  This
helper runs before app-server starts, when this runtime is the database's only
writer, so it can safely compact the log database without touching Codex state.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


LOG_DATABASE_NAME = "logs_2.sqlite"
REQUIRED_LOG_COLUMNS = {"id", "feedback_log_body", "estimated_bytes"}
DEFAULT_MAX_ROWS = 20_000
DEFAULT_MAX_ESTIMATED_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_VACUUM_PAGES = 262_144
VACUUM_BATCH_PAGES = 32_768


@dataclass(frozen=True)
class Settings:
    max_rows: int = DEFAULT_MAX_ROWS
    max_estimated_bytes: int = DEFAULT_MAX_ESTIMATED_BYTES
    max_vacuum_pages: int = DEFAULT_MAX_VACUUM_PAGES


@dataclass(frozen=True)
class Result:
    status: str
    deleted_rows: int = 0
    reclaimed_pages: int = 0
    before_bytes: int = 0
    after_bytes: int = 0


def _log(message: str) -> None:
    # stdout is the JSONL stream consumed by the runtime bridge.
    print(f"[argus-codex-log-maintenance] {message}", file=sys.stderr, flush=True)


def _nonnegative_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        _log(f"ignoring invalid {name}={raw!r}; using {default}")
        return default
    if value < 0:
        _log(f"ignoring negative {name}={raw!r}; using {default}")
        return default
    return value


def settings_from_environment() -> Settings:
    return Settings(
        max_rows=_nonnegative_env("ARGUS_CODEX_LOG_MAX_ROWS", DEFAULT_MAX_ROWS),
        max_estimated_bytes=_nonnegative_env(
            "ARGUS_CODEX_LOG_MAX_ESTIMATED_BYTES", DEFAULT_MAX_ESTIMATED_BYTES
        ),
        max_vacuum_pages=_nonnegative_env(
            "ARGUS_CODEX_LOG_MAX_VACUUM_PAGES", DEFAULT_MAX_VACUUM_PAGES
        ),
    )


def _database_size(path: Path) -> int:
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            total += candidate.stat().st_size
        except FileNotFoundError:
            pass
    return total


def _is_codex_log_database(conn: sqlite3.Connection) -> bool:
    if int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) != 2:
        return False
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
    ).fetchone()
    if row is None:
        return False
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(logs)")
        if isinstance(row[1], str)
    }
    return REQUIRED_LOG_COLUMNS.issubset(columns)


def _retained_minimum_id(
    conn: sqlite3.Connection, settings: Settings
) -> int | None:
    if settings.max_rows == 0 and settings.max_estimated_bytes == 0:
        return None

    retained_rows = 0
    retained_bytes = 0
    oldest_retained_id: int | None = None
    for log_id, raw_estimated_bytes in conn.execute(
        "SELECT id, COALESCE(estimated_bytes, 0) FROM logs ORDER BY id DESC"
    ):
        estimated_bytes = max(0, int(raw_estimated_bytes or 0))
        exceeds_row_limit = (
            settings.max_rows > 0 and retained_rows >= settings.max_rows
        )
        exceeds_byte_limit = (
            settings.max_estimated_bytes > 0
            and retained_rows > 0
            and retained_bytes + estimated_bytes > settings.max_estimated_bytes
        )
        if exceeds_row_limit or exceeds_byte_limit:
            break
        oldest_retained_id = int(log_id)
        retained_rows += 1
        retained_bytes += estimated_bytes
    return oldest_retained_id


def _incremental_vacuum(conn: sqlite3.Connection, max_pages: int) -> int:
    if max_pages <= 0:
        return 0
    initial_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
    before_pages = initial_pages
    remaining = max_pages
    while remaining > 0:
        batch = min(remaining, VACUUM_BATCH_PAGES)
        conn.execute(f"PRAGMA incremental_vacuum({batch})")
        after_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        reclaimed = before_pages - after_pages
        if reclaimed <= 0:
            break
        before_pages = after_pages
        remaining -= min(batch, reclaimed)
        if reclaimed < batch:
            break
    return max(0, initial_pages - int(conn.execute("PRAGMA page_count").fetchone()[0]))


def maintain_log_database(path: Path, settings: Settings) -> Result:
    """Trim and compact one verified Codex log database.

    Any lock, malformed database, or schema mismatch is reported and skipped.
    A failure here must never prevent a runtime from coming up.
    """

    if path.name != LOG_DATABASE_NAME or not path.is_file():
        return Result(status="absent")

    before_bytes = _database_size(path)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        if not _is_codex_log_database(conn):
            return Result(status="schema-mismatch", before_bytes=before_bytes, after_bytes=before_bytes)

        conn.execute("BEGIN IMMEDIATE")
        minimum_id = _retained_minimum_id(conn, settings)
        deleted_rows = 0
        if minimum_id is not None:
            deleted_rows = conn.execute(
                "DELETE FROM logs WHERE id < ?", (minimum_id,)
            ).rowcount
        conn.commit()

        # Checkpoint before compacting so WAL frames do not keep disk space alive.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        freelist_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        page_count_before_vacuum = int(conn.execute("PRAGMA page_count").fetchone()[0])
        if freelist_pages > 0 and settings.max_vacuum_pages > 0:
            _incremental_vacuum(conn, settings.max_vacuum_pages)
        page_count_after_vacuum = int(conn.execute("PRAGMA page_count").fetchone()[0])
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        after_bytes = _database_size(path)
        return Result(
            status="maintained",
            deleted_rows=max(0, deleted_rows),
            reclaimed_pages=max(0, page_count_before_vacuum - page_count_after_vacuum),
            before_bytes=before_bytes,
            after_bytes=after_bytes,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return Result(status=f"skipped: {exc}", before_bytes=before_bytes, after_bytes=_database_size(path))
    finally:
        if conn is not None:
            conn.close()


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    codex_home = Path(args[0]) if args else Path(os.getenv("CODEX_HOME", ".codex"))
    result = maintain_log_database(codex_home / LOG_DATABASE_NAME, settings_from_environment())
    if result.status == "maintained":
        _log(
            "maintained logs_2.sqlite: "
            f"deleted_rows={result.deleted_rows} reclaimed_pages={result.reclaimed_pages} "
            f"bytes={result.before_bytes}->{result.after_bytes}"
        )
    elif result.status != "absent":
        _log(f"{result.status}; left logs_2.sqlite unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
