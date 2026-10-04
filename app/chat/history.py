"""에이전트별 대화 기록. 에이전트 폴더 안의 agent.db(SQLite)에 쌓는다."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.agents.store import agent_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    ts      TEXT NOT NULL
)
"""


def _connect(agent_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(agent_dir(agent_id) / "agent.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    # 전문자료(RAG)가 생기기 전에 만든 대화 기록에는 sources 열이 없다 → 한 번만 추가한다
    if "sources" not in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}:
        conn.execute("ALTER TABLE messages ADD COLUMN sources TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
    return conn


def list_messages(agent_id: str) -> list[dict]:
    with closing(_connect(agent_id)) as conn:
        rows = conn.execute("SELECT id, role, content, ts, sources FROM messages ORDER BY id").fetchall()
    return [{**dict(r), "sources": json.loads(r["sources"])} for r in rows]


def add_exchange(agent_id: str, user_text: str, assistant_text: str, sources: list[dict] | None = None) -> dict:
    """사용자 메시지와 답변을 한 트랜잭션으로 저장한다. 답변이 실패한 턴은 기록에 남기지 않는다."""
    ts = datetime.now(timezone.utc).isoformat()
    with closing(_connect(agent_id)) as conn, conn:
        conn.execute("INSERT INTO messages (role, content, ts) VALUES ('user', ?, ?)", (user_text, ts))
        cur = conn.execute(
            "INSERT INTO messages (role, content, ts, sources) VALUES ('assistant', ?, ?, ?)",
            (assistant_text, ts, json.dumps(sources or [], ensure_ascii=False)),
        )
        return {"id": cur.lastrowid, "role": "assistant", "content": assistant_text, "ts": ts, "sources": sources or []}


def clear(agent_id: str) -> None:
    with closing(_connect(agent_id)) as conn, conn:
        conn.execute("DELETE FROM messages")
