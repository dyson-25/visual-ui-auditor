from playwright.async_api import async_playwright

from backend.engine.runtime_scanner import RuntimeCollector


async def test_javascript_error_keeps_stack_source(fixture_url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            collector = RuntimeCollector(page)
            await page.goto(fixture_url)
            async with page.expect_event("pageerror"):
                await page.add_script_tag(content="setTimeout(function sourceExample(){throw new Error('Source retained')}, 0);\n//# sourceURL=frame-runtime-fixture.js")
            finding = next(b for b in collector.bugs if b["type"] == "javascript_error")
            assert "Source retained" in finding["message"]
            assert "frame-runtime-fixture.js" in finding["message"]
            assert finding["rect"] is None
        finally:
            await browser.close()
