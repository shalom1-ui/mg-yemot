"""
Maxus (MIFA 7 / MIFA 9) adapter - STUB, not yet functional.

Unlike MG, there is no existing open-source library for the Maxus/MIFA
cloud API as of 2026-08-20. Maxus vehicles are controlled via the official
"Hi MAXUS Europe" app (package com.saicmaxus.ismarteu on Android), which is
a SEPARATE app from MG iSMART, even though both brands belong to SAIC Motor.

The app id hints that it MIGHT share backend infrastructure with MG's
tap-eu.soimt.com / gateway-eu.soimt.com (region-based host names, not
brand-based) - but this is an unverified hypothesis, not a fact.

TODO to make this real (see README.md "מיפה - הצעדים הבאים"):
  1. Confirm the Hi MAXUS Europe app is installed and successfully paired
     with a real MIFA 7/9 in the account we'll use.
  2. Capture the app's network traffic during login + a remote-climate
     action (e.g. with mitmproxy on the same Wi-Fi / a proxy profile on the
     phone) to learn the real API host, auth flow and command payloads.
  3. Try the existing saic-python-client-ng login flow against
     tap-eu.soimt.com with the Maxus account credentials, in case the
     backend really is shared - this is the fastest thing to try first,
     before doing full traffic capture.
  4. Once the protocol is known, implement it here the same way mg.py
     implements MG's (either by talking to a new lightweight gateway
     process the same way mg.py talks to MQTT, or by calling the cloud
     API directly from this file if it turns out to be simple enough).

Until then, every action just tells the caller the feature isn't ready yet,
so the phone menu is safe to ship and test end-to-end (PIN, routing, vehicle
selection) without pretending the car integration works.
"""

from .base import VehicleAdapter


class MaxusAdapter(VehicleAdapter):
    brand_id = "maxus"
    display_name = "מקסוס"

    def menu_prompt(self) -> str:
        return (
            "התמיכה ברכבי מקסוס מיפה עדיין בפיתוח. "
            "לחץ כוכבית לחזרה לתפריט הראשי."
        )

    def handle_choice(self, choice: str):
        # No real digit choices yet - anything typed just repeats the
        # "not ready" message via the menu prompt above.
        return None
