"""
Bridge between a Yemot Hamashiach (ימות המשיח) IVR extension and one or more
cars, via a per-brand adapter (see vehicles/).

This file only knows the Yemot IVR protocol and call authentication - it has
NO car-brand-specific logic. Each brand (MG today, Maxus/others later) is a
separate module in vehicles/ implementing the VehicleAdapter interface.

Flow:
  1. Yemot calls this server's /yemot endpoint on every step of the call
     (first hit, then again after each digit the caller presses).
  2. This server replies with a small plain-text "mini language" that Yemot
     understands: id_list_message (play a message), read (ask for digits and
     store them under a variable name), hangup, etc.
  3. Caller enters a PIN, then (if more than one brand is configured on this
     deployment) picks which car, then picks an action from that car's menu.
     Each adapter turns the chosen digit into a real command to the car.

NOTE ON PROTOCOL ACCURACY:
  Fixed 2026-09-15: the original syntax here (based on generic community
  docs, never verified against a real call) was wrong in two ways that
  real testing caught - `read=` needs an `=<varname>` (not `,<varname>`)
  PLUS a full positional options list after it, and `id_list_message` +
  hangup must be ONE combined command via a `.g-hangup` suffix (not a
  separate `hangup=yes` field). Confirmed-correct format copied from
  [[hapinkas-sheli-accounts-app]]'s `backend/src/services/yemot.js` (a
  live, working Yemot API-module integration) - see `read_digits()` and
  `id_list_message_hangup()` below for the exact reproduction. Before this
  fix, every real call got the request through fine (Render logs showed
  200 OK) but the caller heard nothing at all and the same URL got
  re-hit every ~2-3s - Yemot was receiving 200 responses it couldn't
  parse as valid protocol commands, not a connectivity problem.

CONFIGURING WHICH BRANDS ARE ACTIVE:
  Set VEHICLES to a comma-separated list of brand ids, e.g. "mg" or
  "mg,maxus". Defaults to "mg" alone for backward compatibility with the
  original single-car deployment. Each brand only needs its own env vars
  (see .env.example) - unconfigured/irrelevant vars are simply unused by the
  other brands' adapters.
"""

import os
import re
import time
import logging

from flask import Flask, request, Response

from vehicles.registry import ADAPTER_CLASSES

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yemot-bridge")

# ---------------------------------------------------------------------------
# Configuration (from environment / .env)
# ---------------------------------------------------------------------------
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")
YEMOT_PIN = os.environ.get("YEMOT_PIN", "")
ALLOWED_NUMBERS = {
    n.strip() for n in os.environ.get("YEMOT_ALLOWED_NUMBERS", "").split(",") if n.strip()
}

# Render assigns a port dynamically via $PORT - always bind to that if present.
PORT = int(os.environ.get("PORT", os.environ.get("BRIDGE_PORT", "10000")))

# ---------------------------------------------------------------------------
# Vehicle adapters - one instance per brand listed in VEHICLES.
# ---------------------------------------------------------------------------
_brand_ids = [b.strip() for b in os.environ.get("VEHICLES", "mg").split(",") if b.strip()]

adapters = {}
for _brand in _brand_ids:
    _cls = ADAPTER_CLASSES.get(_brand)
    if not _cls:
        log.warning("Unknown brand id %r in VEHICLES (known: %s) - skipping",
                    _brand, ", ".join(ADAPTER_CLASSES))
        continue
    adapters[_brand] = _cls()

if not adapters:
    raise RuntimeError(
        f"No valid brand in VEHICLES={os.environ.get('VEHICLES')!r}. "
        f"Known brands: {', '.join(ADAPTER_CLASSES)}"
    )

SINGLE_BRAND = next(iter(adapters)) if len(adapters) == 1 else None

_HEBREW_DIGIT_WORDS = [
    "אחת", "שתיים", "שלוש", "ארבע", "חמש", "שש", "שבע", "שמונה", "תשע",
]

def vehicle_select_prompt() -> str:
    # Comma, not period, between phrases - see clean()'s comment.
    parts = [
        f"לחץ {_HEBREW_DIGIT_WORDS[i]} עבור {adapter.display_name}"
        for i, adapter in enumerate(adapters.values())
    ]
    return ", ".join(parts)

_brand_by_index = list(adapters.keys())

# ---------------------------------------------------------------------------
# Yemot protocol helpers
# ---------------------------------------------------------------------------
def clean(text: str) -> str:
    # Strip characters that are reserved/structural in Yemot's own mini-
    # language, not just our own "key=value&key=value" framing: "." is used
    # as a command separator (e.g. "id_list_message=t-<text>.g-hangup"), so
    # a period *inside* our own message text can get misparsed as ending
    # the text early. Matches the working reference's sanitizeForYemot()
    # exactly ([.\-"'&|]) plus our own & = \n \r.
    return re.sub(r"[.\-\"'&|=\n\r]", " ", text)

def id_list_message_hangup(text: str) -> str:
    """Play text, then hang up - one combined command (`.g-hangup` suffix)."""
    return f"id_list_message=t-{clean(text)}.g-hangup"

def read_digits(prompt: str, varname: str, digits: int, result_text: str = "") -> str:
    """
    Ask the caller to type an exact-length digit sequence (PIN, a menu
    digit, ...) into `varname`. Optionally prefixes `result_text` (e.g.
    "the AC command was sent.") before the prompt, since combining a
    separate id_list_message + read= into one response was never verified
    and turned out to be one of the things that was silently wrong - one
    read= call, with any feedback folded into its own prompt text, is the
    confirmed-working shape.

    Option order (verified against a live working integration):
      valName, re_enter_if_exists, max_digits, min_digits, sec_wait,
      typing_playback_mode, block_asterisk_key, block_zero_key,
      replace_char, digits_allowed, amount_attempts, allow_empty,
      empty_val, block_change_keyboard
    "No" for typing_playback_mode = don't read back each digit as typed
    (relevant for PINs). block_asterisk_key="no" = "*" still comes through
    as a value (used for the "hang up" menu option).
    re_enter_if_exists="yes" is required: this same varname (e.g.
    "car_choice") gets read over and over in a loop for as long as the
    call lasts, and with "no" Yemot silently reuses the value from the
    FIRST time it was ever collected in this call instead of prompting
    for/reading a fresh digit - every subsequent key press was being
    ignored in favor of the original one (2026-09-15 bug: every key
    acted like the very first choice made in the call).
    """
    text = f"{result_text} {prompt}".strip() if result_text else prompt
    ops = ["yes", str(digits), str(digits), "7", "No", "no", "no", "", "", "", "", "", ""]
    return f"read=t-{clean(text)}={varname},{','.join(ops)}"

def yemot_response(body: str) -> Response:
    log.info("yemot response: %s", body)
    return Response(body, mimetype="text/plain; charset=utf-8")

# ---------------------------------------------------------------------------
# Call-state tracking (very small in-memory store, fine for personal use)
# ---------------------------------------------------------------------------
authenticated_calls = {}  # call_id -> last_seen_timestamp
call_vehicle = {}         # call_id -> brand_id, once chosen for this call
call_round = {}           # call_id -> int, see next_choice_var() below
CALL_TTL_SECONDS = 30 * 60

def touch_call(call_id: str):
    authenticated_calls[call_id] = time.time()
    # opportunistic cleanup
    stale = [cid for cid, ts in authenticated_calls.items() if time.time() - ts > CALL_TTL_SECONDS]
    for cid in stale:
        authenticated_calls.pop(cid, None)
        call_vehicle.pop(cid, None)
        call_round.pop(cid, None)

def next_choice_var(call_id: str) -> str:
    """
    A fresh, never-before-seen varname ("car_choice_0", "car_choice_1", ...)
    for each round of the action menu within one call.

    Bug found 2026-09-15: the action menu re-reads the SAME varname
    ("car_choice") in a loop for as long as the call lasts. Even with
    read='s re_enter_if_exists option set to "yes", live testing showed
    every key press after the first kept re-running the FIRST action ever
    chosen in the call (e.g. every press "unlocked" because that's what
    was pressed first) - Yemot appears to keep reusing the first value it
    ever collected for a given varname within a call regardless of that
    flag. Using a brand-new varname every round sidesteps the ambiguity
    entirely: Yemot cannot have a stale cached value for a name it has
    never seen before.
    """
    n = call_round.get(call_id, 0)
    call_round[call_id] = n + 1
    return f"car_choice_{n}"

def first_menu_response(call_id: str) -> str:
    """The response for a just-authenticated call: the action menu directly
    (single brand) or the vehicle-picker (multiple brands)."""
    if SINGLE_BRAND:
        call_vehicle[call_id] = SINGLE_BRAND
        return read_digits(adapters[SINGLE_BRAND].menu_prompt(), next_choice_var(call_id), 1)
    return read_digits(vehicle_select_prompt(), "car_select", 1)

# ---------------------------------------------------------------------------
# Main webhook
# ---------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/yemot", methods=["GET", "POST"])
@app.route("/yemot/<path_token>", methods=["GET", "POST"])
def yemot_webhook(path_token=None):
    params = {**request.args.to_dict(), **request.form.to_dict()}
    log.info("yemot request: call_id=%s params=%s", params.get("ApiCallId"), params)

    # The token lives in the URL PATH (/yemot/<token>), not a query string
    # param - Yemot's api_link feature appends its own params with a
    # second "?" instead of "&" when the configured URL already contains a
    # "?", which corrupts a "?token=..." query param beyond recognition.
    # A path segment is untouched by that, since Yemot only ever appends
    # its own query string after whatever URL you configured.
    if YEMOT_TOKEN and path_token != YEMOT_TOKEN:
        log.warning("Rejected request with bad/missing token")
        return yemot_response(id_list_message_hangup("אין הרשאה."))

    call_id = params.get("ApiCallId", "unknown")
    phone = params.get("ApiPhone", "")

    if ALLOWED_NUMBERS and phone not in ALLOWED_NUMBERS:
        log.warning("Rejected call from unauthorized number: %s", phone)
        return yemot_response(
            id_list_message_hangup("מצטערים, מספר זה אינו מורשה להשתמש בשירות.")
        )

    # Step 1: PIN entry (4 digits, "car_pin") - SKIPPED for a phone number
    # already on YEMOT_ALLOWED_NUMBERS. That allow-list is itself already a
    # per-number authentication (caller ID isn't something a normal caller
    # can spoof on the phone network), so asking a known, trusted number to
    # also type a PIN is redundant friction. The PIN remains the ONLY
    # authentication when YEMOT_ALLOWED_NUMBERS isn't configured at all
    # (e.g. a future deployment open to any caller who self-identifies with
    # a PIN).
    # NOTE: when the PIN path IS used, we branch on server-side call state
    # (authenticated_calls), NOT on "is car_pin present in params" - Yemot
    # may keep echoing back earlier collected fields (car_pin included) on
    # every later hit of the same call, and branching on their mere
    # presence would re-trigger this block forever, trapping the caller in
    # a loop that never reaches the menu.
    if call_id not in authenticated_calls:
        if ALLOWED_NUMBERS:
            touch_call(call_id)
            return yemot_response(first_menu_response(call_id))
        if "car_pin" not in params:
            # very first hit of the call - nothing collected yet.
            return yemot_response(read_digits("נא להקליד קוד סודי בן ארבע ספרות", "car_pin", 4))
        if not YEMOT_PIN or params["car_pin"] != YEMOT_PIN:
            log.warning("Wrong PIN attempt from %s", phone)
            return yemot_response(id_list_message_hangup("קוד שגוי. השיחה תנותק."))
        touch_call(call_id)
        return yemot_response(first_menu_response(call_id))

    touch_call(call_id)

    # Step 2: vehicle selection (only when more than one brand is configured
    # and this call hasn't picked one yet)
    if call_id not in call_vehicle:
        idx = params.get("car_select", "")
        chosen = None
        if idx.isdigit() and 1 <= int(idx) <= len(_brand_by_index):
            chosen = _brand_by_index[int(idx) - 1]
        if not chosen:
            return yemot_response(read_digits(vehicle_select_prompt(), "car_select", 1, "בחירה לא תקינה."))
        call_vehicle[call_id] = chosen
        return yemot_response(read_digits(adapters[chosen].menu_prompt(), next_choice_var(call_id), 1))

    # Step 3: action choice within the already-chosen vehicle
    adapter = adapters[call_vehicle[call_id]]
    # the varname we most recently prompted with is call_round[call_id] - 1
    # (next_choice_var() already advanced the counter for next time)
    choice = params.get(f"car_choice_{call_round.get(call_id, 1) - 1}", "")

    if choice == "*":
        return yemot_response(id_list_message_hangup("להתראות."))

    if choice == "0":
        # replay the menu, no "invalid choice" framing - this is a
        # deliberate "hear the options again / do another action" key,
        # not a mistake.
        return yemot_response(read_digits(adapter.menu_prompt(), next_choice_var(call_id), 1))

    try:
        result_text = adapter.handle_choice(choice)
    except Exception:
        log.exception("Action failed")
        result_text = "אירעה שגיאה בביצוע הפעולה."

    if result_text is None:
        return yemot_response(read_digits(adapter.menu_prompt(), next_choice_var(call_id), 1, "בחירה לא תקינה."))

    return yemot_response(read_digits(adapter.menu_prompt(), next_choice_var(call_id), 1, result_text))


@app.route("/yemot-test", methods=["GET", "POST"])
def yemot_test():
    # TEMPORARY diagnostic route (2026-09-15): always plays a known Yemot
    # SYSTEM message (M1000, pre-recorded by Yemot itself) instead of our
    # own custom Hebrew TTS text, to isolate whether "read=t-<hebrew text>"
    # TTS rendering specifically is silent/broken on this account, vs. the
    # whole protocol round-trip being broken. Point a spare extension's
    # api_link at this route for one test call, then remove this route
    # once the real cause is found - not meant to stay in the codebase.
    log.info("yemot-test hit: %s", request.args.to_dict() | request.form.to_dict())
    return yemot_response("id_list_message=M1000.g-hangup")


@app.route("/health")
def health():
    info = {"ok": True, "vehicles": list(adapters)}
    mg = adapters.get("mg")
    if mg is not None:
        info["mg_cached_keys"] = list(mg.status_cache.keys())
    return info


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
