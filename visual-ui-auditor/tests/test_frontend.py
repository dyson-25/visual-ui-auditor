import io
import json

from PIL import Image
import socket
import threading
import time

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from backend.main import create_app


@pytest.fixture
def live_dashboard(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path / "storage"), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "Test server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
    assert not thread.is_alive(), "Test server did not stop"


async def test_dashboard_audit_inspect_export_restore(live_dashboard, tmp_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        await page.goto(live_dashboard)
        await page.locator("#try-demo").click()
        await expect(page.locator("#audit-status")).to_contain_text("complete", ignore_case=True, timeout=60000)
        await expect(page.locator("canvas[data-viewport]")).to_have_count(3)
        for viewport in ("desktop", "tablet", "mobile"):
            await expect(page.locator(f'canvas[data-viewport="{viewport}"]')).to_be_visible()
        first = page.locator("#bug-list button").first
        await expect(first).to_be_visible()
        await first.click()
        await expect(page.locator("#inspector-selector")).not_to_be_empty()
        await page.locator('button[data-view="mobile"]').click()
        await expect(page.locator('canvas[data-viewport="mobile"]')).to_be_visible()
        await expect(page.locator('canvas[data-viewport="desktop"]')).not_to_be_visible()
        audit = await page.evaluate("""async () => {
            const list = await (await fetch('/api/audits')).json();
            return (await fetch(`/api/audit/${list[0].id}`)).json();
        }""")
        mobile = next(v for v in audit["report"]["viewports"] if v["name"] == "mobile")
        tiny = next(b for b in mobile["bugs"] if b["selector"] == "#tiny")
        canvas = page.locator('canvas[data-viewport="mobile"]')
        box = await canvas.bounding_box()
        await canvas.click(position={
            "x": (tiny["rect"]["x"] + tiny["rect"]["width"] / 2) * box["width"] / mobile["screenshot_width"],
            "y": (tiny["rect"]["y"] + tiny["rect"]["height"] / 2) * box["height"] / mobile["screenshot_height"],
        })
        await expect(page.locator("#inspector-selector")).to_have_text("#tiny")
        overlay_pixels = await canvas.evaluate("el => el.toDataURL()")
        await page.locator("#overlay-toggle").uncheck()
        await expect(page.locator("#overlay-toggle")).not_to_be_checked()
        await page.wait_for_function("([el, pixels]) => el.toDataURL() !== pixels", arg=[await canvas.element_handle(), overlay_pixels])
        await page.locator("#overlay-toggle").check()
        for format in ("json", "html", "pdf"):
            async with page.expect_download() as download_event:
                await page.locator(f'[data-export="{format}"]').click()
            download = await download_event.value
            export_path = tmp_path / f"report.{format}"
            await download.save_as(export_path)
            assert export_path.stat().st_size > 0
            if format == "json":
                report = json.loads(export_path.read_text())
                assert report["url"] == f"{live_dashboard}/demo"
                assert report["report"]["summary"]["total"] > 0
            elif format == "pdf":
                assert export_path.read_bytes().startswith(b"%PDF")
            else:
                assert "<html" in export_path.read_text()
        await page.reload()
        await expect(page.locator("#audit-status")).to_contain_text("complete", ignore_case=True, timeout=10000)
        await expect(page.locator("#audit-history")).to_contain_text("/demo")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.locator('button[data-view="mobile"]').click()
        await expect(page.locator('canvas[data-viewport="mobile"]')).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert errors == []
        await browser.close()


async def load_interaction_report(page, base_url):
    """Use deterministic tall captures without creating or modifying saved audits."""
    viewports = []
    images = {}
    for name, width in (("desktop", 1440), ("tablet", 768), ("mobile", 375)):
        image = io.BytesIO()
        Image.new("RGB", (width, 3200), "#172a36").save(image, format="PNG")
        images[name] = image.getvalue()
        overlap = {"x": width * .55, "y": 1600, "width": width * .25, "height": 400}
        bugs = []
        for index, (pillar, rect) in enumerate([
            ("overflow", overlap), ("collision", overlap),
            ("touch", {"x": width - 3, "y": 2300, "width": 2, "height": 2}),
            ("runtime", None),
            ("accessibility", {"x": 10, "y": 4000, "width": 30, "height": 30}),
            ("overflow", {"x": -10, "y": 2500, "width": 30, "height": 30}),
        ]):
            bugs.append({"pillar": pillar, "type": pillar, "rect": rect,
                         "severity": "high" if index % 2 == 0 else "medium",
                         "selector": f"#{name}-{index}", "message": f"Finding {index}",
                         "suggestion": "Review the element's layout."})
        viewports.append({"name": name, "width": width, "height": 900,
                          "screenshot_width": width, "screenshot_height": 3200,
                          "screenshot": f"/screenshots/interaction/{name}.png",
                          "bugs": bugs, "warnings": []})
    report = {"id": "interaction", "status": "completed", "progress": 100,
              "url": f"{base_url}/demo", "created_at": "2026-11-08T10:00:00Z",
              "report": {"viewports": viewports, "duration_seconds": 1,
                         "summary": {"total": 18, "warning_count": 0}}}

    async def api(route):
        if route.request.url.endswith("/api/audits"):
            await route.fulfill(json=[report, {**report, "id": "older"}])
        else:
            await route.fulfill(json={**report, "id": route.request.url.rsplit("/", 1)[-1]})

    async def screenshot(route):
        name = route.request.url.rsplit("/", 1)[-1].split(".")[0]
        await route.fulfill(content_type="image/png", body=images[name])

    await page.route("**/api/**", api)
    await page.route("**/screenshots/interaction/**", screenshot)
    await page.goto(base_url)
    await page.locator('[data-audit="interaction"]').click()
    await expect(page.locator('[data-capture-label="mobile"]')).to_contain_text("READY")
    return viewports


async def canvas_point(page, name, x, y, width):
    canvas = page.locator(f'canvas[data-viewport="{name}"]')
    await page.locator("#workspace").evaluate("el => el.scrollIntoView({block:'start'})")
    await canvas.evaluate("""(el, point) => {
        const s = el.parentElement;
        s.scrollLeft = point.x / point.width * el.clientWidth - s.clientWidth / 2;
        s.scrollTop = point.y / 3200 * el.clientHeight - s.clientHeight / 2;
    }""", {"x": x, "y": y, "width": width})
    box = await canvas.bounding_box()
    return {"x": box["x"] + x / width * box["width"],
            "y": box["y"] + y / 3200 * box["height"]}


@pytest.mark.parametrize("name,width", [("desktop", 1440), ("tablet", 768), ("mobile", 375)])
@pytest.mark.parametrize("view", ["all", "single"])
async def test_canvas_overlap_and_badges(live_dashboard, name, width, view):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        await load_interaction_report(page, live_dashboard)
        await page.locator(f'button[data-view="{name if view == "single" else "all"}"]').click()
        for zoom in (50, 100, 200, 400):
            while int((await page.locator("#zoom-value").inner_text()).rstrip("%")) != zoom:
                current = int((await page.locator("#zoom-value").inner_text()).rstrip("%"))
                await page.locator("#zoom-in" if current < zoom else "#zoom-out").click()
            point = await canvas_point(page, name, width * .7, 1800, width)
            await page.mouse.move(**point)
            canvas = page.locator(f'canvas[data-viewport="{name}"]')
            await expect(canvas).to_have_css("cursor", "pointer")
            for index in (0, 1, 0):
                await page.mouse.click(**point)
                await expect(page.locator("#inspector-selector")).to_have_text(f"#{name}-{index}")
                await expect(page.locator("#zoom-value")).to_have_text(f"{zoom}%")
            point = await canvas_point(page, name, width * .55, 1600, width)
            # Identical boxes keep separate, stable numbered badges.
            for index, offset in ((1, 30), (0, 9)):
                await page.mouse.click(point["x"] + offset, point["y"] + 9)
                await expect(page.locator("#inspector-selector")).to_have_text(f"#{name}-{index}")
            point = await canvas_point(page, name, 10, 2525, width)
            await page.mouse.click(**point)
            await expect(page.locator("#inspector-selector")).to_have_text(f"#{name}-5")
            # The tiny box's 19px badge extends left of the 2px document rectangle.
            point = await canvas_point(page, name, width - 1, 2301, width)
            box = await canvas.bounding_box()
            badge_point = {"x": box["x"] + box["width"] - 12, "y": point["y"] + 8}
            await page.mouse.move(**badge_point)
            await expect(canvas).to_have_css("cursor", "pointer")
            await page.mouse.click(**badge_point)
            await expect(page.locator("#inspector-selector")).to_have_text(f"#{name}-2")
            await page.locator("#overlay-toggle").uncheck()
            await page.mouse.move(**badge_point)
            await expect(canvas).to_have_css("cursor", "default")
            await page.mouse.click(**badge_point)
            await expect(page.locator("#inspector-selector")).to_have_text(f"#{name}-2")
            await page.locator("#overlay-toggle").check()
        assert errors == []
        await browser.close()


@pytest.mark.parametrize("screen_width,screen_height", [(1600, 1000), (800, 900), (390, 844)])
async def test_finding_navigation_and_controls(live_dashboard, screen_width, screen_height):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": screen_width, "height": screen_height})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        await load_interaction_report(page, live_dashboard)
        for view in ("desktop", "tablet", "mobile", "all"):
            await page.locator(f'button[data-view="{view}"]').click()
            await expect(page.locator(f'button[data-view="{view}"]')).to_have_attribute("aria-pressed", "true")
            for pillar in ("overflow", "collision", "touch", "runtime", "accessibility", "all"):
                button = page.locator(f'[data-pillar="{pillar}"]')
                await button.click()
                await expect(button).to_have_attribute("aria-pressed", "true")
                expected = (6 if pillar == "all" else 2 if pillar == "overflow" else 1) * (3 if view == "all" else 1)
                await expect(page.locator("#visible-count")).to_have_text(str(expected))
        for name in ("desktop", "tablet", "mobile"):
            await page.locator(f'[data-finding="{name}:2"]').click()
            await page.wait_for_function("Math.abs(document.querySelector('#workspace').getBoundingClientRect().top - 20) < 2")
            await expect(page.locator("#inspector-selector")).to_have_text(f"#{name}-2")
            await expect(page.locator("#inspector-selector")).to_be_in_viewport()
            await expect(page.locator(f'[data-scroll="{name}"]')).to_be_in_viewport()
            assert await page.locator(f'[data-scroll="{name}"]').evaluate("el => el.scrollTop > 0")
            assert await page.locator(f'canvas[data-viewport="{name}"]').evaluate("""el => {
                const c = el.getBoundingClientRect(), s = el.parentElement.getBoundingClientRect();
                const x = c.left + (el.width - 2) / el.width * c.width;
                const y = c.top + 2301 / 3200 * c.height;
                return x >= s.left && x <= s.right && y >= s.top && y <= s.bottom;
            }""")
        await page.locator('button[data-view="mobile"]').click()
        await page.locator('[data-finding="mobile:2"]').click()
        await expect(page.locator("#inspector-selector")).to_be_in_viewport()
        await expect(page.locator('[data-scroll="mobile"]')).to_be_in_viewport()
        await page.locator("#zoom-in").click()
        await page.locator("#zoom-reset").click()
        await expect(page.locator("#zoom-value")).to_have_text("100%")
        await page.locator("#zoom-in").click()
        await page.locator("#zoom-fit").click()
        await expect(page.locator("#zoom-value")).to_have_text("100%")
        for name in ("desktop", "tablet", "mobile"):
            assert await page.locator(f'[data-scroll="{name}"]').evaluate("el => el.scrollTop === 0 && el.scrollLeft === 0")
        for index, text in ((3, "No visual bounding box"), (4, "Not shown in capture")):
            await page.locator(f'[data-finding="mobile:{index}"]').click()
            await expect(page.locator("#inspector-location")).to_contain_text(text)
            await expect(page.locator("#inspector-selector")).to_be_in_viewport()
        await page.locator('[data-audit="older"]').click()
        await expect(page.locator('[data-audit="older"]')).to_have_attribute("aria-pressed", "true")
        await expect(page.locator("#audit-status")).to_contain_text("complete")
        await page.locator("#refresh-history").click()
        await expect(page.locator('[data-audit="older"]')).to_have_attribute("aria-pressed", "true")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert errors == []
        await browser.close()
