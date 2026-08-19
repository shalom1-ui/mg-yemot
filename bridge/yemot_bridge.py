"""
Bridge between a Yemot Hamashiach (ימות המשיח) IVR extension and an MG car,
via the SAIC iSMART MQTT gateway (https://github.com/SAIC-iSmart-API/saic-python-mqtt-gateway).

Flow:
  1. Yemot calls this server's /yemot endpoint on every step of the call
     (first hit, then again after each digit the caller presses).
  2. This server replies with a small plain-text "mini language" that Yemot
     understands: id_list_message (play a message), read (ask for digits and
     store them under a variable name), hangup, etc.
  3. When the caller picks a car action, we publish an MQTT command to the
     gateway, which talks to MG's cloud on our behalf.

NOTE ON PROTOCOL ACCURACY:
  The exact Yemot response syntax below is based on community documentation
  (the yemot-router2 / yemot-api open source projects), not a fetch of
  Yemot's official developer PDF (that page blocked automated access).
  Test carefully and check the "מדריך למתכנתים" inside your Yemot management
  panel (הגדרות מתקדמות) if something doesn't behave as expected - the
  parameter names below (ApiPhone, ApiCallId, read=...,varname, etc.) are the
  most likely candidates but may need small adjustments.

  The MQTT topic names, on the other hand, ARE verified directly against the
  gateway's own source code (src/mqtt_topics.py in
  SAIC-iSmart-API/saic-python-mqtt-gateway) - not guessed.
"""

import os
import re
import time
import threading
import logging

from flask import Flask, request, Response
import paho.mqtt.client as mqtt

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

# Everything runs in ONE container on Render, so the broker is always local.
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

MG_ACCOUNT = os.environ.get("MG_ACCOUNT", "")
# MG_VEHICLE_ID must be the car's VIN, exactly as printed in the mg-gateway
# startup logs - the gateway's own topic scheme is saic/<user>/vehicles/<VIN>.
MG_VEHICLE_ID = os.environ.get("MG_VEHICLE_ID", "")
MQTT_TOPIC_ROOT = os.environ.get("MQTT_TOPIC", "saic")
BASE_TOPIC = f"{MQTT_TOPIC_ROOT}/{MG_ACCOUNT}/vehicles/{MG_VEHICLE_ID}"

# Render assigns a port dynamically via $PORT - always bind to that if present.
PORT = int(os.environ.get("PORT", os.environ.get("BRIDGE_PORT", "10000")))

# ---------------------------------------------------------------------------
# MQTT client - keeps a live cache of the last known vehicle status, and lets
# us publish commands to the gateway.
# ---------------------------------------------------------------------------
status_cache = {}

def on_connect(client, userdata, flags, rc):
    log.info("Connected to MQTT broker (rc=%s), subscribing to %s/#", rc, BASE_TOPIC)
    client.subscribe(f"{BASE_TOPIC}/#")

def on_message(client, userdata, msg):
    key = msg.topic[len(BASE_TOPIC) + 1:]
    try:
        status_cache[key] = msg.payload.decode("utf-8", errors="replace")
    except Exception:
        status_cache[key] = str(msg.payload)

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def start_mqtt():
    while True:
        try:
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            mqtt_client.loop_forever()
        except Exception as e:
            log.warning("MQTT connection failed (%s), retrying in 5s", e)
            time.sleep(5)

threading.Thread(target=start_mqtt, daemon=True).start()

def publish(subtopic: str, payload: str):
    topic = f"{BASE_TOPIC}/{subtopic}"
    log.info("Publishing %s -> %s", topic, payload)
    mqtt_client.publish(topic, payload, qos=1, retain=False)

# ---------------------------------------------------------------------------
# Car actions
# ---------------------------------------------------------------------------
def action_ac_on():
    publish("climate/remoteClimateState/set", "on")
    return "הפקודה להדלקת המזגן נשלחה. הרכב אמור להגיב תוך דקה עד שתיים."

def action_ac_off():
    publish("climate/remoteClimateState/set", "off")
    return "הפקודה לכיבוי המזגן נשלחה."

def action_lock():
    publish("doors/locked/set", "true")
    return "הפקודה לנעילת הדלתות נשלחה."

def action_unlock():
    publish("doors/locked/set", "false")
    return "הפקודה לפתיחת הדלתות נשלחה."

def action_find_car():
    publish("location/findMyCar/set", "activate")

    def stop_later():
        time.sleep(20)
        publish("location/findMyCar/set", "stop")

    threading.Thread(target=stop_later, daemon=True).start()
    return "הרכב יצפצף ויהבהב באורות למשך כעשרים שניות."

def action_status():
    # Topic names verified against src/mqtt_topics.py in the gateway repo.
    soc = status_cache.get("drivetrain/soc")
    fuel = status_cache.get("drivetrain/fossilFuel/percentage")  # relevant for the S9 PHEV
    locked = status_cache.get("doors/locked")
    lat = status_cache.get("location/latitude")
    lon = status_cache.get("location/longitude")

    parts = []
    if soc:
        parts.append(f"רמת הסוללה {soc} אחוז")
    else:
        parts.append("אין עדיין נתון על רמת הסוללה")

    if fuel:
        parts.append(f"רמת הדלק {fuel} אחוז")

    if locked is not None:
        locked_txt = "נעולה" if locked.lower() in ("true", "1", "locked") else "לא נעולה"
        parts.append(f"הרכב {locked_txt}")

    if lat and lon:
        parts.append("יש נתון מיקום עדכני לרכב")
    else:
        parts.append("אין עדיין נתון מיקום")

    return ". ".join(parts) + "."

MENU = {
    "1": action_ac_on,
    "2": action_ac_off,
    "3": action_lock,
    "4": action_unlock,
    "5": action_status,
    "6": action_find_car,
}

MENU_PROMPT = (
    "לחץ אחת להדלקת מזגן. "
    "לחץ שתיים לכיבוי מזגן. "
    "לחץ שלוש לנעילת דלתות. "
    "לחץ ארבע לפתיחת דלתות. "
    "לחץ חמש לשמיעת סטטוס הרכב. "
    "לחץ שש לצפצוף ואיתור הרכב. "
    "לחץ כוכבית לסיום."
)

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
CALL_TTL_SECONDS = 30 * 60

def touch_call(call_id: str):
    authenticated_calls[call_id] = time.time()
    # opportunistic cleanup
    stale = [cid for cid, ts in authenticated_calls.items() if time.time() - ts > CALL_TTL_SECONDS]
    for cid in stale:
        authenticated_calls.pop(cid, None)

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

    # Step 1: PIN entry
    if "mg_pin" in params:
        if not YEMOT_PIN or params["mg_pin"] != YEMOT_PIN:
            log.warning("Wrong PIN attempt from %s", phone)
            return yemot_response(combine(id_list_message("קוד שגוי. השיחה תנותק."), hangup()))
        touch_call(call_id)
        return yemot_response(read_digits(MENU_PROMPT, "mg_choice"))

    if call_id not in authenticated_calls:
        return yemot_response(read_digits("נא להקליד קוד סודי בן ארבע ספרות", "mg_pin"))

    touch_call(call_id)

    # Step 2: menu choice
    choice = params.get("mg_choice", "")

    if choice == "*":
        return yemot_response(combine(id_list_message("להתראות."), hangup()))

    action = MENU.get(choice)
    if not action:
        return yemot_response(read_digits("בחירה לא תקינה. " + MENU_PROMPT, "mg_choice"))

    try:
        result_text = action()
    except Exception as e:
        log.exception("Action failed")
        result_text = "אירעה שגיאה בביצוע הפעולה."

    return yemot_response(
        combine(id_list_message(result_text), read_digits(MENU_PROMPT, "mg_choice"))
    )


@app.route("/health")
def health():
    return {"ok": True, "cached_keys": list(status_cache.keys())}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
