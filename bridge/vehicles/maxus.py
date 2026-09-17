"""
Maxus / MIFA (7, 9, eT60, eDeliver...) adapter.

2026-09-17: found a community-maintained Dart client
(tanguymossion/saic_ismart, MIT) whose own README states Maxus/LDV
vehicles are controlled through the SAME protocol as MG - not a separate
backend, despite the official consumer app being a different one ("Hi
MAXUS Europe"). This lines up with something a real service technician
(at the parallel-import dealer "אוטו חן") told the user the same day:
the official Maxus app isn't available in Israel due to European
certification/regulatory reasons - which is a distribution problem with
THAT specific app, not evidence the vehicle's cloud connectivity itself
is unavailable.

Working theory, not yet confirmed on a real Maxus vehicle: if a Maxus/MIFA
owner can register their VIN through the MG iSMART app instead of the
unavailable Maxus one (both talk to the same SAIC iSMART cloud), this
exact adapter - unmodified - should work, since it's just MgAdapter with a
different brand id/display name. If real-world testing shows Maxus
needs something MG doesn't (a different tenant id, a different vehicle
list shape, extra vehicle-type handling), that's the first thing to
check and adjust here.
"""

from __future__ import annotations

from .mg import MgAdapter


class MaxusAdapter(MgAdapter):
    brand_id = "maxus"
    display_name = "מקסוס / מיפה"
