from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, DATABASE_PATH
from app.crypto import encrypt_secret


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def _conn() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "photos").mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                writing_style TEXT NOT NULL DEFAULT 'Professional, authentic, clear, and thoughtful.',
                timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
                photo_dir TEXT NOT NULL,
                linkedin_member_urn TEXT,
                linkedin_access_token_enc TEXT,
                linkedin_refresh_token_enc TEXT,
                linkedin_token_expires_at TEXT,
                created_at TEXT NOT NULL,
                last_topic_nudge_at TEXT
            );

            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                source_transcript TEXT NOT NULL,
                draft_text TEXT NOT NULL,
                photo_path TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                approved_at TEXT,
                scheduled_at TEXT,
                published_at TEXT,
                linkedin_post_id TEXT,
                last_reminder_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_posts_customer_status
              ON posts(customer_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_posts_due
              ON posts(status, scheduled_at);

            CREATE TABLE IF NOT EXISTS connect_codes (
                code_hash TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS oauth_states (
                state_hash TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                used_at TEXT
            );
            """
        )


def create_customer(
    telegram_user_id: int,
    display_name: str,
    writing_style: str = "Professional, authentic, clear, and thoughtful.",
    timezone_name: str = "Europe/Berlin",
) -> dict[str, Any]:
    customer_id = str(uuid.uuid4())
    photo_dir = (DATA_DIR / "photos" / customer_id).resolve()
    photo_dir.mkdir(parents=True, exist_ok=True)
    now = iso_now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO customers
               (id, display_name, telegram_user_id, writing_style, timezone, photo_dir, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (customer_id, display_name, telegram_user_id, writing_style, timezone_name,
             str(photo_dir), now),
        )
    return get_customer_by_id(customer_id)  # type: ignore[return-value]


def get_customer_by_id(customer_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return dict(row) if row else None


def get_customer_by_telegram_id(telegram_user_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE telegram_user_id = ?", (telegram_user_id,)
        ).fetchone()
    return dict(row) if row else None


def list_customers() -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM customers ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]


def update_linkedin_credentials(
    customer_id: str,
    member_urn: str,
    access_token: str,
    expires_at: str | None,
    refresh_token: str | None = None,
) -> None:
    access_enc = encrypt_secret(access_token)
    refresh_enc = encrypt_secret(refresh_token)
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE customers SET linkedin_member_urn = ?, linkedin_access_token_enc = ?,
               linkedin_refresh_token_enc = ?, linkedin_token_expires_at = ? WHERE id = ?""",
            (member_urn, access_enc, refresh_enc, expires_at, customer_id),
        )
        if cur.rowcount != 1:
            raise ValueError("Customer not found while saving LinkedIn credentials")


def create_connect_code(customer_id: str, ttl_minutes: int = 15) -> str:
    code = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    expires_at = (utc_now() + timedelta(minutes=ttl_minutes)).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO connect_codes(code_hash, customer_id, expires_at) VALUES (?, ?, ?)",
            (code_hash, customer_id, expires_at),
        )
    return code


def consume_connect_code(code: str) -> str | None:
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    now = iso_now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT customer_id, expires_at, used_at FROM connect_codes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()
        if not row or row["used_at"] or row["expires_at"] < now:
            return None
        conn.execute("UPDATE connect_codes SET used_at = ? WHERE code_hash = ?", (now, code_hash))
        return str(row["customer_id"])


def create_oauth_state(customer_id: str, ttl_minutes: int = 10) -> str:
    state = secrets.token_urlsafe(32)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    expires_at = (utc_now() + timedelta(minutes=ttl_minutes)).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO oauth_states(state_hash, customer_id, expires_at) VALUES (?, ?, ?)",
            (state_hash, customer_id, expires_at),
        )
    return state


def get_oauth_state_customer(state: str) -> str | None:
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    with _conn() as conn:
        row = conn.execute(
            "SELECT customer_id, expires_at, used_at FROM oauth_states WHERE state_hash = ?",
            (state_hash,),
        ).fetchone()
    if not row or row["used_at"] or row["expires_at"] < iso_now():
        return None
    return str(row["customer_id"])


def consume_oauth_state(state: str) -> None:
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    with _conn() as conn:
        conn.execute(
            "UPDATE oauth_states SET used_at = ? WHERE state_hash = ? AND used_at IS NULL",
            (iso_now(), state_hash),
        )


def create_post(customer_id: str, transcript: str, draft_text: str, photo_path: str) -> dict[str, Any]:
    post_id = str(uuid.uuid4())
    now = iso_now()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO posts
               (id, customer_id, source_transcript, draft_text, photo_path, status,
                revision, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'awaiting_approval', 1, ?, ?)""",
            (post_id, customer_id, transcript, draft_text, photo_path, now, now),
        )
    return get_post_for_customer(post_id, customer_id)  # type: ignore[return-value]


def get_post_for_customer(post_id: str, customer_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM posts WHERE id = ? AND customer_id = ?", (post_id, customer_id)
        ).fetchone()
    return dict(row) if row else None


def get_pending_post(customer_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM posts WHERE customer_id = ? AND status = 'awaiting_approval'
               ORDER BY updated_at DESC LIMIT 1""",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def get_latest_scheduled_post(customer_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM posts WHERE customer_id = ? AND status = 'scheduled'
               ORDER BY scheduled_at LIMIT 1""",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def update_post_draft(
    post_id: str, customer_id: str, transcript: str, draft_text: str, photo_path: str
) -> dict[str, Any]:
    now = iso_now()
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE posts SET source_transcript = ?, draft_text = ?, photo_path = ?,
               status = 'awaiting_approval', revision = revision + 1,
               approved_at = NULL, scheduled_at = NULL, last_error = NULL,
               last_reminder_at = NULL, updated_at = ?
               WHERE id = ? AND customer_id = ? AND status = 'awaiting_approval'""",
            (transcript, draft_text, photo_path, now, post_id, customer_id),
        )
        if cur.rowcount != 1:
            raise ValueError("Draft is no longer awaiting approval; refusing to overwrite it")
    return get_post_for_customer(post_id, customer_id)  # type: ignore[return-value]


def cancel_pending_post(post_id: str, customer_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE posts SET status = 'cancelled', updated_at = ?
               WHERE id = ? AND customer_id = ? AND status = 'awaiting_approval'""",
            (iso_now(), post_id, customer_id),
        )
    return cur.rowcount == 1


def approve_and_schedule(post_id: str, customer_id: str, scheduled_at: datetime) -> dict[str, Any] | None:
    now = iso_now()
    approved_at = iso_now()
    schedule_iso = scheduled_at.astimezone(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE posts SET status = 'scheduled', approved_at = ?, scheduled_at = ?,
               last_error = NULL, updated_at = ?
               WHERE id = ? AND customer_id = ? AND status = 'awaiting_approval'""",
            (approved_at, schedule_iso, now, post_id, customer_id),
        )
        if cur.rowcount != 1:
            return None
    return get_post_for_customer(post_id, customer_id)


def reschedule_post(post_id: str, customer_id: str, scheduled_at: datetime) -> bool:
    schedule_iso = scheduled_at.astimezone(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE posts SET scheduled_at = ?, updated_at = ?
               WHERE id = ? AND customer_id = ? AND status = 'scheduled' AND approved_at IS NOT NULL""",
            (schedule_iso, iso_now(), post_id, customer_id),
        )
    return cur.rowcount == 1


def recent_published_posts(customer_id: str, limit: int = 3) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT draft_text, published_at FROM posts WHERE customer_id = ?
               AND status = 'published' ORDER BY published_at DESC LIMIT ?""",
            (customer_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_due_posts(now: datetime | None = None) -> list[dict[str, Any]]:
    moment = (now or utc_now()).astimezone(timezone.utc).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT p.*, c.telegram_user_id FROM posts p
               JOIN customers c ON c.id = p.customer_id
               WHERE p.status = 'scheduled' AND p.approved_at IS NOT NULL
               AND p.scheduled_at <= ? ORDER BY p.scheduled_at LIMIT 30""",
            (moment,),
        ).fetchall()
    return [dict(row) for row in rows]


def claim_post_for_publishing(post_id: str, customer_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE posts SET status = 'publishing', updated_at = ?
               WHERE id = ? AND customer_id = ? AND status = 'scheduled'
               AND approved_at IS NOT NULL AND scheduled_at <= ?""",
            (iso_now(), post_id, customer_id, iso_now()),
        )
    return cur.rowcount == 1


def finish_published(post_id: str, customer_id: str, linkedin_post_id: str | None) -> None:
    now = iso_now()
    with _conn() as conn:
        conn.execute(
            """UPDATE posts SET status = 'published', published_at = ?,
               linkedin_post_id = ?, last_error = NULL, updated_at = ?
               WHERE id = ? AND customer_id = ? AND status = 'publishing'""",
            (now, linkedin_post_id, now, post_id, customer_id),
        )


def fail_publishing(post_id: str, customer_id: str, reason: str) -> None:
    now = iso_now()
    safe_reason = reason[:1000]
    with _conn() as conn:
        conn.execute(
            """UPDATE posts SET status = 'publish_error', last_error = ?, updated_at = ?
               WHERE id = ? AND customer_id = ? AND status = 'publishing'""",
            (safe_reason, now, post_id, customer_id),
        )


def approval_reminders_due(timeout_hours: int) -> list[dict[str, Any]]:
    threshold = (utc_now() - timedelta(hours=timeout_hours)).isoformat()
    reminder_threshold = (utc_now() - timedelta(hours=timeout_hours)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT p.id, p.customer_id, p.updated_at, c.telegram_user_id
               FROM posts p JOIN customers c ON c.id = p.customer_id
               WHERE p.status = 'awaiting_approval' AND p.updated_at <= ?
               AND (p.last_reminder_at IS NULL OR p.last_reminder_at <= ?)""",
            (threshold, reminder_threshold),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_approval_reminded(post_id: str, customer_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE posts SET last_reminder_at = ? WHERE id = ? AND customer_id = ? AND status = 'awaiting_approval'",
            (iso_now(), post_id, customer_id),
        )


def customers_due_for_topic_nudge() -> list[dict[str, Any]]:
    now = utc_now()
    nudge_threshold = (now - timedelta(days=7)).isoformat()
    with _conn() as conn:
        customers = conn.execute(
            """SELECT c.*,
               COALESCE((SELECT MAX(p.created_at) FROM posts p WHERE p.customer_id = c.id), c.created_at) AS last_post_activity
               FROM customers c
               WHERE last_topic_nudge_at IS NULL OR last_topic_nudge_at <= ?""",
            (nudge_threshold,),
        ).fetchall()
    due: list[dict[str, Any]] = []
    for row in customers:
        item = dict(row)
        if item["last_post_activity"] <= nudge_threshold:
            due.append(item)
    return due


def mark_topic_nudge_sent(customer_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE customers SET last_topic_nudge_at = ? WHERE id = ?",
            (iso_now(), customer_id),
        )


def recent_post_statuses(customer_id: str, limit: int = 5) -> list[dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, status, revision, scheduled_at, published_at, last_error, created_at
               FROM posts WHERE customer_id = ? ORDER BY created_at DESC LIMIT ?""",
            (customer_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def count_customers() -> int:
    with _conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()
    return int(row["n"])


def mark_stale_publishing_as_uncertain(age_minutes: int = 30) -> int:
    """Never automatically retry uncertain publishes: the external POST may have succeeded."""
    threshold = (utc_now() - timedelta(minutes=age_minutes)).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE posts SET status = 'publish_error',
               last_error = 'Publication process stopped after the request may have started. Verify LinkedIn manually before retrying to avoid duplicates.',
               updated_at = ? WHERE status = 'publishing' AND updated_at <= ?""",
            (iso_now(), threshold),
        )
    return cur.rowcount
