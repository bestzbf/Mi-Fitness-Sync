"""Credential handoff must remain in memory when installing a locally signed update."""
import importlib.util
from pathlib import Path
import subprocess


spec = importlib.util.spec_from_file_location(
    "mac_build", Path(__file__).resolve().parents[2] / "scripts" / "build_mac_app.py"
)
mac_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mac_build)


def test_upgrade_passes_private_snapshot_only_on_stdin(monkeypatch, tmp_path, capsys):
    secret = b"synthetic-private-cookie-snapshot"
    calls = []

    def run(command, **options):
        calls.append((command, options))
        assert all(secret.decode() not in arg for arg in command)
        assert options["capture_output"] is True
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 0, stdout=secret, stderr=b"")
        assert options["input"] == secret
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(mac_build.subprocess, "run", run)
    assert mac_build.migrate_strava_session(tmp_path, "4", "5")
    assert len(calls) == 2
    assert capsys.readouterr() == ("", "")


def test_rejected_keychain_access_does_not_start_import(monkeypatch, tmp_path):
    calls = []

    def run(command, **options):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"access denied")

    monkeypatch.setattr(mac_build.subprocess, "run", run)
    assert not mac_build.migrate_strava_session(tmp_path, "4", "5")
    assert len(calls) == 1


def test_keychain_migration_timeout_can_fall_back_to_login(monkeypatch, tmp_path):
    def run(command, **options):
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(mac_build.subprocess, "run", run)
    assert not mac_build.migrate_strava_session(tmp_path, "4", "5")
