"""
Multi-tenant user store: one row per person who has connected their own car
account to this bridge. SQLite on a local file (a Render persistent disk in
production - see render.yaml) since the whole dataset is tiny (a handful of
users, not a high-write workload) and this avoids paying for/operating a
separate database service.

Car-account credentials are encrypted at rest with Fernet (symmetric,
authenticated encryption) - this file holds OTHER PEOPLE's car account
credentials, so storing them in plaintext would be irresponsible even on a
private disk. The encryption key itself lives only in the ENCRYPTION_KEY
env var, never in the database.

Credentials are a brand-agnostic encrypted JSON blob (`credentials`), not
named columns - added 2026-09-17 when a second brand (Chery/Jaecoo/Omoda)
needed a completely different credential shape (email + OAuth access/
refresh tokens + a second in-app security PIN) than MG's (email + password
+ region/base-uri/tenant-id). Each vehicle adapter defines and interprets
its own dict shape; store.py doesn't need to know it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from cryptography.fernet import Fernet

DB_PATH = os.environ.get("DB_PATH", "/data/mg_yemot.db")

_lock = threading.Lock()
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("ENCRYPTION_KEY", "")
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY is not set - required to store car-account "
                "credentials. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode())
    return _fernet


def encryption_error() -> str | None:
    """None if ENCRYPTION_KEY is usable, else a short reason. Lets the signup
    page refuse up front (before the person's car password goes anywhere)
    instead of failing with a bare 500 only after the cloud login succeeded."""
    try:
        _get_fernet()
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE,
        pin TEXT UNIQUE NOT NULL,
        brand TEXT NOT NULL,
        credentials_enc BLOB NOT NULL,
        vin TEXT,
        created_at REAL NOT NULL
    )
"""


def _migrate_v1_schema(conn: sqlite3.Connection) -> None:
    """
    The first multi-tenant deploy created `users` with MG-specific columns
    (mg_email, mg_password_enc, saic_*). `CREATE TABLE IF NOT EXISTS` never
    alters an existing table, so on a persistent disk that already holds the
    old table every INSERT failed with "no column named credentials_enc"
    (found 2026-10-27 on the first real signup). Convert in place: rows are
    decrypted/re-packed BEFORE any DDL runs, so a bad ENCRYPTION_KEY aborts
    with nothing changed; the table swap itself is one transaction.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if not cols or "credentials_enc" in cols or "mg_password_enc" not in cols:
        return
    converted = []
    old_rows = conn.execute("SELECT * FROM users").fetchall()
    if old_rows:
        f = _get_fernet()
        for row in old_rows:
            creds = {
                "mg_email": row["mg_email"],
                "mg_password": f.decrypt(row["mg_password_enc"]).decode(),
                "saic_base_uri": row["saic_base_uri"],
                "saic_region": row["saic_region"],
                "saic_tenant_id": row["saic_tenant_id"],
            }
            converted.append((
                row["id"], row["phone"], row["pin"], row["brand"] or "mg",
                f.encrypt(json.dumps(creds).encode()), row["vin"], row["created_at"],
            ))
    conn.commit()
    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE users RENAME TO users_v1_old")
        conn.execute(_SCHEMA)
        conn.executemany(
            "INSERT INTO users (id, phone, pin, brand, credentials_enc, vin, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            converted,
        )
        conn.execute("DROP TABLE users_v1_old")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _migrate_v1_schema(conn)
    conn.execute(_SCHEMA)
    return conn


@dataclass
class User:
    id: int
    phone: str | None
    pin: str
    brand: str
    credentials: dict = field(default_factory=dict)  # decrypted, brand-specific shape
    vin: str | None = None


def _row_to_user(row: sqlite3.Row) -> User:
    creds_json = _get_fernet().decrypt(row["credentials_enc"]).decode()
    return User(
        id=row["id"],
        phone=row["phone"],
        pin=row["pin"],
        brand=row["brand"],
        credentials=json.loads(creds_json),
        vin=row["vin"],
    )


def get_user_by_phone(phone: str) -> User | None:
    if not phone:
        return None
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return _row_to_user(row) if row else None


def get_user_by_pin(pin: str) -> User | None:
    if not pin:
        return None
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE pin = ?", (pin,)).fetchone()
        return _row_to_user(row) if row else None


def create_user(
    *,
    phone: str | None,
    pin: str,
    brand: str,
    credentials: dict,
    vin: str | None = None,
) -> User:
    enc = _get_fernet().encrypt(json.dumps(credentials).encode())
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (phone, pin, brand, credentials_enc, vin, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (phone, pin, brand, enc, vin, time.time()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_user(row)


def set_vin(user_id: int, vin: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("UPDATE users SET vin = ? WHERE id = ?", (vin, user_id))
        conn.commit()


def update_credentials(user_id: int, credentials: dict) -> None:
    """
    Overwrite a user's stored credentials. Needed for brands whose tokens
    rotate on use (Chery/Jaecoo/Omoda's refresh_token is invalidated the
    moment a new one is issued) - the freshly-issued value must be persisted
    immediately, not just held in memory, or a restart before the next
    natural refresh permanently loses access for that user.
    """
    enc = _get_fernet().encrypt(json.dumps(credentials).encode())
    with _lock, _connect() as conn:
        conn.execute("UPDATE users SET credentials_enc = ? WHERE id = ?", (enc, user_id))
        conn.commit()
