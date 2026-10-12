from dataclasses import replace

from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage

from mi_fitness_sync.export.render import render_export


def test_fit_keeps_real_heart_rate_before_gps_starts_and_during_gps_gaps(sample_activity_detail):
    original = sample_activity_detail
    start = original.start_time
    activity = replace(original.activity, end_time=start + 2, duration_seconds=2, distance_meters=5)
    gps = replace(original.track_points[0], timestamp=start + 1, heart_rate=None)
    samples = [replace(original.samples[0], timestamp=start + i, heart_rate=hr) for i, hr in enumerate([130, 140, 150])]
    detail = replace(original, activity=activity, track_points=[gps], samples=samples)
    fit = FitFile.from_bytes(render_export(detail, "fit", smooth=False).payload)
    records = [row.message for row in fit.records if isinstance(row.message, RecordMessage)]
    session = next(row.message for row in fit.records if isinstance(row.message, SessionMessage))
    assert [record.heart_rate for record in records] == [130, 140, 150]
    assert records[0].position_lat is None
    assert records[1].position_lat is not None
    assert records[2].position_lat is None
    assert session.start_position_lat is not None
    assert gps.heart_rate is None
