from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from mi_fitness_sync.cli.sync_runs import MATCH_WINDOW_SECONDS
from mi_fitness_sync.exceptions import MiFitnessError


RUN_NAMES = frozenset({"run", "trailrun", "virtualrun", "running", "trail run", "virtual run", "跑步", "越野跑", "虚拟跑步"})
MONTHS = {name: month for month, name in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def parse_strava_date(value: str, *, naive_timezone=timezone.utc) -> int:
    """CSV timestamps without offsets use the timezone selected in the import UI."""
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        match = re.fullmatch(r"([A-Za-z]{3}) (\d{1,2}), (\d{4}),? (\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)", value, re.IGNORECASE)
        if not match:
            raise ValueError("unrecognized date")
        month, day, year, hour, minute, second, meridiem = match.groups()
        hour = int(hour)
        if hour not in range(1, 13):
            raise ValueError("invalid hour")
        parsed = datetime(int(year), MONTHS[month.title()], int(day), hour % 12 + (12 if meridiem.upper() == "PM" else 0), int(minute), int(second))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return int(parsed.timestamp())


def read_strava_csv(path: Path, *, naive_timezone=timezone.utc) -> list[dict]:
    """Ignore all rides; fail an import rather than silently miss a malformed run."""
    if path.stat().st_size > 50 * 1024 * 1024:
        raise MiFitnessError("导入文件过大，请选择 Strava 导出的活动 CSV。")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        type_column = next((name for name in ("Activity Type", "sport_type", "type", "运动类型") if name in headers), None)
        date_column = next((name for name in ("start_date", "Activity Date", "开始时间") if name in headers), None)
        if not type_column or not date_column:
            raise MiFitnessError("CSV 缺少活动类型或开始时间，请选择 Strava 的 activities.csv。")
        records = []
        for line, row in enumerate(reader, 2):
            if (row.get(type_column) or "").strip().lower() not in RUN_NAMES:
                continue
            try:
                start = parse_strava_date(row[date_column], naive_timezone=naive_timezone)
            except (ValueError, TypeError, KeyError) as exc:
                raise MiFitnessError(f"CSV 第 {line} 行的跑步时间无法读取，未替换原查重记录。") from exc
            records.append({"start_time": start, "strava_id": row.get("Activity ID", row.get("id"))})
    return records


def matches_run(start_time: int | None, records: list[dict]) -> bool:
    return type(start_time) is int and any(
        type(record.get("start_time")) is int and abs(start_time - record["start_time"]) <= MATCH_WINDOW_SECONDS
        for record in records
    )
