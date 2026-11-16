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
                CREATE TABLE IF NOT EXISTS public_audit_owners (
                    audit_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS public_owner_audits ON public_audit_owners(owner_id);
            ''')
            cursor = await db.execute("SELECT id FROM audits WHERE status IN ('queued','running')")
            for (audit_id,) in await cursor.fetchall():
                payload = {"status": "failed", "progress": 100, "message": "Server restarted before this audit completed."}
                await db.execute("UPDATE audits SET status='failed', progress=100, error=? WHERE id=?", (payload["message"], audit_id))
                await db.execute("INSERT INTO events(audit_id,payload) VALUES (?,?)", (audit_id, json.dumps(payload)))
            await db.commit()

    async def create(self, audit_id: str, url: str, *, owner: str | None = None, expires_at: int | None = None) -> None:
        if owner is not None and expires_at is None:
            raise ValueError("Public audits require an expiry")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO audits(id,url,status,created_at) VALUES (?,?,'queued',?)", (audit_id, url, datetime.now(timezone.utc).isoformat()))
            if owner is not None:
                await db.execute("INSERT INTO public_audit_owners VALUES (?,?,?)", (audit_id, owner, expires_at))
            await db.commit()

    async def event(self, audit_id: str, status: str, progress: int, message: str, report: dict | None = None) -> None:
        payload = {"status": status, "progress": progress, "message": message}
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE audits SET status=?,progress=?,error=?,report=COALESCE(?,report) WHERE id=?", (status, progress, message if status == "failed" else None, json.dumps(report) if report is not None else None, audit_id))
            await db.execute("INSERT INTO events(audit_id,payload) VALUES (?,?)", (audit_id, json.dumps(payload)))
            await db.commit()

    async def get(self, audit_id: str, *, owner: str | None = None) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if owner is None:
                cursor = await db.execute("SELECT * FROM audits WHERE id=?", (audit_id,))
            else:
                cursor = await db.execute("SELECT a.* FROM audits a JOIN public_audit_owners p ON p.audit_id=a.id WHERE a.id=? AND p.owner_id=? AND p.expires_at>?", (audit_id, owner, int(datetime.now(timezone.utc).timestamp())))
            row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            result["report"] = json.loads(result["report"]) if result["report"] else None
            return result

    async def history(self, limit: int = 20, *, owner: str | None = None) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if owner is None:
                cursor = await db.execute("SELECT id,url,status,created_at,progress,error FROM audits ORDER BY created_at DESC LIMIT ?", (limit,))
            else:
                cursor = await db.execute("SELECT a.id,a.url,a.status,a.created_at,a.progress,a.error FROM audits a JOIN public_audit_owners p ON p.audit_id=a.id WHERE p.owner_id=? AND p.expires_at>? ORDER BY a.created_at DESC LIMIT ?", (owner, int(datetime.now(timezone.utc).timestamp()), limit))
            return [dict(row) for row in await cursor.fetchall()]

    async def public_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM public_audit_owners")
            row = await cursor.fetchone()
            return row[0]

    async def expired_public(self, now: int) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT a.id FROM audits a JOIN public_audit_owners p ON p.audit_id=a.id WHERE p.expires_at<=? AND a.status IN ('completed','failed')", (now,))
            return [row[0] for row in await cursor.fetchall()]

    async def delete_expired_public(self, audit_id: str, now: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT a.id FROM audits a JOIN public_audit_owners p ON p.audit_id=a.id WHERE a.id=? AND p.expires_at<=? AND a.status IN ('completed','failed')", (audit_id, now))
            if await cursor.fetchone():
                await db.execute("DELETE FROM events WHERE audit_id=?", (audit_id,))
                await db.execute("DELETE FROM audits WHERE id=?", (audit_id,))
                await db.execute("DELETE FROM public_audit_owners WHERE audit_id=?", (audit_id,))
            await db.commit()

    async def events(self, audit_id: str, after: int = 0) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT seq,payload FROM events WHERE audit_id=? AND seq>? ORDER BY seq LIMIT 200", (audit_id, after))
            return [{"seq": seq, **json.loads(payload)} for seq, payload in await cursor.fetchall()]
