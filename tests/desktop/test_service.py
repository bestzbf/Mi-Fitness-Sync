from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mi_fitness_sync.cli import export_runs
from mi_fitness_sync.desktop import service as desktop
from mi_fitness_sync.desktop.duplicates import parse_strava_date, read_strava_csv
from mi_fitness_sync.exceptions import MiFitnessError, StravaError


@pytest.fixture
def scenario(monkeypatch, tmp_path, auth_state, sample_activity_detail):
    legacy = tmp_path / "legacy"
    (legacy / ".local").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=10)).isoformat()
    (legacy / ".local" / "free-sync-settings.json").write_text(json.dumps({"since": cutoff}))
    app = desktop.DesktopService(tmp_path / "support")
    app.initialize(legacy)
    start = int((now - timedelta(hours=1)).timestamp())
    run = replace(sample_activity_detail.activity, activity_id="run:one:1", start_time=start, end_time=start + 3600)
    second = replace(run, activity_id="run:two:2", start_time=start - 3600, end_time=start)
    ride = replace(run, activity_id="ride:same:3", sport_type=6)
    detail = replace(sample_activity_detail, activity=run)
    data = SimpleNamespace(app=app, activities=[run, second, ride], reads=[], uploads=[], run=run, second=second, ride=ride, cutoff=cutoff)
    monkeypatch.setattr(desktop, "load_state", lambda: auth_state)
    monkeypatch.setattr(desktop, "load_tokens", lambda: None)
    monkeypatch.setattr(export_runs, "load_state", lambda _: auth_state)

    class Client:
        def __init__(self, state, **kwargs):
            assert kwargs["country_code"] == "CN"

        def list_activities(self, **kwargs):
            return [item for item in data.activities if kwargs["start_time"] <= item.start_time <= kwargs["end_time"]]

        def get_activity_by_id(self, activity_id):
            return next(item for item in data.activities if item.activity_id == activity_id)

        def get_activity_detail(self, activity):
            data.reads.append(activity.activity_id)
            return replace(detail, activity=activity)

    monkeypatch.setattr(desktop, "MiFitnessActivitiesClient", Client)
    monkeypatch.setattr(export_runs, "MiFitnessActivitiesClient", Client)
    app.refresh(30, lambda _: None)
    return data


def test_upgrade_keeps_original_cutoff_and_uses_original_output_folder(scenario, tmp_path):
    app = scenario.app
    assert app.settings()["since"] == scenario.cutoff
    assert app.settings()["output_dir"] == str(tmp_path / "legacy" / "待上传跑步")
    assert app.initialize()["since"] == scenario.cutoff
    assert app.settings_path.stat().st_mode & 0o777 == 0o600


def test_refresh_reads_list_without_exporting_or_downloading_details(scenario):
    current = scenario.app.status()
    assert len(current["activities"]) == 3
    ride = next(row for row in current["activities"] if row["id"] == scenario.ride.activity_id)
    assert ride["ride"] and not ride["selectable"]
    assert scenario.reads == []
    assert not list(scenario.app.support_dir.parent.rglob("*.fit"))


def test_only_selected_run_is_exported_and_retries_reuse_exact_fit(scenario):
    ids = [scenario.run.activity_id]
    first = scenario.app.synchronize(ids, lambda _: None)
    fit = next(Path(first["batch_dir"]).glob("*.fit"))
    original = fit.read_bytes()
    assert scenario.reads == ids
    assert first["batch_ids"] == ids
    assert len(first["pending"]) == 1
    assert all(row["state"] != "已确认上传" for row in first["activities"])
    second = scenario.app.synchronize(ids, lambda _: None)
    assert scenario.reads == ids
    assert next(Path(second["batch_dir"]).glob("*.fit")).read_bytes() == original


def test_riding_and_pre_configuration_runs_are_rejected(scenario):
    app = scenario.app
    old = replace(scenario.run, activity_id="run:old:4", start_time=int(datetime.fromisoformat(scenario.cutoff).timestamp()) - 60)
    scenario.activities.append(old)
    app.refresh(30, lambda _: None)
    for activity_id in (scenario.ride.activity_id, old.activity_id):
        with pytest.raises(MiFitnessError):
            app.synchronize([activity_id], lambda _: None)
    assert scenario.reads == []
    assert app.status()["pending"] == []


def test_user_confirmation_archives_file_and_blocks_future_duplicates(scenario):
    app = scenario.app
    ids = [scenario.run.activity_id]
    prepared = app.synchronize(ids, lambda _: None)
    name = prepared["pending"][0]["file"]
    completed = app.archive(ids)
    assert completed["pending"] == []
    assert (Path(completed["output_dir"]) / "已上传" / name).is_file()
    history = json.loads((app.support_dir / "sync-history.json").read_text())
    assert history["records"][ids[0]]["source"] == "user_confirmed_web"
    with pytest.raises(MiFitnessError):
        app.synchronize(ids, lambda _: None)
    scenario.activities.append(replace(scenario.run, activity_id="run:same-time:5", start_time=scenario.run.start_time + 3))
    app.refresh(30, lambda _: None)
    duplicate = next(row for row in app.status()["activities"] if row["id"] == "run:same-time:5")
    assert not duplicate["selectable"]
    assert duplicate["state"] == "已确认上传"


def test_imported_strava_runs_match_utc_time_but_rides_do_not(scenario, tmp_path):
    path = tmp_path / "activities.csv"
    first = datetime.fromtimestamp(scenario.run.start_time + 4, timezone.utc).isoformat()
    second = datetime.fromtimestamp(scenario.second.start_time, timezone.utc).isoformat()
    path.write_text(f"Activity ID,Activity Date,Activity Type\n1,{first},Run\n2,{second},Ride\n")
    current = scenario.app.import_strava(path, 0)
    rows = {row["id"]: row for row in current["activities"]}
    assert rows[scenario.run.activity_id]["state"] == "Strava 已存在"
    assert not rows[scenario.run.activity_id]["selectable"]
    assert rows[scenario.second.activity_id]["selectable"]
    with pytest.raises(MiFitnessError):
        scenario.app.synchronize([scenario.run.activity_id], lambda _: None)


def test_failed_csv_import_keeps_existing_duplicate_index(scenario, tmp_path):
    index = scenario.app.support_dir / "strava-index.json"
    original = {"records": [{"start_time": scenario.run.start_time}], "imported_at": "2026-10-08"}
    desktop.write_private_json(index, original)
    path = tmp_path / "invalid.csv"
    path.write_text("Activity Type,Activity Date\nRun,not a date\n")
    with pytest.raises(MiFitnessError):
        scenario.app.import_strava(path, 0)
    assert json.loads(index.read_text()) == original


def test_csv_date_parsing_does_not_depend_on_system_locale(tmp_path):
    path = tmp_path / "activities.csv"
    path.write_text('Activity ID,Activity Date,Activity Type\n1,"Oct 8, 2026, 7:33:49 AM",Run\n2,"Oct 8, 2026, 7:33:49 AM",Ride\n')
    records = read_strava_csv(path)
    assert len(records) == 1
    assert records[0]["start_time"] == parse_strava_date("2026-10-08T15:33:49+08:00")
    assert parse_strava_date("2026-10-08 15:33:49", naive_timezone=timezone(timedelta(hours=8))) == records[0]["start_time"]


def test_missing_or_corrupt_sync_history_blocks_new_exports(scenario):
    path = scenario.app.support_dir / "sync-history.json"
    path.write_text("not json")
    with pytest.raises(MiFitnessError):
        scenario.app.synchronize([scenario.run.activity_id], lambda _: None)
    assert scenario.reads == []
    assert path.read_text() == "not json"


def test_duplicate_check_failure_stops_api_upload_and_retains_pending_fit(scenario, monkeypatch):
    settings = scenario.app.settings()
    settings["mode"] = "api"
    desktop.write_private_json(scenario.app.settings_path, settings)
    monkeypatch.setattr(desktop, "load_tokens", lambda: object())
    monkeypatch.setattr(desktop, "StravaClient", lambda _: object())

    def fail(*args):
        raise StravaError("cannot read remote records")

    monkeypatch.setattr(desktop, "fetch_existing_run_starts", fail)
    with pytest.raises(StravaError):
        scenario.app.synchronize([scenario.run.activity_id], lambda _: None)
    assert len(scenario.app.status()["pending"]) == 1
    assert not (scenario.app.support_dir / "sync-history.json").exists()


def test_api_rechecks_remote_and_uploads_only_missing_selected_run(scenario, monkeypatch):
    settings = scenario.app.settings()
    settings["mode"] = "api"
    desktop.write_private_json(scenario.app.settings_path, settings)
    monkeypatch.setattr(desktop, "load_tokens", lambda: object())

    class Strava:
        def __init__(self, _):
            pass

        def upload_activity(self, fit, **kwargs):
            scenario.uploads.append(kwargs)
            assert fit
            return {"activity_id": 123}

    monkeypatch.setattr(desktop, "StravaClient", Strava)
    monkeypatch.setattr(desktop, "fetch_existing_run_starts", lambda *args: {scenario.run.start_time + 4})
    result = scenario.app.synchronize([scenario.run.activity_id, scenario.second.activity_id], lambda _: None)
    assert [item["external_id"] for item in scenario.uploads] == [scenario.second.activity_id]
    assert result["pending"] == []
    history = json.loads((scenario.app.support_dir / "sync-history.json").read_text())["records"]
    assert history[scenario.run.activity_id]["source"] == "strava_api_existing"
    assert history[scenario.second.activity_id]["strava_id"] == 123


def test_existing_tokens_do_not_change_explicit_free_mode(scenario, monkeypatch):
    monkeypatch.setattr(desktop, "load_tokens", lambda: object())
    assert not scenario.app.status()["api_connected"]
    result = scenario.app.synchronize([scenario.run.activity_id], lambda _: None)
    assert "batch_dir" in result


def test_repeated_cloud_records_cannot_be_selected_in_one_batch(scenario):
    duplicate = replace(scenario.run, activity_id="run:duplicate:9", start_time=scenario.run.start_time + 2)
    scenario.activities.append(duplicate)
    result = scenario.app.refresh(30, lambda _: None)
    rows = [row for row in result["activities"] if row["id"] in {scenario.run.activity_id, duplicate.activity_id}]
    assert sum(row["selectable"] for row in rows) == 1
    with pytest.raises(MiFitnessError):
        scenario.app.synchronize([row["id"] for row in rows], lambda _: None)
    assert scenario.reads == []


def write_web_receipt(prepared, activity_id, *, strava_id=12345, result="uploaded", filename=None):
    batch = Path(prepared["batch_dir"])
    item = next(row for row in prepared["batch_items"] if row["id"] == activity_id)
    desktop.write_private_json(batch / "strava-confirmed.json", {
        "version": 1, "batch": batch.name,
        "records": [{"id": activity_id, "file": filename or item["file"], "strava_id": strava_id, "result": result}],
    })
    return batch


def test_web_success_confirms_only_verified_item_and_is_idempotent(scenario):
    ids = [scenario.run.activity_id, scenario.second.activity_id]
    prepared = scenario.app.synchronize(ids, lambda _: None)
    batch = write_web_receipt(prepared, ids[0])
    completed = scenario.app.complete_web(batch, [ids[0]])
    assert [row["id"] for row in completed["pending"]] == [ids[1]]
    history_path = scenario.app.support_dir / "sync-history.json"
    history_bytes = history_path.read_bytes()
    record = json.loads(history_bytes)["records"][ids[0]]
    assert record["strava_id"] == 12345 and record["source"] == "strava_web"
    original = next(row for row in prepared["batch_items"] if row["id"] == ids[0])["file"]
    assert (Path(completed["output_dir"]) / "已上传" / original).read_bytes() == (batch / original).read_bytes()
    scenario.app.complete_web(batch, [ids[0]])
    assert history_path.read_bytes() == history_bytes
    assert next(row for row in completed["activities"] if row["id"] == ids[1])["selectable"]


def test_web_duplicate_keeps_existing_strava_id_without_creating_another_upload(scenario):
    activity_id = scenario.run.activity_id
    prepared = scenario.app.synchronize([activity_id], lambda _: None)
    batch = write_web_receipt(prepared, activity_id, strava_id=54321, result="duplicate")
    result = scenario.app.complete_web(batch, [activity_id])
    assert result["pending"] == []
    history = json.loads((scenario.app.support_dir / "sync-history.json").read_text())["records"]
    assert history[activity_id]["source"] == "strava_web_existing"
    assert history[activity_id]["strava_id"] == 54321


@pytest.mark.parametrize("strava_id", [None, 0, -1, True, "12345"])
def test_web_unverified_results_do_not_mark_files_uploaded(scenario, strava_id):
    activity_id = scenario.run.activity_id
    prepared = scenario.app.synchronize([activity_id], lambda _: None)
    batch = write_web_receipt(prepared, activity_id, strava_id=strava_id)
    with pytest.raises(MiFitnessError, match="尚未收到"):
        scenario.app.complete_web(batch, [activity_id])
    assert not (scenario.app.support_dir / "sync-history.json").exists()
    assert len(scenario.app.status()["pending"]) == 1


def test_web_missing_wrong_or_outside_batch_receipts_do_not_confirm(scenario, tmp_path):
    activity_id = scenario.run.activity_id
    prepared = scenario.app.synchronize([activity_id], lambda _: None)
    batch = Path(prepared["batch_dir"])
    with pytest.raises(MiFitnessError):
        scenario.app.complete_web(batch, [activity_id])
    write_web_receipt(prepared, activity_id, filename="another.fit")
    with pytest.raises(MiFitnessError):
        scenario.app.complete_web(batch, [activity_id])
    with pytest.raises(MiFitnessError, match="路径无效"):
        scenario.app.complete_web(tmp_path / batch.name, [activity_id])
    assert len(scenario.app.status()["pending"]) == 1


def test_web_receipt_recovers_after_application_stops_before_local_confirmation(scenario):
    activity_id = scenario.run.activity_id
    prepared = scenario.app.synchronize([activity_id], lambda _: None)
    write_web_receipt(prepared, activity_id)
    # A new service instance represents restarting the app after receipt fsync/rename.
    restarted = desktop.DesktopService(scenario.app.support_dir)
    restarted.recover_web()
    assert restarted.status()["pending"] == []
    restarted.recover_web()
    with pytest.raises(MiFitnessError):
        restarted.synchronize([activity_id], lambda _: None)
