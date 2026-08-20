"""
One-shot experiment: does the Maxus/MIFA account log in through the SAME
backend as MG iSMART? If yes, we can very likely reuse the whole existing
mg-gateway integration for Maxus almost for free. If no, we fall back to
the traffic-capture plan in README.md ("מיפה - הצעדים הבאים").

This talks to the REAL saic-python-client-ng library (the same one the
mg-gateway container uses) - the exact usage below (SaicApi +
SaicApiConfiguration, async login(), vehicle_list()) is copied from
mqtt_gateway.py in https://github.com/SAIC-iSmart-API/saic-python-mqtt-gateway
(not guessed), but this specific script has NOT been run/tested yet, since
doing so requires a real Maxus account - that's exactly what running it here
is for. If a method name has changed upstream, the error message will say so
clearly (AttributeError/TypeError) rather than fail silently.

HOW TO RUN THIS SAFELY (don't paste the friend's password into chat with
Claude - fill it only in the local .env file, which stays on this machine):

  1. cd tools
  2. python -m venv .venv && .venv\\Scripts\\activate      (Windows)
     or: python3 -m venv .venv && source .venv/bin/activate (Mac/Linux)
  3. pip install saic_ismart_client_ng
  4. Create a file named maxus_test.env next to this script (NOT committed -
     it's already covered by the repo's .gitignore pattern for *.env) with:
        MAXUS_USER=the_friend's_hi_maxus_login_email_or_phone
        MAXUS_PASSWORD=the_friend's_hi_maxus_password
        MAXUS_REGION=eu
        MAXUS_USERNAME_IS_EMAIL=true
  5. Run: python try_maxus_login.py maxus_test.env
  6. Read the output. Three possible outcomes:
       - "LOGIN FAILED" with an auth error -> different backend (or wrong
         credentials - double check those first). Move to the mitmproxy plan.
       - "LOGIN OK" but "NO VEHICLES FOUND" -> ambiguous, worth a second look
         but probably means a separate backend too.
       - "LOGIN OK" and a vehicle shows up with a VIN -> great news, tell
         Claude the VIN + the brand/model text that was printed, and the
         Maxus adapter can very likely be built the same way mg.py was.
  7. Delete maxus_test.env when done (or at least don't share it).
"""

import asyncio
import os
import sys


def load_env_file(path: str) -> dict:
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


async def main(env_path: str):
    env = load_env_file(env_path)
    for required in ("MAXUS_USER", "MAXUS_PASSWORD"):
        if not env.get(required):
            print(f"Missing {required} in {env_path}")
            sys.exit(1)

    try:
        from saic_ismart_client_ng import SaicApi
        from saic_ismart_client_ng.model import SaicApiConfiguration
    except ImportError:
        print("saic_ismart_client_ng isn't installed. Run: pip install saic_ismart_client_ng")
        sys.exit(1)

    config = SaicApiConfiguration(
        username=env["MAXUS_USER"],
        password=env["MAXUS_PASSWORD"],
        username_is_email=env.get("MAXUS_USERNAME_IS_EMAIL", "true").lower() == "true",
        region=env.get("MAXUS_REGION", "eu"),
    )
    api = SaicApi(configuration=config)

    print(f"Attempting login as {env['MAXUS_USER']!r} against the MG/SAIC backend (region={config.region})...")
    try:
        await api.login()
    except Exception as e:
        print(f"LOGIN FAILED: {type(e).__name__}: {e}")
        print("-> Backend is likely NOT shared with MG (or the credentials are wrong - double check those first).")
        print("-> Next step: the mitmproxy traffic-capture plan in README.md.")
        return

    print("LOGIN OK - now listing vehicles on this account...")
    try:
        vehicles = await api.vehicle_list()
    except Exception as e:
        print(f"Login succeeded but vehicle_list() failed: {type(e).__name__}: {e}")
        return

    vin_list = getattr(vehicles, "vinList", None) or getattr(vehicles, "vin_list", None) or vehicles
    if not vin_list:
        print("LOGIN OK but NO VEHICLES FOUND on this account via the MG backend.")
        print("-> Ambiguous - probably a separate backend. Move to the mitmproxy plan.")
        return

    print(f"LOGIN OK and {len(vin_list)} vehicle(s) found via the MG/SAIC backend:")
    for v in vin_list:
        print(" -", v)
    print()
    print("^ Copy this output and share it with Claude - this is great news if it got here.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python try_maxus_login.py <path-to-env-file>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
