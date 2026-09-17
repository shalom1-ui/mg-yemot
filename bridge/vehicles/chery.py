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

    def _action_seat_heat_on(self):
        self._enqueue("seat_heat_on")
        return "הפקודה להדלקת חימום מושבים נשלחה"

    def _action_seat_heat_off(self):
        self._enqueue("seat_heat_off")
        return "הפקודה לכיבוי חימום מושבים נשלחה"

    def _action_front_defrost(self):
        self._enqueue("front_defrost")
        return "הפקודה להפשרת השמשה הקדמית נשלחה"

    def _action_find_car(self):
        self._enqueue("find_car")
        return "הרכב יצפצף ויהבהב באורות לאיתור"

    _MENU = {
        "1": _action_ac_on,
        "2": _action_ac_off,
        "3": _action_lock,
        "4": _action_unlock,
        "6": _action_find_car,
        "7": _action_seat_heat_on,
        "8": _action_seat_heat_off,
        "9": _action_front_defrost,
    }

    def menu_prompt(self) -> str:
        return (
            "לחץ אחת להדלקת מזגן, "
            "לחץ שתיים לכיבוי מזגן, "
            "לחץ שלוש לנעילת דלתות, "
            "לחץ ארבע לפתיחת דלתות, "
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
        except chery_client.Busy:
            return "יש עדיין פקודה קודמת בביצוע, נסו שוב בעוד כמה שניות"
        except Exception:
            log.exception("Chery action %r failed for user %s", choice, self.user.id)
            return "אירעה שגיאה בתקשורת עם הענן, נסו שוב בעוד רגע"
