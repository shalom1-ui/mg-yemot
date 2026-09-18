"""
On-demand Chery/Jaecoo/Omoda cloud API access (all three brands share one
backend - Chery Group's "legend" BFF, aka "Chery Europe" / CarLinko).

Ported (MIT license), with attribution, from
Przemko92/chery-ha-integration - https://github.com/Przemko92/chery-ha-integration
(auth.py, api.py, signing.py, tsp_sign.py, crypto.py, vehicle_commands.py) -
a byte-verified reverse-engineering of the real app's protocol. Trimmed for
this project's needs: email login only (the SMS path needs TLS-fingerprint
evasion to get past a WAF - only the email path is ported, since our signup
form is email-based anyway), no MQTT (not needed - see saic_client.py's own
docstring for why on-demand HTTP beats a live broker connection for a
phone-triggered command), no Home Assistant coordinator/entity glue.

Unlike MG (bare username+password), a Chery/Jaecoo/Omoda login is a genuine
two-step, interactive flow:
  1. request_email_code(email) - solves a slide-puzzle captcha
     (chery_captcha.py) and asks the cloud to email a one-time code.
  2. login(email, code) - the caller supplies the code they received; this
     SM4-encrypts it and exchanges it for an access/refresh token pair.
Every remote *command* also needs the account's own in-app security PIN
(distinct from our own Yemot call-PIN) - a per-vehicle "taskId" must be
minted via a PIN-protected checkPassword call before a command is accepted.

Architecture mirrors saic_client.py: one background thread runs a private
asyncio event loop, `call()`/`enqueue()` submit coroutines to it, and
commands are serialized per user via a lock - the same reliability lessons
learned building the MG side apply identically here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import aiohttp

import store
from chery_captcha import solve_captcha

log = logging.getLogger("yemot-bridge.chery_client")

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Constants (from the app's own decompiled code / defaultEnv bootstrap)
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://eu-chery.cheryinternational.com"
DEFAULT_ENV_URL = f"{DEFAULT_BASE_URL}/api/tsp/v1/app/env/defaultEnv"
DEFAULT_TSP_HOST = "https://tspconsole-eu.cheryinternational.com"
LOGIN_ENDPOINT = "/api/auth/oauth2/token"
SEND_MAIL_CODE_ENDPOINT = "/api/marketing/v2/app/code/sendMailCode"
API_TSP_LOGIN_PATH = "/api/tsp/v1/app/auth/login"
API_VMC_QUERY_LIST_PATH = "/api/tsp/v1/app/vmc/queryList"
API_VMC_SET_VEC_DEFAULT_PATH = "/api/tsp/v1/app/vmc/setVecDefault"
API_CPM_CHECK_PASSWORD_PATH = "/api/tsp/v1/app/cpm/checkPassword"
DEFAULT_CHANNEL_ID = 5
DEFAULT_USER_AGENT = "CheryEurope/1.0.4 Flutter/Dio"
LOGIN_EMAIL_PREFIX = "APP-LOGIN@"
BASIC_AUTH = "Basic bGVnZW5kQXBwOmxlZ2VuZEFwcA=="  # base64("legendApp:legendApp")
TSP_CODE_OK = "000000"
TASK_ID_TTL_SECONDS = 600

# BFF request signing (distinct from the marketing/captcha signing in chery_captcha.py)
SIGN_SECRET = "cX5fR8lJ6pK2xD4uH1eK4pY6wA4xO0sK"
SIGN_NONCE = "chery_legend_h5"
MARKETING_SIGN_SECRET = "5c7af05e6fbf562842ef483ee96e06a0"
MARKETING_SIGN_NONCE = "chery_legend_marketing"

# SM4-ECB PKCS7 - fixed 16-byte key hardcoded in the app, shared by the
# whole Chery/Omoda/Jaecoo "legend" BFF login flow.
SM4_LOGIN_KEY = b"mHU80av2zFtf4OY6"

# TSP request signing (tspconsole vehicle-control endpoints)
_TSP_APP_ID = "eu-1"
_TSP_APP_SECRET = "EBUJPYr7oDd48C9Te9c755942Y7T48dV293Y4Z931J098X41aYf0"
_TSP_HALF_SECRET = "".join(c for i, c in enumerate(_TSP_APP_SECRET) if i % 2 == 0)


class CheryError(Exception):
    """Any Chery/Jaecoo/Omoda API rejection or transport failure."""


class CheryAuthError(CheryError):
    """Login/token/captcha failure."""


# ---------------------------------------------------------------------------
# Crypto (SM4 login-code encryption, TSP PIN encryption)
# ---------------------------------------------------------------------------
def _sm4_encrypt_ecb_pkcs7(plaintext: str, key: bytes = SM4_LOGIN_KEY) -> str:
    import base64

    from gmssl.sm4 import SM4_ENCRYPT, CryptSM4

    cipher = CryptSM4()
    cipher.set_key(key, SM4_ENCRYPT)
    ciphertext = cipher.crypt_ecb(plaintext.encode("utf-8"))
    return base64.b64encode(ciphertext).decode("ascii")


def _encrypt_command_pin(pin: str) -> str:
    digest = hashlib.md5(pin.encode("utf-8"), usedforsecurity=False).hexdigest()
    return _sm4_encrypt_ecb_pkcs7(digest.ljust(32))


# ---------------------------------------------------------------------------
# BFF request signing (identity headers for /api/tsp, /api/auth, ... calls)
# ---------------------------------------------------------------------------
_API_PREFIX_RE = re.compile(r"^/api/[^/]+/")


def _strip_api_prefix(path: str) -> str:
    if path.startswith("/api/auth/"):
        return path[len("/api"):]
    if path.startswith("/api/marketing/"):
        return path[len("/api/"):]
    return _API_PREFIX_RE.sub("/", path, count=1)


def _identity_headers(path: str) -> dict[str, str]:
    ts = int(time.time() * 1000)
    url_header = _strip_api_prefix(path)
    base = f"{SIGN_SECRET}{SIGN_NONCE}{url_header}{ts}"
    signature = hashlib.sha256(base.encode("utf-8"), usedforsecurity=False).hexdigest()
    return {
        "signature": signature, "nonce": SIGN_NONCE, "url": url_header,
        "timestamp": str(ts), "contentType": "application/json; charset=UTF-8",
        "agent": "android", "version": "1.0.6", "DEPT-ID": "48",
        "TENANT-ID": "300001", "TENANT-CODE": "300001", "CLIENT-TOC": "Y",
    }


def _marketing_v2_headers(url_header: str) -> dict[str, str]:
    ts = int(time.time() * 1000)
    base = f"{MARKETING_SIGN_SECRET}{MARKETING_SIGN_NONCE}{url_header}{ts}"
    signature = hashlib.md5(base.encode("utf-8"), usedforsecurity=False).hexdigest()
    return {
        "signature": signature, "nonce": MARKETING_SIGN_NONCE, "url": url_header,
        "timestamp": str(ts), "contentType": "application/x-www-form-urlencoded",
        "agent": "android", "version": "1.0.6", "DEPT-ID": "48",
        "TENANT-ID": "300001", "TENANT-CODE": "300001", "CLIENT-TOC": "Y",
    }


# ---------------------------------------------------------------------------
# TSP request signing (tspconsole vehicle-control endpoints - a DIFFERENT
# scheme from the BFF one above)
# ---------------------------------------------------------------------------
def _tsp_flatten(obj: dict[str, Any]) -> dict[str, Any]:
    return obj


def _tsp_build_sign(params: dict[str, Any], timestamp_ms: int) -> str:
    import base64

    parts = []
    for key in sorted(params.keys()):
        value = params[key]
        if value in (None, ""):
            continue
        parts.append(f"{key}={value}&")
    base = "".join(parts) + f"secretKey={_TSP_HALF_SECRET}&timestamp={timestamp_ms}"
    digest = hashlib.sha256(base.encode("utf-8"), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode().upper()


def _tsp_sign_body(body_params: dict[str, Any], timestamp_ms: int) -> dict[str, Any]:
    body = dict(body_params)
    body["appId"] = _TSP_APP_ID
    body["sign"] = _tsp_build_sign(body, timestamp_ms)
    return body


# ---------------------------------------------------------------------------
# Command catalog - id -> (tspconsole endpoint, body builder). Subset of
# Przemko92/chery-ha-integration's vehicle_commands.py covering this
# project's menu (AC, lock/unlock, seat heat, front defrost, find car).
# ---------------------------------------------------------------------------
def _air_control_body(on: bool) -> dict[str, str]:
    return {"airControlType": "1" if on else "0", "airType": "1", "temperature": "22.0", "times": "15"}


def _lock_control_body(unlock: bool) -> dict[str, str]:
    return {"lockType": "1" if unlock else "0"}


def _seat_control_body(on: bool) -> dict[str, str]:
    body = {"mSeatHeating": "3" if on else "0"}
    if on:
        body["times"] = "15"
    return body


def _front_windshield_body(on: bool) -> dict[str, str]:
    body = {"frontWindshieldHeat": "1" if on else "0"}
    if on:
        body["times"] = "15"
    return body


def _liftgate_body(open_: bool) -> dict[str, str]:
    return {"controlType": "1" if open_ else "0"}


COMMAND_SPECS: dict[str, tuple[str, Callable[[], dict[str, Any]]]] = {
    "ac_on": ("airControl", lambda: _air_control_body(True)),
    "ac_off": ("airControl", lambda: _air_control_body(False)),
    "lock": ("lockControl", lambda: _lock_control_body(False)),
    "unlock": ("lockControl", lambda: _lock_control_body(True)),
    "seat_heat_on": ("seatControl", lambda: _seat_control_body(True)),
    "seat_heat_off": ("seatControl", lambda: _seat_control_body(False)),
    "front_defrost": ("frontWindshieldControl", lambda: _front_windshield_body(True)),
    "find_car": ("findCar", lambda: {}),
    "trunk": ("powerLiftgateControl", lambda: _liftgate_body(True)),
}


# ---------------------------------------------------------------------------
# The client itself
# ---------------------------------------------------------------------------
@dataclass
class _Session:
    access_token: str | None = None
    refresh_token: str | None = None
    t_user_id: str | None = None
    user_token: str | None = None
    task_ids: dict[str, tuple[str, float]] | None = None

    def __post_init__(self):
        if self.task_ids is None:
            self.task_ids = {}


class CheryClient:
    """One instance per user - holds its own aiohttp session + token state."""

    def __init__(self, http: aiohttp.ClientSession) -> None:
        self._http = http
        self.session = _Session()

    # -- OAuth-tier (BFF) --------------------------------------------------
    async def request_email_code(self, email: str) -> None:
        """Solve the captcha and ask the cloud to email a one-time login code."""
        verification = await solve_captcha(self._http, base_url=DEFAULT_BASE_URL)
        if not verification:
            raise CheryAuthError("Captcha solving failed")
        headers = _marketing_v2_headers("/marketing/v2/app/code/sendMailCode")
        headers["Authorization"] = BASIC_AUTH
        body = {"email": email, "module": "APP-LOGIN", "captchaVerification": verification}
        data = await self._post_form(SEND_MAIL_CODE_ENDPOINT, body, headers)
        if not (data.get("ok") or data.get("key") == "operation.successful"):
            raise CheryAuthError(f"Failed to send login code: {data.get('msg') or data.get('key')}")

    async def login(self, email: str, code: str) -> None:
        """Exchange an email + one-time code for an access/refresh token pair."""
        encrypted_code = _sm4_encrypt_ecb_pkcs7(code)
        params = {
            "email": f"{LOGIN_EMAIL_PREFIX}{email}", "code": encrypted_code,
            "needDecode": "0", "grant_type": "email", "scope": "server",
            "loginType": "email", "loginAction": "1",
        }
        headers = {
            "contentType": "application/json; charset=UTF-8", "agent": "android",
            "version": "1.0.6", "DEPT-ID": "48", "TENANT-ID": "300001",
            "TENANT-CODE": "300001", "CLIENT-TOC": "Y",
            "Content-Type": "application/json; charset=UTF-8", "Authorization": BASIC_AUTH,
        }
        url = f"{DEFAULT_BASE_URL}{LOGIN_ENDPOINT}"
        async with self._http.post(url, params=params, json={}, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 401:
                raise CheryAuthError("Login rejected - wrong or expired code")
            if resp.status >= 400:
                raise CheryAuthError(f"Login failed (HTTP {resp.status})")
            data = await resp.json(content_type=None)
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        access_token = payload.get("accessToken") or payload.get("access_token")
        if not access_token:
            raise CheryAuthError("Login response had no access token")
        self.session.access_token = access_token
        self.session.refresh_token = payload.get("refreshToken") or payload.get("refresh_token")

    async def refresh_access_token(self, refresh_token: str) -> str | None:
        """
        Exchange a refresh token for a new access token, WITHOUT needing a
        fresh OTP - the only re-auth path available after the initial login,
        since (unlike MG) there's no stored password to log back in with.
        Returns the NEW refresh token if the caller must persist it (Chery
        rotates the refresh token on every use per the app's own behavior -
        the old one stops working immediately after), or None if the
        response didn't include a new one (kept the same one, rare).
        """
        params = {"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": "server"}
        headers = {
            "contentType": "application/x-www-form-urlencoded", "agent": "android",
            "version": "1.0.6", "DEPT-ID": "48", "TENANT-ID": "300001",
            "TENANT-CODE": "300001", "CLIENT-TOC": "Y",
            "Content-Type": "application/x-www-form-urlencoded", "Authorization": BASIC_AUTH,
        }
        url = f"{DEFAULT_BASE_URL}{LOGIN_ENDPOINT}"
        async with self._http.post(url, data=params, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status >= 400:
                raise CheryAuthError(f"Token refresh failed (HTTP {resp.status}) - needs a fresh login code")
            data = await resp.json(content_type=None)
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        access_token = payload.get("accessToken") or payload.get("access_token")
        if not access_token:
            raise CheryAuthError("Token refresh response had no access token")
        self.session.access_token = access_token
        self.session.t_user_id = None  # TSP session tied to the old access token
        self.session.user_token = None
        new_refresh = payload.get("refreshToken") or payload.get("refresh_token")
        if new_refresh:
            self.session.refresh_token = new_refresh
        return new_refresh

    async def _post_form(self, endpoint: str, body: dict, headers: dict) -> dict:
        url = f"{DEFAULT_BASE_URL}{endpoint}"
        async with self._http.post(url, data=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status >= 400:
                raise CheryAuthError(f"Request to {endpoint} failed (HTTP {resp.status})")
            data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}

    async def _bff_request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{DEFAULT_BASE_URL}{path}"
        headers = {"Accept": "application/json", "Content-Type": "application/json", **_identity_headers(path)}
        if self.session.access_token:
            headers["Authorization"] = f"Bearer {self.session.access_token}"
        async with self._http.request(method, url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=20), **kwargs) as resp:
            if resp.status == 401:
                raise CheryAuthError("return code: 401, Chery session expired")
            if resp.status >= 400:
                raise CheryError(f"Chery API {path} returned HTTP {resp.status}")
            if resp.status == 204:
                return None
            return await resp.json(content_type=None)

    # -- TSP-tier (vehicle data/control) ------------------------------------
    async def tsp_login(self) -> None:
        response = await self._bff_request(
            "POST", API_TSP_LOGIN_PATH, json={"channelId": DEFAULT_CHANNEL_ID},
        )
        payload = response.get("data") if isinstance(response, dict) else None
        if not isinstance(payload, dict) or payload.get("tUserId") is None:
            raise CheryAuthError("TSP login did not return tUserId")
        self.session.t_user_id = str(payload["tUserId"])
        self.session.user_token = str(payload.get("userToken") or "") or None

    async def get_vehicle_list(self) -> list[dict]:
        if not self.session.t_user_id:
            await self.tsp_login()
        response = await self._bff_request(
            "POST", API_VMC_QUERY_LIST_PATH,
            json={"tUserId": self.session.t_user_id, "channelId": DEFAULT_CHANNEL_ID},
        )
        data = response.get("data") if isinstance(response, dict) else response
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("list", "vehicleList", "vehicles"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    async def _get_task_id(self, vin: str, account_pin: str) -> str:
        cached = (self.session.task_ids or {}).get(vin)
        if cached and cached[1] > time.time():
            return cached[0]
        await self._bff_request("POST", API_VMC_SET_VEC_DEFAULT_PATH, json={"vin": vin})
        response = await self._bff_request(
            "POST", API_CPM_CHECK_PASSWORD_PATH,
            json={
                "vin": vin, "tUserId": self.session.t_user_id, "channelId": DEFAULT_CHANNEL_ID,
                "password": _encrypt_command_pin(account_pin), "needDecode": 0, "scene": 0, "type": 0,
            },
        )
        payload = response.get("data") if isinstance(response, dict) else None
        task_id = (payload or {}).get("taskId") or (response or {}).get("taskId")
        if not task_id:
            msg = (response or {}).get("msg") or (response or {}).get("key")
            raise CheryAuthError(msg or "Chery rejected the vehicle control PIN")
        self.session.task_ids[vin] = (str(task_id), time.time() + TASK_ID_TTL_SECONDS)
        return str(task_id)

    async def send_command(self, vin: str, command_id: str, account_pin: str) -> dict:
        if not self.session.t_user_id:
            await self.tsp_login()
        spec = COMMAND_SPECS.get(command_id)
        if spec is None:
            raise CheryError(f"Unsupported command id: {command_id}")
        endpoint, build_body = spec
        task_id = await self._get_task_id(vin, account_pin)
        timestamp_ms = int(time.time() * 1000)
        payload = {
            **build_body(), "clientType": "1", "seq": f"{vin}-{timestamp_ms}",
            "taskId": task_id, "vin": vin,
        }
        body = _tsp_sign_body(payload, timestamp_ms)
        headers = {
            "Authorization": self.session.user_token or "", "timestamp": str(timestamp_ms),
            "x-TenantId": "", "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json, text/plain, */*", "User-Agent": "okhttp/4.9.0",
            "version": "1.0.6", "agent": "android",
        }
        url = f"{DEFAULT_TSP_HOST}/asc/vehicleControl/{endpoint}"
        async with self._http.post(url, data=json.dumps(body), headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status in (401, 424):
                raise CheryAuthError("TSP authentication failed")
            if resp.status >= 400:
                raise CheryError(f"Chery vehicle control returned HTTP {resp.status}")
            response = await resp.json(content_type=None)
        code = response.get("code") if isinstance(response, dict) else None
        ok = code in (TSP_CODE_OK, "0", 0) or (isinstance(response, dict) and response.get("ok") is True)
        if not ok:
            raise CheryError(f"Command {command_id} rejected: {response}")
        return response


# ---------------------------------------------------------------------------
# Shared asyncio loop + per-user client cache/lock, mirroring saic_client.py
# ---------------------------------------------------------------------------
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="chery-asyncio").start()

_http_session: aiohttp.ClientSession | None = None
_clients: dict[int, CheryClient] = {}
_state_lock = threading.Lock()
_call_locks: dict[int, threading.Lock] = {}
_call_locks_guard = threading.Lock()


class Busy(Exception):
    """Raised by call() when another command for the same user is still in progress."""


def run_async(coro: Awaitable[T], timeout: float = 20.0) -> T:
    """See saic_client.run_async()'s docstring - same cancel-on-timeout fix applies here."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise


async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


def _get_call_lock(user_id: int) -> threading.Lock:
    with _call_locks_guard:
        lock = _call_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _call_locks[user_id] = lock
        return lock


def _get_client(user_id: int) -> CheryClient:
    with _state_lock:
        client = _clients.get(user_id)
        if client is None:
            http = run_async(_get_http_session())
            client = CheryClient(http)
            _clients[user_id] = client
        return client


def new_standalone_client() -> CheryClient:
    """A client not cached by user id - used for signup, before a user row exists."""
    http = run_async(_get_http_session())
    return CheryClient(http)


def _refresh_and_persist(user: store.User, client: CheryClient, action_timeout: float) -> None:
    """
    Get a fresh access token from the stored refresh token - the only
    re-auth path after the initial OTP login (no password to fall back on,
    unlike MG). Chery rotates the refresh token on every use, so the new
    one is written back to the DB immediately: losing it would strand this
    user needing a brand new OTP login next time the process restarts.
    """
    refresh_token = user.credentials.get("chery_refresh_token")
    if not refresh_token:
        raise CheryAuthError(f"No stored refresh token for user {user.id}")
    new_refresh = run_async(client.refresh_access_token(refresh_token), timeout=action_timeout)
    if new_refresh and new_refresh != refresh_token:
        user.credentials["chery_refresh_token"] = new_refresh
        store.update_credentials(user.id, user.credentials)


def call(
    user: store.User,
    action: Callable[[CheryClient], Awaitable[T]],
    wait_for_lock: float = 5.0,
    action_timeout: float = 15.0,
) -> T:
    """Run one action for this user, serialized per user - see saic_client.call()
    for the full reasoning (identical design, same lessons learned on MG)."""
    lock = _get_call_lock(user.id)
    if not lock.acquire(timeout=wait_for_lock):
        raise Busy(f"user {user.id} has a command still in progress")
    try:
        client = _get_client(user.id)
        if not client.session.access_token:
            # Freshly-created client for this process: bootstrap it from the
            # stored refresh token instead of starting with no token and
            # failing the first real call.
            _refresh_and_persist(user, client, action_timeout)

        try:
            return run_async(action(client), timeout=action_timeout)
        except CheryAuthError as e:
            log.warning("Chery auth error for user %s (%s), refreshing token", user.id, e)
            _refresh_and_persist(user, client, action_timeout)
            return run_async(action(client), timeout=action_timeout)
        except CheryError as e:
            log.warning("Chery rejected call for user %s: %s", user.id, e)
            raise
    finally:
        lock.release()


def enqueue(user: store.User, action: Callable[[CheryClient], Awaitable[T]]) -> None:
    """Fire-and-forget version of call() - see saic_client.enqueue()."""
    def run():
        try:
            call(user, action, wait_for_lock=120.0, action_timeout=60.0)
        except Exception:
            log.exception("Background Chery command failed for user %s", user.id)
    threading.Thread(target=run, daemon=True, name=f"chery-bg-{user.id}").start()
