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

    # -- "is the car running?" guard ---------------------------------------
    # Remote commands don't go through while the engine is on (MG's own
    # rejections even say "restart the vehicle with the physical key... turn
    # off the engine"), and because every action here is fire-and-forget the
    # caller would otherwise hear "command sent" and never learn it was
    # dropped. So before sending anything, ask the cloud for the engine state
    # (cached briefly so a burst of key presses costs one lookup) and say so
    # out loud instead. Fails OPEN: if the lookup errors, times out, is
    # blocked behind another command, or the cloud's status looks stale, we
    # return "not running" and let the command through - a flaky pre-check
    # must never be what stops a working command.
    _RUNNING_CACHE_TTL = 20.0
    _STATUS_MAX_AGE = 15 * 60  # same drift limit the upstream gateway enforces
    _STATUS_TIME_INVALID = frozenset({0, 2147483647})
    _running_cache: tuple[float, bool] | None = None

    def _vehicle_running(self) -> bool:
        cached = self._running_cache
        if cached and time.time() - cached[0] < self._RUNNING_CACHE_TTL:
            return cached[1]
        running = False
        try:
            status = saic_client.call(
                self.user, lambda api: api.get_vehicle_status(self.vin),
                wait_for_lock=2.0, action_timeout=5.0, quick=True,
            )
            basic = status.basicVehicleStatus
            status_time = status.statusTime
            fresh = (
                status_time is not None
                and status_time not in self._STATUS_TIME_INVALID
                and abs(time.time() - status_time) <= self._STATUS_MAX_AGE
            )
            running = bool(basic and basic.is_engine_running and fresh)
        except Exception:
            log.info("Could not check engine state for user %s, assuming not running", self.user.id)
        self._running_cache = (time.time(), running)
        return running

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

    def _action_ac_blowing(self):
        saic_client.enqueue(self.user, lambda api: api.start_ac_blowing(self.vin))
        return "הפקודה להפעלת אוורור בלבד (בלי קירור) נשלחה"

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

    def _action_trunk(self):
        saic_client.enqueue(self.user, lambda api: api.open_tailgate(self.vin))
        return "הפקודה לפתיחת דלת המטען נשלחה"

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
        if basic and basic.is_engine_running:
            parts.append("הרכב מונע")
        if status.gpsPosition is not None:
            parts.append("יש נתון מיקום עדכני לרכב")
        if not parts:
            parts.append("אין כרגע נתון זמין על הרכב")

        return ", ".join(parts)

    # -- menu ------------------------------------------------------------
    # Fixed 2-digit codes (2026-09-18) - see base.py's menu_prompt()
    # docstring for why (no submenu, no "#").
    _MENU = {
        "01": _action_ac_on,
        "02": _action_ac_off,
        "03": _action_lock,
        "04": _action_unlock,
        "05": _action_status,
        "06": _action_find_car,
        "07": _action_seat_heat_on,
        "08": _action_seat_heat_off,
        "09": _action_front_defrost,
        "10": _action_trunk,
        "11": _action_ac_blowing,
    }

    def menu_prompt(self) -> str:
        # Commas (not periods) between phrases: periods are a reserved
        # structural character in Yemot's protocol and get stripped to a
        # bare space by clean() (no TTS pause at all); commas survive and
        # give the speech engine a natural pause between menu options.
        return (
            "לחץ אפס אחת להדלקת מזגן, "
            "לחץ אפס שתיים לכיבוי מזגן, "
            "לחץ אפס שלוש לנעילת דלתות, "
            "לחץ אפס ארבע לפתיחת דלתות, "
            "לחץ אפס חמש לשמיעת סטטוס הרכב, "
            "לחץ אפס שש לצפצוף ואיתור הרכב, "
            "לחץ אפס שבע להדלקת חימום מושבים, "
            "לחץ אפס שמונה לכיבוי חימום מושבים, "
            "לחץ אפס תשע להפשרת השמשה הקדמית, "
            "לחץ אחת אפס לפתיחת דלת המטען, "
            "לחץ אחת אחת להפעלת אוורור בלבד, "
            "לחץ כוכבית לסיום"
        )

    # Choices that only READ state (status) are allowed while the car runs.
    _READ_ONLY_CHOICES = frozenset({"05"})

    def handle_choice(self, choice: str):
        action = self._MENU.get(choice)
        if not action:
            return None
        if choice not in self._READ_ONLY_CHOICES and self._vehicle_running():
            return "הרכב מונע כרגע ולכן אי אפשר לשלוח פקודות מרחוק, כבו את הרכב ונסו שוב"
        try:
            return action(self)
        except saic_client.Busy:
            return "יש עדיין פקודה קודמת בביצוע, נסו שוב בעוד כמה שניות"
        except Exception:
            log.exception("%s action %r failed for user %s", self.brand_id, choice, self.user.id)
            return "אירעה שגיאה בתקשורת עם הענן, נסו שוב בעוד רגע"
