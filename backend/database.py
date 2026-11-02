import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "cleaning.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # prevents "database is locked" with concurrent users
    return conn


def init():
    with get_conn() as conn:
        # Single shared dataset (no user_id) — the app is open, no login.
        # Sections reference their parent by name (not id) so the JSON log
        # payloads stay human-readable.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS house_persons (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    UNIQUE NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS house_sections (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT    UNIQUE NOT NULL,
                parent_name    TEXT,            -- nullable, references house_sections.name
                type           TEXT    NOT NULL DEFAULT 'Primary', -- 'Primary' | 'Secondary'
                active         INTEGER NOT NULL DEFAULT 1,
                clean_cooldown INTEGER NOT NULL DEFAULT 7,
                created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS house_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                log_id      TEXT    UNIQUE NOT NULL,
                person_name TEXT    NOT NULL,
                type        TEXT    NOT NULL,   -- 'Cleaning' | 'Absent'
                sections    TEXT    NOT NULL DEFAULT '[]', -- JSON array of section names
                date        TEXT    NOT NULL,   -- 'YYYY-MM-DDTHH:mm:ss'
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
