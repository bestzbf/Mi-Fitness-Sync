from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from mi_fitness_sync.activity.client import MiFitnessActivitiesClient
from mi_fitness_sync.auth.store import load_state
from mi_fitness_sync.auth.state import AuthState
from mi_fitness_sync.cli.speed_parser import parse_speed_input
from mi_fitness_sync.cli.sync_runs import RUN_TYPES, is_run, scan_window
from mi_fitness_sync.exceptions import AuthStateNotFoundError, MiFitnessError
from mi_fitness_sync.export.render import render_export


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("export-runs", help="Export only new runs as FIT for free Strava web upload")
    parser.add_argument("--since", help="Inclusive start time (default: 30 days ago)")
    parser.add_argument("--until", help="Inclusive end time (default: now)")
    parser.add_argument("--limit", type=int, default=1000, help="Maximum total workouts to scan")
    parser.add_argument("--country-code", default="CN", help="Mi Fitness country (default: CN)")
    parser.add_argument("--outdoor-only", action="store_true", help="Exclude treadmill and trail runs")
    parser.add_argument("--output-dir", default="待上传跑步", help="Folder for FIT files and local export history")
    parser.add_argument("--state-path", help="Override the Mi Fitness auth state path")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--cache-dir")
    parser.add_argument("--no-smooth", action="store_true")
    parser.add_argument("--outlier-speed", help="Max plausible speed as km/h or pace")
    parser.add_argument("--smooth-mode", choices=("match", "full"), default="match")
    parser.add_argument("--verbose", action="store_true")


def _save_history(path: Path, history: dict) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(history, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


@dataclass(frozen=True)
class ExportSummary:
    scanned: int
    runs: int
    exported: int
    already_exported: int
    pending: int
    output_dir: Path


def export_runs(
    args: argparse.Namespace, *, report: Callable[[str], None] = print,
    selected_ids: frozenset[str] | None = None,
    auth_refresher: Callable[[AuthState], AuthState] | None = None,
) -> ExportSummary:
    start_time, end_time = scan_window(args)
    state = load_state(args.state_path)
    if state is None:
        raise AuthStateNotFoundError("No Mi Fitness login found. Complete Xiaomi login first.")
    output_dir = Path(args.output_dir).expanduser().resolve()
    history_path = output_dir / ".exported.json"
    try:
        history = json.loads(history_path.read_text()) if history_path.exists() else {"version": 1, "records": {}}
        if history.get("version") != 1 or not isinstance(history.get("records"), dict):
            raise ValueError("invalid export history")
    except (ValueError, AttributeError) as exc:
        raise MiFitnessError("Local export history could not be read; no files exported.") from exc
    kwargs = {"country_code": args.country_code, "no_cache": args.no_cache}
    if args.cache_dir is not None:
        kwargs["cache_dir"] = args.cache_dir
    if auth_refresher is not None:
        kwargs["auth_refresher"] = auth_refresher
    client = MiFitnessActivitiesClient(state, **kwargs)
    try:
        activities = client.list_activities(start_time=start_time, end_time=end_time, limit=args.limit + 1)
        if len(activities) > args.limit:
            raise MiFitnessError("Scan exceeded --limit; no files exported. Increase --limit or narrow the dates.")
        runs = [activity for activity in activities if is_run(activity, outdoor_only=args.outdoor_only)]
        if selected_ids is not None:
            if not selected_ids or not selected_ids <= {activity.activity_id for activity in runs}:
                raise MiFitnessError("选择的跑步记录已变化，请刷新活动后重试。")
            runs = [activity for activity in runs if activity.activity_id in selected_ids]
        report(f"Scanned {len(activities)} workouts; {len(runs)} runs; skipped {len(activities) - len(runs)} others.")
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        smoothing = {"smooth": not args.no_smooth, "smooth_mode": args.smooth_mode}
        if args.outlier_speed:
            smoothing["outlier_speed_mps"] = parse_speed_input(args.outlier_speed)
        exported = 0
        already_exported = 0
        for activity in sorted(runs, key=lambda item: item.start_time):
            if activity.activity_id in history["records"]:
                already_exported += 1
                continue
            detail = client.get_activity_detail(activity)
            if not is_run(detail.activity, outdoor_only=args.outdoor_only) or (
                detail.activity.activity_id != activity.activity_id or detail.start_time != activity.start_time
            ):
                raise MiFitnessError(f"Activity {activity.activity_id} changed during lookup; export stopped.")
            report_hr = detail.activity.raw_report.get("avg_hrm")
            if isinstance(report_hr, (int, float)) and report_hr > 0 and not any(
                sample.heart_rate is not None and 0 < sample.heart_rate < 255
                for sample in [*detail.samples, *detail.track_points]
            ):
                raise MiFitnessError("小米云端有心率，但详细心率暂时无法读取；请重试，避免导出缺少心率的文件。")
            export = render_export(detail, "fit", **smoothing)
            digest = hashlib.sha256(activity.activity_id.encode()).hexdigest()[:16]
            date = datetime.fromtimestamp(activity.start_time, timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"run_{date}_{digest}.fit"
            output = output_dir / filename
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(export.payload)
            history["records"][activity.activity_id] = {
                "file": filename, "sport_type": activity.sport_type,
                "start_time": activity.start_time,
                "distance_meters": detail.total_distance_meters,
                "duration_seconds": detail.total_duration_seconds,
            }
            _save_history(history_path, history)
            exported += 1
            report(f"FIT for upload: {output}")
        allowed = {1} if args.outdoor_only else RUN_TYPES
        pending = 0
        for record in history["records"].values():
            if not isinstance(record, dict):
                raise MiFitnessError("Invalid local export history; check .exported.json.")
            name = record.get("file")
            if isinstance(name, str) and Path(name).name == name and record.get("sport_type") in allowed:
                pending += (output_dir / name).is_file()
        report(f"Exported {exported} runs; {already_exported} already exported; {pending} awaiting web upload.")
        report(f"Upload folder: {output_dir}")
        report("Local history tracks exported files; it does not confirm a Strava upload.")
        return ExportSummary(len(activities), len(runs), exported, already_exported, pending, output_dir)
    except (requests.RequestException, OSError) as exc:
        raise MiFitnessError(f"Export stopped: {exc}") from exc


def handle_export_runs(args: argparse.Namespace) -> int:
    export_runs(args)
    return 0
