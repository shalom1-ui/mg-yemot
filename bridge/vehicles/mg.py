"""
MG adapter - talks directly to MG's cloud (SAIC iSMART) via
saic_ismart_client_ng.SaicApi, on demand, per phone command. No MQTT, no
per-account gateway process: see saic_client.py for why - in short, the
upstream saic-python-mqtt-gateway project's own car-control calls are
one-line async SaicApi calls, so a live broker connection isn't needed for
a bridge that only issues a command when a caller presses a key.

One VehicleAdapter instance per (user, brand) - see base.py - so self.user
is this call's already-identified store.User record (their own MG
credentials, saic_base_uri/region/tenant_id, and cached VIN).
"""

from __future__ import annotations

import logging
import threading
import time

import store
import saic_client
from .base import VehicleAdapter

log = logging.getLogger("yemot-bridge.mg")


class MgAdapter(VehicleAdapter):
    brand_id = "mg"
    display_name = "אם. ג'י"

    @property
    def vin(self) -> str:
        if not self.user.vin:
            # first-ever command for this user in this process: discover
            # and cache the VIN via a real vehicle_list() call.
            resp = saic_client.call(self.user, lambda api: api.vehicle_list())
            vin = resp.vinList[0].vin
            store.set_vin(self.user.id, vin)
            self.user.vin = vin
        return self.user.vin

    # -- actions -------------------------------------------------------
    # Every command below is fire-and-forget (saic_client.enqueue), not
    # just AC-on: live testing found unlock alone could take 12s, then
    # 24s+, then time out entirely (real TimeoutError from MG's cloud, not
    # a bug in the retry logic) when sent shortly after AC-on - MG's cloud
    # can genuinely be slow to confirm ANY command while another one for
    # the same car is still settling, not just climate ones. A phone call
    # can't be blocked waiting that long, so every action now returns
    # "sent" immediately and the actual work happens in the background,
    # still serialized per user (saic_client.call()'s lock) so commands
    # never race each other regardless of how the caller presses keys.
    def _action_ac_on(self):
        saic_client.enqueue(self.user, lambda api: api.start_ac(self.vin))
        return "הפקודה להדלקת המזגן נשלחה, הרכב אמור להגיב תוך דקה עד שתיים"

    def _action_ac_off(self):
        saic_client.enqueue(self.user, lambda api: api.stop_ac(self.vin))
        return "הפקודה לכיבוי המזגן נשלחה"

    def _action_seat_heat_on(self):
        saic_client.enqueue(self.user, lambda api: api.control_heated_seats(
            self.vin, left_side_level=3, right_side_level=3))
        return "הפקודה להדלקת חימום מושבים נשלחה"

    def _action_seat_heat_off(self):
        saic_client.enqueue(self.user, lambda api: api.control_heated_seats(
            self.vin, left_side_level=0, right_side_level=0))
        return "הפקודה לכיבוי חימום מושבים נשלחה"

    def _action_front_defrost(self):
        saic_client.enqueue(self.user, lambda api: api.start_front_defrost(self.vin))
        return "הפקודה להפשרת השמשה הקדמית נשלחה"

    def _action_lock(self):
        saic_client.enqueue(self.user, lambda api: api.lock_vehicle(self.vin))
        return "הפקודה לנעילת הדלתות נשלחה"

    def _action_unlock(self):
        saic_client.enqueue(self.user, lambda api: api.unlock_vehicle(self.vin))
        return "הפקודה לפתיחת הדלתות נשלחה"

    def _action_find_car(self):
        saic_client.enqueue(self.user, lambda api: api.control_find_my_car(self.vin))

        def stop_later():
            time.sleep(20)
            saic_client.enqueue(
                self.user, lambda api: api.control_find_my_car(self.vin, should_stop=True)
            )

        threading.Thread(target=stop_later, daemon=True).start()
        return "הרכב יצפצף ויהבהב באורות למשך כעשרים שניות"

    def _action_status(self):
        status = saic_client.call(self.user, lambda api: api.get_vehicle_status(self.vin))
        basic = status.basicVehicleStatus

        parts = []
        if basic and basic.fuelLevelPrc is not None:
            parts.append(f"רמת הדלק {basic.fuelLevelPrc} אחוז")
        if basic and basic.lockStatus is not None:
            locked_txt = "נעולה" if basic.lockStatus == 1 else "לא נעולה"
            parts.append(f"הרכב {locked_txt}")
        if status.gpsPosition is not None:
            parts.append("יש נתון מיקום עדכני לרכב")
        if not parts:
            parts.append("אין כרגע נתון זמין על הרכב")

        return ", ".join(parts)

    _MENU = {
        "1": _action_ac_on,
        "2": _action_ac_off,
        "3": _action_lock,
        "4": _action_unlock,
        "5": _action_status,
        "6": _action_find_car,
        "7": _action_seat_heat_on,
        "8": _action_seat_heat_off,
        "9": _action_front_defrost,
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
            "לחץ שבע להדלקת חימום מושבים, "
            "לחץ שמונה לכיבוי חימום מושבים, "
            "לחץ תשע להפשרת השמשה הקדמית, "
            "לחץ כוכבית לסיום"
        )

    def handle_choice(self, choice: str):
        action = self._MENU.get(choice)
        if not action:
            return None
        try:
            return action(self)
        except saic_client.Busy:
            return "יש עדיין פקודה קודמת בביצוע, נסו שוב בעוד כמה שניות"
        except Exception:
            log.exception("MG action %r failed for user %s", choice, self.user.id)
            return "אירעה שגיאה בתקשורת עם הענן של MG, נסו שוב בעוד רגע"
