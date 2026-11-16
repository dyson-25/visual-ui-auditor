import pytest
from PIL import Image
from playwright.async_api import async_playwright

from backend.engine import a11y_scanner
from backend.engine.orchestrator import run_audit
from backend.reports import render_html


@pytest.mark.parametrize("violation", [False, True])
async def test_accessibility_execution_status(violation):
    assert a11y_scanner.AXE_PATH.is_file()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content('<html lang="en"><head><title>Accessible fixture</title></head>'
                                   '<body><main><h1>Example</h1>'
                                   + ('<button></button>' if violation else '<button>Continue</button>')
                                   + '</main></body></html>')
            result = await a11y_scanner.scan_accessibility(page)
            assert result["status"] == ("violations" if violation else "passed")
            assert bool(result["bugs"]) is violation
            assert result["issue_count"] == len(result["bugs"])
            assert result["warnings"] == []
        finally:
            await browser.close()


async def test_missing_axe_is_not_a_pass(monkeypatch, tmp_path, fixture_url):
    monkeypatch.setattr(a11y_scanner, "AXE_PATH", tmp_path / "missing-axe.js")
    with pytest.raises(RuntimeError, match="axe-core is missing"):
        await a11y_scanner.scan_accessibility(None)

    async def progress(value, message):
        pass

    output = tmp_path / "screenshots" / "unavailable"
    report = await run_audit(fixture_url, "unavailable", output, progress)
    assert report["summary"]["accessibility_status"] == "incomplete"
    assert report["summary"]["accessibility_incomplete_viewports"] == ["desktop", "tablet", "mobile"]
    for viewport in report["viewports"]:
        assert viewport["accessibility_status"] == "incomplete"
        assert viewport["accessibility_issue_count"] is None
        assert any("axe-core is missing" in warning for warning in viewport["warnings"])
        assert not any(b["pillar"] == "accessibility" for b in viewport["bugs"])
    html = render_html({"id": "unavailable", "url": fixture_url, "created_at": "2026-11-16", "report": report}, output.parent)
    assert html.count("Accessibility: incomplete") == 3
    assert "not a clean accessibility result" in html
    assert "axe-core is missing" in html


@pytest.mark.parametrize("status,count,text", [
    ("passed", 0, "checked — no detected violations"),
    ("violations", 2, "checked — 2 detected findings"),
    ("incomplete", None, "incomplete — not a clean accessibility result"),
])
def test_export_coverage_labels(tmp_path, status, count, text):
    folder = tmp_path / "report"
    folder.mkdir()
    Image.new("RGB", (20, 20)).save(folder / "mobile-heatmap.png")
    viewport = {"name": "mobile", "width": 375, "height": 812,
                "screenshot_width": 375, "screenshot_height": 812,
                "bugs": [], "warnings": [], "accessibility_status": status,
                "accessibility_issue_count": count}
    html = render_html({"id": "report", "url": "http://localhost/demo", "created_at": "today",
                        "report": {"duration_seconds": 1, "summary": {"total": 0},
                                   "viewports": [viewport]}}, tmp_path)
    assert text in html


async def test_manual_review_is_incomplete(monkeypatch):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            inject = page.add_script_tag

            async def axe_with_incomplete_rule(**kwargs):
                await inject(**kwargs)
                await page.evaluate("axe.run = async () => ({violations: [], incomplete: [{id: 'color-contrast'}]})")

            monkeypatch.setattr(page, "add_script_tag", axe_with_incomplete_rule)
            result = await a11y_scanner.scan_accessibility(page)
            assert result["status"] == "incomplete"
            assert result["issue_count"] == 0
            assert "color-contrast" in result["warnings"][0]
        finally:
            await browser.close()
