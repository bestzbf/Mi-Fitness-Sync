from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from mi_fitness_sync.auth.client import ACCOUNT_BASE, LoginSession, MiFitnessAuthClient
from mi_fitness_sync.exceptions import MiFitnessError, XiaomiApiError


@dataclass(frozen=True)
class QrChallenge:
    image_url: str
    polling_url: str
    expires_at: float


def _account_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not (
        parsed.hostname == "account.xiaomi.com"
        or (parsed.hostname or "").endswith(".account.xiaomi.com")
    ):
        raise MiFitnessError("Xiaomi QR login returned an unexpected account URL.")
    return url


def begin_qr_login(client: MiFitnessAuthClient, *, device_id: str) -> QrChallenge:
    client.session.cookies.set("deviceId", device_id, domain="account.xiaomi.com")
    response = client.session.get(
        f"{ACCOUNT_BASE}/longPolling/loginUrl",
        params={
            "_qrsize": "480",
            "qs": f"%3Fsid%3D{client.service_id}%26_json%3Dtrue",
            "callback": "https://sts-hlth.io.mi.com/healthapp/sts",
            "_hasLogo": "false", "sid": client.service_id,
            "serviceParam": "", "_locale": "zh_CN",
        },
        timeout=client.timeout,
    )
    response.raise_for_status()
    payload = client._load_json_payload(response.text)
    client._raise_for_login_error(payload)
    image_url = payload.get("qr")
    polling_url = payload.get("lp")
    if not isinstance(image_url, str) or not isinstance(polling_url, str):
        raise MiFitnessError("Xiaomi did not provide a QR login challenge.")
    timeout = payload.get("timeout")
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
        raise MiFitnessError("Xiaomi returned an invalid QR login expiry.")
    return QrChallenge(
        image_url=_account_url(image_url), polling_url=_account_url(polling_url),
        expires_at=time.monotonic() + timeout,
    )


def download_qr_image(client: MiFitnessAuthClient, challenge: QrChallenge) -> bytes:
    response = client.session.get(_account_url(challenge.image_url), timeout=client.timeout)
    response.raise_for_status()
    data = response.content
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        raise MiFitnessError("Xiaomi did not return a valid QR image.")
    return data


def wait_for_qr_login(
    client: MiFitnessAuthClient, challenge: QrChallenge, *, device_id: str,
) -> LoginSession:
    while time.monotonic() < challenge.expires_at:
        remaining = challenge.expires_at - time.monotonic()
        try:
            response = client.session.get(
                _account_url(challenge.polling_url), timeout=(10, min(45, remaining)),
            )
        except requests.ReadTimeout:
            continue
        response.raise_for_status()
        payload = client._load_json_payload(response.text)
        client._raise_for_login_error(payload)
        fields = {key: payload.get(key) for key in ("userId", "cUserId", "passToken", "ssecurity", "location")}
        if not all(fields.values()):
            raise XiaomiApiError("Xiaomi QR approval did not include a complete login session.")
        location = fields["location"]
        if not isinstance(location, str):
            raise XiaomiApiError("Xiaomi QR approval returned an invalid STS URL.")
        parsed = urlsplit(location)
        if parsed.scheme != "https" or parsed.hostname not in {
            "sts-hlth.io.mi.com", "account.xiaomi.com", "sts.api.account.xiaomi.com",
        }:
            raise XiaomiApiError("Xiaomi QR approval returned an unexpected STS URL.")
        nonce = payload.get("nonce") or client._extension_value(response, "nonce")
        if nonce is not None:
            sts = client._follow_sts(auto_login_url=location, nonce=str(nonce), ssecurity=str(fields["ssecurity"]))
        else:
            sts = client.session.get(location, timeout=client.timeout, allow_redirects=True)
            sts.raise_for_status()
        service_token = client._extract_service_token(sts)
        return LoginSession(
            email=str(fields["userId"]), user_id=str(fields["userId"]),
            c_user_id=str(fields["cUserId"]), service_id=client.service_id,
            pass_token=str(fields["passToken"]), service_token=service_token,
            ssecurity=str(fields["ssecurity"]), psecurity=payload.get("psecurity"),
            auto_login_url=location, device_id=device_id,
            slh=client._read_sts_cookie(sts, f"{client.service_id}_slh"),
            ph=client._read_sts_cookie(sts, f"{client.service_id}_ph"),
            sts_cookie_header=client._build_cookie_header(sts), cookies=client._serialize_cookies(),
        )
    raise MiFitnessError("二维码已过期，请重新生成二维码后扫码确认登录。")
