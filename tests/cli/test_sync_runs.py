from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from mi_fitness_sync.cli.app import main
from mi_fitness_sync.cli import sync_runs
from mi_fitness_sync.exceptions import StravaError


@pytest.fixture
def scenario(monkeypatch, tmp_path, auth_state, sample_activity_detail):
    scenario = SimpleNamespace(
        activities=[], tokens=None, pages=[[]], error=None,
        config={}, scan={}, reads=[], queries=[], uploads=[], detail_overrides={},
    )
    monkeypatch.setattr(sync_runs, "load_state", lambda path: auth_state)
    monkeypatch.setattr(sync_runs, "load_tokens", lambda path: scenario.tokens)
    monkeypatch.setattr(sync_runs, "get_exports_dir", lambda: tmp_path)

    class MiClient:
        def __init__(self, state, **kwargs):
            scenario.config.update(kwargs)

        def list_activities(self, **kwargs):
            scenario.scan.update(kwargs)
            return scenario.activities

        def get_activity_detail(self, activity):
            scenario.reads.append(activity.activity_id)
            return scenario.detail_overrides.get(
                activity.activity_id, replace(sample_activity_detail, activity=activity)
            )

    class StravaClient:
        def __init__(self, tokens, **kwargs):
            pass

        def list_activities(self, **kwargs):
            scenario.queries.append(kwargs)
            if scenario.error:
                raise scenario.error
            return scenario.pages[kwargs["page"] - 1]

        def upload_activity(self, payload, **kwargs):
            scenario.uploads.append((payload, kwargs))
            return {"activity_id": 100 + len(scenario.uploads)}

    monkeypatch.setattr(sync_runs, "MiFitnessActivitiesClient", MiClient)
    monkeypatch.setattr(sync_runs, "StravaClient", StravaClient)
    return scenario


def make_activity(detail, sport_type=1, offset=0, **kwargs):
    return replace(
        detail.activity, activity_id=f"sid:key:{detail.start_time + offset}",
        sport_type=sport_type, start_time=detail.start_time + offset, **kwargs,
    )


def strava_activity(activity, sport_type="Run", offset=0):
    return {
        "sport_type": sport_type,
        "start_date": datetime.fromtimestamp(
            activity.start_time + offset, tz=timezone.utc
        ).isoformat(),
    }


@pytest.mark.parametrize("sport_type, expected", [
    (1, True), (3, True), (5, True), (6, False), (7, False), (324, False),
    (2, False), (17, False), (999, False), (None, False), ("1", False), (True, False),
])
def test_only_recognized_run_types_pass(sample_activity_detail, sport_type, expected):
    # Misleading titles/categories must never cause cycling/unknown types to upload.
    activity = make_activity(sample_activity_detail, sport_type, title="Morning Run", category="outdoor_run")
    assert sync_runs.is_run(activity) is expected


@pytest.mark.parametrize("sport_type, expected", [(1, True), (3, False), (5, False)])
def test_outdoor_only(sample_activity_detail, sport_type, expected):
    assert sync_runs.is_run(make_activity(sample_activity_detail, sport_type), outdoor_only=True) is expected


def test_deleted_and_undated_runs_are_not_candidates(sample_activity_detail):
    assert not sync_runs.is_run(replace(sample_activity_detail.activity, sync_state="deleted"))
    assert not sync_runs.is_run(replace(sample_activity_detail.activity, start_time=None))


def test_preview_never_fetches_detail_exports_or_uploads(scenario, sample_activity_detail, tmp_path, capsys):
    scenario.activities = [make_activity(sample_activity_detail, 6), make_activity(sample_activity_detail)]
    assert main(["sync-runs"]) == 0
    assert scenario.config["country_code"] == "CN"
    assert scenario.reads == scenario.uploads == scenario.queries == []
    assert not list(tmp_path.glob("*.fit"))
    assert "1 runs; skipped 1 others" in capsys.readouterr().out
    assert 29 * 86400 < scenario.scan["end_time"] - scenario.scan["start_time"] <= 30 * 86400


def test_mixed_workouts_upload_only_runs_and_leave_fit_backups(scenario, sample_activity_detail, tmp_path):
    scenario.tokens = object()
    scenario.activities = [
        make_activity(sample_activity_detail, sport_type, index * 600)
        for index, sport_type in enumerate([6, 1, 7, 3, 324, 5, None])
    ]
    assert main(["sync-runs", "--upload"]) == 0
    allowed_ids = [activity.activity_id for activity in scenario.activities if activity.sport_type in {1, 3, 5}]
    assert scenario.reads == allowed_ids
    assert [kwargs["external_id"] for payload, kwargs in scenario.uploads] == allowed_ids
    assert [kwargs["sport_type"] for payload, kwargs in scenario.uploads] == ["Run", "Run", "TrailRun"]
    assert all(payload for payload, kwargs in scenario.uploads)
    assert len(list(tmp_path.glob("*.fit"))) == 3


def test_ride_at_same_time_does_not_suppress_run(scenario, sample_activity_detail):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity]
    scenario.pages = [[strava_activity(activity, "Ride")]]
    assert main(["sync-runs", "--upload"]) == 0
    assert len(scenario.uploads) == 1


@pytest.mark.parametrize("sport_type", ["Run", "TrailRun", "VirtualRun"])
def test_existing_run_is_skipped_before_detail_fetch(scenario, sample_activity_detail, sport_type):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity]
    scenario.pages = [[strava_activity(activity, sport_type, offset=6)]]
    assert main(["sync-runs", "--upload"]) == 0
    assert scenario.reads == scenario.uploads == []


def test_strava_duplicates_on_second_page_are_found(scenario, sample_activity_detail):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity]
    scenario.pages = [[strava_activity(activity, "Ride")] * 200, [strava_activity(activity)]]
    assert main(["sync-runs", "--upload"]) == 0
    assert [query["page"] for query in scenario.queries] == [1, 2]
    assert scenario.uploads == []


def test_duplicate_time_with_china_offset_matches_utc(scenario, sample_activity_detail):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity]
    scenario.pages = [[{"type": "Run", "start_date": "2024-06-01T08:00:00+08:00"}]]
    assert main(["sync-runs", "--upload"]) == 0
    assert scenario.uploads == []


def test_distinct_run_outside_duplicate_window_can_upload(scenario, sample_activity_detail):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity]
    scenario.pages = [[strava_activity(activity, offset=7)]]
    assert main(["sync-runs", "--upload"]) == 0
    assert len(scenario.uploads) == 1


@pytest.mark.parametrize("error", [StravaError("rate limit"), requests.ConnectionError("offline")])
def test_duplicate_check_failure_stops_all_uploads(scenario, sample_activity_detail, error, capsys):
    scenario.tokens = object()
    scenario.activities = [make_activity(sample_activity_detail)]
    scenario.error = error
    assert main(["sync-runs", "--upload"]) == 1
    assert scenario.reads == scenario.uploads == []
    assert str(error) in capsys.readouterr().err


@pytest.mark.parametrize("start_date", [None, "invalid", "2026-10-01T08:00:00"])
def test_invalid_existing_run_time_stops_uploads(scenario, sample_activity_detail, start_date):
    scenario.tokens = object()
    scenario.activities = [make_activity(sample_activity_detail)]
    scenario.pages = [[{"sport_type": "Run", "start_date": start_date}]]
    assert main(["sync-runs", "--upload"]) == 1
    assert scenario.uploads == []


def test_missing_strava_login_stops_before_network_scan(scenario, sample_activity_detail):
    scenario.activities = [make_activity(sample_activity_detail)]
    assert main(["sync-runs", "--upload"]) == 1
    assert scenario.scan == {}
    assert scenario.uploads == []


def test_scan_limit_cannot_hide_older_runs_during_upload(scenario, sample_activity_detail):
    scenario.tokens = object()
    scenario.activities = [make_activity(sample_activity_detail, 6), make_activity(sample_activity_detail)]
    assert main(["sync-runs", "--limit", "1", "--upload"]) == 1
    assert scenario.scan["limit"] == 2
    assert scenario.queries == scenario.reads == scenario.uploads == []


def test_success_is_remembered_within_same_batch(scenario, sample_activity_detail):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity, replace(activity, activity_id="other:key:1717200000")]
    assert main(["sync-runs", "--upload"]) == 0
    assert len(scenario.uploads) == 1


def test_type_changed_to_ride_during_detail_lookup_stops_upload(scenario, sample_activity_detail):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity]
    scenario.detail_overrides[activity.activity_id] = replace(
        sample_activity_detail, activity=replace(activity, sport_type=6)
    )
    assert main(["sync-runs", "--upload"]) == 1
    assert scenario.uploads == []


def test_other_run_returned_during_detail_lookup_stops_upload(scenario, sample_activity_detail):
    scenario.tokens = object()
    activity = make_activity(sample_activity_detail)
    scenario.activities = [activity]
    scenario.detail_overrides[activity.activity_id] = replace(
        sample_activity_detail, activity=make_activity(sample_activity_detail, offset=600)
    )
    assert main(["sync-runs", "--upload"]) == 1
    assert scenario.uploads == []


def test_outdoor_only_upload_excludes_treadmill_and_trail(scenario, sample_activity_detail):
    scenario.tokens = object()
    scenario.activities = [
        make_activity(sample_activity_detail, sport_type, index * 600)
        for index, sport_type in enumerate([1, 3, 5])
    ]
    assert main(["sync-runs", "--outdoor-only", "--upload"]) == 0
    assert len(scenario.uploads) == 1


@pytest.mark.parametrize("arguments", [
    ["--limit", "0"], ["--since", "invalid"],
    ["--since", "2026-10-08", "--until", "2026-10-01"],
])
def test_invalid_input_does_not_contact_services(scenario, arguments):
    assert main(["sync-runs", *arguments]) == 1
    assert scenario.scan == {}
    assert scenario.queries == scenario.uploads == []
