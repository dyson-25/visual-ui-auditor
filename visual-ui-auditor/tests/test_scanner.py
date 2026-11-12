from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from backend.engine.a11y_scanner import scan_accessibility
from backend.engine.orchestrator import VIEWPORTS
from backend.engine.runtime_scanner import RuntimeCollector
from backend.models import BugItem

SCANNER = (Path(__file__).parents[1] / "backend/engine/dom_scanner.js").read_text()


@pytest.mark.parametrize("name", VIEWPORTS)
async def test_five_pillars(name, fixture_url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport=VIEWPORTS[name])
        runtime = RuntimeCollector(page)
        await page.goto(fixture_url)
        result = await page.evaluate(SCANNER, {"touch": name != "desktop"})
        bugs = result["bugs"]
        kinds = {bug["type"] for bug in bugs}
        assert {"overlap", "broken_image"} <= kinds
        assert any("Calibration console error" in bug["message"] for bug in runtime.bugs)
        assert {"http_error", "console_error"} <= {bug["type"] for bug in runtime.bugs}
        if name != "desktop":
            assert {"overflow", "small_tap_target", "crowded_tap_target"} <= kinds
            assert any(b["rect"]["y"] > 1000 for b in bugs if b["type"] == "small_tap_target")
        else:
            assert "small_tap_target" not in kinds
            assert "overflow" not in kinds
        assert not any("offscreen" in b["selector"] for b in bugs)
        healthy_rects = await page.locator('.good button').evaluate_all("els => els.map(el => {const r = el.getBoundingClientRect(); return {x:r.x,y:r.y};})")
        assert len(healthy_rects) == 2
        assert not any(b["rect"]["x"] == r["x"] and b["rect"]["y"] == r["y"] for b in bugs for r in healthy_rects)
        a11y = await scan_accessibility(page)
        assert {"color-contrast", "button-name"} <= {b["type"] for b in a11y["bugs"]}
        for bug in bugs + a11y["bugs"]:
            BugItem.model_validate(bug)
        await browser.close()


async def test_parent_child_and_clipping_are_not_defects():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 375, "height": 812})
        await page.set_content('''<style>body{margin:0}.clip{width:300px;overflow:hidden}.wide{width:900px}button{width:90px;height:48px}</style>
          <div class="clip"><div class="wide">Clipped on purpose</div></div>
          <button><span>Nested</span></button>''')
        result = await page.evaluate(SCANNER, {"touch": True})
        assert result["bugs"] == []
        await browser.close()
