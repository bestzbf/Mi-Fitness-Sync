from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mi_fitness_sync.cli.app import main
from mi_fitness_sync.cli import export_runs


@pytest.fixture
def export_scenario(monkeypatch, tmp_path, auth_state, sample_activity_detail):
    data = SimpleNamespace(activities=[], reads=[], overrides={}, scan={})
    monkeypatch.setattr(export_runs, "load_state", lambda path: auth_state)

    class Client:
        def __init__(self, state, **kwargs):
            assert kwargs["country_code"] == "CN"

        def list_activities(self, **kwargs):
            data.scan.update(kwargs)
            return data.activities

        def get_activity_detail(self, activity):
            data.reads.append(activity.activity_id)
            return data.overrides.get(activity.activity_id, replace(sample_activity_detail, activity=activity))

    monkeypatch.setattr(export_runs, "MiFitnessActivitiesClient", Client)
    data.command = ["export-runs", "--output-dir", str(tmp_path)]
    return data


def test_mixed_workouts_export_only_running_fits(export_scenario, sample_activity_detail, tmp_path):
    data = export_scenario
    data.activities = [
        replace(sample_activity_detail.activity, activity_id=f"sid:key:{index}", sport_type=sport_type)
        for index, sport_type in enumerate([1, 6, 3, 7, 5, 324, None])
    ]
    assert main(data.command) == 0
    assert data.reads == ["sid:key:0", "sid:key:2", "sid:key:4"]
    assert len(list(tmp_path.glob("*.fit"))) == 3
    records = json.loads((tmp_path / ".exported.json").read_text())["records"]
    assert {value["sport_type"] for value in records.values()} == {1, 3, 5}


def test_repeated_run_keeps_pending_files_without_fetching_details_again(
    export_scenario, sample_activity_detail, tmp_path, capsys,
):
    data = export_scenario
    data.activities = [sample_activity_detail.activity]
    assert main(data.command) == 0
    files = {path.name: path.read_bytes() for path in tmp_path.glob("*.fit")}
    data.reads.clear()
    assert main(data.command) == 0
    assert data.reads == []
    assert files == {path.name: path.read_bytes() for path in tmp_path.glob("*.fit")}
    assert "0 runs; 1 already exported; 1 awaiting web upload" in capsys.readouterr().out


def test_archived_file_is_not_reexported_or_shown_as_pending(export_scenario, sample_activity_detail, tmp_path, capsys):
    data = export_scenario
    data.activities = [sample_activity_detail.activity]
    assert main(data.command) == 0
    fit = next(tmp_path.glob("*.fit"))
    archive = tmp_path / "已上传"
    archive.mkdir()
    fit.rename(archive / fit.name)
    data.reads.clear()
    assert main(data.command) == 0
    assert data.reads == []
    assert "0 awaiting web upload" in capsys.readouterr().out


def test_ride_returned_for_running_detail_is_never_exported(export_scenario, sample_activity_detail, tmp_path):
    data = export_scenario
    activity = sample_activity_detail.activity
    data.activities = [activity]
    data.overrides[activity.activity_id] = replace(sample_activity_detail, activity=replace(activity, sport_type=6))
    assert main(data.command) == 1
    assert not list(tmp_path.glob("*.fit"))
    assert not (tmp_path / ".exported.json").exists()


def test_no_runs_is_successful_setup_without_files(export_scenario, sample_activity_detail, tmp_path):
    data = export_scenario
    data.activities = [replace(sample_activity_detail.activity, sport_type=6)]
    assert main(data.command) == 0
    assert data.reads == []
    assert not list(tmp_path.glob("*.fit"))


def test_outdoor_only_excludes_treadmill_and_trail(export_scenario, sample_activity_detail, tmp_path):
    data = export_scenario
    data.activities = [
        replace(sample_activity_detail.activity, activity_id=str(sport_type), sport_type=sport_type)
        for sport_type in [1, 3, 5]
    ]
    assert main([*data.command, "--outdoor-only"]) == 0
    assert data.reads == ["1"]


def test_corrupt_export_history_is_not_overwritten(export_scenario, tmp_path):
    history = tmp_path / ".exported.json"
    history.write_text("invalid history")
    assert main(export_scenario.command) == 1
    assert history.read_text() == "invalid history"
    assert export_scenario.scan == {}


def test_scan_limit_stops_export_before_writing_any_files(export_scenario, sample_activity_detail, tmp_path):
    export_scenario.activities = [sample_activity_detail.activity] * 2
    assert main([*export_scenario.command, "--limit", "1"]) == 1
    assert export_scenario.reads == []
    assert not list(tmp_path.glob("*.fit"))


def test_known_heart_rate_cannot_silently_export_without_sensor_samples(export_scenario, sample_activity_detail, tmp_path):
    activity = replace(sample_activity_detail.activity, raw_report={"avg_hrm": 162})
    export_scenario.activities = [activity]
    export_scenario.overrides[activity.activity_id] = replace(
        sample_activity_detail, activity=activity, samples=[],
        track_points=[replace(point, heart_rate=None) for point in sample_activity_detail.track_points],
    )
    assert main(export_scenario.command) == 1
    assert not list(tmp_path.glob("*.fit"))
    assert not (tmp_path / ".exported.json").exists()
