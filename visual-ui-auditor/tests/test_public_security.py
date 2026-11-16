import asyncio
import io
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from PIL import Image
from playwright.async_api import Error, async_playwright
from starlette.websockets import WebSocketDisconnect

from backend.config import SecurityConfig
from backend.database import Database
from backend.main import create_app
from backend.security import COOKIE_NAME, PUBLIC_BROWSER_ARGS, RateLimiter, SessionSigner, restrict_public_context

ORIGIN = "https://testserver"
TARGET = ORIGIN + "/demo"
PUBLIC = SecurityConfig(public_demo=True, session_secret="test-only-signing-material-32-bytes-minimum")


async def completed_audit(url, audit_id, output, progress, *, public_demo=False):
    assert public_demo
    output.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 20), "white").save(output / "desktop.png")
    Image.new("RGB", (20, 20), "white").save(output / "desktop-heatmap.png")
    await progress(10, "Test running")
    return {"duration_seconds": 1, "summary": {"total": 0}, "viewports": [{
        "name": "desktop", "width": 1440, "height": 900, "screenshot_width": 20,
        "screenshot_height": 20, "bugs": [], "warnings": [],
        "screenshot": f"/screenshots/{audit_id}/desktop.png",
    }]}


def await_done(client, audit_id):
    with client.websocket_connect(f"wss://testserver/ws/audit/{audit_id}") as ws:
        while True:
            event = ws.receive_json()
            if event.get("status") in {"completed", "failed"}:
                return event


def test_cookie_ownership_covers_every_resource_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.main.run_audit", completed_audit)
    app = create_app(tmp_path, security=PUBLIC)
    with TestClient(app, base_url=ORIGIN) as owner:
        config = owner.get("/api/config")
        assert config.json()["calibration_url"] == TARGET
        assert config.json()["arbitrary_urls"] is False
        cookie = config.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        created = owner.post("/api/audit", json={"url": TARGET})
        assert created.status_code == 202
        audit_id = created.json()["audit_id"]
        assert await_done(owner, audit_id)["status"] == "completed"
        assert owner.get("/api/audits").json()[0]["id"] == audit_id
        screenshot = f"/screenshots/{audit_id}/desktop.png"
        assert owner.get(screenshot).status_code == 200
        assert owner.get(screenshot).headers["cache-control"] == "private, no-store"
        for format in ("json", "html", "pdf"):
            response = owner.get(f"/api/audit/{audit_id}/export/{format}")
            assert response.status_code == 200
            if format == "pdf":
                assert response.content.startswith(b"%PDF")
        saved_cookie = owner.cookies.get(COOKIE_NAME)
        with TestClient(app, base_url=ORIGIN) as stranger:
            assert stranger.get("/api/audits").json() == []
            for path in [f"/api/audit/{audit_id}", screenshot] + [
                f"/api/audit/{audit_id}/export/{format}" for format in ("json", "html", "pdf")
            ]:
                assert stranger.get(path, headers={"X-Audit-Client": "dashboard"}).status_code == 404
            with pytest.raises(WebSocketDisconnect):
                with stranger.websocket_connect(f"wss://testserver/ws/audit/{audit_id}"):
                    pass
            stranger.cookies.set(COOKIE_NAME, saved_cookie[:-1] + ("1" if saved_cookie[-1] != "1" else "2"))
            assert stranger.get(f"/api/audit/{audit_id}").status_code == 404
        with pytest.raises(WebSocketDisconnect):
            with owner.websocket_connect(f"wss://testserver/ws/audit/{audit_id}", headers={"Origin": "https://foreign.example"}):
                pass
    with TestClient(create_app(tmp_path, security=PUBLIC), base_url=ORIGIN) as restarted:
        assert restarted.get("/api/audits").json() == []
        restarted.cookies.set(COOKIE_NAME, saved_cookie)
        assert restarted.get(f"/api/audit/{audit_id}").status_code == 200


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://127.0.0.1/", "http://127.1/", "http://2130706433/",
    "http://0.0.0.0/", "http://10.0.0.1/", "http://172.16.1.1/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fd00::1]/", "http://[fe80::1]/",
    "http://224.0.0.1/", "http://192.0.2.1/", "https://example.com/", "https://8.8.8.8/",
    "https://testserver/demo?redirect=http://127.0.0.1", "https://testserver/demo#fragment",
    "https://testserver/demo/", "https://testserver/%64emo", "https://testserver:443/demo",
    "https://testserver@169.254.169.254/demo", "file:///etc/passwd", "ftp://example.com/",
])
def test_public_rejects_every_non_calibration_target(tmp_path, monkeypatch, url):
    async def unexpected(*args, **kwargs):
        pytest.fail("Rejected target reached browser")
    monkeypatch.setattr("backend.main.run_audit", unexpected)
    with TestClient(create_app(tmp_path, security=PUBLIC), base_url=ORIGIN) as client:
        assert client.post("/api/audit", json={"url": url}).status_code == 422
        assert client.get("/api/audits").json() == []


def test_public_bounds_body_url_and_cross_origin(tmp_path):
    with TestClient(create_app(tmp_path, security=PUBLIC), base_url=ORIGIN) as client:
        assert client.post("/api/audit", json={"url": "https://testserver/" + "a" * 2100}).status_code == 422
        assert client.post("/api/audit", content=b"x" * 8193, headers={"Content-Type": "application/json"}).status_code == 413
        assert client.post("/api/audit", data={"url": TARGET}).status_code == 415
        assert client.post("/api/audit", json={"url": TARGET}, headers={"Origin": "https://other.example"}).status_code == 403
        assert client.post("/api/audit", json={"url": TARGET}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_session_and_ip_limits_do_not_trust_forwarded_headers(tmp_path, monkeypatch):
    async def paused(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr("backend.main.run_audit", paused)
    policy = replace(PUBLIC, submissions_per_session=2, submissions_per_ip=3)
    with TestClient(create_app(tmp_path, security=policy), base_url=ORIGIN) as client:
        assert client.post("/api/audit", json={"url": TARGET}).status_code == 202
        assert client.post("/api/audit", json={"url": TARGET}).status_code == 202
        assert client.post("/api/audit", json={"url": TARGET}).status_code == 429
        client.cookies.clear()
        assert client.post("/api/audit", json={"url": TARGET}, headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 429


async def test_public_queue_and_active_jobs_are_bounded(tmp_path, monkeypatch):
    active = 0
    async def paused(*args, **kwargs):
        nonlocal active
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    monkeypatch.setattr("backend.main.run_audit", paused)
    app = create_app(tmp_path, security=replace(PUBLIC, max_pending_jobs=2))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
            await client.get("/api/config")
            results = await asyncio.gather(*[client.post("/api/audit", json={"url": TARGET}) for _ in range(3)])
            assert sorted(r.status_code for r in results) == [202, 202, 429]
            assert active == 1


async def test_retention_deletes_only_expired_new_public_owned_data(tmp_path):
    db = Database(tmp_path / "audits.db")
    await db.initialize()
    legacy, expired, fresh, active = (str(uuid4()) for _ in range(4))
    for audit_id in (legacy, expired, fresh, active):
        options = {} if audit_id == legacy else {"owner": "test-owner", "expires_at": int(time.time()) + (100 if audit_id == fresh else -100)}
        await db.create(audit_id, TARGET, **options)
        await db.event(audit_id, "completed", 100, "Done")
        folder = tmp_path / "screenshots" / audit_id
        folder.mkdir(parents=True)
        (folder / "desktop.png").write_bytes(b"keep unless expired public")
    app = create_app(tmp_path, security=PUBLIC)
    async with app.router.lifespan_context(app):
        assert await db.get(legacy) is not None
        assert await db.get(expired) is None
        assert await db.get(fresh) is not None
        assert (tmp_path / "screenshots" / legacy / "desktop.png").exists()
        assert not (tmp_path / "screenshots" / expired).exists()
        assert await db.events(expired) == []
        # Running jobs are never retention candidates, even if their TTL expires.
        await db.create(str(uuid4()), TARGET, owner="test-owner", expires_at=1)
        before = await db.public_count()
        await app.state.cleanup_expired()
        assert await db.public_count() == before
        async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
            assert (await client.get(f"/api/audit/{legacy}")).status_code == 404


def test_storage_admission_never_removes_existing_data(tmp_path):
    preserved = tmp_path / "existing-user-data.bin"
    preserved.write_bytes(b"existing data")
    policy = replace(PUBLIC, max_storage_bytes=1)
    with TestClient(create_app(tmp_path, security=policy), base_url=ORIGIN) as client:
        assert client.post("/api/audit", json={"url": TARGET}).status_code == 507
        assert preserved.read_bytes() == b"existing data"
        assert client.get("/api/audits").json() == []


def test_signer_and_rate_limiter_expiry_and_capacity(monkeypatch):
    signer = SessionSigner(PUBLIC.session_secret, 100)
    owner, token = signer.issue()
    assert signer.verify(token) == owner
    assert signer.verify("../invalid") is None
    assert signer.verify(token + "x") is None
    future = time.time() + 101
    monkeypatch.setattr("backend.security.time.time", lambda: future)
    assert signer.verify(token) is None
    limiter = RateLimiter(1)
    assert limiter.allow("one", 1, 10)
    assert not limiter.allow("one", 1, 10)
    assert not limiter.allow("two", 1, 10)
    assert len(limiter.entries) == 1


def test_environment_default_is_safe_and_factory_policy_is_explicit(monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_MODE", raising=False)
    monkeypatch.delenv("AUDITOR_SESSION_SECRET", raising=False)
    assert SecurityConfig.from_env().public_demo is True
    for value in ("unexpected", "", "off", "Falsee", "disabled", "1", "true"):
        monkeypatch.setenv("PUBLIC_DEMO_MODE", value)
        assert SecurityConfig.from_env().public_demo is True
    for value in ("false", "FALSE", " false ", "0", "no", "NO"):
        monkeypatch.setenv("PUBLIC_DEMO_MODE", value)
        assert SecurityConfig.from_env().public_demo is False


async def test_public_browser_fulfills_fixture_and_blocks_all_other_network(fixture_url):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=PUBLIC_BROWSER_ARGS)
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False)
            await restrict_public_context(context, TARGET)
            page = await context.new_page()
            failed = []
            page.on("requestfailed", lambda request: failed.append(request.url))
            await page.goto(TARGET, wait_until="networkidle")
            assert await page.title() == "Calibration — Signal Studio"
            assert any("missing-fixture-image" in url for url in failed)
            assert any("missing-fixture-api" in url for url in failed)
            for target in [fixture_url, "http://169.254.169.254/latest/meta-data/", "https://example.com/"]:
                assert await page.evaluate("url => fetch(url).then(() => false).catch(() => true)", target)
            assert await page.evaluate("() => new Promise(resolve => { const ws = new WebSocket('wss://example.com/socket'); ws.onclose = () => resolve(true); ws.onerror = () => resolve(true); setTimeout(() => resolve(false), 2000); })")
            assert await page.evaluate("() => { try { new RTCPeerConnection(); return false; } catch { return true; } }")
            with pytest.raises(Error):
                await page.goto(fixture_url, timeout=5000)
            await context.close()
        finally:
            await browser.close()


async def test_public_browser_fails_closed_without_websocket_routing():
    with pytest.raises(RuntimeError, match="WebSocket"):
        await restrict_public_context(object(), TARGET)


def test_incomplete_coverage_completion_message(tmp_path, monkeypatch):
    async def incomplete(*args, **kwargs):
        report = await completed_audit(*args, **kwargs)
        report["summary"]["accessibility_status"] = "incomplete"
        return report
    monkeypatch.setattr("backend.main.run_audit", incomplete)
    with TestClient(create_app(tmp_path, security=PUBLIC), base_url=ORIGIN) as client:
        audit_id = client.post("/api/audit", json={"url": TARGET}).json()["audit_id"]
        event = await_done(client, audit_id)
        assert event["status"] == "completed"
        assert "accessibility coverage incomplete" in event["message"]


def test_oversized_public_output_fails_and_cleans_only_its_files(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.main.run_audit", completed_audit)
    legacy = tmp_path / "screenshots" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "desktop.png").write_bytes(b"preserve")
    with TestClient(create_app(tmp_path, security=replace(PUBLIC, max_audit_bytes=1)), base_url=ORIGIN) as client:
        audit_id = client.post("/api/audit", json={"url": TARGET}).json()["audit_id"]
        assert await_done(client, audit_id)["status"] == "failed"
    assert not (tmp_path / "screenshots" / audit_id).exists()
    assert (legacy / "desktop.png").read_bytes() == b"preserve"


async def test_recurring_retention_does_not_need_a_new_submission(tmp_path):
    policy = replace(PUBLIC, cleanup_interval_seconds=1)
    app = create_app(tmp_path, security=policy)
    async with app.router.lifespan_context(app):
        audit_id = str(uuid4())
        db = app.state.db
        await db.create(audit_id, TARGET, owner="test-owner", expires_at=1)
        await db.event(audit_id, "completed", 100, "Done")
        for _ in range(30):
            if await db.get(audit_id) is None:
                break
            await asyncio.sleep(0.1)
        assert await db.get(audit_id) is None


def test_explicit_local_mode_preserves_private_targets(tmp_path, monkeypatch):
    called = []
    async def local_audit(*args):
        called.append(args[0])
        return {"summary": {}, "viewports": []}
    monkeypatch.setattr("backend.main.run_audit", local_audit)
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    with TestClient(create_app(tmp_path, security=SecurityConfig(public_demo=False))) as client:
        assert client.get("/api/config").json()["arbitrary_urls"] is True
        for url in ("http://localhost:1234/", "http://192.168.1.2/", "http://[::1]/"):
            audit_id = client.post("/api/audit", json={"url": url}, headers={"X-Audit-Client": "dashboard"}).json()["audit_id"]
            with client.websocket_connect(f"/ws/audit/{audit_id}") as ws:
                while ws.receive_json()["status"] != "completed":
                    pass
        assert len(called) == 3


def test_real_public_api_audit_scanners_exports_and_isolation(tmp_path, monkeypatch):
    from backend.engine import orchestrator

    real_guard = orchestrator.restrict_public_context
    guarded_contexts = []
    finished_requests = []
    failed_requests = []

    async def observed_guard(context, url):
        await real_guard(context, url)
        guarded_contexts.append(url)
        context.on("requestfinished", lambda request: finished_requests.append(request.url))
        context.on("requestfailed", lambda request: failed_requests.append((request.url, request.failure)))

    # Keep actual scanners and screenshots. A real clipped capture supplies a deterministic warning.
    monkeypatch.setattr(orchestrator, "restrict_public_context", observed_guard)
    monkeypatch.setattr(orchestrator, "MAX_PAGE_HEIGHT", 1024)
    monkeypatch.setattr("backend.main.AUDIT_TIMEOUT_SECONDS", 45)
    app = create_app(tmp_path, security=PUBLIC)
    with TestClient(app, base_url=ORIGIN) as owner:
        response = owner.post("/api/audit", json={"url": TARGET})
        assert response.status_code == 202
        audit_id = response.json()["audit_id"]
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            detail = owner.get(f"/api/audit/{audit_id}").json()
            if detail["status"] in {"completed", "failed"}:
                break
            time.sleep(0.75)
        assert detail["status"] == "completed", detail
        report = detail["report"]
        assert guarded_contexts == [TARGET] * 3
        assert finished_requests == [TARGET] * 3
        for path in ("/missing-fixture-image.png", "/missing-fixture-api"):
            assert sum(url == ORIGIN + path for url, _ in failed_requests) == 3
        assert all("BLOCKED_BY_CLIENT" in failure.upper() for _, failure in failed_requests)
        incomplete = [v["name"] for v in report["viewports"] if v["accessibility_status"] == "incomplete"]
        assert report["summary"]["accessibility_status"] == ("incomplete" if incomplete else "violations")
        assert report["summary"]["accessibility_incomplete_viewports"] == incomplete
        assert set(report["summary"]["by_pillar"]) == {"overflow", "collision", "touch", "runtime", "accessibility"}
        assert [(v["name"], v["width"], v["height"]) for v in report["viewports"]] == [
            ("desktop", 1440, 900), ("tablet", 768, 1024), ("mobile", 375, 812),
        ]
        screenshot_paths = []
        for viewport in report["viewports"]:
            assert viewport["accessibility_status"] in {"violations", "incomplete"}
            assert viewport["accessibility_issue_count"] > 0
            if viewport["accessibility_status"] == "incomplete":
                assert any("accessibility rules need manual review" in warning for warning in viewport["warnings"])
            assert any(bug["pillar"] == "accessibility" for bug in viewport["bugs"])
            assert any("Screenshot clipped" in warning for warning in viewport["warnings"])
            assert any("missing-fixture-api" in bug["message"] for bug in viewport["bugs"] if bug["pillar"] == "runtime")
            for key in ("screenshot", "heatmap"):
                screenshot_paths.append(viewport[key])
                response = owner.get(viewport[key])
                assert response.status_code == 200
                with Image.open(io.BytesIO(response.content)) as image:
                    assert image.size == (viewport["width"], 1024)
        assert report["summary"]["warning_count"] == sum(len(v["warnings"]) for v in report["viewports"])
        assert owner.get(f"/api/audit/{audit_id}/export/json").json() == detail
        html = owner.get(f"/api/audit/{audit_id}/export/html")
        assert html.status_code == 200
        assert "Screenshot clipped" in html.text
        assert ("Accessibility: incomplete" if incomplete else "Accessibility: checked") in html.text
        assert html.text.count("data:image/png;base64,") == 3
        pdf = owner.get(f"/api/audit/{audit_id}/export/pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        completion = await_done(owner, audit_id)
        assert completion["status"] == "completed"
        if incomplete:
            assert "accessibility coverage incomplete" in completion["message"]

        original_cookie = owner.cookies.get(COOKIE_NAME)
        owner.cookies.clear()
        assert owner.get("/api/audits").json() == []
        for path in [f"/api/audit/{audit_id}", *screenshot_paths, *[
            f"/api/audit/{audit_id}/export/{format}" for format in ("json", "html", "pdf")
        ]]:
            assert owner.get(path).status_code == 404
        with pytest.raises(WebSocketDisconnect):
            with owner.websocket_connect(f"wss://testserver/ws/audit/{audit_id}"):
                pass
        owner.cookies.clear()
        owner.cookies.set(COOKIE_NAME, original_cookie)
        assert owner.get(f"/api/audit/{audit_id}").status_code == 200


def test_tls_termination_uses_configured_origin_not_forwarded_headers(tmp_path, monkeypatch):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    external = "https://frame.onrender.com"
    policy = replace(PUBLIC, public_origin=external, submissions_per_session=10, submissions_per_ip=2)
    app = create_app(tmp_path, security=policy)
    monkeypatch.setattr("backend.main.run_audit", completed_audit)
    untrusted_headers_app = ProxyHeadersMiddleware(app, trusted_hosts=[])

    async def tls_terminating_proxy(scope, receive, send):
        # Browser uses HTTPS/WSS; the reverse proxy reaches the server over HTTP/WS.
        if scope["type"] in {"http", "websocket"}:
            scope = dict(scope)
            scope["scheme"] = "http" if scope["type"] == "http" else "ws"
            scope["client"] = ("10.20.30.40", 45678)
        await untrusted_headers_app(scope, receive, send)

    with TestClient(tls_terminating_proxy, base_url=external) as client:
        config = client.get("/api/config", headers={
            "X-Forwarded-Proto": "http", "X-Forwarded-Host": "attacker.example", "X-Forwarded-For": "8.8.8.8",
        })
        assert config.json()["calibration_url"] == external + "/demo"
        assert "Secure" in config.headers["set-cookie"]
        assert client.post("/api/audit", json={"url": external + "/demo"}, headers={"Origin": "https://attacker.example"}).status_code == 403
        created = client.post("/api/audit", json={"url": external + "/demo"}, headers={"Origin": external})
        assert created.status_code == 202
        audit_id = created.json()["audit_id"]
        with client.websocket_connect(f"wss://frame.onrender.com/ws/audit/{audit_id}", headers={"Origin": external}) as ws:
            for _ in range(10):
                event = ws.receive_json()
                if event["status"] in {"completed", "failed"}:
                    break
            assert event["status"] == "completed"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"wss://frame.onrender.com/ws/audit/{audit_id}", headers={"Origin": "https://attacker.example"}):
                pass
        client.cookies.clear()
        assert client.post("/api/audit", json={"url": external + "/demo"}, headers={"Origin": external, "X-Forwarded-For": "1.1.1.1"}).status_code == 202
        client.cookies.clear()
        # Changing a user-controlled forwarded address must not create a new IP quota.
        assert client.post("/api/audit", json={"url": external + "/demo"}, headers={"Origin": external, "X-Forwarded-For": "9.9.9.9"}).status_code == 429


def test_public_origin_environment_precedence_and_validation(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    monkeypatch.delenv("PUBLIC_ORIGIN", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://frame.onrender.com")
    assert SecurityConfig.from_env().public_origin == "https://frame.onrender.com"
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://frame.example/")
    assert SecurityConfig.from_env().public_origin == "https://frame.example"
    for invalid in ("http://frame.example", "https://user:pass@frame.example", "https://frame.example/path", "https://frame.example?query", "https://frame.example#fragment"):
        monkeypatch.setenv("PUBLIC_ORIGIN", invalid)
        with pytest.raises(ValueError):
            SecurityConfig.from_env()
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "false")
    assert SecurityConfig.from_env().public_origin is None
