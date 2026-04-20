"""
Database - SQLite persistence

Schema (one table for sessions + two config tables):

  conversations  — project name and working dir
  pipeline_steps — per-conversation agent pipeline config
  sessions       — every agent step execution (the ONE table)

sessions columns:
  id          PK
  conv_id     → conversations.id
  round_index 1-based round number  (N in Round N-M)
  step_index  1-based step number   (M in Round N-M)
  agent_type  'codex' | 'claude' | ...
  session_id  CLI session ID for --resume (NULL until agent emits it)
  chat        summary text; NULL if killed / no output
  created_at  wall clock
"""

import aiosqlite
from pathlib import Path
from typing import Optional
import asyncio

DEFAULT_DB_PATH = './data/sessions.db'


class Database:

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()

    async def _create_tables(self) -> None:
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                working_dir     TEXT,
                max_rounds      INTEGER DEFAULT 0,
                api_err_retries INTEGER DEFAULT 3
            );

            CREATE TABLE IF NOT EXISTS pipeline_steps (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                step_order      INTEGER NOT NULL,
                agent_type      TEXT NOT NULL,
                prompt_template TEXT,
                is_async        INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id     INTEGER NOT NULL,
                round_index INTEGER NOT NULL,
                step_index  INTEGER NOT NULL,
                agent_type  TEXT NOT NULL,
                session_id  TEXT,
                chat        TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sess_conv  ON sessions(conv_id);
            CREATE INDEX IF NOT EXISTS idx_sess_round ON sessions(conv_id, round_index, step_index);
            CREATE INDEX IF NOT EXISTS idx_pipe_conv  ON pipeline_steps(conversation_id);
        """)

        # Migrate existing DB: add new columns if absent
        for col, default in [('max_rounds', 0), ('api_err_retries', 3)]:
            try:
                await self._db.execute(
                    f"ALTER TABLE conversations ADD COLUMN {col} INTEGER DEFAULT {default}"
                )
                await self._db.commit()
            except Exception:
                pass  # column already exists

        try:
            await self._db.execute(
                "ALTER TABLE pipeline_steps ADD COLUMN is_async INTEGER DEFAULT 0"
            )
            await self._db.commit()
        except Exception:
            pass

        # Ensure a default conversation exists for any legacy data
        cursor = await self._db.execute("SELECT COUNT(*) FROM conversations")
        if (await cursor.fetchone())[0] == 0:
            await self._db.execute("INSERT INTO conversations (name) VALUES ('默认会话')")

        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    async def create_conversation(self, name: str) -> int:
        async with self._lock:
            cur = await self._db.execute(
                "INSERT INTO conversations (name) VALUES (?)", (name,)
            )
            await self._db.commit()
            return cur.lastrowid

    async def get_conversation(self, conv_id: int) -> Optional[dict]:
        cur = await self._db.execute(
            "SELECT * FROM conversations WHERE id = ?", (conv_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_all_conversations(self) -> list:
        cur = await self._db.execute(
            "SELECT * FROM conversations ORDER BY id DESC"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def rename_conversation(self, conv_id: int, name: str) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE conversations SET name = ? WHERE id = ?", (name, conv_id)
            )
            await self._db.commit()

    async def update_conversation_working_dir(self, conv_id: int, working_dir: str) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE conversations SET working_dir = ? WHERE id = ?",
                (working_dir, conv_id)
            )
            await self._db.commit()

    async def delete_conversation(self, conv_id: int) -> None:
        async with self._lock:
            await self._db.execute(
                "DELETE FROM pipeline_steps WHERE conversation_id = ?", (conv_id,)
            )
            await self._db.execute(
                "DELETE FROM sessions WHERE conv_id = ?", (conv_id,)
            )
            await self._db.execute(
                "DELETE FROM conversations WHERE id = ?", (conv_id,)
            )
            await self._db.commit()

    # ------------------------------------------------------------------
    # Pipeline steps
    # ------------------------------------------------------------------

    async def get_pipeline_steps(self, conv_id: int) -> list:
        cur = await self._db.execute(
            "SELECT * FROM pipeline_steps WHERE conversation_id = ? ORDER BY step_order",
            (conv_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def save_pipeline_steps(
        self,
        conv_id: int,
        steps: list,
        working_dir: Optional[str] = None,
        max_rounds: Optional[int] = None,
        api_err_retries: Optional[int] = None,
    ) -> None:
        async with self._lock:
            updates, params = [], []
            if working_dir is not None:
                updates.append("working_dir = ?"); params.append(working_dir)
            if max_rounds is not None:
                updates.append("max_rounds = ?"); params.append(max_rounds)
            if api_err_retries is not None:
                updates.append("api_err_retries = ?"); params.append(api_err_retries)
            if updates:
                params.append(conv_id)
                await self._db.execute(
                    f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?", params
                )
            await self._db.execute(
                "DELETE FROM pipeline_steps WHERE conversation_id = ?", (conv_id,)
            )
            for i, step in enumerate(steps):
                await self._db.execute(
                    "INSERT INTO pipeline_steps (conversation_id, step_order, agent_type, prompt_template, is_async)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (conv_id, i, step.get('agent_type', 'claude'), step.get('prompt_template'),
                     1 if step.get('is_async') else 0),
                )
            await self._db.commit()

    # ------------------------------------------------------------------
    # Sessions — the ONE table
    # ------------------------------------------------------------------

    async def create_session(
        self,
        conv_id:     int,
        round_index: int,
        step_index:  int,
        agent_type:  str,
    ) -> int:
        """Insert a new session row. Returns the new row id (session PK)."""
        async with self._lock:
            cur = await self._db.execute(
                "INSERT INTO sessions (conv_id, round_index, step_index, agent_type)"
                " VALUES (?, ?, ?, ?)",
                (conv_id, round_index, step_index, agent_type),
            )
            await self._db.commit()
            return cur.lastrowid

    async def add_user_message(
        self,
        conv_id:     int,
        round_index: int,
        step_index:  int,
        content:     str,
    ) -> int:
        """Insert a user message row (agent_type='user', session_id=NULL, chat=content)."""
        async with self._lock:
            cur = await self._db.execute(
                "INSERT INTO sessions (conv_id, round_index, step_index, agent_type, chat)"
                " VALUES (?, ?, ?, 'user', ?)",
                (conv_id, round_index, step_index, content),
            )
            await self._db.commit()
            return cur.lastrowid

    async def update_session_id(self, pk: int, session_id: str) -> None:
        """Write the CLI session ID once the agent emits it."""
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET session_id = ? WHERE id = ?", (session_id, pk)
            )
            await self._db.commit()

    async def update_session_chat(self, pk: int, chat: Optional[str]) -> None:
        """Write the summary / last paragraph into the chat column."""
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET chat = ? WHERE id = ?", (chat, pk)
            )
            await self._db.commit()

    async def get_session(self, pk: int) -> Optional[dict]:
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE id = ?", (pk,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_sessions(self, conv_id: int, limit: int = 20, offset: int = 0) -> list:
        """Agent sessions for a conversation, newest first. Excludes user messages."""
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE conv_id = ? AND agent_type != 'user'"
            " ORDER BY id DESC LIMIT ? OFFSET ?",
            (conv_id, limit, offset),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_session_count(self, conv_id: int) -> int:
        """Total number of agent sessions for a conversation."""
        cur = await self._db.execute(
            "SELECT COUNT(*) FROM sessions WHERE conv_id = ? AND agent_type != 'user'",
            (conv_id,),
        )
        return (await cur.fetchone())[0]

    async def get_chat_history_range(self, conv_id: int, min_id: int, max_id: int) -> list:
        """All rows (agent + user) with id between min_id and max_id, oldest first."""
        cur = await self._db.execute(
            "SELECT agent_type as role, chat as content, id as session_id,"
            " created_at as timestamp, round_index, step_index"
            " FROM sessions"
            " WHERE conv_id = ? AND id BETWEEN ? AND ? AND chat IS NOT NULL"
            " ORDER BY id ASC",
            (conv_id, min_id, max_id),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_latest_session(self, conv_id: int) -> Optional[dict]:
        """Most recently created agent session for a conversation. Excludes user messages."""
        cur = await self._db.execute(
            "SELECT * FROM sessions WHERE conv_id = ? AND agent_type != 'user' ORDER BY id DESC LIMIT 1",
            (conv_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_sessions_at(
        self, conv_id: int, round_index: int, step_index: int
    ) -> list:
        """All attempts at a specific (round, step) position, newest first."""
        cur = await self._db.execute(
            "SELECT * FROM sessions"
            " WHERE conv_id = ? AND round_index = ? AND step_index = ?"
            " ORDER BY id DESC",
            (conv_id, round_index, step_index),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def delete_session(self, pk: int) -> Optional[int]:
        """Delete one session row. Returns its conv_id, or None if not found."""
        async with self._lock:
            cur = await self._db.execute(
                "SELECT conv_id FROM sessions WHERE id = ?", (pk,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            conv_id = row[0]
            await self._db.execute("DELETE FROM sessions WHERE id = ?", (pk,))
            await self._db.commit()
            return conv_id

    # ------------------------------------------------------------------
    # Chat history — for prompt injection
    # ------------------------------------------------------------------

    async def get_chat_history(self, conv_id: int, limit: int = 40) -> list:
        """
        Return the most recent `limit` session rows,
        ordered oldest-first (so agents read history in chronological order).
        Returns fields: role, content, session_id, timestamp (for frontend compatibility).
        """
        cur = await self._db.execute(
            "SELECT agent_type as role, chat as content, id as session_id,"
            " created_at as timestamp, round_index, step_index"
            " FROM sessions"
            " WHERE conv_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (conv_id, limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        return list(reversed(rows))   # oldest first

    async def get_latest_user_message(self, conv_id: int, limit: int = 1) -> list:
        """Return the most recent user message(s) content for a conversation.

        Returns list of strings ordered newest-first. Empty list if none.
        """
        cur = await self._db.execute(
            "SELECT chat FROM sessions"
            " WHERE conv_id = ? AND agent_type = 'user' AND chat IS NOT NULL"
            " ORDER BY id DESC LIMIT ?",
            (conv_id, limit),
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def get_recent_summaries(self, conv_id: int, limit: int = 3) -> list:
        """Return context for the last `limit` agent sessions, including any user messages in between.

        Finds the ID of the Nth-from-last agent session, then returns all rows
        (agent + user) with id >= that threshold, oldest-first.
        """
        cur = await self._db.execute(
            "SELECT id FROM sessions"
            " WHERE conv_id = ? AND agent_type != 'user' AND chat IS NOT NULL"
            " ORDER BY id DESC LIMIT 1 OFFSET ?",
            (conv_id, limit - 1),
        )
        threshold_row = await cur.fetchone()

        if not threshold_row:
            cur = await self._db.execute(
                "SELECT agent_type as role, chat as content, created_at as timestamp"
                " FROM sessions WHERE conv_id = ? AND chat IS NOT NULL ORDER BY id ASC",
                (conv_id,),
            )
        else:
            cur = await self._db.execute(
                "SELECT agent_type as role, chat as content, created_at as timestamp"
                " FROM sessions WHERE conv_id = ? AND id >= ? AND chat IS NOT NULL ORDER BY id ASC",
                (conv_id, threshold_row[0]),
            )

        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    async def mark_stale_sessions(self) -> None:
        """On startup, sessions with no session_id are orphaned — leave them as-is.
        The UI will show them with empty chat (null); nothing to clean up."""
        pass  # no-op: NULL session_id rows are valid "killed before start" records

    async def get_stats(self) -> dict:
        stats = {}
        for col, table in [('conversations', 'conversations'),
                            ('sessions', 'sessions')]:
            cur = await self._db.execute(f"SELECT COUNT(*) FROM {table}")
            stats[f'total_{col}'] = (await cur.fetchone())[0]
        return stats
