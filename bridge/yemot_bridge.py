"""
Bridge between a Yemot Hamashiach (ימות המשיח) IVR extension and one or more
cars, via a per-brand adapter (see vehicles/).

This file only knows the Yemot IVR protocol and caller identification - it
has NO car-brand-specific logic. Each brand (MG today, Maxus/others later)
is a separate module in vehicles/ implementing the VehicleAdapter interface.

Flow:
  1. Yemot calls this server's /yemot endpoint on every step of the call
     (first hit, then again after each digit the caller presses).
  2. This server replies with a small plain-text "mini language" that Yemot
     understands: id_list_message (play a message), read (ask for digits and
     store them under a variable name), hangup, etc.
  3. The caller is identified by a 4-digit PIN (see store.py) - always
     required, never skipped based on caller ID (a stolen phone can still
     place calls as its own number, so the number alone proves nothing).
     Once identified, they get straight to their own car's action menu -
     each user has exactly one car/brand, so there is no separate "pick a
     vehicle" step.

MULTI-TENANCY (2026-09-15): originally this whole file was wired to exactly
one hard-coded MG account via env vars. It now looks callers up by PIN in a
small per-user SQLite store (store.py) so more than one person can use the
same Yemot line/number, each with their own MG account - see saic_client.py
for how commands reach each user's own car directly (no MQTT, no
per-account gateway process anymore). The single original account still
works exactly as before with zero setup, via a "legacy user" synthesized
from the same env vars it always used (SAIC_USER/PASSWORD/REGION,
YEMOT_PIN) - see resolve_user() below - so nothing needs to change for
that account; new people get added via POST /signup/<ADMIN_TOKEN> instead.

NOTE ON PROTOCOL ACCURACY:
  Fixed 2026-09-15: the original syntax here (based on generic community
  docs, never verified against a real call) was wrong in two ways that
  real testing caught - `read=` needs an `=<varname>` (not `,<varname>`)
  PLUS a full positional options list after it, and `id_list_message` +
  hangup must be ONE combined command via a `.g-hangup` suffix (not a
  separate `hangup=yes` field). Confirmed-correct format copied from
  [[hapinkas-sheli-accounts-app]]'s `backend/src/services/yemot.js` (a
  live, working Yemot API-module integration) - see `read_digits()` and
  `id_list_message_hangup()` below for the exact reproduction.

  Also fixed 2026-09-15: `read=`'s re_enter_if_exists option alone wasn't
  enough to force a fresh digit read on every round of a repeating menu -
  live testing showed every key press after the first kept re-running the
  very first action ever chosen in the call. Each round now reads into a
  brand-new, never-before-seen varname (car_choice_0, car_choice_1, ...)
  via next_choice_var() so Yemot can't have a stale cached value for it.
"""

import os
import re
import sqlite3
import time
import logging

from flask import Flask, request, Response

import store
import saic_client
import chery_client
from vehicles.registry import ADAPTER_CLASSES

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yemot-bridge")

# ---------------------------------------------------------------------------
# Configuration (from environment / .env)
# ---------------------------------------------------------------------------
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")
YEMOT_PIN = os.environ.get("YEMOT_PIN", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# Render assigns a port dynamically via $PORT - always bind to that if present.
PORT = int(os.environ.get("PORT", os.environ.get("BRIDGE_PORT", "10000")))

# Defaults for the signup form - the values already proven working for an
# Israeli MG iSMART account this session (see research/NOTES.md). A signup
# for a different region can override them.
DEFAULT_SAIC_BASE_URI = "https://gateway-mg-il.soimt.com/api.app/v1/"
DEFAULT_SAIC_REGION = "tr"
DEFAULT_SAIC_TENANT_ID = "459771"

# ---------------------------------------------------------------------------
# The original single-account setup, kept working with zero migration as a
# fallback "user" resolved from env vars instead of the database - see
# resolve_user() below.
# ---------------------------------------------------------------------------
_legacy_user = None
if os.environ.get("SAIC_USER") and os.environ.get("SAIC_PASSWORD"):
    _legacy_user = store.User(
        id=0,
        phone=None,
        pin=YEMOT_PIN,
        brand="mg",
        credentials={
            "mg_email": os.environ["SAIC_USER"],
            "mg_password": os.environ["SAIC_PASSWORD"],
            "saic_base_uri": os.environ.get("SAIC_REST_URI", DEFAULT_SAIC_BASE_URI),
            "saic_region": os.environ.get("SAIC_REGION", DEFAULT_SAIC_REGION),
            "saic_tenant_id": os.environ.get("SAIC_TENANT_ID", DEFAULT_SAIC_TENANT_ID),
        },
        vin=os.environ.get("MG_VEHICLE_ID") or None,
    )


def resolve_user(pin: str) -> "store.User | None":
    """
    Identify the caller by PIN alone - always required, never skipped for a
    recognized phone number (2026-09-16: originally a caller-ID match on
    YEMOT_ALLOWED_NUMBERS skipped the PIN prompt entirely, but the user
    pointed out that's not real authentication - a stolen phone can still
    place calls as its own number, and would then get straight to the car
    menu with no PIN at all. PINs are unique per user (store.py's UNIQUE
    constraint), so the phone number was never actually needed to resolve
    who's calling, just to *skip* asking - removing that removes the gap.
    """
    user = store.get_user_by_pin(pin)
    if user:
        return user
    if _legacy_user and YEMOT_PIN and pin == YEMOT_PIN:
        return _legacy_user
    return None


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
    "the AC command was sent.") before the prompt - one read= call, with
    any feedback folded into its own prompt text, is the confirmed-working
    shape (a separate id_list_message + read= was tried and silently
    broken).

    Option order (verified against a live working integration):
      valName, re_enter_if_exists, max_digits, min_digits, sec_wait,
      typing_playback_mode, block_asterisk_key, block_zero_key,
      replace_char, digits_allowed, amount_attempts, allow_empty,
      empty_val, block_change_keyboard
    "No" for typing_playback_mode = don't read back each digit as typed
    (relevant for PINs). block_asterisk_key="no" = "*" still comes through
    as a value (used for the "hang up" menu option).
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
call_user = {}   # call_id -> store.User, once identified for this call
call_round = {}  # call_id -> int, see next_choice_var() below
_last_seen = {}  # call_id -> timestamp, for TTL cleanup below
CALL_TTL_SECONDS = 30 * 60

def touch_call(call_id: str):
    _last_seen[call_id] = time.time()
    # opportunistic cleanup
    stale = [cid for cid, ts in _last_seen.items() if time.time() - ts > CALL_TTL_SECONDS]
    for cid in stale:
        _last_seen.pop(cid, None)
        call_user.pop(cid, None)
        call_round.pop(cid, None)

def next_choice_var(call_id: str) -> str:
    """
    A fresh, never-before-seen varname ("car_choice_0", "car_choice_1", ...)
    for each round of the action menu within one call - see the module
    docstring's "Also fixed 2026-09-15" note for why this is necessary.
    """
    n = call_round.get(call_id, 0)
    call_round[call_id] = n + 1
    return f"car_choice_{n}"

# ---------------------------------------------------------------------------
# Adapter cache - one VehicleAdapter instance per (user, brand), built
# lazily the first time it's needed and reused after that.
# ---------------------------------------------------------------------------
_adapters = {}  # (user_id, brand) -> VehicleAdapter

def get_adapter(user: "store.User"):
    key = (user.id, user.brand)
    adapter = _adapters.get(key)
    if adapter is None:
        cls = ADAPTER_CLASSES.get(user.brand)
        if not cls:
            raise RuntimeError(f"Unknown brand {user.brand!r} for user {user.id}")
        adapter = cls(user)
        _adapters[key] = adapter
    return adapter

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
    if YEMOT_TOKEN and path_token != YEMOT_TOKEN:
        log.warning("Rejected request with bad/missing token")
        return yemot_response(id_list_message_hangup("אין הרשאה."))

    call_id = params.get("ApiCallId", "unknown")
    phone = params.get("ApiPhone", "")

    # Step 1: identify the caller by a 4-digit PIN - always required, even
    # for a recognized number (see resolve_user()'s docstring for why).
    # NOTE: we branch on server-side call state (call_user), NOT on "is
    # car_pin present in params" - Yemot may keep echoing back earlier
    # collected fields on every later hit of the same call, and branching
    # on their mere presence would re-trigger this block forever, trapping
    # the caller in a loop that never reaches the menu.
    if call_id not in call_user:
        if "car_pin" not in params:
            # very first hit of the call - nothing collected yet.
            return yemot_response(read_digits("נא להקליד קוד סודי בן ארבע ספרות", "car_pin", 4))
        user = resolve_user(params["car_pin"])
        if not user:
            log.warning("Wrong PIN attempt from %s", phone)
            return yemot_response(id_list_message_hangup("קוד שגוי. השיחה תנותק."))

        touch_call(call_id)
        call_user[call_id] = user
        adapter = get_adapter(user)
        return yemot_response(read_digits(adapter.menu_prompt(), next_choice_var(call_id), 1))

    touch_call(call_id)

    # Step 2: action choice within the already-identified user's vehicle
    user = call_user[call_id]
    adapter = get_adapter(user)
    choice = params.get(f"car_choice_{call_round.get(call_id, 1) - 1}", "")

    if choice == "*":
        return yemot_response(id_list_message_hangup("להתראות."))

    if choice == "0":
        # replay the menu, no "invalid choice" framing - this is a
        # deliberate "hear the options again / do another action" key.
        return yemot_response(read_digits(adapter.menu_prompt(), next_choice_var(call_id), 1))

    try:
        result_text = adapter.handle_choice(choice)
    except Exception:
        log.exception("Action failed")
        result_text = "אירעה שגיאה בביצוע הפעולה."

    if result_text is None:
        return yemot_response(read_digits(adapter.menu_prompt(), next_choice_var(call_id), 1, "בחירה לא תקינה."))

    return yemot_response(read_digits(adapter.menu_prompt(), next_choice_var(call_id), 1, result_text))


# ---------------------------------------------------------------------------
# Signup - adds a new user (their own car account + a PIN, the only thing
# that identifies them on a call - see resolve_user()'s docstring for why
# phone number alone is deliberately not enough).
# Not exposed over the Yemot phone flow (no practical way to type an email +
# password on a kosher-phone keypad) - a one-time web form instead, meant to
# be filled in from a real browser by whoever is onboarding a new person.
#
# MG is a single-step signup (email+password validated against the cloud
# immediately). Chery/Jaecoo/Omoda (2026-09-17) is inherently two-step and
# interactive - a real email must receive a one-time code before signup can
# finish - so the form has a hidden `stage` field: stage 1 collects the
# account details and triggers the email; stage 2 (same form, brand/phone/
# pin/email carried as hidden fields) collects the code the user just
# received and completes the login.
# ---------------------------------------------------------------------------
SIGNUP_FORM_HTML = """
<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<title>חיבור רכב למערכת</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 32em; margin: 2em auto; padding: 0 1em; }}
label {{ display: block; margin-top: 1em; }}
input {{ width: 100%; box-sizing: border-box; padding: 0.4em; font-size: 1em; }}
button {{ margin-top: 1.5em; padding: 0.6em 1.5em; font-size: 1em; }}
.msg {{ margin-top: 1em; padding: 1em; border-radius: 0.3em; }}
.ok {{ background: #e6ffed; }}
.err {{ background: #ffe6e6; }}
fieldset {{ margin-top: 1em; border-radius: 0.3em; }}
</style></head><body>
<h1>חיבור רכב למערכת הטלפונית</h1>
{message}
<form method="post">
<label>מותג הרכב:
<select name="brand" required onchange="
document.getElementById('mg-fields').hidden = (this.value !== 'mg' &amp;&amp; this.value !== 'maxus');
document.getElementById('chery-fields').hidden = this.value !== 'chery';
">
<option value="">- בחר/י -</option>
<option value="mg" {mg_selected}>MG</option>
<option value="maxus" {maxus_selected}>מקסוס / מיפה (Maxus/MIFA)</option>
<option value="chery" {chery_selected}>צ'רי / ג'קו / אומודה (Chery/Jaecoo/Omoda)</option>
</select></label>
<label>מספר טלפון (אופציונלי, לתיעוד בלבד - הזיהוי בשיחה הוא תמיד לפי הקוד הסודי):
<input name="phone" placeholder="0501234567" value="{phone}"></label>
<label>קוד סודי בן 4 ספרות לשיחות הטלפון (חובה):
<input name="pin" required pattern="[0-9]{{4}}" maxlength="4" value="{pin}"></label>

<fieldset id="mg-fields" hidden>
<legend>פרטי חשבון iSMART (MG / מקסוס-מיפה - אותה מערכת חשבונות)</legend>
<p>למקסוס/מיפה: נסו לרשום את הרכב באפליקציית <b>MG iSMART</b> (לא Hi MAXUS
Europe) עם ה-VIN שלו - אם זה מצליח, ממלאים כאן את פרטי אותו חשבון.</p>
<label>אימייל:
<input name="mg_email" type="email"></label>
<label>סיסמה:
<input name="mg_password" type="password"></label>
<label>אזור (ברירת מחדל מתאימה לישראל):
<input name="saic_region" value="{default_region}"></label>
<label>כתובת שרת (ברירת מחדל מתאימה לישראל):
<input name="saic_base_uri" value="{default_base_uri}"></label>
</fieldset>

<fieldset id="chery-fields" hidden>
<legend>פרטי חשבון Chery/Jaecoo/Omoda</legend>
<label>אימייל:
<input name="chery_email" type="email" value="{chery_email}"></label>
<label>קוד PIN הפנימי של אפליקציית הרכב (משמש לאישור כל פקודה):
<input name="chery_account_pin" value="{chery_account_pin}"></label>
{code_field}
</fieldset>

<input type="hidden" name="stage" value="{stage}">
<button type="submit">{submit_label}</button>
</form>
</body></html>
"""

def _signup_render(
    message: str = "", ok: bool = True, *, phone: str = "", pin: str = "",
    brand: str = "", chery_email: str = "", chery_account_pin: str = "",
    stage: str = "1", submit_label: str = "התחבר ובדוק", status: int = 200,
) -> Response:
    code_field = ""
    if stage == "2":
        code_field = (
            '<label>הקוד שקיבלת עכשיו באימייל:'
            '<input name="code" required autofocus></label>'
        )
    return Response(
        SIGNUP_FORM_HTML.format(
            message=f'<div class="msg {"ok" if ok else "err"}">{message}</div>' if message else "",
            mg_selected="selected" if brand == "mg" else "",
            maxus_selected="selected" if brand == "maxus" else "",
            chery_selected="selected" if brand == "chery" else "",
            phone=phone, pin=pin,
            default_region=DEFAULT_SAIC_REGION, default_base_uri=DEFAULT_SAIC_BASE_URI,
            chery_email=chery_email, chery_account_pin=chery_account_pin,
            code_field=code_field, stage=stage, submit_label=submit_label,
        ),
        status=status, mimetype="text/html; charset=utf-8",
    )


def _finish_signup(*, phone, pin, brand, credentials, vin) -> Response:
    try:
        user = store.create_user(phone=phone, pin=pin, brand=brand, credentials=credentials, vin=vin)
    except sqlite3.IntegrityError:
        return _signup_render(
            "קוד סודי או מספר טלפון כבר קיימים במערכת", ok=False, status=400,
            phone=phone or "", pin=pin, brand=brand,
        )
    return _signup_render(f"נוצר בהצלחה! משתמש מספר {user.id}, רכב {vin}", ok=True)


@app.route("/signup/<token>", methods=["GET", "POST"])
def signup(token):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return Response("Not found", status=404)

    if request.method == "GET":
        return _signup_render()

    phone = request.form.get("phone", "").strip() or None
    pin = request.form.get("pin", "").strip()
    brand = request.form.get("brand", "").strip()
    stage = request.form.get("stage", "1").strip()

    if not (pin and brand):
        return _signup_render("חסרים שדות חובה (קוד סודי, מותג)", ok=False, status=400,
                               phone=phone or "", pin=pin, brand=brand)

    # ---- MG / Maxus-MIFA: single step, validated immediately against the
    # real cloud. Same fields, same credential shape, same underlying
    # SAIC iSMART account system for both - see vehicles/maxus.py's
    # docstring for why Maxus is just "MG with a different brand id". ----
    if brand in ("mg", "maxus"):
        mg_email = request.form.get("mg_email", "").strip()
        mg_password = request.form.get("mg_password", "")
        saic_base_uri = request.form.get("saic_base_uri", "").strip() or DEFAULT_SAIC_BASE_URI
        saic_region = request.form.get("saic_region", "").strip() or DEFAULT_SAIC_REGION

        if not (mg_email and mg_password):
            return _signup_render("חסרים שדות חובה (אימייל, סיסמה)", ok=False, status=400,
                                   phone=phone or "", pin=pin, brand=brand)

        credentials = {
            "mg_email": mg_email, "mg_password": mg_password,
            "saic_base_uri": saic_base_uri, "saic_region": saic_region,
            "saic_tenant_id": DEFAULT_SAIC_TENANT_ID,
        }
        # A throwaway user record, not saved yet: validate the credentials
        # against the real cloud before writing anything, so a typo is
        # caught here instead of silently failing on the first real call.
        temp_user = store.User(id=0, phone=phone, pin=pin, brand=brand, credentials=credentials)
        try:
            client = saic_client.validate_credentials(temp_user)
            vehicle_list = saic_client.run_async(client.vehicle_list())
            vin = vehicle_list.vinList[0].vin
        except Exception as e:
            log.exception("Signup login failed for %s (brand=%s)", mg_email, brand)
            return _signup_render(f"ההתחברות נכשלה: {e}", ok=False, status=400,
                                   phone=phone or "", pin=pin, brand=brand)
        return _finish_signup(phone=phone, pin=pin, brand=brand, credentials=credentials, vin=vin)

    # ---- Chery/Jaecoo/Omoda: two steps (request email code, then use it) ----
    if brand == "chery":
        chery_email = request.form.get("chery_email", "").strip()
        chery_account_pin = request.form.get("chery_account_pin", "").strip()

        if not (chery_email and chery_account_pin):
            return _signup_render("חסרים שדות חובה (אימייל, קוד PIN של הרכב)", ok=False, status=400,
                                   phone=phone or "", pin=pin, brand=brand)

        if stage != "2":
            # Stage 1: solve the captcha and ask the cloud to email a code.
            try:
                client = chery_client.new_standalone_client()
                chery_client.run_async(client.request_email_code(chery_email), timeout=30.0)
            except Exception as e:
                log.exception("Chery signup code request failed for %s", chery_email)
                return _signup_render(f"שליחת קוד האימות נכשלה: {e}", ok=False, status=400,
                                       phone=phone or "", pin=pin, brand=brand)
            return _signup_render(
                "קוד אימות נשלח לאימייל - הזן/י אותו למטה", ok=True,
                phone=phone or "", pin=pin, brand=brand, chery_email=chery_email,
                chery_account_pin=chery_account_pin, stage="2", submit_label="סיים הרשמה",
            )

        # Stage 2: use the code the caller just received.
        code = request.form.get("code", "").strip()
        if not code:
            return _signup_render(
                "חסר קוד האימות מהאימייל", ok=False, status=400,
                phone=phone or "", pin=pin, brand=brand, chery_email=chery_email,
                chery_account_pin=chery_account_pin, stage="2", submit_label="סיים הרשמה",
            )
        try:
            client = chery_client.new_standalone_client()
            chery_client.run_async(client.login(chery_email, code), timeout=30.0)
            chery_client.run_async(client.tsp_login(), timeout=30.0)
            vehicles = chery_client.run_async(client.get_vehicle_list(), timeout=30.0)
            vin = vehicles[0]["vin"]
        except Exception as e:
            log.exception("Chery signup login failed for %s", chery_email)
            return _signup_render(
                f"ההתחברות נכשלה: {e}", ok=False, status=400,
                phone=phone or "", pin=pin, brand=brand, chery_email=chery_email,
                chery_account_pin=chery_account_pin, stage="2", submit_label="סיים הרשמה",
            )

        credentials = {
            "chery_email": chery_email, "chery_account_pin": chery_account_pin,
            "chery_refresh_token": client.session.refresh_token,
        }
        return _finish_signup(phone=phone, pin=pin, brand="chery", credentials=credentials, vin=vin)

    return _signup_render("מותג לא מוכר", ok=False, status=400, phone=phone or "", pin=pin)


@app.route("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
