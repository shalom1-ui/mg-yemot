"""
Reverse-engineered login attempt against the MG iSMART IL ("Israel") API,
reimplementing the client-side request signing/encryption found by reading
research/mg-ismart-il_index.cc83b2fd.js and research/mg-ismart-il_login.js
(the app's own frontend JS) - see research/NOTES.md for the full writeup.

NOT yet validated against a real captured request/response pair (the
friend's phone never sent a fresh /oauth/token call through our mitmproxy
capture during the session this was written in - it reused a cached
session). Treat every field here as "very likely correct, derived from
reading the real client code" rather than "confirmed working".

Algorithm summary (see research/NOTES.md for the full derivation):
  - Base URL: https://opt-svc.soimt.com/api.app/v1 (bootstrap default)
  - tenantId: "459771" (same default the open-source SAIC-iSmart-API
    library already uses for the old, non-IL region servers)
  - AES key  = MD5( MD5(path + tenantId + bladeAuth + "app") + appSendDate + "1" + contentType )
  - AES IV   = MD5(appSendDate)
  - body     = AES-128-CBC/PKCS7 encrypt of the form-urlencoded login
               fields, ciphertext hex-encoded (CryptoJS default) - this
               hex string IS the raw HTTP body, no extra JSON wrapper.
  - signature (header APP-VERIFICATION-STRING) =
      HMAC-SHA256(path+tenantId+bladeAuth+"app"+appSendDate+"1"+contentType+encryptedBodyHex,
                   key=MD5(aesKeyHex + appSendDate))
  - login form fields: grant_type=password, username=<email>,
    password=SHA1(<plaintext password>), deviceType, deviceId, loginType,
    countryCode, scope=all  (deviceType/deviceId/loginType/countryCode
    exact required values NOT confirmed - using plausible placeholders,
    see TODO markers below)

HOW TO RUN (don't paste real credentials into chat - only into a local
.env-style file, same pattern as tools/try_maxus_login.py):
  1. pip install pycryptodome requests
  2. Create il_test.env next to this script with:
       IL_USER=the account email
       IL_PASSWORD=the account password (plaintext - this script hashes it)
  3. python try_il_login.py il_test.env
"""

import hashlib
import hmac
import json
import sys
import time
import uuid
from urllib.parse import urlencode

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

BASE_URL = "https://opt-svc.soimt.com/api.app/v1"
TENANT_ID = "459771"
OAUTH_BASIC_AUTH = "Basic c3dvcmQ6c3dvcmRfc2VjcmV0"  # base64("sword:sword_secret")


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


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def hmac_sha256_hex(message: str, key_hex: str) -> str:
    return hmac.new(bytes.fromhex(key_hex), message.encode("utf-8"), hashlib.sha256).hexdigest()


def aes128_encrypt_hex(key_hex: str, iv_hex: str, plaintext: str) -> str:
    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return ct.hex()


def aes128_decrypt_hex(key_hex: str, iv_hex: str, ciphertext_hex: str) -> str:
    key = bytes.fromhex(key_hex)
    iv = bytes.fromhex(iv_hex)
    ct = bytes.fromhex(ciphertext_hex)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pt = unpad(cipher.decrypt(ct), AES.block_size)
    return pt.decode("utf-8")


def sign_and_encrypt(path: str, content_type: str, form_fields: dict, blade_auth: str = ""):
    """Returns (headers_to_add, encrypted_body_hex)."""
    app_send_date = str(int(time.time() * 1000))
    body_plain = urlencode(form_fields) if content_type == "application/x-www-form-urlencoded" else json.dumps(form_fields)

    p = md5_hex(f"{path}{TENANT_ID}{blade_auth}app")
    m = f"{app_send_date}1{content_type}"
    aes_key = md5_hex(p + m)
    aes_iv = md5_hex(app_send_date)
    encrypted_body_hex = aes128_encrypt_hex(aes_key, aes_iv, body_plain)

    hmac_key = md5_hex(aes_key + app_send_date)
    signing_base = f"{path}{TENANT_ID}{blade_auth}app{app_send_date}1{content_type}{encrypted_body_hex}"
    verification_string = hmac_sha256_hex(signing_base, hmac_key)

    headers = {
        "ORIGINAL-CONTENT-TYPE": content_type,
        "Content-Type": content_type + ";" if content_type == "application/x-www-form-urlencoded" else "text/plain",
        "User-Type": "app",
        "APP-CONTENT-ENCRYPTED": "1",
        "tenant-id": TENANT_ID,
        "APP-LANGUAGE-TYPE": "en",
        "APP-SEND-DATE": app_send_date,
        "APP-VERIFICATION-STRING": verification_string,
        "Global-APP": "1",
        "APP-Brand": "MG",
    }
    if blade_auth:
        headers["Blade-Auth"] = f"bearer {blade_auth}"
    return headers, encrypted_body_hex, aes_key, aes_iv


def login(username: str, password: str):
    path = "/oauth/token"
    content_type = "application/x-www-form-urlencoded"
    fields = {
        "grant_type": "password",
        "username": username,
        "password": sha1_hex(password),
        # TODO: these four are best-effort guesses from reading the JS - not
        # confirmed against a real request. If login fails with something
        # OTHER than a clean auth error, try adjusting these first.
        "deviceType": "0",
        "deviceId": str(uuid.uuid4()),
        "loginType": "1",
        "countryCode": "",
        "scope": "all",
    }
    headers, body_hex, aes_key, aes_iv = sign_and_encrypt(path, content_type, fields)
    headers["Authorization"] = OAUTH_BASIC_AUTH

    url = BASE_URL + path
    print(f"POST {url}")
    print(f"headers: {headers}")
    resp = requests.post(url, headers=headers, data=bytes.fromhex(body_hex), timeout=15)
    print(f"HTTP {resp.status_code}")
    raw = resp.text
    print(f"raw response ({len(raw)} chars): {raw[:300]}")

    if resp.status_code == 200 and raw:
        try:
            decrypted = aes128_decrypt_hex(aes_key, aes_iv, raw.strip())
            print("DECRYPTED RESPONSE:")
            print(decrypted)
        except Exception as e:
            print(f"(response wasn't decryptable with the request's own key/iv - {type(e).__name__}: {e})")
            print("This likely means the response uses different key derivation than assumed - see getSignatureParam in research/NOTES.md, may need the response's own headers, not the request's.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python try_il_login.py <path-to-env-file>")
        sys.exit(1)
    env = load_env_file(sys.argv[1])
    for required in ("IL_USER", "IL_PASSWORD"):
        if not env.get(required):
            print(f"Missing {required} in {sys.argv[1]}")
            sys.exit(1)
    login(env["IL_USER"], env["IL_PASSWORD"])
