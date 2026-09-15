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
import threading
from typing import Awaitable, Callable, TypeVar

from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.model import SaicApiConfiguration

import store

log = logging.getLogger("yemot-bridge.saic_client")

T = TypeVar("T")

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="saic-asyncio").start()

_clients: dict[int, SaicApi] = {}
_logged_in: set[int] = set()
_state_lock = threading.Lock()


def run_async(coro: Awaitable[T], timeout: float = 20.0) -> T:
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


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


def call(user: store.User, action: Callable[[SaicApi], Awaitable[T]]) -> T:
    """
    Run one authenticated SaicApi call for this user: logs in first if this
    is the first call for them in this process, and retries once after a
    fresh login if the call raises (covers token expiry) - simpler and more
    robust than trying to predict/track token expiry ourselves.
    """
    client = _get_client(user)
    with _state_lock:
        already_logged_in = user.id in _logged_in
    if not already_logged_in:
        run_async(client.login())
        with _state_lock:
            _logged_in.add(user.id)

    try:
        return run_async(action(client))
    except Exception:
        log.warning("SaicApi call failed for user %s, retrying after re-login", user.id)
        run_async(client.login())
        return run_async(action(client))
