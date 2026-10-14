"""Run isolated native/WebKit probes without uploading activities to Strava."""
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "mac-app" / "upload-probes"
PROBES = {
    "WebUploadProbe": [],
    "UploadCoordinatorProbe": ["StravaUpload.swift"],
    "BackgroundSyncProbe": ["StravaSession.swift", "StravaUpload.swift", "StravaWebPanel.swift"],
    "SessionProbe": ["StravaSession.swift"],
}

for name, sources in PROBES.items():
    folder = BUILD / name
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "desktop" / "tests" / f"{name}.swift", folder / "main.swift")
    binary = folder / name
    subprocess.run([
        "xcrun", "swiftc", "-swift-version", "5", "-framework", "AppKit",
        "-framework", "WebKit", "-framework", "Security",
        *[str(ROOT / "desktop" / source) for source in sources],
        str(folder / "main.swift"), "-o", str(binary),
    ], check=True)
    if name == "SessionProbe":
        identifier = str(uuid.uuid4())
        for phase in ("write", "read"):
            subprocess.run([str(binary), phase, identifier], check=True)
    else:
        subprocess.run([str(binary), str(ROOT / "desktop" / "strava-upload.js")], check=True)
