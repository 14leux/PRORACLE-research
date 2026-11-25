"""Manual backfill of regulation-time scores for fixtures that already have a result.

Off by default and run by hand only. The scheduled run captures a score with each NEW result when
PRORACLE_CAPTURE_SCORES=1; results stored before that have no score, and this fills them in from the
provider's finished events. A dry run (the default) only counts. Writing needs BOTH ``--execute``
and PRORACLE_ALLOW_SCORE_BACKFILL=1, so a stray flag or variable alone does nothing.

Environment: BSD_API_TOKEN; with --execute also SUPABASE_URL and SUPABASE_SERVICE_KEY (and
optionally SUPABASE_API_KEY), as for the main run. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from proracle.bsd import BSDClient, BSDSourceError
from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError

ALLOW_SWITCH = "PRORACLE_ALLOW_SCORE_BACKFILL"
DEFAULT_DAYS = 30
MAX_DAYS = 365
CHUNK_DAYS = 30
# After this many failed writes the run stops, so a missing table cannot cause a flood of requests.
MAX_WRITE_ERRORS = 5


def run_backfill(
    *,
    execute: bool,
    days: int = DEFAULT_DAYS,
    bsd_client: BSDClient | None = None,
    store: SupabaseRESTStore | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--days must be between 1 and {MAX_DAYS}")
    if execute and os.environ.get(ALLOW_SWITCH, "") != "1":
        raise ValueError(f"{ALLOW_SWITCH}=1 is required with --execute")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if bsd_client is None:
        token = os.environ.get("BSD_API_TOKEN", "")
        if not token:
            raise ValueError("BSD_API_TOKEN is required")
        bsd_client = BSDClient(token, timeout_seconds=30.0, max_retries=2)

    summary = {
        "finished_events": 0,
        "with_score": 0,
        "already_scored": 0,
        "fixture_missing": 0,
        "scores_submitted": 0,
        "write_errors": 0,
    }
    known: dict[str, str] = {}
    scored: set[str] = set()
    writer: SupabaseRESTStore | None = None
    if execute:
        if store is None:
            url, key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", "")
            if not url or not key:
                raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required with --execute")
            store = SupabaseRESTStore(
                url,
                key,
                schema=os.environ.get("PRORACLE_DB_SCHEMA", "proracle"),
                api_key=os.environ.get("SUPABASE_API_KEY") or None,
            )
        writer = store
        known = writer.fixture_ids_by_source_key()
        scored = writer.score_fixture_ids()

    end = now.date()
    start = end - timedelta(days=days)
    chunk_start = start
    while chunk_start < end and summary["write_errors"] < MAX_WRITE_ERRORS:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
        for match in bsd_client.fetch_events("finished", chunk_start.isoformat(), chunk_end.isoformat()):
            summary["finished_events"] += 1
            if match.home_score is None or match.away_score is None:
                continue
            summary["with_score"] += 1
            if not execute:
                continue
            fixture_id = known.get(match.source_event_key)
            if fixture_id is None:
                summary["fixture_missing"] += 1
            elif fixture_id in scored:
                summary["already_scored"] += 1
            elif writer is not None and summary["write_errors"] < MAX_WRITE_ERRORS:
                try:
                    writer.append_match_score(fixture_id, match.home_score, match.away_score, now)
                    scored.add(fixture_id)
                    summary["scores_submitted"] += 1
                except SupabaseStoreError:
                    summary["write_errors"] += 1
        chunk_start = chunk_end
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill regulation-time scores (manual, off by default).")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"how far back, 1 to {MAX_DAYS}")
    parser.add_argument(
        "--execute",
        action="store_true",
        help=f"write scores (also needs {ALLOW_SWITCH}=1); without it the run only counts",
    )
    args = parser.parse_args(argv)
    try:
        summary = run_backfill(execute=args.execute, days=args.days)
    except BSDSourceError as error:
        print(f"PRORACLE backfill stopped: {error.error_code}", file=sys.stderr)
        return 2
    except SupabaseStoreError as error:
        print(f"PRORACLE backfill stopped: {error.error_code}", file=sys.stderr)
        return 3
    except ValueError as error:
        print(f"PRORACLE backfill configuration/input error: {error}", file=sys.stderr)
        return 2
    label = "write" if args.execute else "preview"
    fields = " ".join(f"{key}={value}" for key, value in summary.items())
    if summary["write_errors"]:
        print(f"PRORACLE backfill {label} incomplete: {fields}", file=sys.stderr)
        return 3
    print(f"PRORACLE backfill {label} complete: {fields}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
