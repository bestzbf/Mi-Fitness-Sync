from __future__ import annotations

import struct

import pytest

from mi_fitness_sync.fds.sport_records import parse_sport_record
from tests.fds.support import build_header


@pytest.mark.parametrize("version", [8, 9])
def test_outdoor_step_keeps_heart_rate_across_distance_format_change(version):
    modern = version == 9
    rows = []
    for hr in (150, 155):
        row = bytes([0x20, hr])
        row += struct.pack("<IH", 100, 0x8002) if modern else bytes([0x80, 8])
        row += bytes([125]) + struct.pack("<I", 1 << 26) + bytes([25, 180])
        row += struct.pack("<HHHHH", 300, 301, 190, 201, 202)
        assert len(row) == (25 if modern else 21)
        rows.append(row)
    header = build_header(1717200000, 32, version, 22, b"\xff" * 6 + b"\xf0")
    body = bytes(4) + struct.pack("<II", 2, 1717200000) + bytes(15) + b"".join(rows)
    samples = parse_sport_record(header + body, 22)
    assert len(samples) == 2
    assert [sample.heart_rate for sample in samples] == [150, 155]
    assert [sample.timestamp for sample in samples] == [1717200000, 1717200001]
    assert [sample.cadence for sample in samples] == [180, 180]
    assert [sample.running_power for sample in samples] == [190, 190]


@pytest.mark.parametrize("version", [5, 6])
def test_outdoor_no_step_keeps_heart_rate_across_distance_format_change(version):
    rows = []
    for hr in (140, 145):
        row = bytes([0x10, hr])
        row += struct.pack("<IH", 100, 0x8002) if version == 6 else bytes([0x80, 8])
        row += struct.pack("<H", 500) + bytes([90])
        rows.append(row)
    header = build_header(1717200000, 32, version, 23, b"\xff\xff\xff")
    body = bytes(4) + struct.pack("<II", 2, 1717200000) + bytes(11) + b"".join(rows)
    samples = parse_sport_record(header + body, 23)
    assert len(samples) == 2
    assert [sample.heart_rate for sample in samples] == [140, 145]
    assert [sample.cadence for sample in samples] == [90, 90]
