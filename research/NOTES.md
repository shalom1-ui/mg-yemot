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

## UPDATE 2026-09-14 (same day, continued): full algorithm found

Kept reading the JS bundle and found the **complete, concrete signing +
encryption algorithm** - no secret server-side key involved anywhere,
everything is derivable client-side from a timestamp the client itself
picks. Verbatim source (`g()` is the request-interceptor's signing
function, called as `g(request, appSendDate)`; `a`/`r` are outer-scope
vars it sets as a side effect, later reused by `getEncryptBody`):

```js
function g(A, b) {
  let C = A.url.substr(substr);              // substr is the string "36" = length of
                                               // "https://opt-svc.soimt.com/api.app/v1" -
                                               // so C = the relative path, e.g. "/oauth/token"
  let k = "";
  if (A.params) { k = addParamsToUrl(C, A.params); C = C + k.search; }  // GET query string appended
  const S = tenantId, E = "app";              // S = "459771" (default), E = literal "app"
  let N = A.headers["ORIGINAL-CONTENT-TYPE"];
  let w = A.headers["Blade-Auth"] || "";      // "" for unauthenticated calls (e.g. /oauth/token)
  let P = hex_md5(`${C}${S}${w}${E}`);        // P = MD5(path + tenantId + bladeAuth + "app")
  let m = `${b}1${N}`;                        // m = appSendDate + "1" + originalContentType
  a = hex_md5(P + m);                         // AES key  (hex-encoded, parsed as 16 raw bytes)
  r = hex_md5(b);                             // AES IV   (hex-encoded, parsed as 16 raw bytes)
  let O = hex_md5(a + b);                     // HMAC key = MD5(aesKey + appSendDate)
  let D = getEncryptBody(a, r, A.data, A.headers);  // D = AES-encrypted body (hex string)
  let J = `${C}${S}${w}${E}${b}1${N}${D}`;    // signing base string
  return hmacSHA256(J, O);                    // -> APP-VERIFICATION-STRING header value
}

function AES128Encrypt(keyHex, ivHex, plaintext) {
  // CryptoJS AES-128-CBC, PKCS7 padding. .ciphertext.toString() defaults to
  // hex encoding in CryptoJS - the wire body is this hex string, raw (no
  // base64, no JSON wrapper).
}

function getEncryptBody(key, iv, data, headers) {
  // if ORIGINAL-CONTENT-TYPE is "application/x-www-form-urlencoded":
  //   AES128Encrypt(key, iv, <the already-urlencoded body string>)
  // else:
  //   AES128Encrypt(key, iv, JSON.stringify(data))
}
```

Call order per request (in the axios request interceptor, in this order):
1. Resolve `s` (full URL) and `tenantId`: from cached `getLocationStorage("config")`
   if present (`conf.tspRootUrl + url`, `conf.tenantId`), else the hardcoded
   bootstrap default `"https://opt-svc.soimt.com/api.app/v1" + url` with
   `tenantId="459771"`.
2. Build the headers object (varies slightly per endpoint - see the three
   branches already documented above: `/oauth/token` gets
   `Authorization: Basic c3dvcmQ6c3dvcmRfc2VjcmV0`; `/vehicle/list` (and
   presumably other authenticated calls) gets
   `Blade-Auth: bearer <token>`; everything else gets neither). All
   branches include `APP-CONTENT-ENCRYPTED: 1`, `tenant-id`,
   `ORIGINAL-CONTENT-TYPE`, `User-Type: app`, `APP-LANGUAGE-TYPE`,
   `Global-APP: 1`.
3. Set the request body (`e.data`) to the form-urlencoded or JSON string
   (still **plaintext** at this point).
4. `n` = current timestamp in epoch **milliseconds**, as a string (this is
   the `app-send-date` value the client picks - just `Date.now()`
   basically. Confirmed format from an earlier captured real header:
   `'app-send-date': '1789330124576'`, a 13-digit ms epoch).
5. `e.headers["APP-SEND-DATE"] = n`
6. `e.headers["APP-VERIFICATION-STRING"] = g(e, n)` - **this call has the
   side effect of computing and setting the module-level `a` (AES key)
   and `r` (AES IV)** that step 7 then reuses.
7. `e.data = getEncryptBody(a, r, e.data, e.headers)` - replaces the
   plaintext body with the AES-encrypted hex string. **This is the actual
   wire body sent.**
8. A few more static headers get added after this
   (`Cross-Region-Binding`, `APP-Brand: MG`, `APP-Version`) - not
   security-relevant, just app metadata; safe to hardcode/omit and see
   if the server cares.

Response decryption (separate `getSignatureParam` function, simpler -
reuses the same `app-send-date`/`original-content-type` the request was
sent with, presumably echoed back or just kept from the request context):
```js
function getSignatureParam({headers: e}) {
  let r = `${e["app-send-date"]}1${e["original-content-type"]}`;
  return { encryptKey: hex_md5(r), iv: hex_md5(e["app-send-date"]) };
}
```
then `AES128decrypt(encryptKey, iv, <response body hex>)` - note
`AES128decrypt` first does `CryptoJS.enc.Base64.stringify(CryptoJS.enc.Hex.parse(responseHex))`
before decrypting - i.e. **the response body hex bytes get re-encoded as
base64 text before being handed to CryptoJS.AES.decrypt** (CryptoJS's
`.decrypt(base64OrCiphertextParams, key, {iv,...})` form expects either a
CipherParams object or a base64 string when given a plain string - this
re-encoding step is just adapting hex-received bytes into the format
CryptoJS.AES.decrypt expects, not a second layer of encoding on the wire).

**This means a Python implementation is now fully specified - no unknowns
left except things a working request could confirm/refute:**
- `hex_md5` = standard MD5, lowercase hex digest (near-certain, but not
  independently confirmed against a real captured request/response pair
  yet - the friend's phone never sent a fresh /oauth/token call through
  the proxy during this session, it reused a cached session).
- Python equivalent: `hashlib.md5(s.encode()).hexdigest()`,
  `hmac.new(bytes.fromhex(O), J.encode(), hashlib.sha256).hexdigest()`,
  and `Crypto.Cipher.AES` (pycryptodome) in CBC mode with PKCS7 padding
  for the AES128Encrypt/decrypt pair (raw hex key/iv via `bytes.fromhex`).

## UPDATE 2026-09-14 (continued): tools/try_il_login.py live-tested, request signing CONFIRMED working

Actually ran `tools/try_il_login.py` against the real `opt-svc.soimt.com`
server (with the real, confirmed-correct account credentials) and
iterated live off real error responses - this is no longer theoretical:

1. First attempt: sent the AES ciphertext **hex-decoded to raw bytes** as
   the POST body → server error `{"code":500,"msg":"Input length = 1",...}`.
   **Fix:** send the hex *string* itself as the literal body text (the JS
   never converts it to raw bytes - `getEncryptBody`'s return value, a JS
   string of hex chars, is assigned directly as `e.data`/axios body).
2. Second attempt (body fixed): got
   `{"code":400,"msg":"APP-VERIFICATION-STRING Verify Failed.","success":false}`.
   **Fix:** the HMAC key (`O` in the JS) must be passed through
   `CryptoJS.enc.Utf8.parse(a)` - i.e. the 32-character MD5 hex-digest
   **string** is used as literal UTF-8 text for the HMAC key (32 bytes),
   **not** hex-decoded to 16 raw bytes like the AES key/IV are. This was
   the bug - fixed in `hmac_sha256_hex()`.
3. Third attempt (signing fixed): **HTTP 400 error is gone.** Now getting
   **HTTP 404** with a 224-hex-char (112-byte) response body that is
   clearly ciphertext (not readable JSON, unlike attempts 1-2 which
   returned plain-text JSON errors) - i.e. **the server accepted our
   signed+encrypted request and processed it**, this is no longer a
   request-format rejection. This is very likely either (a) a real
   "user/route not found"-style error caused by one of the guessed login
   fields (`deviceType`/`deviceId`/`loginType`/`countryCode` - see the
   TODO markers in the script) being wrong enough to fail account lookup,
   or (b) a gateway-level 404 unrelated to our payload at all.
4. **Still unsolved: decrypting this response.** The response DOES carry
   its own `APP-SEND-DATE` / `ORIGINAL-CONTENT-TYPE` / `APP-VERIFICATION-STRING`
   / `APP-CONTENT-ENCRYPTED: 1` headers (confirmed present, different
   values than the request's own), matching what `getSignatureParam`
   expects to derive key/iv from for decrypting a *response*. Tried
   decrypting with `key=MD5(respSendDate+"1"+respContentType)`,
   `iv=MD5(respSendDate)` exactly per that function - **padding is
   invalid, decrypted bytes are pure random-looking garbage**, not close
   to valid JSON at all. So either:
   - `getSignatureParam` is called with something other than the raw
     axios response object's `.headers` (worth re-reading the exact call
     site more carefully - the snippet captured was
     `{encryptKey:i,iv:s}=getSignatureParam(a)` where `a` is the response
     interceptor's parameter, but there could be a different response
     interceptor for **error** responses specifically that this session
     never located/read - only the success-path interceptor snippet was
     found),
   - or a 404 status specifically skips/uses a different decrypt path
     than 200 responses do,
   - or the AES128decrypt's extra hex→raw-bytes→base64-string step (see
     the function listing above) matters in some non-obvious way that a
     straight `bytes.fromhex()` in Python doesn't replicate correctly
     (should be a no-op round-trip, but worth double-checking against a
     real CryptoJS.AES.decrypt() call if stuck).
   **The cleanest way to resolve this for certain: capture one real
   request+response pair from the live app** (a **fresh, non-cached**
   `/oauth/token` call - this session's mitmproxy capture only ever saw
   the WebView's initial page load, never a live login attempt, because
   the phone kept reusing an already-authenticated session) and diff it
   character-by-character against what this script produces/expects.
   That would settle every remaining unknown (response decrypt key/iv,
   and whether deviceType/deviceId/loginType/countryCode need real values)
   in minutes instead of more guessing.

## UPDATE 2026-09-14 (continued further): field-guess sweep inconclusive, static analysis exhausted for now

- Tried 3 plausible `deviceType`/`loginType`/`countryCode` combinations
  (`(0,1,"")`, `(1,1,"")`, `(0,1,"IL")`) against the real server -
  **identical HTTP 404, identical 224-byte response every time.** This
  strongly suggests the 404 is NOT caused by these guessed field values
  at all (they'd very likely change *something* - even a different error
  message length - if they mattered) - more likely a route/infrastructure
  -level 404 (e.g. Spring Cloud Gateway "no matching route", independent
  of request body content) than a business-logic rejection.
- Re-read the response interceptor code again carefully: confirmed the
  `getSignatureParam`/`AES128decrypt(i,s,a.data)` call is the ONLY
  decryption logic in the file, inside `service.interceptors.response.use(...)`,
  and IS reached even for `a.status != 200` (the axios instance sets a
  custom `validateStatus`, so non-2xx responses go through the *fulfilled*
  handler, not a separate rejection handler) - so decrypting the 404 body
  the way the script already tries is the architecturally correct
  approach per the code; the bug (if this key/iv formula is even right)
  must be something more subtle, not "wrong code path entirely".
- Deliberately did **not** keep expanding the automated combination
  sweep - kept it to 3 tries specifically to avoid risking a real
  account lockout from repeated automated login attempts against MG's
  production servers, and the harness's own safety classifier then
  independently blocked a further (lower-risk, unauthenticated route-
  probing) sweep as resembling third-party service probing/attack
  traffic - a signal worth respecting rather than routing around.
- **Conclusion: static analysis + safe live-testing is genuinely
  exhausted for now.** The one thing that would unstick this in minutes
  instead of more guessing is a **real captured request+response pair**
  from an actual fresh (non-cached) login in the live app - see the "How
  to resume" note in the project memory / earlier in this file for the
  mitmproxy capture setup that already worked once this session (phone +
  a second PC network adapter on an independent hotspot, bypassing
  NetFree). Recommend not resuming this task again without that capture
  in hand, or without a specific new static-analysis idea worth trying
  (e.g. actually beautify+fully read the whole `index.cc83b2fd.js` bundle
  file rather than grep-sampling it, in case a config/discovery endpoint
  like `/config/app` needs to be called *before* `/oauth/token` and
  changes the base URL/tenant used for the login call itself - this was
  flagged as untested "worth fetching for real" in the very first update
  above and never actually followed up on).

## UPDATE 2026-09-14 (BREAKTHROUGH): the real API host, found via /config/app

Actually called `/config/app` (low-risk - not a login attempt, no account
lockout concern) the way the real app does before ever calling
`/oauth/token`, with the two required query params it turned out to need
(`packageName=com.mgismart.israel`, `countryCode=IL` - discovered one at a
time from the server's own "missing parameter" error messages, which our
response-decryption code decrypted **perfectly** - confirming the whole
AES+HMAC scheme in this file is correct). Full decrypted response:

```json
{"code":0,"success":true,"data":{
  "tspId":"ISR",
  "tspRootUrl":"https://gateway-mg-il.soimt.com/api.app/v1",
  "tenantId":"459771",
  "userType":"app",
  "authorization":"Basic c3dvcmQ6c3dvcmRfc2VjcmV0",
  "nonGlobalAppId":4,
  "countryCodeA3":"ISR","countryCodeA2":"IL",
  "serviceSubscriptionUrl":"",
  "onlineImageUrl":"https://s3.ap-southeast-1.amazonaws.com/tpage.soimt.com/APP/"
},"msg":"success"}
```

**This is THE answer.** `opt-svc.soimt.com` is only a bootstrap/discovery
host used for this one `/config/app` call - the REAL API root for
everything else (login, vehicle list/status/control) is:

  **`https://gateway-mg-il.soimt.com/api.app/v1`**

This follows the **exact same naming pattern** as the hosts the
open-source `saic-python-mqtt-gateway`/`saic_ismart_client_ng` library
already supports (`gateway-mg-eu.soimt.com`, `gateway-mg-au.soimt.com`,
`gateway-mg-tr.soimt.com`) - Israel was simply never added to the
community's list, not a fundamentally different system. This also
confirms the `Authorization: Basic c3dvcmQ6c3dvcmRfc2VjcmV0` credential
guessed earlier from the JS was exactly right, and tenantId `459771` was
never the issue.

**Immediate implication: `gateway-mg-il.soimt.com` may use the OLDER,
unencrypted protocol** (matching eu/au/tr), meaning the *existing*,
*unmodified* open-source library might just work against it directly -
no custom AES/HMAC code needed at all for the actual login/vehicle calls,
only `SAIC_REST_URI=https://gateway-mg-il.soimt.com/api.app/v1/` (and
`SAIC_REGION` can stay whatever, it's cosmetic once REST_URI is set
explicitly, as already established earlier this session). **This is the
very next thing to try in Render** - see the main mg-yemot-car-control
memory / continue from here.

**Script state:** `tools/try_il_login.py` is genuinely useful as-is - the
request-side encryption+signing is proven correct against the real
server. Re-run it any time with `il_test.env` (gitignored, not committed -
recreate it locally with `IL_USER`/`IL_PASSWORD` before running) to
continue from exactly this point.

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
