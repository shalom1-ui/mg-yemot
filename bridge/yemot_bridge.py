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
  The exact Yemot response syntax below is based on community documentation
  (the yemot-router2 / yemot-api open source projects), not a fetch of
  Yemot's official developer PDF (that page blocked automated access).
  Test carefully and check the "מדריך למתכנתים" inside your Yemot management
  panel (הגדרות מתקדמות) if something doesn't behave as expected - the
  parameter names below (ApiPhone, ApiCallId, read=...,varname, etc.) are the
  most likely candidates but may need small adjustments.

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
    parts = [
        f"לחץ {_HEBREW_DIGIT_WORDS[i]} עבור {adapter.display_name}."
        for i, adapter in enumerate(adapters.values())
    ]
    return " ".join(parts)

_brand_by_index = list(adapters.keys())

# ---------------------------------------------------------------------------
# Yemot protocol helpers
# ---------------------------------------------------------------------------
def clean(text: str) -> str:
    # Strip characters that would break the "key=value&key=value" style
    # response Yemot expects.
    return re.sub(r"[&=\n\r]", " ", text)

def id_list_message(text: str) -> str:
    return f"id_list_message=t-{clean(text)}."

def read_digits(prompt: str, varname: str) -> str:
    return f"read=t-{clean(prompt)},{varname}"

def hangup() -> str:
    return "hangup=yes"

def combine(*parts: str) -> str:
    return "&".join(p for p in parts if p)

def yemot_response(body: str) -> Response:
    return Response(body, mimetype="text/plain; charset=utf-8")

# ---------------------------------------------------------------------------
# Call-state tracking (very small in-memory store, fine for personal use)
# ---------------------------------------------------------------------------
authenticated_calls = {}  # call_id -> last_seen_timestamp
call_vehicle = {}         # call_id -> brand_id, once chosen for this call
CALL_TTL_SECONDS = 30 * 60

def touch_call(call_id: str):
    authenticated_calls[call_id] = time.time()
    # opportunistic cleanup
    stale = [cid for cid, ts in authenticated_calls.items() if time.time() - ts > CALL_TTL_SECONDS]
    for cid in stale:
        authenticated_calls.pop(cid, None)
        call_vehicle.pop(cid, None)

# ---------------------------------------------------------------------------
# Main webhook
# ---------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/yemot", methods=["GET", "POST"])
def yemot_webhook():
    params = {**request.args.to_dict(), **request.form.to_dict()}

    if YEMOT_TOKEN and params.get("token") != YEMOT_TOKEN:
        log.warning("Rejected request with bad/missing token")
        return yemot_response(combine(id_list_message("אין הרשאה."), hangup()))

    call_id = params.get("ApiCallId", "unknown")
    phone = params.get("ApiPhone", "")

    if ALLOWED_NUMBERS and phone not in ALLOWED_NUMBERS:
        log.warning("Rejected call from unauthorized number: %s", phone)
        return yemot_response(
            combine(id_list_message("מצטערים, מספר זה אינו מורשה להשתמש בשירות."), hangup())
        )

    # Step 1: PIN entry.
    # NOTE: we branch on server-side call state (authenticated_calls), NOT on
    # "is car_pin present in params" - Yemot may keep echoing back earlier
    # collected fields (car_pin included) on every later hit of the same
    # call, and branching on their mere presence would re-trigger this block
    # forever, trapping the caller in a loop that never reaches the menu.
    if call_id not in authenticated_calls:
        if "car_pin" not in params:
            # very first hit of the call - nothing collected yet.
            return yemot_response(read_digits("נא להקליד קוד סודי בן ארבע ספרות", "car_pin"))
        if not YEMOT_PIN or params["car_pin"] != YEMOT_PIN:
            log.warning("Wrong PIN attempt from %s", phone)
            return yemot_response(combine(id_list_message("קוד שגוי. השיחה תנותק."), hangup()))
        touch_call(call_id)
        if SINGLE_BRAND:
            call_vehicle[call_id] = SINGLE_BRAND
            return yemot_response(read_digits(adapters[SINGLE_BRAND].menu_prompt(), "car_choice"))
        return yemot_response(read_digits(vehicle_select_prompt(), "car_select"))

    touch_call(call_id)

    # Step 2: vehicle selection (only when more than one brand is configured
    # and this call hasn't picked one yet)
    if call_id not in call_vehicle:
        idx = params.get("car_select", "")
        chosen = None
        if idx.isdigit() and 1 <= int(idx) <= len(_brand_by_index):
            chosen = _brand_by_index[int(idx) - 1]
        if not chosen:
            return yemot_response(read_digits("בחירה לא תקינה. " + vehicle_select_prompt(), "car_select"))
        call_vehicle[call_id] = chosen
        return yemot_response(read_digits(adapters[chosen].menu_prompt(), "car_choice"))

    # Step 3: action choice within the already-chosen vehicle
    adapter = adapters[call_vehicle[call_id]]
    choice = params.get("car_choice", "")

    if choice == "*":
        return yemot_response(combine(id_list_message("להתראות."), hangup()))

    try:
        result_text = adapter.handle_choice(choice)
    except Exception:
        log.exception("Action failed")
        result_text = "אירעה שגיאה בביצוע הפעולה."

    if result_text is None:
        return yemot_response(read_digits("בחירה לא תקינה. " + adapter.menu_prompt(), "car_choice"))

    return yemot_response(
        combine(id_list_message(result_text), read_digits(adapter.menu_prompt(), "car_choice"))
    )


@app.route("/health")
def health():
    info = {"ok": True, "vehicles": list(adapters)}
    mg = adapters.get("mg")
    if mg is not None:
        info["mg_cached_keys"] = list(mg.status_cache.keys())
    return info


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
