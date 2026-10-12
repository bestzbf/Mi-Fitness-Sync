from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

from mi_fitness_sync.desktop.service import DesktopService
from mi_fitness_sync.exceptions import AuthStateNotFoundError, MiFitnessError, XiaomiApiError, XiaomiLoginRequiredError


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="小米跑步同步的本机桌面服务")
    parser.add_argument("command", choices=("status", "refresh", "sync", "login", "archive", "complete-web", "initialize", "import-strava", "connect-strava"))
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--ids", nargs="*", default=[])
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--batch-dir", type=Path)
    parser.add_argument("--csv-timezone", type=int, choices=(0, 8), default=0)
    args = parser.parse_args(argv)
    try:
        service = DesktopService()
        service.initialize(args.legacy_root)
        if args.command == "refresh":
            result = service.refresh(args.days, emit)
        elif args.command == "sync":
            result = service.synchronize(args.ids, emit)
        elif args.command == "login":
            result = service.login(emit)
        elif args.command == "archive":
            result = service.archive(args.ids)
        elif args.command == "complete-web":
            if args.batch_dir is None:
                raise MiFitnessError("同步批次缺失，未修改上传记录。")
            result = service.complete_web(args.batch_dir, args.ids)
        elif args.command == "import-strava":
            result = service.import_strava(args.csv, args.csv_timezone)
        elif args.command == "connect-strava":
            credentials = json.loads(sys.stdin.read(8192))
            result = service.connect_strava(credentials, emit)
        else:
            if args.command == "status":
                service.recover_web()
            result = service.status()
        emit({"event": "result", "data": result})
        return 0
    except AuthStateNotFoundError:
        emit({"event": "error", "message": "请先扫码登录与手表绑定的小米账号。", "login_needed": True})
    except XiaomiLoginRequiredError:
        emit({"event": "error", "message": "小米已要求重新验证账号，自动续期无法完成。请扫码登录一次；原设置和上传记录已保留。", "login_needed": True})
    except XiaomiApiError as exc:
        # Distinguish HTTP failures from API codes without exposing credential-bearing bodies.
        if exc.http_status == 401:
            message = "自动续期后小米云端仍拒绝访问（HTTP 401）。请稍后重试；登录凭证已保留。"
        elif exc.http_status is not None and exc.http_status >= 500:
            message = f"小米服务暂时不可用（HTTP {exc.http_status}）。请稍后重试，无需重新扫码。"
        elif exc.code is not None:
            message = f"小米云端返回错误（错误码 {exc.code}）。请稍后重试；登录凭证已保留。"
        elif exc.http_status is not None:
            message = f"小米服务响应异常（HTTP {exc.http_status}）。请重试；登录凭证已保留。"
        else:
            message = "小米服务响应异常，暂时无法读取。请稍后重试；登录凭证已保留。"
        emit({"event": "error", "message": message})
    except requests.RequestException:
        emit({"event": "error", "message": "连接小米云端失败。请检查网络后重试。"})
    except (MiFitnessError, OSError, ValueError, KeyError, TypeError) as exc:
        safe_messages = {
            "小米云端有心率，但详细心率暂时无法读取；请重试，避免导出缺少心率的文件。":
                "小米云端有心率，但详细心率暂时无法读取。本次已停止，请重试。",
            "Scan exceeded --limit; no files exported. Increase --limit or narrow the dates.":
                "运动记录超过扫描上限，本次已停止。请保留原设置文件。",
            "Local export history could not be read; no files exported.":
                "无法读取导出历史，请保留 .exported.json 文件。",
        }
        message = safe_messages.get(str(exc))
        if message is None and isinstance(exc, MiFitnessError) and str(exc).startswith(("同步", "另一个", "无法读取", "选择", "归档", "导入", "CSV", "连接")):
            message = str(exc)
        emit({"event": "error", "message": message or "本次操作未完成，已保留登录和文件。请重试。"})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
