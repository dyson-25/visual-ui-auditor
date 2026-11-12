import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.config import AUDIT_TIMEOUT_SECONDS, MAX_CONCURRENT_AUDITS, ROOT, STORAGE
from backend.database import Database
from backend.engine.orchestrator import run_audit
from backend.models import AuditRequest
from backend.reports import render_html, render_pdf

logger = logging.getLogger(__name__)


def create_app(storage: Path = STORAGE) -> FastAPI:
    db = Database(storage / "audits.db")
    screenshots = storage / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.initialize()
        app.state.slots = asyncio.Semaphore(MAX_CONCURRENT_AUDITS)
        app.state.pdf_slot = asyncio.Semaphore(1)
        app.state.submission_lock = asyncio.Lock()
        app.state.db = db
        yield
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Visual UI Auditor", lifespan=lifespan)
    app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver", "*.onrender.com"],
   )
    @app.middleware("http")
    async def local_client(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if request.headers.get("x-audit-client") != "dashboard" or (origin and origin != expected):
                return Response("Same-origin audit client required", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    async def job(audit_id: str, url: str) -> None:
        try:
            async with app.state.slots:
                async def progress(value: int, message: str):
                    await db.event(audit_id, "running", value, message)

                async with asyncio.timeout(AUDIT_TIMEOUT_SECONDS):
                    report = await run_audit(url, audit_id, screenshots / audit_id, progress)
                await db.event(audit_id, "completed", 100, "Audit complete — ready to inspect and export", report)
        except asyncio.CancelledError:
            await db.event(audit_id, "failed", 100, "Audit stopped because the server is shutting down.")
            raise
        except Exception as exc:
            logger.exception("Audit %s failed", audit_id)
            message = "Audit exceeded the configured time limit." if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {str(exc)[:600]}"
            await db.event(audit_id, "failed", 100, message)

    async def get_audit(audit_id: UUID) -> dict:
        audit = await db.get(str(audit_id))
        if audit is None:
            raise HTTPException(404, "Audit not found")
        return audit

    @app.post("/api/audit", status_code=202)
    async def start_audit(request: AuditRequest):
        async with app.state.submission_lock:
            if len(tasks) >= MAX_CONCURRENT_AUDITS * 3:
                raise HTTPException(429, "Audit queue is full; try again when a run finishes.")
            audit_id = str(uuid4())
            await db.create(audit_id, request.url)
            await db.event(audit_id, "queued", 0, "Queued — waiting for a browser slot")
            task = asyncio.create_task(job(audit_id, request.url))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return {"audit_id": audit_id, "status": "queued"}

    @app.get("/api/audits")
    async def history():
        return await db.history()

    @app.get("/api/audit/{audit_id}")
    async def audit_detail(audit_id: UUID):
        return await get_audit(audit_id)

    @app.get("/api/audit/{audit_id}/export/{format}")
    async def export(audit_id: UUID, format: str):
        if format not in {"json", "html", "pdf"}:
            raise HTTPException(404, "Supported report formats: json, html, pdf")
        audit = await get_audit(audit_id)
        if audit["status"] != "completed":
            raise HTTPException(409, "Report is available after the audit completes")
        headers = {"Content-Disposition": f'attachment; filename="visual-audit-{audit_id}.{format}"'}
        if format == "json":
            return Response(json.dumps(audit, indent=2, ensure_ascii=False), media_type="application/json", headers=headers)
        html = await asyncio.to_thread(render_html, audit, screenshots)
        if format == "html":
            return Response(html, media_type="text/html", headers=headers)
        try:
            async with asyncio.timeout(45):
                async with app.state.pdf_slot:
                    pdf = await render_pdf(html)
        except TimeoutError as exc:
            raise HTTPException(503, "PDF export timed out. Try HTML export and print from your browser.") from exc
        return Response(pdf, media_type="application/pdf", headers=headers)

    @app.websocket("/ws/audit/{audit_id}")
    async def live_audit(websocket: WebSocket, audit_id: UUID, after: int = Query(0, ge=0)):
        origin = websocket.headers.get("origin")
        expected = f"{'https' if websocket.url.scheme == 'wss' else 'http'}://{websocket.headers.get('host', '')}"
        if (origin and origin != expected) or await db.get(str(audit_id)) is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        cursor, heartbeat = after, 0
        try:
            while True:
                events = await db.events(str(audit_id), cursor)
                for event in events:
                    await websocket.send_json(event)
                    cursor = event["seq"]
                    if event["status"] in {"completed", "failed"}:
                        await websocket.close()
                        return
                if not events:
                    audit = await db.get(str(audit_id))
                    if audit and audit["status"] in {"completed", "failed"}:
                        await websocket.send_json({"status": audit["status"], "progress": 100, "message": audit["error"] or "Audit complete"})
                        await websocket.close()
                        return
                heartbeat += 1
                if heartbeat % 40 == 0:
                    await websocket.send_json({"heartbeat": True})
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return

    @app.get("/")
    async def dashboard():
        return FileResponse(ROOT / "frontend" / "index.html")

    @app.get("/demo")
    async def demo():
        return FileResponse(ROOT / "tests" / "fixtures" / "layout.html")

    app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")
    app.mount("/screenshots", StaticFiles(directory=screenshots), name="screenshots")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
