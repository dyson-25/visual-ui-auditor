import asyncio

from httpx import ASGITransport, AsyncClient
from PIL import Image

from backend.config import MAX_CONCURRENT_AUDITS
from backend.engine.heatmap_drawer import draw_heatmap
from backend.main import create_app
from backend.reports import render_html


def test_heatmap_border_is_brighter_than_fill(tmp_path):
    source, dest = tmp_path / "raw.png", tmp_path / "map.png"
    Image.new("RGB", (100, 100), "black").save(source)
    draw_heatmap(source, dest, [
        {"severity": "high", "rect": {"x": 20, "y": 20, "width": 60, "height": 60}},
        {"severity": "medium", "rect": None},
        {"severity": "high", "rect": {"x": 200, "y": 200, "width": 20, "height": 20}},
    ])
    with Image.open(dest) as image:
        assert image.getpixel((20, 60))[0] > image.getpixel((50, 60))[0] * 3
        assert image.getpixel((0, 0)) == (0, 0, 0)


def test_html_export_escapes_target_content(tmp_path):
    folder = tmp_path / "sample"
    folder.mkdir()
    Image.new("RGB", (20, 20)).save(folder / "mobile-heatmap.png")
    bad = '<script>alert("untrusted")</script>'
    viewport = {"name": "mobile", "width": 375, "height": 812,
                "screenshot_width": 375, "screenshot_height": 812, "warnings": [bad],
                "bugs": [{"type": bad, "selector": bad, "message": bad, "suggestion": bad, "severity": "high"}]}
    html = render_html({"id": "sample", "url": bad, "created_at": "today",
                        "report": {"duration_seconds": 1, "summary": {"total": 1}, "viewports": [viewport]}}, tmp_path)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


async def test_concurrent_submissions_respect_queue_limit(tmp_path, monkeypatch):
    gate = asyncio.Event()

    async def paused(*args):
        await gate.wait()

    monkeypatch.setattr("backend.main.run_audit", paused)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            results = await asyncio.gather(*[
                client.post("/api/audit", json={"url": "http://localhost"}, headers={"X-Audit-Client": "dashboard"})
                for _ in range(MAX_CONCURRENT_AUDITS * 3 + 4)
            ])
        assert sum(r.status_code == 202 for r in results) == MAX_CONCURRENT_AUDITS * 3
        assert sum(r.status_code == 429 for r in results) == 4
