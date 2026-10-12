from __future__ import annotations

import base64
import os
from dataclasses import replace
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # The desktop app is macOS; keep the auth helper importable elsewhere.
    fcntl = None

from mi_fitness_sync.auth.client import MiFitnessAuthClient, URL_LOGIN
from mi_fitness_sync.auth.state import AuthState, utc_now_iso
from mi_fitness_sync.auth.store import load_state, resolve_state_path, save_state
from mi_fitness_sync.exceptions import XiaomiApiError, XiaomiLoginRequiredError


STS_HOSTS = frozenset({"sts-hlth.io.mi.com", "account.xiaomi.com", "sts.api.account.xiaomi.com"})


def refresh_with_pass_token(state: AuthState, *, client: MiFitnessAuthClient | None = None) -> AuthState:
    """Renew the short health session using the saved account credential, without a password."""
    if not state.pass_token or not state.user_id or not state.device_id:
        raise XiaomiLoginRequiredError("Saved Xiaomi account credential is incomplete.")
    client = client or MiFitnessAuthClient(service_id=state.service_id, timeout=20)
    if client.service_id != state.service_id:
        raise XiaomiApiError("Xiaomi renewal service does not match the saved session.")
    # Start with account credentials only. Never reuse the expired STS serviceToken.
    client.session.cookies.clear()
    for name, value in {
        "passToken": state.pass_token, "userId": state.user_id,
        "cUserId": state.c_user_id, "deviceId": state.device_id,
    }.items():
        client.session.cookies.set(name, value, domain=".account.xiaomi.com", secure=True)
    client.session.cookies.set("sdkVersion", "accountsdk-18.8.15", domain=".xiaomi.com", secure=True)
    response = client.session.get(
        URL_LOGIN, params={"sid": state.service_id, "_json": "true"}, timeout=client.timeout,
    )
    if response.status_code == 401:
        raise XiaomiLoginRequiredError("Xiaomi rejected the saved account credential.", http_status=401)
    response.raise_for_status()
    payload = client._load_json_payload(response.text)
    code = payload.get("code")
    if payload.get("notificationUrl") or payload.get("captchaUrl") or code in (70016, 81003, 87001):
        raise XiaomiLoginRequiredError("Xiaomi requires account verification.", code=code)
    if code != 0:
        raise XiaomiApiError("Xiaomi account renewal failed.", code=code)
    user_id = payload.get("userId") or client._cookie_value(response.cookies, "userId") or state.user_id
    if str(user_id) != state.user_id:
        raise XiaomiApiError("Xiaomi renewal returned a different account.")
    location = payload.get("location")
    security = payload.get("ssecurity") or client._extension_value(response, "ssecurity")
    if not isinstance(location, str) or not isinstance(security, str) or not security:
        if payload.get("_sign"):
            raise XiaomiLoginRequiredError("Xiaomi requires a new account login.")
        raise XiaomiApiError("Xiaomi renewal returned an incomplete session.")
    parsed = urlsplit(location)
    if parsed.scheme != "https" or parsed.hostname not in STS_HOSTS or parsed.username or parsed.password:
        raise XiaomiApiError("Xiaomi renewal returned an unexpected STS URL.")
    try:
        if not base64.b64decode(security, validate=True):
            raise ValueError("empty security key")
    except ValueError as exc:
        raise XiaomiApiError("Xiaomi renewal returned an invalid security key.") from exc
    nonce = payload.get("nonce")
    if nonce is None:
        nonce = client._extension_value(response, "nonce")
    params = None
    if nonce is not None:
        params = {
            "clientSign": client._generate_client_sign(nonce=str(nonce), ssecurity=security),
            "_userIdNeedEncrypt": "true",
        }
    # The first STS response sets the token. Account cookies remain scoped to Xiaomi
    # Account, and credentials are never followed through an unrelated redirect.
    sts = client.session.get(location, params=params, timeout=client.timeout, allow_redirects=False)
    sts.raise_for_status()
    service_token = client._extract_service_token(sts)
    pass_token = (
        payload.get("passToken") or client._cookie_value(response.cookies, "passToken")
        or client._cookie_value(client.session.cookies, "passToken") or state.pass_token
    )
    c_user_id = payload.get("cUserId") or client._cookie_value(response.cookies, "cUserId") or state.c_user_id
    return replace(
        state, service_token=service_token, ssecurity=security,
        c_user_id=str(c_user_id), pass_token=str(pass_token),
        psecurity=payload.get("psecurity") or state.psecurity,
        auto_login_url=location, slh=client._read_sts_cookie(sts, f"{state.service_id}_slh"),
        ph=client._read_sts_cookie(sts, f"{state.service_id}_ph"),
        sts_cookie_header=client._build_cookie_header(sts), cookies=client._serialize_cookies(),
        updated_at=utc_now_iso(),
    )


def refresh_saved_state(state: AuthState, state_path: str | None = None) -> AuthState:
    """Serialize renewal and atomically replace token and its matching security key."""
    path = resolve_state_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(path.suffix + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_state(str(path))
        if current is None:
            raise XiaomiLoginRequiredError("Saved Xiaomi login was removed.")
        if current.user_id != state.user_id or current.service_id != state.service_id:
            raise XiaomiLoginRequiredError("Saved Xiaomi account has changed.")
        if (current.service_token, current.ssecurity) != (state.service_token, state.ssecurity):
            return current  # Another client already renewed; use the entire matching pair.
        renewed = refresh_with_pass_token(current)
        save_state(renewed, str(path))
        return renewed
