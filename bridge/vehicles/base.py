"""
Common interface every vehicle-brand adapter must implement.

The platform's idea: yemot_bridge.py only knows how to talk Yemot's IVR
protocol and how to authenticate a caller. It does NOT know anything about
any specific car brand. Each brand lives in its own module here and exposes
a small, uniform surface: a menu prompt (Hebrew text) and a way to run an
action for a chosen digit. Adding a new brand later means adding one new
file in this folder and one line in vehicles/registry.py - nothing in
yemot_bridge.py has to change.
"""

from abc import ABC, abstractmethod


class VehicleAdapter(ABC):
    """One instance per configured vehicle brand."""

    #: short id used in VEHICLE_BRAND / VEHICLES env vars, e.g. "mg", "maxus"
    brand_id: str = ""

    #: human-readable name announced to the caller, e.g. "אם. ג'י", "מקסוס"
    display_name: str = ""

    @abstractmethod
    def menu_prompt(self) -> str:
        """Hebrew text listing the available digit choices for this vehicle."""
        raise NotImplementedError

    @abstractmethod
    def handle_choice(self, choice: str) -> str | None:
        """
        Run the action for `choice` (a single digit string).
        Returns the Hebrew text to read back to the caller, or None if the
        digit isn't a valid choice for this vehicle (caller will be
        re-prompted).
        """
        raise NotImplementedError

    def not_ready_text(self) -> str:
        """Shared message for brands whose API integration isn't done yet."""
        return (
            f"התמיכה ב{self.display_name} עדיין בפיתוח ואינה זמינה כרגע, "
            "מצטערים על אי הנוחות"
        )
