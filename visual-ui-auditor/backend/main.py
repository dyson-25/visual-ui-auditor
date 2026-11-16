import asyncio
import json
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.config import AUDIT_TIMEOUT_SECONDS, MAX_CONCURRENT_AUDITS, ROOT, STORAGE, SecurityConfig
from backend.database import Database
from backend.engine.orchestrator import run_audit
from backend.models import AuditRequest
from backend.reports import render_html, render_pdf
from backend.security import COOKIE_NAME, RateLimiter, SessionSigner, SocketLimiter, calibration_url, directory_bytes, is_calibration_url

logger = logging.getLogger(__name__)


def create_app(storage: Path = STORAGE, *, security: SecurityConfig | None = None) -> FastAPI:
    # Factory callers explicitly select their policy; environment applies only to app below.
    policy = security if security is not None else SecurityConfig()
    db = Database(storage / "audits.db")
    screenshots = storage / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    tasks: set[asyncio.Task] = set()
    signer = SessionSigner(policy.session_secret, policy.session_seconds)
    limiter = RateLimiter(policy.max_rate_keys)
    sockets = SocketLimiter(policy.max_websockets, policy.max_session_websockets)

    def request_origin(request: Request) -> str:
        # A configured external origin avoids trusting forwarded headers to recover TLS.
        if policy.public_demo and policy.public_origin:
            return policy.public_origin
        return str(request.base_url).rstrip("/")

    async def cleanup_expired() -> None:
        now = int(time.time())
        for audit_id in await db.expired_public(now):
            try:
                # Only rows explicitly created as public-owned qualify, never legacy/local data.
                folder = screenshots / str(UUID(audit_id))
                if folder.is_dir() and not folder.is_symlink():
                    await asyncio.to_thread(shutil.rmtree, folder)
                await db.delete_expired_public(audit_id, now)
            except (OSError, ValueError):
                logger.exception("Could not remove expired public audit %s", audit_id)

    async def retention_loop() -> None:
        while True:
            await asyncio.sleep(policy.cleanup_interval_seconds)
            try:
                async with app.state.submission_lock:
                    await cleanup_expired()
            except Exception:
                logger.exception("Public retention maintenance failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.initialize()
        app.state.slots = asyncio.Semaphore(policy.max_active_jobs if policy.public_demo else MAX_CONCURRENT_AUDITS)
        app.state.pdf_slot = asyncio.Semaphore(1)
        app.state.submission_lock = asyncio.Lock()
        app.state.export_inflight = 0
        app.state.db = db
        app.state.security = policy
        app.state.cleanup_expired = cleanup_expired
        maintenance = None
        if policy.public_demo:
            await cleanup_expired()
            maintenance = asyncio.create_task(retention_loop())
        try:
            yield
        finally:
            stopping = list(tasks) + ([maintenance] if maintenance is not None else [])
            for task in stopping:
                task.cancel()
            await asyncio.gather(*stopping, return_exceptions=True)

    app = FastAPI(title="Visual UI Auditor", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver", "*.onrender.com"]
        + ([urlsplit(policy.public_origin).hostname] if policy.public_demo and policy.public_origin else []),
    )

    @app.middleware("http")
    async def client_security(request: Request, call_next):
        token = None
        request.state.owner = None
        if policy.public_demo:
            request.state.owner = signer.verify(request.cookies.get(COOKIE_NAME))
            if request.state.owner is None:
                request.state.owner, token = signer.issue()
            if request.url.path.startswith(("/api/", "/screenshots/")):
                ip = request.client.host if request.client else "unknown"
                if (not limiter.allow(f"read-ip:{ip}", policy.requests_per_minute * 3, 60)
                        or not limiter.allow(f"read-owner:{request.state.owner}", policy.requests_per_minute, 60)):
                    return Response("Request rate limit exceeded", status_code=429, headers={"Retry-After": "60"})
        if request.method == "POST" and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            expected = request_origin(request)
            if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
                return Response("Same-origin audit client required", status_code=403)
            if not policy.public_demo and request.headers.get("x-audit-client") != "dashboard":
                return Response("Same-origin audit client required", status_code=403)
            if policy.public_demo:
                if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                    return Response("JSON request required", status_code=415)
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 8192:
                        return Response("Request body too large", status_code=413)
                    body.extend(chunk)
                request._body = bytes(body)
        response = await call_next(request)
        if token is not None:
            response.set_cookie(COOKIE_NAME, token, max_age=policy.session_seconds, secure=True,
                                httponly=True, samesite="strict", path="/")
        if policy.public_demo and request.url.path.startswith(("/api/", "/screenshots/")):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Cookie"
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
                    if policy.public_demo:
                        report = await run_audit(url, audit_id, screenshots / audit_id, progress, public_demo=True)
                        size = await asyncio.to_thread(directory_bytes, screenshots / audit_id)
                        if size > policy.max_audit_bytes or len(json.dumps(report).encode()) > policy.max_report_bytes:
                            raise ValueError("Public audit exceeded its storage allowance")
                    else:
                        report = await run_audit(url, audit_id, screenshots / audit_id, progress)
                message = ("Audit complete — accessibility coverage incomplete; review warnings"
                           if report.get("summary", {}).get("accessibility_status") == "incomplete"
                           else "Audit complete — ready to inspect and export")
                await db.event(audit_id, "completed", 100, message, report)
        except asyncio.CancelledError:
            await db.event(audit_id, "failed", 100, "Audit stopped because the server is shutting down.")
            raise
        except Exception as exc:
            logger.exception("Audit %s failed", audit_id)
            message = "Audit exceeded the configured time limit." if isinstance(exc, TimeoutError) else (
                "Public calibration audit could not complete. Please try again later." if policy.public_demo
                else f"{type(exc).__name__}: {str(exc)[:600]}"
            )
            if policy.public_demo:
                folder = screenshots / audit_id
                if folder.is_dir() and not folder.is_symlink():
                    try:
                        await asyncio.to_thread(shutil.rmtree, folder)
                    except OSError:
                        logger.exception("Could not remove failed public audit files %s", audit_id)
            await db.event(audit_id, "failed", 100, message)

    async def get_audit(audit_id: UUID, owner: str | None = None) -> dict:
        audit = await db.get(str(audit_id), owner=owner)
        if audit is None:
            raise HTTPException(404, "Audit not found")
        return audit

    @app.get("/api/config")
    async def client_config(request: Request):
        return {"public_demo_mode": policy.public_demo, "arbitrary_urls": not policy.public_demo,
                "calibration_url": calibration_url(request_origin(request)),
                "retention_seconds": policy.retention_seconds if policy.public_demo else None}

    @app.post("/api/audit", status_code=202)
    async def start_audit(request: Request, payload: AuditRequest):
        if len(payload.url) > (policy.max_url_length if policy.public_demo else 4096):
            raise HTTPException(422, "URL exceeds the configured maximum length")
        async with app.state.submission_lock:
            if policy.public_demo:
                ip = request.client.host if request.client else "unknown"
                if (not limiter.allow(f"submit-ip:{ip}", policy.submissions_per_ip, policy.rate_window_seconds)
                        or not limiter.allow(f"submit-owner:{request.state.owner}", policy.submissions_per_session, policy.rate_window_seconds)):
                    raise HTTPException(429, "Public demo submission rate exceeded", headers={"Retry-After": str(policy.rate_window_seconds)})
                if not is_calibration_url(payload.url, request_origin(request)):
                    raise HTTPException(422, "Public demo accepts only the built-in calibration URL; external and private targets are disabled.")
            queue_limit = policy.max_pending_jobs if policy.public_demo else MAX_CONCURRENT_AUDITS * 3
            if len(tasks) >= queue_limit:
                raise HTTPException(429, "Audit queue is full; try again when a run finishes.")
            if policy.public_demo:
                await cleanup_expired()
                used = await asyncio.to_thread(directory_bytes, storage)
                reserved = (len(tasks) + 1) * (policy.max_audit_bytes + policy.max_report_bytes)
                if await db.public_count() >= policy.max_public_audits or used + reserved > policy.max_storage_bytes:
                    raise HTTPException(507, "Public demo storage allowance reached; retry after retained audits expire.")
            audit_id = str(uuid4())
            await db.create(audit_id, payload.url, owner=request.state.owner,
                            expires_at=int(time.time()) + policy.retention_seconds if policy.public_demo else None)
            await db.event(audit_id, "queued", 0, "Queued — waiting for a browser slot")
            task = asyncio.create_task(job(audit_id, payload.url))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return {"audit_id": audit_id, "status": "queued"}

    @app.get("/api/audits")
    async def history(request: Request):
        return await db.history(owner=request.state.owner)

    @app.get("/api/audit/{audit_id}")
    async def audit_detail(request: Request, audit_id: UUID):
        return await get_audit(audit_id, request.state.owner)

    @app.get("/api/audit/{audit_id}/export/{format}")
    async def export(request: Request, audit_id: UUID, format: str):
        if format not in {"json", "html", "pdf"}:
            raise HTTPException(404, "Supported report formats: json, html, pdf")
        audit = await get_audit(audit_id, request.state.owner)
        if audit["status"] != "completed":
            raise HTTPException(409, "Report is available after the audit completes")
        headers = {"Content-Disposition": f'attachment; filename="visual-audit-{audit_id}.{format}"'}
        if format == "json":
            return Response(json.dumps(audit, indent=2, ensure_ascii=False), media_type="application/json", headers=headers)
        if policy.public_demo and app.state.export_inflight >= 2:
            raise HTTPException(429, "Export queue is full; try again shortly.")
        app.state.export_inflight += 1
        try:
            async with asyncio.timeout(45):
                html = await asyncio.to_thread(render_html, audit, screenshots)
                if format == "html":
                    return Response(html, media_type="text/html", headers=headers)
                async with app.state.pdf_slot:
                    pdf = await render_pdf(html)
                return Response(pdf, media_type="application/pdf", headers=headers)
        except TimeoutError as exc:
            raise HTTPException(503, "PDF export timed out. Try HTML export and print from your browser.") from exc
        finally:
            app.state.export_inflight -= 1

    @app.websocket("/ws/audit/{audit_id}")
    async def live_audit(websocket: WebSocket, audit_id: UUID, after: int = Query(0, ge=0)):
        origin = websocket.headers.get("origin")
        expected = (policy.public_origin if policy.public_demo and policy.public_origin else
                    f"{'https' if websocket.url.scheme == 'wss' else 'http'}://{websocket.headers.get('host', '')}")
        owner = signer.verify(websocket.cookies.get(COOKIE_NAME)) if policy.public_demo else None
        ip = websocket.client.host if websocket.client else "unknown"
        if ((origin and origin != expected) or (policy.public_demo and owner is None)
                or await db.get(str(audit_id), owner=owner) is None):
            await websocket.close(code=1008)
            return
        if policy.public_demo and (not limiter.allow(f"ws-ip:{ip}", policy.requests_per_minute, 60)
                                   or not sockets.acquire(owner)):
            await websocket.close(code=1013)
            return
        cursor, heartbeat = after, 0
        try:
            await websocket.accept()
            while True:
                if policy.public_demo and (signer.verify(websocket.cookies.get(COOKIE_NAME)) is None
                                           or await db.get(str(audit_id), owner=owner) is None):
                    await websocket.close(code=1008)
                    return
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
        finally:
            if policy.public_demo:
                sockets.release(owner)

    @app.get("/screenshots/{audit_id}/{filename}")
    async def screenshot(request: Request, audit_id: UUID, filename: str):
        await get_audit(audit_id, request.state.owner)
        allowed = {f"{viewport}{suffix}.png" for viewport in ("desktop", "tablet", "mobile") for suffix in ("", "-heatmap")}
        folder = screenshots / str(audit_id)
        path = folder / filename
        if filename not in allowed or folder.is_symlink() or path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Screenshot not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/")
    async def dashboard():
        return FileResponse(ROOT / "frontend" / "index.html")

    @app.get("/demo")
    async def demo():
        return FileResponse(ROOT / "tests" / "fixtures" / "layout.html")

    app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")
    return app


app = create_app(security=SecurityConfig.from_env())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), proxy_headers=False)
