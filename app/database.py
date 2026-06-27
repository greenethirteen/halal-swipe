"""Database layer that works with both SQLite (local) and Postgres (production).

The engine is chosen from DATABASE_URL:
  - "app.db" / "sqlite:///..."  -> SQLite (default, local dev)
  - "postgres://..." / "postgresql://..." -> Postgres (Railway)
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .settings import get_settings


def _url() -> str:
    url = (get_settings().database_url or "").strip()
    # Empty, or an unresolved Railway reference like "${{Postgres.DATABASE_URL}}",
    # falls back to a local SQLite file so the app boots instead of crashing.
    if not url or url.startswith("${"):
        return "app.db"
    return url


def is_postgres() -> bool:
    return _url().startswith(("postgres://", "postgresql://"))


def _pg_dsn() -> str:
    url = _url()
    # psycopg prefers the postgresql:// scheme
    return url.replace("postgres://", "postgresql://", 1)


def _sqlite_path() -> Path:
    url = _url()
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", ""))
    return Path(url)


class _Conn:
    """Thin wrapper so the same `?`-placeholder SQL runs on both engines."""

    def __init__(self, raw, pg: bool):
        self._raw = raw
        self._pg = pg

    def execute(self, query: str, params: tuple[Any, ...] = ()):
        if self._pg:
            if "INSERT OR IGNORE" in query.upper():
                query = (
                    query.replace("INSERT OR IGNORE INTO", "INSERT INTO").rstrip().rstrip(";")
                    + " ON CONFLICT DO NOTHING"
                )
            query = query.replace("?", "%s")
        return self._raw.execute(query, params)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


@contextmanager
def db() -> Iterable[_Conn]:
    if is_postgres():
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(_pg_dsn(), row_factory=dict_row, autocommit=False)
        conn = _Conn(raw, pg=True)
    else:
        path = _sqlite_path()
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        conn = _Conn(raw, pg=False)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ignore_conflict(query: str, pg: bool) -> str:
    """Translate SQLite 'INSERT OR IGNORE' to Postgres 'ON CONFLICT DO NOTHING'."""
    if pg and "INSERT OR IGNORE" in query.upper():
        q = query.replace("INSERT OR IGNORE INTO", "INSERT INTO").rstrip().rstrip(";")
        return q + " ON CONFLICT DO NOTHING"
    return query


def one(query: str, params: tuple[Any, ...] = ()):
    with db() as conn:
        return conn.execute(query, params).fetchone()


def all_rows(query: str, params: tuple[Any, ...] = ()) -> list:
    with db() as conn:
        return conn.execute(query, params).fetchall()


def execute(query: str, params: tuple[Any, ...] = ()) -> int | None:
    pg = is_postgres()
    query = _ignore_conflict(query, pg)
    with db() as conn:
        if pg and query.lstrip().upper().startswith("INSERT") and "RETURNING" not in query.upper():
            query = query.rstrip().rstrip(";") + " RETURNING id"
            cur = conn.execute(query, params)
            row = cur.fetchone()
            return row["id"] if row else None
        cur = conn.execute(query, params)
        return getattr(cur, "lastrowid", None)


# --- Schema ---------------------------------------------------------------

def _schema_statements(pg: bool) -> list[str]:
    pk = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts = "TIMESTAMP" if pg else "TEXT"
    return [
        f"""
        CREATE TABLE IF NOT EXISTS users (
            id {pk},
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            subscription_status TEXT NOT NULL DEFAULT 'none',
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS profiles (
            id {pk},
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
            created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_profiles_status ON profiles(status)",
        "CREATE INDEX IF NOT EXISTS idx_profiles_type ON profiles(profile_type)",
        "CREATE INDEX IF NOT EXISTS idx_profiles_city ON profiles(city)",
        "CREATE INDEX IF NOT EXISTS idx_profiles_age ON profiles(age)",
        f"""
        CREATE TABLE IF NOT EXISTS contact_views (
            id {pk},
            user_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL,
            created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, profile_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS import_batches (
            id {pk},
            filename TEXT NOT NULL,
            source_name TEXT,
            total_candidates INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            created_by_user_id INTEGER,
            created_at {ts} NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]


def init_db() -> None:
    pg = is_postgres()
    with db() as conn:
        for stmt in _schema_statements(pg):
            conn.execute(stmt)
