"""
On-demand MG cloud API access, direct via saic_ismart_client_ng - no MQTT,
no per-account gateway process. One SaicApi instance is kept per user (so we
don't re-authenticate on every button press), created lazily on first use.

saic_ismart_client_ng is async; Flask here is synchronous. Rather than
rewrite the app as async, one background thread runs a single asyncio event
loop for the whole process's lifetime, and `call()` submits coroutines to it
via run_coroutine_threadsafe() and blocks for the result - the same
"one background thread" shape the old mg.py used for its MQTT connection,
just doing HTTP calls to MG's cloud instead of holding a broker connection
open.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Awaitable, Callable, TypeVar

from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.exceptions import SaicApiException
from saic_ismart_client_ng.model import SaicApiConfiguration

import store

log = logging.getLogger("yemot-bridge.saic_client")

T = TypeVar("T")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="saic-asyncio").start()

_clients: dict[int, SaicApi] = {}
_logged_in: set[int] = set()
_state_lock = threading.Lock()

_call_locks: dict[int, threading.Lock] = {}
_call_locks_guard = threading.Lock()


class Busy(Exception):
    """Raised by call() when another command for the same user is still
    being processed and the wait_for_lock window ran out - see call()."""


def _is_auth_error(e: SaicApiException) -> bool:
    """
    True for a SaicApiException that's a plain expired/missing auth token
    (HTTP-style 401/403 - "Token missing, Authentication failed!" is what
    a stale cached login actually looks like, found via live testing
    2026-09-16) - the one kind of SaicApiException a fresh login CAN fix,
    unlike a business-logic rejection like "too frequent operations" or
    "another command in progress". SaicApiException doesn't expose the
    numeric return code as its own attribute, only baked into the message
    string, hence the regex.
    """
    m = re.search(r"return code:\s*(\d+)", str(e))
    return m is not None and m.group(1) in ("401", "403")


def _get_call_lock(user_id: int) -> threading.Lock:
    with _call_locks_guard:
        lock = _call_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _call_locks[user_id] = lock
        return lock


def run_async(coro: Awaitable[T], timeout: float = 20.0) -> T:
    """
    Run one coroutine on the shared event loop and wait up to `timeout`.

    Bug found 2026-09-16 via live testing: `Future.result(timeout=...)`
    raising TimeoutError only stops *waiting* - it does NOT cancel the
    coroutine, which keeps running on the event loop regardless. call()'s
    retry-after-failure logic would then submit a SECOND, genuinely
    concurrent request for the same command while the first (abandoned
    but still alive) one was still in flight - almost certainly what
    triggered MG's own "Too frequent operations" rejection, not real
    external rate limiting. Explicitly cancelling on timeout is required
    before it's safe to retry anything.
    """
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise


def _get_client(user: store.User) -> SaicApi:
    with _state_lock:
        client = _clients.get(user.id)
        if client is None:
            config = SaicApiConfiguration(
                username=user.mg_email,
                password=user.mg_password,
                base_uri=user.saic_base_uri,
                region=user.saic_region,
                tenant_id=user.saic_tenant_id,
            )
            client = SaicApi(config)
            _clients[user.id] = client
        return client


def validate_credentials(user: store.User) -> SaicApi:
    """
    Build a standalone SaicApi client and log in - used only at signup time
    to validate a submitted account (and discover its VIN) before anything
    is saved. Deliberately NOT cached in _clients/_logged_in: the signup
    form's user record uses a placeholder id (0) that would otherwise
    collide with the legacy env-var account's real id (also 0), wiping out
    its live cached session every time someone fills in the signup form.
    """
    config = SaicApiConfiguration(
        username=user.mg_email,
        password=user.mg_password,
        base_uri=user.saic_base_uri,
        region=user.saic_region,
        tenant_id=user.saic_tenant_id,
    )
    client = SaicApi(config)
    run_async(client.login())
    return client


def call(
    user: store.User,
    action: Callable[[SaicApi], Awaitable[T]],
    wait_for_lock: float = 5.0,
    action_timeout: float = 12.0,
) -> T:
    """
    Run one authenticated SaicApi call for this user: logs in first if this
    is the first call for them in this process, and retries once after a
    fresh login if the call raises (covers token expiry) - simpler and more
    robust than trying to predict/track token expiry ourselves.

    Serialized per user via a lock: real testing found a fast command
    (unlock) sent shortly after a slow one (AC-on, still running in the
    background via enqueue() below) could silently fail to reach the car -
    MG's own smartphone app avoids this by disabling its buttons until the
    previous command settles server-side; holding a per-user lock for the
    duration of each command has the same effect. If the lock is still
    held after `wait_for_lock` seconds, raises Busy instead of blocking
    the phone call indefinitely - the caller should tell the user to wait
    and try again rather than hang the call.

    `action_timeout` is deliberately short (worst case with the one retry:
    roughly 3x this) - a foreground call blocks the phone's HTTP response,
    and Yemot itself won't wait around forever for us to answer. Slow
    commands should go through enqueue() below instead of raising this.
    """
    lock = _get_call_lock(user.id)
    if not lock.acquire(timeout=wait_for_lock):
        raise Busy(f"user {user.id} has a command still in progress")
    try:
        client = _get_client(user)
        with _state_lock:
            already_logged_in = user.id in _logged_in
        if not already_logged_in:
            run_async(client.login(), timeout=action_timeout)
            with _state_lock:
                _logged_in.add(user.id)

        try:
            return run_async(action(client), timeout=action_timeout)
        except SaicApiException as e:
            if _is_auth_error(e):
                # A plain expired/missing token ("Token missing,
                # Authentication failed!") - the login cached from earlier
                # in the process's life is just stale. A fresh login
                # genuinely fixes this, unlike the business-logic
                # rejections below.
                log.warning(
                    "SaicApi auth error for user %s (%s), retrying after re-login",
                    user.id, e,
                )
                with _state_lock:
                    _logged_in.discard(user.id)
                try:
                    run_async(client.login(), timeout=action_timeout)
                    with _state_lock:
                        _logged_in.add(user.id)
                    return run_async(action(client), timeout=action_timeout)
                except Exception as e2:
                    log.error(
                        "SaicApi call failed again for user %s after re-login (%s: %s)",
                        user.id, type(e2).__name__, e2,
                    )
                    raise
            # Everything else here is a real, structured BUSINESS-LOGIC
            # rejection FROM MG's OWN API - e.g. "Too frequent operations.
            # Please use the physical key to restart the vehicle..." or
            # "Other remote command in progress. Please try again later."
            # Found via live testing (2026-09-16) that blindly retrying on
            # every failure was making these specific errors worse -
            # retrying right after a rate-limit rejection is itself
            # another "too frequent" operation, and no login can fix
            # either of them anyway - propagate MG's own message as-is.
            log.warning("SaicApi rejected call for user %s: %s", user.id, e)
            raise
        except Exception as e:
            log.warning(
                "SaicApi call failed for user %s (%s: %s), retrying after re-login",
                user.id, type(e).__name__, e,
            )
            try:
                run_async(client.login(), timeout=action_timeout)
                return run_async(action(client), timeout=action_timeout)
            except SaicApiException as e2:
                log.warning("SaicApi rejected retry for user %s: %s", user.id, e2)
                raise
            except Exception as e2:
                log.error(
                    "SaicApi call failed again for user %s after re-login (%s: %s)",
                    user.id, type(e2).__name__, e2,
                )
                raise
    finally:
        lock.release()


def enqueue(user: store.User, action: Callable[[SaicApi], Awaitable[T]]) -> None:
    """
    Fire-and-forget version of call() for slow commands (AC-on, front
    defrost): runs in a background thread so the phone can respond
    immediately instead of blocking on cloud confirmation. Goes through
    the same per-user lock as call() (by calling it), with generous
    timeouts since nothing user-facing is waiting on it.
    """
    def run():
        try:
            call(user, action, wait_for_lock=120.0, action_timeout=60.0)
        except Exception:
            log.exception("Background SaicApi command failed for user %s", user.id)
    threading.Thread(target=run, daemon=True, name=f"saic-bg-{user.id}").start()
