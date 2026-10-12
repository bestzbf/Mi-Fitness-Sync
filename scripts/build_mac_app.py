"""Build a standalone native Mac app; credentials and preferences stay outside it."""
from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "小米跑步同步"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", help="Install to this user's Applications folder")
    args = parser.parse_args()
    build = ROOT / "build" / "mac-app"
    dist = ROOT / "dist"
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "xiaomi-sync-backend", "--distpath", str(build / "runtime"),
        "--workpath", str(build / "pyinstaller"), "--specpath", str(build),
        "--paths", str(ROOT / "src"), "--exclude-module", "tkinter", "--exclude-module", "pytest",
        "--log-level", "WARN", str(ROOT / "scripts" / "desktop_backend.py"),
    ], check=True, cwd=ROOT)
    app = dist / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    resources = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    shutil.copytree(build / "runtime" / "xiaomi-sync-backend", resources / "backend", symlinks=True)
    shutil.copy(ROOT / "LICENSE", resources / "LICENSE.txt")
    shutil.copy(ROOT / "desktop" / "strava-upload.js", resources / "strava-upload.js")
    iconset = build / "AppIcon.iconset"
    subprocess.run(["xcrun", "swift", str(ROOT / "desktop" / "Icon.swift"), str(iconset)], check=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")], check=True)
    swift_main = build / "main.swift"
    shutil.copy(ROOT / "desktop" / "MacApp.swift", swift_main)
    subprocess.run([
        "xcrun", "swiftc", "-O", "-swift-version", "5", "-target", "arm64-apple-macosx14.0",
        "-framework", "AppKit", "-framework", "SwiftUI", "-framework", "UniformTypeIdentifiers",
        "-framework", "WebKit", "-framework", "Security",
        str(ROOT / "desktop" / "StravaSession.swift"), str(ROOT / "desktop" / "StravaUpload.swift"), str(swift_main),
        "-o", str(macos / "XiaomiRunSync"),
    ], check=True, cwd=ROOT)
    info = {
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": "local.xiaomi.running-sync", "CFBundleExecutable": "XiaomiRunSync",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0.3", "CFBundleVersion": "4",
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Based on Mi-Fitness-Sync (MIT license). Local running-only integration.",
    }
    with (app / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump(info, stream)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    # One-time migration preserves the existing cutoff and stable Xiaomi device ID.
    from mi_fitness_sync.desktop.service import DesktopService
    DesktopService().initialize(ROOT)
    print(f"Built: {app}")
    if args.install:
        installed = Path.home() / "Applications" / f"{APP_NAME}.app"
        if installed.exists():
            with (installed / "Contents" / "Info.plist").open("rb") as stream:
                previous = plistlib.load(stream)
            if previous.get("CFBundleIdentifier") != info["CFBundleIdentifier"]:
                raise SystemExit("An unrelated application already uses this name; leaving it untouched.")
            shutil.rmtree(installed)
        installed.parent.mkdir(exist_ok=True)
        shutil.copytree(app, installed, symlinks=True)
        print(f"Installed: {installed}")


if __name__ == "__main__":
    main()
