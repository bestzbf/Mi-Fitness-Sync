from __future__ import annotations

import argparse
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mi_fitness_sync.cli import app
from mi_fitness_sync.exceptions import MiFitnessError
from scripts import setup_cn


@pytest.mark.parametrize("existing_login", [False, True])
def test_login_uses_stable_device_for_verification_retry(monkeypatch, auth_state, existing_login):
    received = []

    class Client:
        def __init__(self, **kwargs):
            pass

        @staticmethod
        def generate_device_id():
            raise AssertionError("Verification retry must keep its device identity")

        def login_with_password(self, **kwargs):
            received.append(kwargs["device_id"])
            return SimpleNamespace(to_auth_state=lambda: replace(auth_state, device_id=kwargs["device_id"]))

    monkeypatch.setattr(app, "MiFitnessAuthClient", Client)
    monkeypatch.setattr(app, "load_state", lambda path: auth_state if existing_login else None)
    monkeypatch.setattr(app, "save_state", lambda state, path: "test-state.json")
    args = argparse.Namespace(email="test@example.com", password="test-password", state_path=None)
    assert app.handle_login(args, device_id="verification-device") == 0
    assert received == [auth_state.device_id if existing_login else "verification-device"]


@pytest.fixture
def local_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_cn, "LOCAL", tmp_path)
    monkeypatch.setattr(setup_cn, "STATUS_PATH", tmp_path / "setup-status.json")
    monkeypatch.setattr(setup_cn, "status", {"stage": "starting", "uploaded_activity_ids": []})
    monkeypatch.setattr(setup_cn.MiFitnessAuthClient, "generate_device_id", lambda: "persistent-device")
    return tmp_path


def test_qr_login_saves_approved_session_and_removes_image(monkeypatch, local_setup, auth_state):
    challenge = object()
    session = SimpleNamespace(to_auth_state=lambda: auth_state)
    monkeypatch.setattr(setup_cn, "begin_qr_login", lambda client, **kwargs: challenge)
    monkeypatch.setattr(setup_cn, "download_qr_image", lambda client, item: b"test-qr-image")
    monkeypatch.setattr(setup_cn, "wait_for_qr_login", lambda client, item, **kwargs: session)
    monkeypatch.setattr(setup_cn.subprocess, "run", lambda args, **kwargs: None)
    saved = []
    monkeypatch.setattr(setup_cn, "save_state", saved.append)
    setup_cn.login_xiaomi()
    assert saved == [auth_state]
    assert not (local_setup / "xiaomi-login-qr.png").exists()
    assert (local_setup / "xiaomi-device-id").read_text() == "persistent-device"


def test_unapproved_qr_login_never_saves_credentials(monkeypatch, local_setup):
    monkeypatch.setattr(setup_cn, "begin_qr_login", lambda client, **kwargs: object())
    monkeypatch.setattr(setup_cn, "download_qr_image", lambda client, item: b"test-qr-image")
    monkeypatch.setattr(setup_cn.subprocess, "run", lambda args, **kwargs: None)
    saved = []
    monkeypatch.setattr(setup_cn, "save_state", saved.append)

    def wait(*args, **kwargs):
        raise MiFitnessError("二维码已过期")

    monkeypatch.setattr(setup_cn, "wait_for_qr_login", wait)
    with pytest.raises(MiFitnessError, match="已过期"):
        setup_cn.login_xiaomi()
    assert saved == []
    assert not (local_setup / "xiaomi-login-qr.png").exists()


def test_free_setup_never_requests_api_access_and_preserves_future_start(
    monkeypatch, local_setup, auth_state,
):
    monkeypatch.setattr(setup_cn, "load_state", lambda: auth_state)
    monkeypatch.setattr(setup_cn, "inspect_china_data", lambda: None)

    def forbidden():
        raise AssertionError("Free setup must not require Strava API authorization")

    monkeypatch.setattr(setup_cn, "load_tokens", forbidden)
    monkeypatch.setattr(setup_cn, "login_strava", forbidden)
    commands = []
    monkeypatch.setattr(setup_cn, "run_cli", lambda *args: commands.append(args) or 0)
    assert setup_cn.main([]) == 0
    first_start = commands[0][commands[0].index("--since") + 1]
    assert commands[0][0] == "export-runs"
    assert setup_cn.main([]) == 0
    assert commands[1][commands[1].index("--since") + 1] == first_start
    assert setup_cn.status["stage"] == "configured_free"
