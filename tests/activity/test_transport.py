from __future__ import annotations

from dataclasses import replace

import pytest
import requests

from mi_fitness_sync.activity import transport
from mi_fitness_sync.exceptions import XiaomiApiError, XiaomiLoginRequiredError


@pytest.fixture
def request_scenario(monkeypatch, auth_state):
    calls, encrypted, decrypted, refreshed = [], [], [], []
    session = requests.Session()
    fresh = replace(auth_state, service_token="renewed-token", ssecurity="renewed-security")
    statuses = [401, 200]
    nonces = iter(["first-nonce", "second-nonce"])
    payload = {"code": 0, "result": {}}

    def renew(state):
        refreshed.append(state)
        return fresh

    def get(endpoint, **kwargs):
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = statuses.pop(0)
        response._content = b"synthetic-cloud-response"
        return response

    def encrypt(**kwargs):
        encrypted.append(kwargs)
        return {"nonce": kwargs["nonce"], "signed_for": kwargs["ssecurity"]}

    def decrypt(text, nonce, security):
        decrypted.append((nonce, security))
        return payload

    monkeypatch.setattr(session, "get", get)
    monkeypatch.setattr(transport, "generate_nonce", lambda _: next(nonces))
    monkeypatch.setattr(transport, "encrypt_query_params", encrypt)
    monkeypatch.setattr(transport, "decrypt_response_payload", decrypt)
    client = transport.ActivityTransport(session, auth_state, timeout=30, country_code="CN", auth_refresher=renew)
    return {"client": client, "calls": calls, "encrypted": encrypted, "decrypted": decrypted, "refreshed": refreshed, "statuses": statuses, "payload": payload, "fresh": fresh}


def send(client):
    return client.request_json(endpoint=transport.ACTIVITY_LIST_ENDPOINT, path="/app/v1/data/get_sport_records_by_time", request_payload={"limit": 1}, request_label="Activities")


def test_401_renews_once_and_reencrypts_retry_with_matching_key(request_scenario, auth_state):
    data = request_scenario
    assert send(data["client"])["code"] == 0
    assert data["refreshed"] == [auth_state]
    assert data["encrypted"][0]["ssecurity"] == auth_state.ssecurity
    assert data["encrypted"][1]["ssecurity"] == data["fresh"].ssecurity
    assert data["encrypted"][0]["nonce"] != data["encrypted"][1]["nonce"]
    assert "serviceToken=service-token" in data["calls"][0]["headers"]["Cookie"]
    assert "serviceToken=renewed-token" in data["calls"][1]["headers"]["Cookie"]
    assert data["decrypted"] == [("second-nonce", "renewed-security")]


def test_second_401_stops_without_refresh_loop(request_scenario):
    data = request_scenario
    data["statuses"][:] = [401, 401]
    with pytest.raises(XiaomiApiError) as error:
        send(data["client"])
    assert error.value.http_status == 401
    assert len(data["calls"]) == 2
    assert len(data["refreshed"]) == 1


@pytest.mark.parametrize("status", [400, 403, 429, 500, 503])
def test_other_http_failures_do_not_refresh_credentials(request_scenario, status):
    data = request_scenario
    data["statuses"][:] = [status]
    with pytest.raises(XiaomiApiError) as error:
        send(data["client"])
    assert error.value.http_status == status
    assert data["refreshed"] == []
    assert len(data["calls"]) == 1


def test_api_business_error_is_not_treated_as_expired_login(request_scenario):
    data = request_scenario
    data["statuses"][:] = [200]
    data["payload"]["code"] = 123
    with pytest.raises(XiaomiApiError) as error:
        send(data["client"])
    assert error.value.code == 123
    assert data["refreshed"] == []


def test_required_account_verification_is_propagated_without_retry(request_scenario):
    data = request_scenario

    def fail(_):
        raise XiaomiLoginRequiredError("Needs verification")

    data["client"]._auth_refresher = fail
    with pytest.raises(XiaomiLoginRequiredError):
        send(data["client"])
    assert len(data["calls"]) == 1


def test_renewal_cannot_switch_account_for_the_retry(request_scenario):
    data = request_scenario
    data["client"]._auth_refresher = lambda state: replace(data["fresh"], user_id="different-account")
    with pytest.raises(XiaomiApiError, match="does not match"):
        send(data["client"])
    assert len(data["calls"]) == 1
