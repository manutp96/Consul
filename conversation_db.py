"""
Conversation Database - RH Tramites Consulares
===============================================

SQLite storage for WhatsApp conversation history,
channel assignments, and message tracking.

All functions are async (using aiosqlite).
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

log = logging.getLogger("ConvDB")

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = SCRIPT_DIR / "conversations.db"

# Channel IDs loaded at init
_channel_ids: list[int] = []


async def init_db(channel_ids: list[int]):
    """Initialize database tables and seed channel assignments."""
    global _channel_ids
    _channel_ids = [ch for ch in channel_ids if ch]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL UNIQUE,
                sender_name TEXT DEFAULT '',
                discord_channel_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                last_message_at TEXT DEFAULT (datetime('now')),
                is_active INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone_number);
            CREATE INDEX IF NOT EXISTS idx_conv_channel ON conversations(discord_channel_id);

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL CHECK(role IN ('client', 'bot', 'employee')),
                content TEXT NOT NULL,
                timestamp TEXT DEFAULT (datetime('now')),
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);

            CREATE TABLE IF NOT EXISTS channel_assignments (
                channel_id INTEGER PRIMARY KEY,
                active_conversations INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pending_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                reply_text TEXT NOT NULL,
                discord_user TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                sent INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_pr_phone ON pending_replies(phone_number);
        """)

        # Migration: add last_client_message_at column
        try:
            await db.execute(
                "ALTER TABLE conversations ADD COLUMN last_client_message_at TEXT DEFAULT NULL"
            )
            await db.commit()
            log.info("Migration: added last_client_message_at column")
        except Exception:
            pass  # Column already exists

        # Seed channel assignments
        for ch_id in _channel_ids:
            await db.execute(
                "INSERT OR IGNORE INTO channel_assignments (channel_id, active_conversations) VALUES (?, 0)",
                (ch_id,)
            )

        # Sync channel_assignments counters with actual active conversations
        # (fixes desyncs from crashes, restarts, or newly added channels)
        for ch_id in _channel_ids:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM conversations WHERE discord_channel_id = ? AND is_active = 1",
                (ch_id,)
            )
            row = await cursor.fetchone()
            actual_count = row[0] if row else 0
            await db.execute(
                "UPDATE channel_assignments SET active_conversations = ? WHERE channel_id = ?",
                (actual_count, ch_id)
            )

        await db.commit()

    log.info(f"DB initialized at {DB_PATH} with {len(_channel_ids)} channels")


async def get_or_create_conversation(phone_number: str, sender_name: str = "") -> dict:
    """
    Get existing conversation or create new one.
    For new conversations, discord_channel_id=0 signals that a channel needs to be created.
    Returns dict with id, phone_number, sender_name, discord_channel_id, is_active.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check existing
        cursor = await db.execute(
            "SELECT * FROM conversations WHERE phone_number = ?",
            (phone_number,)
        )
        row = await cursor.fetchone()

        if row:
            conv = dict(row)
            # Update last_message_at and reactivate if needed
            await db.execute(
                "UPDATE conversations SET last_message_at = datetime('now'), last_client_message_at = datetime('now'), is_active = 1, sender_name = CASE WHEN sender_name = '' THEN ? ELSE sender_name END WHERE id = ?",
                (sender_name, conv["id"])
            )
            if not conv["is_active"]:
                conv["is_active"] = 1
            await db.commit()
            return conv

        # Create new — channel_id=0 means "needs a Discord channel"
        await db.execute(
            "INSERT INTO conversations (phone_number, sender_name, discord_channel_id) VALUES (?, ?, 0)",
            (phone_number, sender_name)
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM conversations WHERE phone_number = ?",
            (phone_number,)
        )
        row = await cursor.fetchone()
        conv = dict(row)
        log.info(f"New conversation for {phone_number} (pending channel creation)")
        return conv


async def assign_channel(conversation_id: int, discord_channel_id: int):
    """Assign a Discord channel to a conversation after channel creation."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE conversations SET discord_channel_id = ? WHERE id = ?",
            (discord_channel_id, conversation_id)
        )
        await db.commit()
    log.info(f"Conversation {conversation_id} assigned to channel {discord_channel_id}")


async def get_all_wa_channel_ids() -> set[int]:
    """Get all Discord channel IDs that are assigned to WhatsApp conversations."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT discord_channel_id FROM conversations WHERE discord_channel_id != 0"
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def _get_least_loaded_channel(db) -> int:
    """Get channel ID with fewest active conversations (random tiebreak for even distribution)."""
    if not _channel_ids:
        raise ValueError("No WhatsApp channels configured")

    cursor = await db.execute(
        "SELECT channel_id, active_conversations FROM channel_assignments ORDER BY active_conversations ASC, RANDOM() LIMIT 1"
    )
    row = await cursor.fetchone()
    if row:
        return row[0]
    return _channel_ids[0]


async def add_message(conversation_id: int, role: str, content: str, metadata: dict | None = None):
    """Add a message to a conversation."""
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, metadata) VALUES (?, ?, ?, ?)",
            (conversation_id, role, content, meta_json)
        )
        await db.execute(
            "UPDATE conversations SET last_message_at = datetime('now') WHERE id = ?",
            (conversation_id,)
        )
        await db.commit()


async def get_recent_messages(conversation_id: int, limit: int = 20) -> list[dict]:
    """Get last N messages for a conversation, ordered chronologically."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT role, content, timestamp FROM messages
               WHERE conversation_id = ?
               ORDER BY id DESC LIMIT ?""",
            (conversation_id, limit)
        )
        rows = await cursor.fetchall()
        # Reverse to get chronological order
        return [dict(r) for r in reversed(rows)]


async def get_conversation_by_phone(phone_number: str) -> dict | None:
    """Lookup conversation by phone number."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM conversations WHERE phone_number = ?",
            (phone_number,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_conversation_by_channel(discord_channel_id: int) -> dict | None:
    """Get the most recently active conversation assigned to a Discord channel."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM conversations WHERE discord_channel_id = ? AND is_active = 1 ORDER BY last_message_at DESC LIMIT 1",
            (discord_channel_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def mark_stale_conversations_inactive():
    """Mark conversations with no messages in 24h as inactive, decrement channel counts."""
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        # Get stale active conversations grouped by channel
        cursor = await db.execute(
            """SELECT discord_channel_id, COUNT(*) as cnt
               FROM conversations
               WHERE is_active = 1 AND last_message_at < ?
               GROUP BY discord_channel_id""",
            (cutoff,)
        )
        rows = await cursor.fetchall()

        for channel_id, count in rows:
            await db.execute(
                "UPDATE channel_assignments SET active_conversations = MAX(0, active_conversations - ?) WHERE channel_id = ?",
                (count, channel_id)
            )

        result = await db.execute(
            "UPDATE conversations SET is_active = 0 WHERE is_active = 1 AND last_message_at < ?",
            (cutoff,)
        )
        await db.commit()

        if result.rowcount > 0:
            log.info(f"Marked {result.rowcount} stale conversations as inactive")


async def get_channel_load() -> dict[int, int]:
    """Get active conversation count per channel."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT channel_id, active_conversations FROM channel_assignments"
        )
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}


# ============================================================================
# 24-HOUR MESSAGING WINDOW
# ============================================================================

async def is_within_24h_window(phone_number: str) -> bool:
    """Check if the customer's last inbound message is within the 24h window."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT last_client_message_at FROM conversations WHERE phone_number = ?",
            (phone_number,),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return False

        try:
            last_ts = datetime.fromisoformat(row[0])
            return (datetime.utcnow() - last_ts) < timedelta(hours=24)
        except (ValueError, TypeError):
            return False


# ============================================================================
# PENDING REPLIES (for 24h window template fallback)
# ============================================================================

async def save_pending_reply(phone_number: str, reply_text: str, discord_user: str = ""):
    """Save a reply that couldn't be sent because the 24h window was closed."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO pending_replies (phone_number, reply_text, discord_user) VALUES (?, ?, ?)",
            (phone_number, reply_text, discord_user),
        )
        await db.commit()
    log.info(f"Pending reply saved for {phone_number}")


async def get_pending_replies(phone_number: str) -> list[dict]:
    """Get unsent pending replies for a phone number."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, reply_text, discord_user, created_at FROM pending_replies "
            "WHERE phone_number = ? AND sent = 0 ORDER BY created_at ASC",
            (phone_number,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_pending_reply_sent(reply_id: int):
    """Mark a pending reply as sent."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_replies SET sent = 1 WHERE id = ?",
            (reply_id,),
        )
        await db.commit()
