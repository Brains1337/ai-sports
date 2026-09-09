"""Database access and run tracking.

Two jobs:

1. One engine factory, so scripts stop each building their own.
2. :func:`run_tracked`, which records every script execution in ``sync_runs``
   and distinguishes "succeeded" from "succeeded but wrote nothing". The
   latter was this platform's dominant failure mode and was invisible: nine
   day old FantasyPros data and a projection source that had never been
   populated both went unnoticed because nothing logged a run.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError  # NOT psycopg.ProgrammingError

_engine: Engine | None = None


def get_engine() -> Engine:
    """Process-wide engine, created lazily."""
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise SystemExit("DATABASE_URL is not set")
        _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


@contextmanager
def begin() -> Iterator[Any]:
    """Transactional connection. Commits on success, rolls back on error."""
    with get_engine().begin() as conn:
        yield conn


class RunStats:
    """Mutable counters a script updates as it works.

    ``skipped`` matters as much as ``written``. The old scripts used bare
    ``continue`` on unmatched rows, so silent row loss was unmeasurable —
    ESPN dropped every unmapped position and every player missed by the
    2000-row fetch cap without a single warning.
    """

    __slots__ = ("written", "skipped", "meta")

    def __init__(self) -> None:
        self.written = 0
        self.skipped = 0
        self.meta: dict[str, Any] = {}

    def add(self, written: int = 0, skipped: int = 0) -> None:
        self.written += written
        self.skipped += skipped

    def note(self, key: str, value: Any) -> None:
        self.meta[key] = value

    def bump(self, key: str, by: int = 1) -> None:
        """Increment a named counter inside meta (e.g. per-reason skip tallies)."""
        self.meta[key] = int(self.meta.get(key, 0)) + by


@contextmanager
def run_tracked(
    script_name: str,
    *,
    empty_is_error: bool = True,
    min_rows: int = 1,
) -> Iterator[RunStats]:
    """Record a script execution in ``sync_runs``.

    Terminal status is one of:

    ``ok``
        Completed and wrote at least ``min_rows`` rows.
    ``empty``
        Completed without error but wrote too few rows. Treated as a failure
        when ``empty_is_error`` (the default) so the process exits non-zero
        and the scheduler/alerting notices. This is the status that would
        have caught every bug in this codebase months ago.
    ``error``
        Raised. The traceback is stored and re-raised.

    The tracking rows are written on their own short-lived connections so a
    failure inside the caller's transaction cannot roll away the audit trail.
    """
    stats = RunStats()

    with begin() as conn:
        run_id = conn.execute(
            text(
                """
                insert into sync_runs (script_name, status)
                values (:script_name, 'running')
                returning id
                """
            ),
            {"script_name": script_name},
        ).scalar_one()

    def _finish(status: str, error_text: str | None = None) -> None:
        try:
            with begin() as conn:
                conn.execute(
                    text(
                        """
                        update sync_runs
                           set finished_at  = now(),
                               status       = :status,
                               rows_written = :written,
                               rows_skipped = :skipped,
                               error_text   = :error_text,
                               meta         = cast(:meta as jsonb)
                         where id = :run_id
                        """
                    ),
                    {
                        "status": status,
                        "written": stats.written,
                        "skipped": stats.skipped,
                        "error_text": error_text,
                        "meta": json.dumps(stats.meta, default=str),
                    },
                )
        except SQLAlchemyError as exc:  # pragma: no cover - best effort
            print(f"[{script_name}] WARNING: could not finalize sync_runs: {exc}",
                  file=sys.stderr)

    try:
        yield stats
    except BaseException:
        _finish("error", traceback.format_exc(limit=20))
        raise

    if stats.written < min_rows:
        msg = (
            f"[{script_name}] wrote {stats.written} rows "
            f"(skipped {stats.skipped}); expected at least {min_rows}"
        )
        _finish("empty", msg)
        print(msg, file=sys.stderr)
        if empty_is_error:
            raise SystemExit(2)
        return

    _finish("ok")
    print(
        f"[{script_name}] ok: wrote {stats.written}, skipped {stats.skipped}"
        + (f", meta={json.dumps(stats.meta, default=str)}" if stats.meta else "")
    )


def resolve_league_id(
    conn: Any,
    platform: str,
    external_league_key: str,
    season: int,
) -> int | None:
    """Look a league up by its canonical identity.

    Always use this instead of ``order by id limit 1``. ``sync_fantrax.py``
    used that shortcut because the real Fantrax league id is alphanumeric and
    would not fit the bigint column, so every league's history was written to
    whichever Fantrax row happened to sort first. Migration 012 added
    ``external_league_key`` to make this lookup possible.
    """
    return conn.execute(
        text(
            """
            select id from leagues
             where platform            = :platform
               and external_league_key = :key
               and season              = :season
            """
        ),
        {"platform": str(platform), "key": str(external_league_key), "season": season},
    ).scalar_one_or_none()
