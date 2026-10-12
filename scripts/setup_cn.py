"""Local interactive setup and running-only sync for this Mac."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import re
import subprocess
import sys
import webbrowser
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from mi_fitness_sync.activity.client import MiFitnessActivitiesClient
from mi_fitness_sync.auth.client import MiFitnessAuthClient
from mi_fitness_sync.auth.qr import begin_qr_login, download_qr_image, wait_for_qr_login
from mi_fitness_sync.auth.store import load_state, save_state
from mi_fitness_sync.cli.app import format_error, handle_strava_login
from mi_fitness_sync.cli.sync_runs import is_run
from mi_fitness_sync.exceptions import MiFitnessError, XiaomiApiError
from mi_fitness_sync.strava.store import load_tokens


ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
STATUS_PATH = LOCAL / "setup-status.json"
status = {"stage": "starting", "uploaded_activity_ids": []}


def update_status(stage: str, **values) -> None:
    status.update(stage=stage, **values)
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    temporary = STATUS_PATH.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(status, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, STATUS_PATH)


def retry(stage: str, action) -> None:
    while True:
        update_status(stage)
        try:
            result = action()
            if result not in (None, 0):
                raise MiFitnessError(f"操作返回错误码 {result}")
            return
        except (MiFitnessError, requests.RequestException, ValueError, OSError) as exc:
            failure = {"error_type": type(exc).__name__}
            if isinstance(exc, XiaomiApiError):
                failure["error_code"] = exc.code
            update_status(stage + "_failed", **failure)
            message = format_error(exc) if isinstance(exc, MiFitnessError) else str(exc)
            print(f"\n这一步未成功：{message}", flush=True)
            if input("按回车重试，输入 q 结束：").strip().lower() == "q":
                raise KeyboardInterrupt


def login_xiaomi() -> None:
    print("\n第一步：用小米手机扫码，登录与手表绑定的同一个小米账号。", flush=True)
    device_path = LOCAL / "xiaomi-device-id"
    if device_path.exists():
        device_id = device_path.read_text().strip()
    else:
        device_id = MiFitnessAuthClient.generate_device_id()
        fd = os.open(device_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(device_id)
    client = MiFitnessAuthClient()
    challenge = begin_qr_login(client, device_id=device_id)
    image = download_qr_image(client, challenge)
    image_path = LOCAL / "xiaomi-login-qr.png"
    fd = os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(image)
    update_status("xiaomi_qr_login_pending", qr_image_path=str(image_path))
    subprocess.run(["open", str(image_path)], check=True)
    print("二维码已经打开。请用小米手机的扫一扫扫描，并在手机上确认登录。", flush=True)
    print("确认使用的是小米运动健康绑定的账号；电脑会自动接收登录结果，无需回车。", flush=True)
    try:
        session = wait_for_qr_login(client, challenge, device_id=device_id)
        save_state(session.to_auth_state())
        print("小米扫码登录已完成。", flush=True)
    finally:
        image_path.unlink(missing_ok=True)


def inspect_china_data() -> None:
    state = load_state()
    now = datetime.now(timezone.utc)
    activities = MiFitnessActivitiesClient(state, country_code="CN").list_activities(
        start_time=int((now - timedelta(days=30)).timestamp()),
        end_time=int(now.timestamp()), limit=1001,
    )
    counts = dict(Counter(str(activity.sport_type) for activity in activities))
    runs = [activity for activity in activities if is_run(activity)]
    update_status(
        "china_data_ready", activities_count=len(activities),
        runs_count=len(runs), sport_type_counts=counts,
    )
    print(f"\n中国区最近 30 天：读取到 {len(activities)} 条运动，其中 {len(runs)} 条跑步。", flush=True)
    if len(activities) > 1000:
        raise MiFitnessError("最近 30 天超过 1000 条运动，需要先调整扫描范围。")
    if not runs:
        print("目前没有可上传的跑步，继续完成设置。", flush=True)


def login_strava() -> None:
    print("\n第二步：连接你自己的 Strava 账号。", flush=True)
    print("浏览器已打开 Strava 的 API 设置页面。", flush=True)
    print("如尚未创建应用，可将名称填为 Xiaomi Run Sync，网站填项目的 GitHub 地址。", flush=True)
    print("Authorization Callback Domain（授权回调域名）填 localhost。", flush=True)
    print("GitHub 地址：https://github.com/kevinkwee/Mi-Fitness-Sync", flush=True)
    print("创建后，把页面上的 Client ID 和 Client Secret 输入下面的本机提示。", flush=True)
    print("接着在浏览器里授权读取和上传运动。", flush=True)
    webbrowser.open("https://www.strava.com/settings/api")
    client_id = input("Strava Client ID：").strip()
    client_secret = getpass.getpass("Strava Client Secret（输入时不显示）：")
    args = argparse.Namespace(
        client_id=client_id, client_secret=client_secret, port=5478, strava_token_path=None,
    )
    try:
        handle_strava_login(args)
    finally:
        args.client_secret = None


def run_cli(*arguments: str) -> int:
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "mi_fitness_sync", *arguments],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8",
    )
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            match = re.fullmatch(r"Uploaded: https://www\.strava\.com/activities/(\d+)\s*", line)
            if match:
                status["uploaded_activity_ids"].append(int(match.group(1)))
                update_status("uploading")
            summary = re.fullmatch(r"Finished: uploaded (\d+) runs; skipped (\d+) existing runs\.\s*", line)
            if summary:
                status.update(uploaded_count=int(summary.group(1)), skipped_count=int(summary.group(2)))
            export_summary = re.fullmatch(
                r"Exported (\d+) runs; (\d+) already exported; (\d+) awaiting web upload\.\s*", line,
            )
            if export_summary:
                status.update(exported_count=int(export_summary.group(1)), pending_file_count=int(export_summary.group(3)))
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        process.stdout.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="小米跑步同步设置（默认使用免费网页上传）")
    parser.add_argument("--api", action="store_true", help="Use a separately configured Strava API application")
    args = parser.parse_args(argv)
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    lock_fd = os.open(LOCAL / "setup.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有跑步同步窗口在运行，请使用那个窗口。", flush=True)
            return 1
        try:
            update_status("starting")
            print("小米国行 Watch S4 → Strava：仅同步跑步", flush=True)
            print("仅选择户外跑步、跑步机和越野跑，自动跳过骑行。", flush=True)
            if load_state() is None:
                retry("xiaomi_login_pending", login_xiaomi)
            update_status("xiaomi_ready")
            retry("checking_china_data", inspect_china_data)
            if not args.api:
                settings_path = LOCAL / "free-sync-settings.json"
                if settings_path.exists():
                    settings = json.loads(settings_path.read_text())
                else:
                    settings = {"since": datetime.now(timezone.utc).isoformat()}
                    fd = os.open(settings_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        json.dump(settings, stream, indent=2)
                output_dir = ROOT / "待上传跑步"
                print("\n免费网页上传模式：只处理本次配置之后的新跑步。", flush=True)
                retry("exporting_runs", lambda: run_cli(
                    "export-runs", "--since", settings["since"], "--output-dir", str(output_dir),
                ))
                pending = status.get("pending_file_count", 0)
                if pending:
                    subprocess.run(["open", str(output_dir)], check=True)
                    webbrowser.open("https://www.strava.com/upload/select")
                    print(f"\n有 {pending} 个待上传跑步文件。请在网页选择 FIT 文件并保存，每批最多 15 个。", flush=True)
                    print("上传成功后可把对应文件移到其他文件夹；再次运行会保留尚未上传的文件。", flush=True)
                    update_status("web_upload_needed")
                else:
                    update_status("configured_free", pending_file_count=0)
                    print("\n免费同步流程已配置。当前没有新的跑步文件，未向 Strava 上传数据。", flush=True)
                print("以后跑步后先同步手表到手机，再双击“同步跑步.command”。", flush=True)
                return 0
            if load_tokens() is None:
                retry("strava_login_pending", login_strava)
            update_status("strava_ready")
            print("\n第三步：检查待同步跑步和 Strava 重复记录。", flush=True)
            retry("checking_duplicates", lambda: run_cli("sync-runs"))
            print("\n开始上传缺失的跑步；每条跑步会保留本地 FIT 备份。", flush=True)
            retry("uploading", lambda: run_cli("sync-runs", "--upload"))
            update_status("complete")
            print("\n设置和本次同步已完成。以后跑步后同步手表到手机，再双击“同步跑步.command”即可。", flush=True)
            return 0
        except (KeyboardInterrupt, EOFError):
            update_status("input_needed")
            print("\n本次设置已结束，已完成的登录会保留。可再次打开窗口继续。", flush=True)
            return 1
        except Exception:
            update_status("failed")
            raise


if __name__ == "__main__":
    raise SystemExit(main())
