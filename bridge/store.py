"""
Multi-tenant user store: one row per person who has connected their own MG
account to this bridge. SQLite on a local file (a Render persistent disk in
production - see render.yaml) since the whole dataset is tiny (a handful of
users, not a high-write workload) and this avoids paying for/operating a
separate database service.

Car-account passwords are encrypted at rest with Fernet (symmetric,
authenticated encryption) - unlike the single-account env-var setup this
replaces, this file now holds OTHER PEOPLE's MG account passwords, so
storing them in plaintext would be irresponsible even on a private disk.
The encryption key itself lives only in the ENCRYPTION_KEY env var, never
in the database.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass

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
                "passwords. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode())
    return _fernet


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            pin TEXT UNIQUE NOT NULL,
            brand TEXT NOT NULL DEFAULT 'mg',
            mg_email TEXT NOT NULL,
            mg_password_enc BLOB NOT NULL,
            saic_base_uri TEXT NOT NULL,
            saic_region TEXT NOT NULL,
            saic_tenant_id TEXT NOT NULL,
            vin TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    return conn


@dataclass
class User:
    id: int
    phone: str | None
    pin: str
    brand: str
    mg_email: str
    mg_password: str  # decrypted
    saic_base_uri: str
    saic_region: str
    saic_tenant_id: str
    vin: str | None


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        phone=row["phone"],
        pin=row["pin"],
        brand=row["brand"],
        mg_email=row["mg_email"],
        mg_password=_get_fernet().decrypt(row["mg_password_enc"]).decode(),
        saic_base_uri=row["saic_base_uri"],
        saic_region=row["saic_region"],
        saic_tenant_id=row["saic_tenant_id"],
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
    mg_email: str,
    mg_password: str,
    saic_base_uri: str,
    saic_region: str,
    saic_tenant_id: str,
    vin: str | None = None,
) -> User:
    enc = _get_fernet().encrypt(mg_password.encode())
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO users
                (phone, pin, brand, mg_email, mg_password_enc,
                 saic_base_uri, saic_region, saic_tenant_id, vin, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (phone, pin, brand, mg_email, enc, saic_base_uri, saic_region,
             saic_tenant_id, vin, time.time()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_user(row)


def set_vin(user_id: int, vin: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("UPDATE users SET vin = ? WHERE id = ?", (vin, user_id))
        conn.commit()
