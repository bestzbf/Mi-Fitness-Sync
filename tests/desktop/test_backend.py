from __future__ import annotations

import json

import pytest
import requests

from mi_fitness_sync.desktop import backend
from mi_fitness_sync.exceptions import XiaomiApiError, XiaomiLoginRequiredError


@pytest.mark.parametrize("failure,expected,requires_login", [
    (XiaomiApiError("Rejected", http_status=401), "HTTP 401", False),
    (XiaomiApiError("Unavailable", http_status=503), "HTTP 503", False),
    (XiaomiApiError("Unknown", payload={"sensitive": "never-print-this-token"}), "响应异常", False),
    (XiaomiApiError("API error", code=123), "错误码 123", False),
    (XiaomiLoginRequiredError("Account rejected", code=70016), "重新验证", True),
    (requests.Timeout("https://example.test/?serviceToken=never-print-this-token"), "连接小米云端失败", False),
])
def test_errors_distinguish_reauthentication_from_network_and_server_failure(monkeypatch, capsys, failure, expected, requires_login):
    class Service:
        def initialize(self, root):
            pass

        def refresh(self, days, emit):
            raise failure

    monkeypatch.setattr(backend, "DesktopService", Service)
    assert backend.main(["refresh"]) == 1
    output = capsys.readouterr().out
    event = json.loads(output)
    assert expected in event["message"]
    assert bool(event.get("login_needed")) is requires_login
    assert "None" not in output
    assert "never-print-this-token" not in output
