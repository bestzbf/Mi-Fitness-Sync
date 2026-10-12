from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest
import requests

from mi_fitness_sync.auth import refresh, store
from mi_fitness_sync.auth.client import MiFitnessAuthClient, URL_LOGIN
from mi_fitness_sync.exceptions import XiaomiApiError, XiaomiLoginRequiredError


@pytest.fixture
def renewal(monkeypatch, auth_state):
    client = MiFitnessAuthClient()
    security = base64.b64encode(b"new-session-security").decode()
    data = {
        "client": client, "calls": [], "account_cookies": [], "sts_cookies": [],
        "payload": {
            "code": 0, "userId": auth_state.user_id, "cUserId": "new-cuser",
            "ssecurity": security, "nonce": 17,
            "location": "https://sts-hlth.io.mi.com/healthapp/sts?ticket=test-ticket",
        }, "token": "new-service-token",
    }

    def get(url, **kwargs):
        data["calls"].append((url, kwargs))
        request = client.session.prepare_request(requests.Request("GET", url))
        cookies = request.headers.get("Cookie", "")
        response = requests.Response()
        response.status_code = 200
        if url == URL_LOGIN:
            data["account_cookies"].append(cookies)
            if data.get("rotated_cookie"):
                response.cookies.set("passToken", data["rotated_cookie"], domain=".account.xiaomi.com")
                client.session.cookies.set("passToken", data["rotated_cookie"], domain=".account.xiaomi.com")
            response._content = ("&&&START&&&" + json.dumps(data["payload"])).encode()
        else:
            data["sts_cookies"].append(cookies)
            if data["token"]:
                client.session.cookies.set("serviceToken", data["token"], domain="sts-hlth.io.mi.com")
            response._content = b"ok"
        return response

    monkeypatch.setattr(client.session, "get", get)
    monkeypatch.setattr(refresh, "utc_now_iso", lambda: "2026-10-12T04:00:00+00:00")
    return data


def test_renewal_updates_matching_token_and_key_without_password(renewal, auth_state):
    client = renewal["client"]
    client.session.cookies.set("serviceToken", "stale-token", domain="sts-hlth.io.mi.com")
    new = refresh.refresh_with_pass_token(auth_state, client=client)
    assert new.service_token == renewal["token"]
    assert new.ssecurity == renewal["payload"]["ssecurity"]
    assert new.c_user_id == "new-cuser"
    assert new.pass_token == auth_state.pass_token
    assert new.device_id == auth_state.device_id
    assert new.created_at == auth_state.created_at
    assert new.updated_at == "2026-10-12T04:00:00+00:00"
    assert "passToken=pass-token" in renewal["account_cookies"][0]
    assert "deviceId=DEVICE123" in renewal["account_cookies"][0]
    assert all("passToken=" not in value for value in renewal["sts_cookies"])
    assert renewal["calls"][1][1]["allow_redirects"] is False
    assert all("data" not in kwargs for _, kwargs in renewal["calls"])
    assert any(cookie["name"] == "serviceToken" and cookie["value"] == renewal["token"] for cookie in new.cookies)


def test_rotated_account_credential_is_preserved_for_next_renewal(renewal, auth_state):
    renewal["payload"]["passToken"] = "new-pass-token"
    new = refresh.refresh_with_pass_token(auth_state, client=renewal["client"])
    assert new.pass_token == "new-pass-token"


def test_rotated_account_cookie_is_preserved_when_body_omits_it(renewal, auth_state):
    renewal["rotated_cookie"] = "rotated-cookie-pass-token"
    new = refresh.refresh_with_pass_token(auth_state, client=renewal["client"])
    assert new.pass_token == "rotated-cookie-pass-token"


def test_renewal_without_nonce_uses_sts_directly(renewal, auth_state):
    renewal["payload"].pop("nonce")
    refresh.refresh_with_pass_token(auth_state, client=renewal["client"])
    assert renewal["calls"][1][1]["params"] is None


@pytest.mark.parametrize("payload", [
    {"code": 70016}, {"code": 81003}, {"code": 87001, "captchaUrl": "test"},
    {"code": 0, "notificationUrl": "https://account.xiaomi.com/verify"},
    {"code": 0, "_sign": "new-login-required"},
])
def test_only_rejected_account_or_required_verification_requests_login(renewal, auth_state, payload):
    renewal["payload"] = payload
    with pytest.raises(XiaomiLoginRequiredError):
        refresh.refresh_with_pass_token(auth_state, client=renewal["client"])
    assert len(renewal["calls"]) == 1


@pytest.mark.parametrize("url", [
    "http://sts-hlth.io.mi.com/sts", "https://other.example/sts",
    "https://account.xiaomi.com.other.example/sts", "https://name:secret@sts-hlth.io.mi.com/sts",
])
def test_renewal_cannot_forward_auth_to_unexpected_sts(renewal, auth_state, url):
    renewal["payload"]["location"] = url
    with pytest.raises(XiaomiApiError, match="unexpected STS"):
        refresh.refresh_with_pass_token(auth_state, client=renewal["client"])
    assert len(renewal["calls"]) == 1


def test_renewal_cannot_switch_accounts(renewal, auth_state):
    renewal["payload"]["userId"] = "another-user"
    with pytest.raises(XiaomiApiError, match="different account"):
        refresh.refresh_with_pass_token(auth_state, client=renewal["client"])
    assert len(renewal["calls"]) == 1


def test_no_sts_token_cannot_return_or_persist_a_new_session(renewal, auth_state):
    renewal["token"] = None
    with pytest.raises(XiaomiApiError, match="serviceToken"):
        refresh.refresh_with_pass_token(auth_state, client=renewal["client"])


@pytest.mark.parametrize("failure", [requests.Timeout(), XiaomiApiError("Temporary failure"), XiaomiLoginRequiredError("Verification required")])
def test_failed_renewal_keeps_saved_credentials_intact(monkeypatch, tmp_path, auth_state, failure):
    path = tmp_path / "auth.json"
    store.save_state(auth_state, str(path))
    original = path.read_bytes()

    def fail(_):
        raise failure

    monkeypatch.setattr(refresh, "refresh_with_pass_token", fail)
    with pytest.raises(type(failure)):
        refresh.refresh_saved_state(auth_state, str(path))
    assert path.read_bytes() == original


def test_saved_renewal_replaces_entire_session_pair(monkeypatch, tmp_path, auth_state):
    path = tmp_path / "auth.json"
    store.save_state(auth_state, str(path))
    renewed = replace(auth_state, service_token="new-token", ssecurity="new-key", pass_token="rotated-pass")
    monkeypatch.setattr(refresh, "refresh_with_pass_token", lambda _: renewed)
    assert refresh.refresh_saved_state(auth_state, str(path)) == renewed
    assert store.load_state(str(path)) == renewed
    assert path.stat().st_mode & 0o777 == 0o600


def test_client_uses_already_renewed_credentials_instead_of_renewing_twice(monkeypatch, tmp_path, auth_state):
    path = tmp_path / "auth.json"
    renewed = replace(auth_state, service_token="already-renewed", ssecurity="matching-key")
    store.save_state(renewed, str(path))

    def never(_):
        pytest.fail("Another client already renewed this account")

    monkeypatch.setattr(refresh, "refresh_with_pass_token", never)
    assert refresh.refresh_saved_state(auth_state, str(path)) == renewed


def test_removed_credentials_are_not_restored_by_a_stale_client(tmp_path, auth_state):
    path = tmp_path / "removed-auth.json"
    with pytest.raises(XiaomiLoginRequiredError):
        refresh.refresh_saved_state(auth_state, str(path))
    assert not path.exists()


def test_atomic_save_failure_keeps_previous_credentials(monkeypatch, tmp_path, auth_state):
    path = tmp_path / "auth.json"
    store.save_state(auth_state, str(path))
    original = path.read_bytes()

    def fail(*args):
        raise OSError("disk write failed")

    monkeypatch.setattr(store.os, "replace", fail)
    with pytest.raises(OSError):
        store.save_state(replace(auth_state, service_token="new-token"), str(path))
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".auth-*.tmp"))
