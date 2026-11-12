import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS audits (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                    report TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_audit_seq ON events(audit_id, seq);
            ''')
            cursor = await db.execute("SELECT id FROM audits WHERE status IN ('queued','running')")
            for (audit_id,) in await cursor.fetchall():
                payload = {"status": "failed", "progress": 100, "message": "Server restarted before this audit completed."}
                await db.execute("UPDATE audits SET status='failed', progress=100, error=? WHERE id=?", (payload["message"], audit_id))
                await db.execute("INSERT INTO events(audit_id,payload) VALUES (?,?)", (audit_id, json.dumps(payload)))
            await db.commit()

    async def create(self, audit_id: str, url: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO audits(id,url,status,created_at) VALUES (?,?,'queued',?)", (audit_id, url, datetime.now(timezone.utc).isoformat()))
            await db.commit()

    async def event(self, audit_id: str, status: str, progress: int, message: str, report: dict | None = None) -> None:
        payload = {"status": status, "progress": progress, "message": message}
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE audits SET status=?,progress=?,error=?,report=COALESCE(?,report) WHERE id=?", (status, progress, message if status == "failed" else None, json.dumps(report) if report is not None else None, audit_id))
            await db.execute("INSERT INTO events(audit_id,payload) VALUES (?,?)", (audit_id, json.dumps(payload)))
            await db.commit()

    async def get(self, audit_id: str) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM audits WHERE id=?", (audit_id,))
            row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            result["report"] = json.loads(result["report"]) if result["report"] else None
            return result

    async def history(self, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id,url,status,created_at,progress,error FROM audits ORDER BY created_at DESC LIMIT ?", (limit,))
            return [dict(row) for row in await cursor.fetchall()]

    async def events(self, audit_id: str, after: int = 0) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT seq,payload FROM events WHERE audit_id=? AND seq>? ORDER BY seq LIMIT 200", (audit_id, after))
            return [{"seq": seq, **json.loads(payload)} for seq, payload in await cursor.fetchall()]
