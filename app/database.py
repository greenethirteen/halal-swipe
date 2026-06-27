import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .settings import get_settings


def _db_path() -> Path:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", ""))
    return Path(url)


@contextmanager
def db() -> Iterable[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True) if path.parent != Path('.') else None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(query, params).fetchone()


def all_rows(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(query, params).fetchall()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db() as conn:
        cur = conn.execute(query, params)
        return cur.lastrowid


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                subscription_status TEXT NOT NULL DEFAULT 'none',
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_code TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                profile_type TEXT NOT NULL DEFAULT 'Unknown',
                full_name TEXT,
                age INTEGER,
                height TEXT,
                city TEXT,
                district TEXT,
                country TEXT DEFAULT 'Sri Lanka',
                marital_status TEXT,
                education TEXT,
                profession TEXT,
                family_background TEXT,
                faith_notes TEXT,
                expectations TEXT,
                bio_summary TEXT,
                contact_details TEXT,
                raw_text TEXT,
                image_path TEXT,
                source_name TEXT,
                source_sender TEXT,
                source_message_at TEXT,
                import_hash TEXT UNIQUE,
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_profiles_status ON profiles(status);
            CREATE INDEX IF NOT EXISTS idx_profiles_type ON profiles(profile_type);
            CREATE INDEX IF NOT EXISTS idx_profiles_city ON profiles(city);
            CREATE INDEX IF NOT EXISTS idx_profiles_age ON profiles(age);

            CREATE TABLE IF NOT EXISTS contact_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, profile_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                source_name TEXT,
                total_candidates INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )
