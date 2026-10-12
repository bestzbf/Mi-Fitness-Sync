from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timedelta, timezone

import requests

from mi_fitness_sync.activity.client import MiFitnessActivitiesClient
from mi_fitness_sync.activity.formatting import parse_cli_time
from mi_fitness_sync.activity.models import Activity
from mi_fitness_sync.activity.utils import render_activities_table
from mi_fitness_sync.auth.store import load_state
from mi_fitness_sync.cli.speed_parser import parse_speed_input
from mi_fitness_sync.exceptions import AuthStateNotFoundError, MiFitnessError, StravaError
from mi_fitness_sync.export.render import render_export
from mi_fitness_sync.paths import get_exports_dir
from mi_fitness_sync.strava.client import StravaClient
from mi_fitness_sync.strava.sport_mapping import strava_sport_type
from mi_fitness_sync.strava.store import load_tokens


RUN_TYPES = frozenset({1, 3, 5})
STRAVA_RUN_TYPES = frozenset({"Run", "TrailRun", "VirtualRun"})
MATCH_WINDOW_SECONDS = 6


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sync-runs", help="Preview or upload only running workouts (China region by default)"
    )
    parser.add_argument("--since", help="Inclusive start time (default: 30 days ago)")
    parser.add_argument("--until", help="Inclusive end time (default: now)")
    parser.add_argument(
        "--limit", type=int, default=1000,
        help="Maximum Mi Fitness workouts to scan, including rides (default: 1000)",
    )
    parser.add_argument("--country-code", default="CN", help="Mi Fitness country (default: CN)")
    parser.add_argument(
        "--outdoor-only", action="store_true", help="Only outdoor running; exclude treadmill and trail runs"
    )
    parser.add_argument("--upload", action="store_true", help="Upload missing runs; otherwise preview only")
    parser.add_argument("--state-path", help="Override the Mi Fitness auth state path")
    parser.add_argument("--strava-token-path", help="Override the Strava token file path")
    parser.add_argument("--no-cache", action="store_true", help="Disable local FDS binary cache")
    parser.add_argument("--cache-dir", help="Override the local FDS cache directory")
    parser.add_argument("--no-smooth", action="store_true", help="Disable GPS smoothing")
    parser.add_argument("--outlier-speed", help="Max plausible speed as km/h or pace, e.g. '7:30'")
    parser.add_argument("--smooth-mode", choices=("match", "full"), default="match")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")


def is_run(activity: Activity, *, outdoor_only: bool = False) -> bool:
    # Never infer a run from a user-editable title or an ambiguous category.
    allowed = {1} if outdoor_only else RUN_TYPES
    return (
        type(activity.sport_type) is int
        and activity.sport_type in allowed
        and activity.start_time is not None
        and activity.sync_state != "deleted"
    )


def fetch_existing_run_starts(strava: StravaClient, runs: list[Activity]) -> set[int]:
    if not runs:
        return set()
    starts = [activity.start_time for activity in runs if activity.start_time is not None]
    after = min(starts) - MATCH_WINDOW_SECONDS - 1
    before = max(starts) + MATCH_WINDOW_SECONDS + 1
    existing: set[int] = set()
    page = 1
    while True:
        batch = strava.list_activities(after=after, before=before, per_page=200, page=page)
        for activity in batch:
            if activity.get("sport_type", activity.get("type")) not in STRAVA_RUN_TYPES:
                continue
            try:
                timestamp = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("missing UTC offset")
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise StravaError("Cannot check duplicates: a Strava run has an invalid start_date.") from exc
            existing.add(int(timestamp.timestamp()))
        if len(batch) < 200:
            return existing
        page += 1


def scan_window(args: argparse.Namespace) -> tuple[int, int]:
    if args.limit <= 0:
        raise MiFitnessError("--limit must be greater than zero.")
    now = datetime.now(timezone.utc)
    try:
        start_time = parse_cli_time(args.since) if args.since else int((now - timedelta(days=30)).timestamp())
        end_time = parse_cli_time(args.until) if args.until else int(now.timestamp())
    except (ValueError, OverflowError) as exc:
        raise MiFitnessError("--since and --until must be unix seconds or ISO-8601 dates.") from exc
    if start_time > end_time:
        raise MiFitnessError("--since must be earlier than or equal to --until.")
    return start_time, end_time


def handle_sync_runs(args: argparse.Namespace) -> int:
    start_time, end_time = scan_window(args)

    state = load_state(args.state_path)
    if state is None:
        raise AuthStateNotFoundError("No Mi Fitness login found. Run 'mi-fitness-sync login' first.")
    tokens = load_tokens(args.strava_token_path)
    if args.upload and tokens is None:
        raise MiFitnessError("No Strava login found. Run 'mi-fitness-sync strava-login' first.")

    client_kwargs = {"country_code": args.country_code, "no_cache": args.no_cache}
    if args.cache_dir is not None:
        client_kwargs["cache_dir"] = args.cache_dir
    client = MiFitnessActivitiesClient(state, **client_kwargs)
    try:
        activities = client.list_activities(
            start_time=start_time, end_time=end_time, limit=args.limit + 1,
        )
        truncated = len(activities) > args.limit
        if truncated and args.upload:
            raise MiFitnessError(
                "Scan exceeded --limit; no runs uploaded. Increase --limit or narrow the date range."
            )
        activities = activities[:args.limit]
        runs = [activity for activity in activities if is_run(activity, outdoor_only=args.outdoor_only)]
        strava = StravaClient(tokens, token_path=args.strava_token_path) if tokens else None
        existing = fetch_existing_run_starts(strava, runs) if strava else set()
        status = {
            activity.activity_id: any(
                abs(activity.start_time - timestamp) <= MATCH_WINDOW_SECONDS for timestamp in existing
            )
            for activity in runs
        }
        print(f"Scanned {len(activities)} workouts; {len(runs)} runs; skipped {len(activities) - len(runs)} others.")
        print(render_activities_table(runs, strava_status=status if strava else None))
        if truncated:
            print("Scan reached --limit; increase it to include older workouts.", file=sys.stderr)
        if not args.upload:
            print("Preview only. Add --upload to sync missing runs.")
            if strava is None:
                print("Strava duplicates not checked. Run 'mi-fitness-sync strava-login' to enable this check.")
            return 0
        if not runs:
            print("No running workouts to upload.")
            return 0

        smoothing = {"smooth": not args.no_smooth, "smooth_mode": args.smooth_mode}
        if args.outlier_speed:
            smoothing["outlier_speed_mps"] = parse_speed_input(args.outlier_speed)
        uploaded = 0
        skipped = 0
        for activity in sorted(runs, key=lambda activity: activity.start_time):
            if any(abs(activity.start_time - timestamp) <= MATCH_WINDOW_SECONDS for timestamp in existing):
                print(f"Already on Strava: {activity.activity_id}")
                skipped += 1
                continue
            detail = client.get_activity_detail(activity)
            if not is_run(detail.activity, outdoor_only=args.outdoor_only):
                raise MiFitnessError(f"Activity {activity.activity_id} is no longer a recognized run; upload stopped.")
            if detail.activity.activity_id != activity.activity_id or detail.start_time != activity.start_time:
                raise MiFitnessError(f"Activity {activity.activity_id} changed during lookup; upload stopped.")
            export = render_export(detail, "fit", **smoothing)
            digest = hashlib.sha256(activity.activity_id.encode()).hexdigest()[:16]
            output_path = get_exports_dir() / f"run_{activity.start_time}_{digest}.fit"
            output_path.write_bytes(export.payload)
            result = strava.upload_activity(
                export.payload, sport_type=strava_sport_type(detail.activity.sport_type),
                external_id=activity.activity_id,
            )
            strava_id = result.get("activity_id")
            if not strava_id:
                raise StravaError(f"Strava did not confirm activity {activity.activity_id}; upload stopped.")
            existing.add(activity.start_time)
            uploaded += 1
            print(f"Uploaded: https://www.strava.com/activities/{strava_id}")
            print(f"FIT backup: {output_path}")
        print(f"Finished: uploaded {uploaded} runs; skipped {skipped} existing runs.")
        return 0
    except requests.RequestException as exc:
        raise MiFitnessError(f"Sync stopped because a network request failed: {exc}") from exc
    except OSError as exc:
        raise MiFitnessError(f"Sync stopped because a local file could not be written: {exc}") from exc
