"""Capture reproducible screenshots; run with python -m backend.engine.orchestrator URL."""
import argparse
import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from backend.config import MAX_PAGE_HEIGHT, NAVIGATION_TIMEOUT_MS
from backend.engine.a11y_scanner import scan_accessibility
from backend.engine.heatmap_drawer import draw_heatmap
from backend.engine.runtime_scanner import RuntimeCollector
from backend.models import BugItem, ViewportReport

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 375, "height": 812},
}
FREEZE_CSS = "*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; scroll-behavior: auto !important; }"
STORAGE = Path(__file__).resolve().parents[1] / "storage" / "screenshots"


async def capture(url: str, output: Path = STORAGE) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for name, viewport in VIEWPORTS.items():
                context = await browser.new_context(viewport=viewport, reduced_motion="reduce", device_scale_factor=1, bypass_csp=True)
                try:
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                    await page.add_style_tag(content=FREEZE_CSS)
                    height = min(MAX_PAGE_HEIGHT, await page.evaluate("Math.max(document.documentElement.scrollHeight, innerHeight)"))
                    await page.screenshot(path=output / f"{name}.png", full_page=True, clip={"x": 0, "y": 0, "width": viewport["width"], "height": height}, animations="disabled")
                    paths.append(output / f"{name}.png")
                finally:
                    await context.close()
        finally:
            await browser.close()
    return paths


async def run_audit(
    url: str,
    audit_id: str,
    output: Path,
    progress: Callable[[int, str], Awaitable[None]],
) -> dict:
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    scanner = (Path(__file__).parent / "dom_scanner.js").read_text(encoding="utf-8")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for index, (name, viewport) in enumerate(VIEWPORTS.items()):
                base = 5 + index * 30
                await progress(base, f"Opening {name.title()} {viewport['width']}×{viewport['height']}")
                context = await browser.new_context(
                    viewport=viewport, device_scale_factor=1, reduced_motion="reduce",
                    bypass_csp=True, service_workers="block", accept_downloads=False,
                )
                try:
                    page = await context.new_page()
                    page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
                    runtime = RuntimeCollector(page)
                    warnings = []
                    await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                    await page.add_style_tag(content=FREEZE_CSS)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=2000)
                    except BrowserTimeout:
                        warnings.append("Network did not become idle within 2 seconds; late-loaded content may differ.")
                    await page.evaluate("Promise.race([document.fonts.ready, new Promise(r=>setTimeout(r,1500))])")
                    await page.evaluate("scrollTo(0,0)")
                    await progress(base + 8, f"Scanning {name.title()} — geometry, touch targets and runtime")
                    geometry = await page.evaluate(scanner, {"touch": name != "desktop"})
                    warnings.extend(geometry["warnings"])
                    await progress(base + 16, f"Checking {name.title()} — WCAG contrast and accessible names")
                    try:
                        accessibility = await asyncio.wait_for(scan_accessibility(page), timeout=25)
                    except Exception as exc:
                        accessibility = {"bugs": [], "warnings": [f"Accessibility check incomplete: {type(exc).__name__}: {str(exc)[:300]}"]}
                    warnings.extend(accessibility["warnings"])
                    height = await page.evaluate("Math.max(document.documentElement.scrollHeight, innerHeight)")
                    capture_height = min(MAX_PAGE_HEIGHT, height)
                    if height > MAX_PAGE_HEIGHT:
                        warnings.append(f"Screenshot clipped at {MAX_PAGE_HEIGHT}px of {height}px document height.")
                    unloaded = await page.locator('img[loading="lazy"]').count()
                    if unloaded:
                        warnings.append("Lazy-loaded images below the fold are not forcibly scrolled into view; verify them manually.")
                    raw = output / f"{name}.png"
                    await page.screenshot(path=raw, full_page=True, animations="disabled", timeout=NAVIGATION_TIMEOUT_MS,
                                          clip={"x": 0, "y": 0, "width": viewport["width"], "height": capture_height})
                    bugs = geometry["bugs"] + accessibility["bugs"] + runtime.bugs
                    warnings.extend(runtime.warnings)
                    offset = sum(len(report["bugs"]) for report in reports)
                    bugs = [BugItem.model_validate({**bug, "id": f"{name}-{i}", "number": offset + i}).model_dump() for i, bug in enumerate(bugs, 1)]
                    annotated = output / f"{name}-heatmap.png"
                    await asyncio.to_thread(draw_heatmap, raw, annotated, bugs)
                    timing = await page.evaluate("({dom_content_loaded_ms:performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd || 0, resource_count:performance.getEntriesByType('resource').length})")
                    report = ViewportReport(
                        name=name, **viewport, screenshot_width=viewport["width"], screenshot_height=capture_height,
                        screenshot=f"/screenshots/{audit_id}/{raw.name}", heatmap=f"/screenshots/{audit_id}/{annotated.name}",
                        bugs=bugs, warnings=warnings, metrics={**geometry["metrics"], **timing},
                    )
                    reports.append(report.model_dump())
                    await progress(base + 28, f"Scanned {name.title()} — {len(bugs)} findings, {len(warnings)} warnings")
                finally:
                    await context.close()
        finally:
            await browser.close()
    findings = [bug for report in reports for bug in report["bugs"]]
    return {
        "audit_id": audit_id, "url": url, "duration_seconds": round(time.monotonic() - started, 2),
        "viewports": reports,
        "summary": {"total": len(findings), "by_severity": dict(Counter(b["severity"] for b in findings)),
                    "by_pillar": dict(Counter(b["pillar"] for b in findings)),
                    "warning_count": sum(len(r["warnings"]) for r in reports)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, default=STORAGE)
    args = parser.parse_args()
    for result in asyncio.run(capture(args.url, args.output)):
        print(result)
