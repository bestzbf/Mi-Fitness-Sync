from __future__ import annotations

import json
import time

import pytest
import requests

from mi_fitness_sync.auth.client import MiFitnessAuthClient
from mi_fitness_sync.auth.qr import QrChallenge, begin_qr_login, download_qr_image, wait_for_qr_login
from mi_fitness_sync.exceptions import MiFitnessError


class Response:
    def __init__(self, payload=None, content=b""):
        self.text = "&&&START&&&" + json.dumps(payload or {})
        self.content = content
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def raise_for_status(self):
        pass


@pytest.fixture
def qr_client(monkeypatch):
    client = MiFitnessAuthClient()
    client.calls = []
    client.responses = []

    def get(url, **kwargs):
        client.calls.append((url, kwargs))
        response = client.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(client.session, "get", get)
    return client


def challenge():
    return QrChallenge(
        "https://account.xiaomi.com/longPolling/qr", "https://ak.lp.account.xiaomi.com/poll",
        time.monotonic() + 60,
    )


def test_qr_is_scoped_to_mi_fitness_and_validates_account_hosts(qr_client):
    qr_client.responses = [Response({
        "code": 0, "qr": challenge().image_url, "lp": challenge().polling_url, "timeout": 300,
    })]
    result = begin_qr_login(qr_client, device_id="test-device")
    assert result.polling_url == challenge().polling_url
    assert qr_client.calls[0][1]["params"]["sid"] == "miothealth"
    assert qr_client.session.cookies.get("deviceId") == "test-device"


@pytest.mark.parametrize("bad_url", ["https://other.example/poll", "http://account.xiaomi.com/poll"])
def test_unexpected_poll_host_is_rejected_before_authentication(qr_client, bad_url):
    qr_client.responses = [Response({"code": 0, "qr": challenge().image_url, "lp": bad_url, "timeout": 300})]
    with pytest.raises(MiFitnessError, match="unexpected account URL"):
        begin_qr_login(qr_client, device_id="test-device")
    assert len(qr_client.calls) == 1


def test_poll_timeout_can_resume_same_challenge(qr_client):
    payload = {
        "code": 0, "userId": "test-user", "cUserId": "test-cuser", "passToken": "test-pass",
        "ssecurity": "test-security", "location": "https://sts-hlth.io.mi.com/healthapp/sts",
    }
    qr_client.responses = [requests.ReadTimeout(), Response(payload), Response()]
    qr_client.session.cookies.set("miothealth_serviceToken", "test-service-token", domain="sts-hlth.io.mi.com")
    result = wait_for_qr_login(qr_client, challenge(), device_id="test-device")
    assert result.service_token == "test-service-token"
    assert result.service_id == "miothealth"
    assert result.user_id == "test-user"
    assert qr_client.calls[0][0] == qr_client.calls[1][0]


def test_unapproved_qr_cannot_produce_auth_state(qr_client):
    qr_client.responses = [Response({"code": 0})]
    with pytest.raises(MiFitnessError, match="complete login session"):
        wait_for_qr_login(qr_client, challenge(), device_id="test-device")


def test_sts_cannot_send_session_to_unrelated_host(qr_client):
    qr_client.responses = [Response({
        "code": 0, "userId": "test-user", "cUserId": "test-cuser", "passToken": "test-pass",
        "ssecurity": "test-security", "location": "https://other.example/sts",
    })]
    with pytest.raises(MiFitnessError, match="unexpected STS URL"):
        wait_for_qr_login(qr_client, challenge(), device_id="test-device")
    assert len(qr_client.calls) == 1


def test_expired_qr_never_calls_login_endpoint(qr_client):
    expired = QrChallenge(challenge().image_url, challenge().polling_url, time.monotonic() - 1)
    with pytest.raises(MiFitnessError, match="过期"):
        wait_for_qr_login(qr_client, expired, device_id="test-device")
    assert qr_client.calls == []


def test_non_image_qr_response_is_rejected(qr_client):
    qr_client.responses = [Response(content=b"<html>Login failed</html>")]
    with pytest.raises(MiFitnessError, match="valid QR image"):
        download_qr_image(qr_client, challenge())
