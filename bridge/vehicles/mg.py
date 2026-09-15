"""
MG adapter - talks to the SAIC iSMART MQTT gateway
(https://github.com/SAIC-iSmart-API/saic-python-mqtt-gateway), which in turn
talks to MG's cloud (SAIC iSMART). This is the original, working integration
that used to live directly inside yemot_bridge.py.

MQTT topic names are verified against src/mqtt_topics.py in the gateway repo.
"""

import os
import time
import threading
import logging

import paho.mqtt.client as mqtt

from .base import VehicleAdapter

log = logging.getLogger("yemot-bridge.mg")


class MgAdapter(VehicleAdapter):
    brand_id = "mg"
    display_name = "אם. ג'י"

    def __init__(self):
        self.mqtt_host = os.environ.get("MQTT_HOST", "127.0.0.1")
        self.mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))

        account = os.environ.get("MG_ACCOUNT", "")
        # MG_VEHICLE_ID must be the car's VIN, exactly as printed in the
        # mg-gateway startup logs - the gateway's own topic scheme is
        # saic/<user>/vehicles/<VIN>.
        vehicle_id = os.environ.get("MG_VEHICLE_ID", "")
        topic_root = os.environ.get("MQTT_TOPIC", "saic")
        self.base_topic = f"{topic_root}/{account}/vehicles/{vehicle_id}"

        self.status_cache = {}
        self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        threading.Thread(target=self._start_mqtt, daemon=True).start()

    # -- MQTT plumbing -----------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        log.info("Connected to MQTT broker (rc=%s), subscribing to %s/#", rc, self.base_topic)
        client.subscribe(f"{self.base_topic}/#")

    def _on_message(self, client, userdata, msg):
        key = msg.topic[len(self.base_topic) + 1:]
        try:
            self.status_cache[key] = msg.payload.decode("utf-8", errors="replace")
        except Exception:
            self.status_cache[key] = str(msg.payload)

    def _start_mqtt(self):
        while True:
            try:
                self._client.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
                self._client.loop_forever()
            except Exception as e:
                log.warning("MQTT connection failed (%s), retrying in 5s", e)
                time.sleep(5)

    def _publish(self, subtopic: str, payload: str):
        topic = f"{self.base_topic}/{subtopic}"
        log.info("Publishing %s -> %s", topic, payload)
        self._client.publish(topic, payload, qos=1, retain=False)

    # -- actions -------------------------------------------------------
    def _action_ac_on(self):
        self._publish("climate/remoteClimateState/set", "on")
        return "הפקודה להדלקת המזגן נשלחה, הרכב אמור להגיב תוך דקה עד שתיים"

    def _action_ac_off(self):
        self._publish("climate/remoteClimateState/set", "off")
        return "הפקודה לכיבוי המזגן נשלחה"

    def _action_lock(self):
        self._publish("doors/locked/set", "true")
        return "הפקודה לנעילת הדלתות נשלחה"

    def _action_unlock(self):
        self._publish("doors/locked/set", "false")
        return "הפקודה לפתיחת הדלתות נשלחה"

    def _action_find_car(self):
        self._publish("location/findMyCar/set", "activate")

        def stop_later():
            time.sleep(20)
            self._publish("location/findMyCar/set", "stop")

        threading.Thread(target=stop_later, daemon=True).start()
        return "הרכב יצפצף ויהבהב באורות למשך כעשרים שניות"

    def _action_status(self):
        soc = self.status_cache.get("drivetrain/soc")
        fuel = self.status_cache.get("drivetrain/fossilFuel/percentage")  # relevant for the S9 PHEV
        locked = self.status_cache.get("doors/locked")
        lat = self.status_cache.get("location/latitude")
        lon = self.status_cache.get("location/longitude")

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

        return ", ".join(parts)

    _MENU = {
        "1": _action_ac_on,
        "2": _action_ac_off,
        "3": _action_lock,
        "4": _action_unlock,
        "5": _action_status,
        "6": _action_find_car,
    }

    def menu_prompt(self) -> str:
        # Commas (not periods) between phrases: periods are a reserved
        # structural character in Yemot's protocol and get stripped to a
        # bare space by clean() (no TTS pause at all); commas survive and
        # give the speech engine a natural pause between menu options.
        return (
            "לחץ אחת להדלקת מזגן, "
            "לחץ שתיים לכיבוי מזגן, "
            "לחץ שלוש לנעילת דלתות, "
            "לחץ ארבע לפתיחת דלתות, "
            "לחץ חמש לשמיעת סטטוס הרכב, "
            "לחץ שש לצפצוף ואיתור הרכב, "
            "לחץ כוכבית לסיום"
        )

    def handle_choice(self, choice: str):
        action = self._MENU.get(choice)
        if not action:
            return None
        return action(self)
