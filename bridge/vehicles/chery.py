"""
Chery/Jaecoo/Omoda adapter - all three brands share one cloud backend
("legend" BFF, aka "Chery Europe"/CarLinko), so one adapter covers all of
them; see chery_client.py for the actual protocol (ported, MIT license,
from Przemko92/chery-ha-integration).

Every command needs the account's own in-app security PIN (SM4-encrypted,
distinct from our own Yemot call-PIN) to mint a one-time "taskId" before
it's accepted - see chery_client.CheryClient._get_task_id().
"""

from __future__ import annotations

import logging

import chery_client
from .base import VehicleAdapter

log = logging.getLogger("yemot-bridge.chery")


class CheryAdapter(VehicleAdapter):
    brand_id = "chery"
    display_name = "צ'רי"

    @property
    def _account_pin(self) -> str:
        return self.user.credentials["chery_account_pin"]

    def _enqueue(self, command_id: str):
        chery_client.enqueue(
            self.user,
            lambda client: client.send_command(self.user.vin, command_id, self._account_pin),
        )

    # -- actions -------------------------------------------------------
    # Fire-and-forget for everything, same lesson learned on MG: a phone
    # call can't be blocked waiting an unpredictable amount of time for
    # cloud confirmation, and this cloud is at least as slow/complex as
    # MG's (two-tier auth, task-id minting per command).
    def _action_ac_on(self):
        self._enqueue("ac_on")
        return "הפקודה להדלקת המזגן נשלחה, הרכב אמור להגיב תוך דקה עד שתיים"

    def _action_ac_off(self):
        self._enqueue("ac_off")
        return "הפקודה לכיבוי המזגן נשלחה"

    def _action_lock(self):
        self._enqueue("lock")
        return "הפקודה לנעילת הדלתות נשלחה"

    def _action_unlock(self):
        self._enqueue("unlock")
        return "הפקודה לפתיחת הדלתות נשלחה"

    def _action_trunk(self):
        self._enqueue("trunk")
        return "הפקודה לפתיחת דלת המטען נשלחה"

    def _action_find_car(self):
        self._enqueue("find_car")
        return "הרכב יצפצף ויהבהב באורות לאיתור"

    def _action_seat_heat_on(self):
        self._enqueue("seat_heat_on")
        return "הפקודה להדלקת חימום מושבים נשלחה"

    def _action_seat_heat_off(self):
        self._enqueue("seat_heat_off")
        return "הפקודה לכיבוי חימום מושבים נשלחה"

    def _action_front_defrost(self):
        self._enqueue("front_defrost")
        return "הפקודה להפשרת השמשה הקדמית נשלחה"

    # Fixed 2-digit codes (2026-09-18), same convention as MG - see
    # base.py's menu_prompt() docstring for why (no submenu, no "#").
    _MENU = {
        "01": _action_ac_on,
        "02": _action_ac_off,
        "03": _action_lock,
        "04": _action_unlock,
        "05": _action_trunk,
        "06": _action_find_car,
        "07": _action_seat_heat_on,
        "08": _action_seat_heat_off,
        "09": _action_front_defrost,
    }

    def menu_prompt(self) -> str:
        return (
            "לחץ אפס אחת להדלקת מזגן, "
            "לחץ אפס שתיים לכיבוי מזגן, "
            "לחץ אפס שלוש לנעילת דלתות, "
            "לחץ אפס ארבע לפתיחת דלתות, "
            "לחץ אפס חמש לפתיחת דלת המטען, "
            "לחץ אפס שש לצפצוף ואיתור הרכב, "
            "לחץ אפס שבע להדלקת חימום מושבים, "
            "לחץ אפס שמונה לכיבוי חימום מושבים, "
            "לחץ אפס תשע להפשרת השמשה הקדמית, "
            "לחץ כוכבית לסיום"
        )

    def handle_choice(self, choice: str):
        action = self._MENU.get(choice)
        if not action:
            return None
        try:
            return action(self)
        except chery_client.Busy:
            return "יש עדיין פקודה קודמת בביצוע, נסו שוב בעוד כמה שניות"
        except Exception:
            log.exception("Chery action %r failed for user %s", choice, self.user.id)
            return "אירעה שגיאה בתקשורת עם הענן, נסו שוב בעוד רגע"
