from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import uuid
import io
from contextlib import contextmanager
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from mi_fitness_sync.activity.client import MiFitnessActivitiesClient
from mi_fitness_sync.auth.client import MiFitnessAuthClient
from mi_fitness_sync.auth.qr import begin_qr_login, download_qr_image, wait_for_qr_login
from mi_fitness_sync.auth.refresh import refresh_saved_state
from mi_fitness_sync.auth.store import load_state, save_state
from mi_fitness_sync.cli.export_runs import export_runs
from mi_fitness_sync.cli.app import handle_strava_login
from mi_fitness_sync.cli.sync_runs import RUN_TYPES, fetch_existing_run_starts, is_run
from mi_fitness_sync.desktop.duplicates import matches_run, read_strava_csv
from mi_fitness_sync.exceptions import AuthStateNotFoundError, MiFitnessError
from mi_fitness_sync.strava.client import StravaClient
from mi_fitness_sync.strava.sport_mapping import strava_sport_type
from mi_fitness_sync.strava.store import load_tokens


APP_NAME = "小米跑步同步"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "XiaomiRunSync"
FIT_NAME = re.compile(r"run_(\d{8}_\d{6})_[0-9a-f]{16}\.fit\Z")
SPORT_NAMES = {1: "户外跑步", 3: "跑步机", 5: "越野跑"}
RIDE_TYPES = frozenset({6, 7, 324})


def write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


class DesktopService:
    def __init__(self, support_dir: Path = SUPPORT_DIR):
        self.support_dir = support_dir
        self.settings_path = support_dir / "settings.json"

    def initialize(self, legacy_root: Path | None = None) -> dict:
        """Keep the original cutoff and export history when upgrading this Mac."""
        self.support_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.settings_path.exists():
            return self.settings()
        legacy = legacy_root / ".local" if legacy_root else None
        settings = {"version": 1, "mode": "web", "since": datetime.now(timezone.utc).isoformat()}
        output_dir = Path.home() / "Documents" / APP_NAME / "待上传跑步"
        if legacy and (legacy / "free-sync-settings.json").exists():
            old = json.loads((legacy / "free-sync-settings.json").read_text(encoding="utf-8"))
            settings["since"] = old["since"]
            output_dir = legacy_root / "待上传跑步"
        self._validate_since(settings["since"])
        settings["output_dir"] = str(output_dir.resolve())
        # The QR flow reuses the same device ID; auth stays in ~/.mi_fitness_sync.
        if legacy and (legacy / "xiaomi-device-id").exists():
            settings["device_id"] = (legacy / "xiaomi-device-id").read_text().strip()
        write_private_json(self.settings_path, settings)
        return settings

    @staticmethod
    def _validate_since(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise MiFitnessError("同步起始时间缺少时区，请保留原设置文件。")
        return parsed

    def settings(self) -> dict:
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if value.get("version") != 1 or not isinstance(value.get("output_dir"), str):
                raise ValueError("invalid settings")
            self._validate_since(value["since"])
            if not Path(value["output_dir"]).is_absolute():
                raise ValueError("invalid output folder")
            return value
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise MiFitnessError("无法读取同步设置。请保留设置文件，避免重新处理历史跑步。") from exc

    @contextmanager
    def operation(self) -> Iterator[None]:
        fd = os.open(self.support_dir / "operation.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MiFitnessError("另一个窗口正在检查或登录，请稍后再试。") from exc
            yield

    def _records(self, output_dir: Path) -> dict:
        path = output_dir / ".exported.json"
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("version") != 1 or not isinstance(value.get("records"), dict):
                raise ValueError("invalid history")
            for record in value["records"].values():
                if not isinstance(record, dict):
                    raise ValueError("invalid record")
            return value["records"]
        except (ValueError, AttributeError) as exc:
            raise MiFitnessError("无法读取导出历史，请保留 .exported.json 文件。") from exc

    def _read_local(self, filename: str, default: dict) -> dict:
        path = self.support_dir / filename
        if not path.exists():
            return default
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid local data")
            for key, expected in default.items():
                if key not in value or not isinstance(value[key], type(expected)):
                    raise ValueError("invalid local data")
            return value
        except (ValueError, AttributeError) as exc:
            raise MiFitnessError("无法读取本地活动或查重记录，请保留原文件。") from exc

    def _api_tokens(self):
        return load_tokens() if self.settings().get("mode", "web") == "api" else None

    @staticmethod
    def _auth_refresher(emit: Callable[[dict], None]):
        def renew(state):
            emit({"event": "progress", "message": "小米会话已失效，正在使用已保存的登录凭证自动续期…"})
            renewed = refresh_saved_state(state)
            emit({"event": "progress", "message": "小米登录已自动恢复，继续读取活动…"})
            return renewed
        return renew

    def status(self) -> dict:
        settings = self.settings()
        output_dir = Path(settings["output_dir"])
        records = self._records(output_dir)
        pending = []
        archived = 0
        seen = set()
        for activity_id, record in records.items():
            name = record.get("file")
            sport = record.get("sport_type")
            if not isinstance(name, str) or not FIT_NAME.fullmatch(name) or type(sport) is not int or sport not in RUN_TYPES:
                continue
            if name in seen:
                continue
            seen.add(name)
            path = output_dir / name
            if path.is_symlink():
                continue
            if not path.is_file():
                archived += (output_dir / "已上传" / name).is_file()
                continue
            timestamp = record.get("start_time")
            if type(timestamp) is int:
                started = datetime.fromtimestamp(timestamp, timezone.utc)
            else:
                started = datetime.strptime(FIT_NAME.fullmatch(name)[1], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            distance = record.get("distance_meters")
            distance_text = f"{distance / 1000:.2f} km" if isinstance(distance, (int, float)) else "—"
            pending.append({
                "id": activity_id, "file": name, "path": str(path), "sport": SPORT_NAMES[sport],
                "date": started.astimezone().strftime("%Y-%m-%d %H:%M"),
                "distance": distance_text,
            })
        pending.sort(key=lambda row: row["file"], reverse=True)
        since = self._validate_since(settings["since"]).astimezone().strftime("%Y-%m-%d %H:%M")
        since_epoch = int(self._validate_since(settings["since"]).timestamp())
        cache = self._read_local("activities.json", {"activities": []})
        confirmed = self._read_local("sync-history.json", {"records": {}})["records"]
        imported = self._read_local("strava-index.json", {"records": []})
        remote_starts = [{"start_time": value} for value in cache.get("strava_starts", [])]
        local_starts = [record for record in confirmed.values() if isinstance(record, dict)]
        pending_ids = {row["id"] for row in pending}
        duplicate_sources = set()
        unique_starts = []
        source_runs = [item for item in cache["activities"] if type(item["sport_type"]) is int and item["sport_type"] in RUN_TYPES and item["start_time"] is not None and not item.get("deleted")]
        for item in sorted(source_runs, key=lambda item: (item["id"] not in records, item["start_time"], item["id"])):
            if matches_run(item["start_time"], unique_starts):
                duplicate_sources.add(item["id"])
            else:
                unique_starts.append({"start_time": item["start_time"]})
        rows = []
        for activity in cache["activities"]:
            activity_id, start, sport = activity["id"], activity["start_time"], activity["sport_type"]
            running = type(sport) is int and sport in RUN_TYPES
            ride = type(sport) is int and sport in RIDE_TYPES
            selectable = False
            if not running:
                state = "骑行不参与同步" if ride else "不参与同步"
            elif start is None or activity.get("deleted"):
                state = "无效或已删除"
            elif activity_id in confirmed or matches_run(start, local_starts):
                state = "已确认上传"
            elif matches_run(start, imported["records"]) or matches_run(start, remote_starts):
                state = "Strava 已存在"
            elif start < since_epoch:
                state = "配置前活动"
            elif activity_id in duplicate_sources:
                state = "重复源记录，跳过"
            elif activity_id in records:
                state = "待自动上传" if activity_id in pending_ids else "已导出／已移出"
                selectable = activity_id in pending_ids
            else:
                state, selectable = "待同步", True
            distance, duration = activity.get("distance_meters"), activity.get("duration_seconds")
            rows.append({
                "id": activity_id, "date": datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M") if start is not None else "—",
                "sport": SPORT_NAMES.get(sport, "骑行" if ride else activity.get("title") or "其他运动"),
                "distance": f"{distance / 1000:.2f} km" if isinstance(distance, (int, float)) else "—",
                "duration": f"{duration // 3600:02d}:{duration // 60 % 60:02d}:{duration % 60:02d}" if type(duration) is int else "—",
                "state": state, "selectable": selectable, "running": running, "ride": ride,
                "file": records.get(activity_id, {}).get("file"),
            })
        tokens = self._api_tokens()
        if tokens:
            duplicate_text = "查重：本地记录 + Strava API（刷新和同步前检查）"
        elif imported.get("imported_at"):
            duplicate_text = f"查重：本地记录 + 已导入 Strava 跑步 {len(imported['records'])} 条（{imported['imported_at']}）"
        else:
            duplicate_text = "查重：本地记录 + Strava 上传页重复检测；成功后自动记录"
        return {
            "logged_in": load_state() is not None, "since": settings["since"], "since_local": since,
            "output_dir": str(output_dir), "pending": pending, "archived": archived,
            "last_check": settings.get("last_check"), "last_summary": settings.get("last_summary"),
            "activities": rows, "days": cache.get("days", 3), "range": cache.get("range", ""),
            "duplicate_text": duplicate_text, "api_connected": tokens is not None,
        }

    def refresh(self, days: int, emit: Callable[[dict], None]) -> dict:
        with self.operation():
            if days not in (1, 3, 7, 30):
                raise MiFitnessError("同步时间范围无效。")
            state = load_state()
            if state is None:
                raise AuthStateNotFoundError("no login")
            settings = self.settings()
            now = datetime.now().astimezone()
            start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            emit({"event": "progress", "message": "正在读取中国区小米云端活动…"})
            activities = MiFitnessActivitiesClient(
                state, country_code="CN", auth_refresher=self._auth_refresher(emit),
            ).list_activities(
                start_time=int(start.timestamp()), end_time=int(now.timestamp()), limit=10001,
            )
            if len(activities) > 10000:
                raise MiFitnessError("同步范围内活动过多，请缩短时间范围。")
            starts = []
            tokens = self._api_tokens()
            runs = [activity for activity in activities if is_run(activity)]
            if tokens:
                emit({"event": "progress", "message": "正在对比 Strava 已有跑步…"})
                starts = sorted(fetch_existing_run_starts(StravaClient(tokens), runs))
            cache = {
                "days": days, "range": f"{start:%Y-%m-%d} → {now:%Y-%m-%d}",
                "strava_starts": starts,
                "activities": [{
                    "id": item.activity_id, "start_time": item.start_time, "sport_type": item.sport_type,
                    "title": item.title, "distance_meters": item.distance_meters,
                    "duration_seconds": item.duration_seconds, "deleted": item.sync_state == "deleted",
                } for item in sorted(activities, key=lambda item: item.start_time or 0, reverse=True)],
            }
            write_private_json(self.support_dir / "activities.json", cache)
            settings["last_check"] = f"{now:%Y-%m-%d %H:%M}"
            write_private_json(self.settings_path, settings)
            return self.status()

    def import_strava(self, path: Path, timezone_hours: int) -> dict:
        with self.operation():
            if timezone_hours not in (0, 8):
                raise MiFitnessError("同步时区无效。")
            records = read_strava_csv(path, naive_timezone=timezone(timedelta(hours=timezone_hours)))
            write_private_json(self.support_dir / "strava-index.json", {
                "records": records, "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            return self.status()

    def synchronize(self, activity_ids: list[str], emit: Callable[[dict], None]) -> dict:
        with self.operation():
            current = self.status()
            allowed = {row["id"] for row in current["activities"] if row["selectable"]}
            if not activity_ids or len(activity_ids) != len(set(activity_ids)) or not set(activity_ids) <= allowed:
                raise MiFitnessError("选择的跑步记录已变化或已经同步，请刷新活动后重试。")
            tokens = self._api_tokens()
            if not tokens and len(activity_ids) > 15:
                raise MiFitnessError("选择最多 15 条跑步，每批上传完成后再继续。")
            settings = self.settings()
            emit({"event": "progress", "message": "正在检查所选跑步并准备 FIT 文件…"})
            args = argparse.Namespace(
                since=settings["since"], until=None, limit=10000, country_code="CN",
                outdoor_only=False, output_dir=settings["output_dir"], state_path=None,
                no_cache=False, cache_dir=None, no_smooth=False, outlier_speed=None,
                smooth_mode="match", verbose=False,
            )
            export_runs(
                args, report=lambda _: None, selected_ids=frozenset(activity_ids),
                auth_refresher=self._auth_refresher(emit),
            )
            current = self.status()
            files = {row["id"]: row for row in current["pending"]}
            if not set(activity_ids) <= files.keys():
                raise MiFitnessError("选择的 FIT 文件缺失，请保留导出历史并检查文件夹。")
            if tokens:
                client = StravaClient(tokens)
                source = MiFitnessActivitiesClient(load_state(), country_code="CN", auth_refresher=self._auth_refresher(emit))
                runs = [source.get_activity_by_id(activity_id) for activity_id in activity_ids]
                if any(not is_run(activity) for activity in runs):
                    raise MiFitnessError("选择的记录已变化，不再是跑步，请刷新活动。")
                existing = [{"start_time": stamp} for stamp in fetch_existing_run_starts(client, runs)]
                for activity in runs:
                    emit({"event": "progress", "message": "正在检查重复记录并同步到 Strava…"})
                    if matches_run(activity.start_time, existing):
                        self._confirm([activity.activity_id], source="strava_api_existing")
                        continue
                    result = client.upload_activity(
                        Path(files[activity.activity_id]["path"]).read_bytes(),
                        sport_type=strava_sport_type(activity.sport_type), external_id=activity.activity_id,
                    )
                    if not result.get("activity_id"):
                        raise MiFitnessError("同步未收到 Strava 完成确认，请刷新后再试。")
                    self._confirm([activity.activity_id], source="strava_api", strava_id=result["activity_id"])
                    existing.append({"start_time": activity.start_time})
                result = self.status()
                result["message"] = "所选跑步已处理，Strava 上已有的记录已跳过。"
                return result
            batch_dir = Path(settings["output_dir"]) / "上传批次" / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
            batch_dir.mkdir(mode=0o700, parents=True)
            for activity_id in activity_ids:
                row = files[activity_id]
                if Path(row["path"]).stat().st_size > 25 * 1024 * 1024:
                    raise MiFitnessError("选择的文件超过 Strava 25 MB 限制，请检查活动。")
                os.link(row["path"], batch_dir / row["file"])
            result = self.status()
            entries = [{"id": activity_id, "file": files[activity_id]["file"]} for activity_id in activity_ids]
            write_private_json(batch_dir / "manifest.json", {"version": 1, "batch": batch_dir.name, "records": entries})
            result["batch_dir"] = str(batch_dir)
            result["batch_ids"] = activity_ids
            result["batch_items"] = entries
            result["message"] = f"已准备 {len(activity_ids)} 条跑步，正在通过内置 Strava 页面自动上传…"
            return result

    def complete_web(self, batch_dir: Path, activity_ids: list[str]) -> dict:
        """Record an official-page activity ID, one verified file at a time."""
        with self.operation():
            settings = self.settings()
            output = Path(settings["output_dir"])
            batches = output / "上传批次"
            if batch_dir.is_symlink() or batches.is_symlink() or batch_dir.parent.resolve() != batches.resolve():
                raise MiFitnessError("同步批次路径无效，未修改上传记录。")
            if not activity_ids or len(activity_ids) != len(set(activity_ids)):
                raise MiFitnessError("同步完成记录无效，未修改上传记录。")
            try:
                if (batch_dir / "manifest.json").is_symlink() or (batch_dir / "strava-confirmed.json").is_symlink():
                    raise ValueError("invalid confirmation file")
                manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
                receipts = json.loads((batch_dir / "strava-confirmed.json").read_text(encoding="utf-8"))
                if any(value.get("version") != 1 or value.get("batch") != batch_dir.name for value in (manifest, receipts)):
                    raise ValueError("invalid batch")
                entries = {row["id"]: row["file"] for row in manifest["records"]}
                verified = {row["id"]: row for row in receipts["records"]}
                if len(entries) != len(manifest["records"]) or len(verified) != len(receipts["records"]):
                    raise ValueError("repeated result")
                if not set(activity_ids) <= entries.keys() & verified.keys():
                    raise ValueError("missing result")
                exported = self._records(output)
                for activity_id in activity_ids:
                    receipt = verified[activity_id]
                    record = exported[activity_id]
                    if (receipt["file"] != entries[activity_id] or record["file"] != entries[activity_id]
                            or not FIT_NAME.fullmatch(entries[activity_id])
                            or type(record["sport_type"]) is not int or record["sport_type"] not in RUN_TYPES
                            or type(receipt["strava_id"]) is not int or receipt["strava_id"] <= 0
                            or receipt["result"] not in ("uploaded", "duplicate")):
                        raise ValueError("invalid result")
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise MiFitnessError("同步尚未收到可核实的 Strava 保存结果，未标记完成。") from exc
            history = self._read_local("sync-history.json", {"records": {}})["records"]
            for activity_id in activity_ids:
                receipt = verified[activity_id]
                if activity_id in history:
                    # A repeated acknowledgement must not re-upload or overwrite a prior result.
                    if history[activity_id].get("strava_id") != receipt["strava_id"]:
                        raise MiFitnessError("同步完成记录与已有记录不同，请刷新活动检查。")
                    continue
                self._confirm([activity_id], source="strava_web_existing" if receipt["result"] == "duplicate" else "strava_web", strava_id=receipt["strava_id"])
            result = self.status()
            result["message"] = "Strava 已确认保存，上传记录已自动保存；重复活动已跳过。"
            return result

    def recover_web(self) -> None:
        """Recover durable page confirmations after a quit or interrupted backend."""
        batches = Path(self.settings()["output_dir"]) / "上传批次"
        if not batches.exists():
            return
        for receipt_path in sorted(batches.glob("*/strava-confirmed.json")):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                activity_ids = [row["id"] for row in receipt["records"]]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise MiFitnessError("无法读取自动上传完成记录，请保留批次文件，避免重复同步。") from exc
            self.complete_web(receipt_path.parent, activity_ids)

    def _confirm(self, activity_ids: list[str], *, source: str, strava_id=None) -> None:
        settings = self.settings()
        output_dir = Path(settings["output_dir"])
        exported = self._records(output_dir)
        history = self._read_local("sync-history.json", {"records": {}})
        for activity_id in activity_ids:
            record = exported[activity_id]
            timestamp = record.get("start_time")
            if timestamp is None:
                timestamp = int(datetime.strptime(FIT_NAME.fullmatch(record["file"])[1], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp())
            history["records"][activity_id] = {
                "start_time": timestamp, "sport_type": record["sport_type"], "source": source,
                "strava_id": strava_id, "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
        write_private_json(self.support_dir / "sync-history.json", history)
        self._archive_files([exported[activity_id]["file"] for activity_id in activity_ids], output_dir)

    def login(self, emit: Callable[[dict], None]) -> dict:
        with self.operation():
            settings = self.settings()
            device_id = settings.get("device_id") or MiFitnessAuthClient.generate_device_id()
            settings["device_id"] = device_id
            write_private_json(self.settings_path, settings)
            client = MiFitnessAuthClient()
            emit({"event": "progress", "message": "正在获取小米登录二维码…"})
            challenge = begin_qr_login(client, device_id=device_id)
            image = download_qr_image(client, challenge)
            emit({"event": "qr", "image": base64.b64encode(image).decode("ascii")})
            session = wait_for_qr_login(client, challenge, device_id=device_id)
            save_state(session.to_auth_state())
            return self.status()

    def connect_strava(self, credentials: dict, emit: Callable[[dict], None]) -> dict:
        with self.operation():
            client_id = credentials.get("client_id", "")
            client_secret = credentials.get("client_secret", "")
            if not isinstance(client_id, str) or not client_id.isdigit() or not isinstance(client_secret, str) or not client_secret.strip():
                raise MiFitnessError("连接 Strava 需要有效的 Client ID 和 Client Secret。")
            emit({"event": "progress", "message": "请在 Strava 官方授权页面确认账号，并允许读取和上传运动。"})
            args = argparse.Namespace(client_id=client_id, client_secret=client_secret, port=5478, strava_token_path=None)
            try:
                # The CLI prints a temporary OAuth URL. Keep it inside this process.
                with redirect_stdout(io.StringIO()):
                    handle_strava_login(args)
            finally:
                args.client_secret = None
                credentials.clear()
            settings = self.settings()
            settings["mode"] = "api"
            write_private_json(self.settings_path, settings)
            result = self.status()
            result["message"] = "Strava API 已连接。刷新活动后，可直接查重并同步选中跑步。"
            return result

    def archive(self, activity_ids: list[str]) -> dict:
        """Archive only the exact files that the user confirmed in the desktop sheet."""
        with self.operation():
            current = self.status()
            pending = {row["id"] for row in current["pending"]}
            allowed = {row["id"] for row in current["activities"] if row["selectable"]}
            if not activity_ids or len(activity_ids) != len(set(activity_ids)) or not set(activity_ids) <= pending & allowed:
                raise MiFitnessError("选择的文件已变化，请刷新后重新选择。")
            self._confirm(activity_ids, source="user_confirmed_web")
            return self.status()

    @staticmethod
    def _archive_files(files: list[str], output_dir: Path) -> None:
        archive_dir = output_dir / "已上传"
        if archive_dir.is_symlink():
            raise MiFitnessError("归档文件夹不能是符号链接。")
        archive_dir.mkdir(mode=0o700, exist_ok=True)
        if any((archive_dir / name).exists() for name in files):
            raise MiFitnessError("归档中已有同名文件，已停止以保留现有文件。")
        for name in files:
            # Keep FIT bytes unchanged and preserve the ledger for future duplicate checks.
            os.link(output_dir / name, archive_dir / name, follow_symlinks=False)
            (output_dir / name).unlink()
