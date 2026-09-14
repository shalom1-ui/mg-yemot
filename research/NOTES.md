# MG iSMART IL (Israel) - reverse-engineering notes (2026-09-14)

Captured/derived while trying to connect the yemot_bridge gateway to a
real Israeli MG iSMART account. The account and vehicle work fine in the
**official app**, but the open-source `saic_ismart_client_ng` library
(used by `bridge/vehicles/mg.py` via the upstream
`saic-python-mqtt-gateway` container) cannot log in against it. This
folder documents why, so the next session doesn't have to redo the
discovery work.

## The real app

- Package name: **`com.mgismart.israel`** ("MG iSMART IL"), confirmed via
  the `x-requested-with` HTTP header captured live from the phone with
  mitmproxy - **not** the international `com.saicmotor.iov.europe` app
  the user/friend thought they'd installed.
- The app's home screen is a WebView loading
  `https://eu-aws.soimt.com/APP/Global/v2/index.html` (hosted on AWS
  CloudFront), which in turn loads a JS bundle:
  `https://eu-aws.soimt.com/APP/Global/v2/assets/index.cc83b2fd.js`
  (saved in this folder as `mg-ismart-il_index.cc83b2fd.js`, ~1.5MB
  minified/obfuscated JS - this is the actual frontend source that talks
  to the real API, safe to re-fetch any time this file goes stale, just
  re-download that URL and grep it).

## The real API

- Bootstrap/default base URL (found in the JS, used when no cached
  runtime config exists yet): **`https://opt-svc.soimt.com/api.app/v1`**
- Default `tenantId`: `"459771"` - **same value** the open-source library
  already defaults to, so tenant ID was a dead end, not the fix.
- Known relative paths (`globalApi` object in the JS):
  - `toLogin: "/oauth/token"` (matches what the old library already calls)
  - `getConfig: "/config/app"` - **called first**, before login; the
    response presumably contains a real `tspRootUrl`/`tenantId`/`tspId`
    that may differ from/override the hardcoded bootstrap default above.
    Never actually captured this response - worth fetching for real next
    time (with the right headers) to see if it points somewhere else
    entirely.
  - `getCountryLanguage: "/config/app/countryLanguage"`
  - `getVerifyCode: "/user/account/verificationCode"`
  - `accountRegister: "/user/account/register"`
  - `findPwd: "/user/account/forgotPassword"`
  - `getUserRegion: "/user/region"`
  - `getVinList: "/vehicle/list"`
  - `verifyCode: "/user/account/verificationCode/verify"`
  - `getCaptcha` / `checkCaptcha: "/user/account/captcha"` (`/check`)

## The actual blocker: request-body encryption

The JS sets a header **`"APP-CONTENT-ENCRYPTED": 1`** on every API call,
and the code path clearly branches on `getLocationStorage("isEncryption")`
to decide whether to encrypt the outgoing body/decrypt the response. This
is a real content-encryption layer on top of HTTPS (not just TLS), which
`saic_ismart_client_ng` (built against the older, unencrypted
`gateway-mg-eu.soimt.com` generation of the API) does not implement at
all. This is almost certainly *why* every login attempt returns either a
generic 404/"account not registered"-style error regardless of region
(`eu`/`au`/`tr`) or REST_URI guessed (`gateway-mg-eu`, `eu-aws`,
`opt-svc`) - the request body itself is unencrypted plaintext that the
new-generation server can't parse as a valid request, so it falls back to
a generic/opaque error rather than a real auth failure.

- The bundle uses **CryptoJS** (confirmed via string search: `AES`, `RSA`,
  `CryptoJS` all present, dozens of hits) - i.e. **standard** AES + RSA,
  not an exotic proprietary cipher. This is realistically replicable in
  Python (`pycryptodome` or `cryptography` packages implement compatible
  AES; CryptoJS's AES output format is well-documented/well-trodden
  territory for reverse-engineering, e.g. OpenSSL-compatible EVP_BytesToKey
  key derivation from a passphrase, or a raw key+IV scheme - need to read
  the actual JS call site to know which).
- Also found a fixed OAuth Basic-Auth credential for the `/oauth/token`
  call: header `Authorization: Basic c3dvcmQ6c3dvcmRfc2VjcmV0` → base64
  decodes to **`sword:sword_secret`** (client_id:client_secret). Worth
  checking whether the existing open-source library already sends this
  same Basic-Auth header (it may - this could be a stable,
  long-unchanged OAuth app registration shared across API generations)
  or not.
- Other headers sent per-endpoint (varies by endpoint, seen in the JS
  around the `APP-CONTENT-ENCRYPTED` header):
  `ORIGINAL-CONTENT-TYPE`, `Content-Type: application/x-www-form-urlencoded;`,
  `User-Type: app`, `tenant-id`, `APP-LANGUAGE-TYPE`, `Global-APP: 1`,
  and for authenticated calls (e.g. `/vehicle/list`):
  `Blade-Auth: bearer <token>` (a Spring-Cloud-Gateway "Blade" auth
  scheme token, presumably returned by the `/oauth/token` call).

## What real reverse-engineering work remains

To make `bridge/vehicles/mg.py` (or a new adapter) work against this
account, someone needs to:
1. Find the actual encryption function calls in the JS bundle (grep for
   `CryptoJS.AES` / `CryptoJS.RSA` usage sites) and read out the exact
   key, IV, mode, and padding used - and whether the key is static
   (hardcoded in the JS, in which case it's just data to copy) or
   derived per-request/per-session (harder).
2. Reimplement that same encrypt/decrypt in Python.
3. Write a minimal Python client that: calls `/config/app` first, then
   `/oauth/token` with the Basic-Auth header + encrypted body, decrypts
   the response, then calls `/vehicle/list` with the returned
   `Blade-Auth` bearer token.
4. Only once that works standalone (a throwaway test script, similar
   in spirit to `tools/try_maxus_login.py`) should it be wired into a
   real `VehicleAdapter`.

This is a genuine multi-hour focused coding task, not a config tweak -
explicitly paused here rather than rushed, per the user's own call at
the end of the 2026-09-14 session.

## How today's findings were obtained (for context/repeatability)

- Real traffic was captured from the friend's actual phone using
  **mitmproxy** (`mitmweb`, installed via `winget install --id
  mitmproxy.mitmproxy --source winget`) running on the user's Windows PC,
  with the phone's Wi-Fi manually proxied to the PC's LAN IP on port
  8080, and the mitmproxy CA cert installed on the phone via `mitm.it`.
  mitmweb needed `--set web_password=<fixed value>` (the auto-generated
  token printed to console was lost when output was redirected to a log
  file - set a fixed password up front next time) and `--ssl-insecure`
  (upstream cert verification otherwise failed with "unable to get local
  issuer certificate" against `eu-aws.soimt.com`).
- **NetFree** (a household "kosher internet" content filter, implemented
  as a Windows VPN connection profile named `RL-Netfree`, server
  `195.60.232.254`, auto-reconnects if manually disconnected via
  `rasdial`/`Disconnect-VpnConnection` - it's actively enforced, not just
  a default) blocked `eu-aws.soimt.com` and `data.winudf.com` (an APK
  mirror CDN) during this session. A same-machine routing-metric trick
  (`Set-NetIPInterface -InterfaceIndex <real adapter> -InterfaceMetric 1`)
  did NOT bypass it - NetFree intercepts deeper than routing metrics.
  What actually worked: connecting both the phone and a **second** PC
  network adapter (a USB Wi-Fi dongle) to a separate mobile hotspot
  network (seen in this session as `shm9922`), i.e. leaving the
  NetFree-filtered home network entirely for the diagnostic session.
  Legitimate unblock requests can be filed at
  netfree.link ("הפניות שלי" → "רשימת הפניות" → "שלח פניה חדשה",
  costs 1 "point", ~16 points/month free) but did not visibly take effect
  within this session's timeframe.
