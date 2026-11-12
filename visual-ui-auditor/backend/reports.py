import base64
from html import escape
from pathlib import Path

from playwright.async_api import async_playwright


def render_html(audit: dict, screenshots: Path) -> str:
    report = audit["report"]
    sections = []
    for viewport in report["viewports"]:
        image_path = screenshots / audit["id"] / f"{viewport['name']}-heatmap.png"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        rows = []
        for number, bug in enumerate(viewport["bugs"], 1):
            number = bug.get("number") or number
            rows.append(f'''<tr><td>{number}</td><td>{escape(bug['severity'])}</td>
              <td><strong>{escape(bug['type'])}</strong><br><code>{escape(bug['selector'])}</code></td>
              <td>{escape(bug['message'])}</td><td><pre>{escape(bug['suggestion'])}</pre></td></tr>''')
        warnings = "".join(f"<li>{escape(w)}</li>" for w in viewport["warnings"])
        sections.append(f'''<section><h2>{escape(viewport['name'].title())} · {viewport['width']} × {viewport['height']}</h2>
          <p>{len(viewport['bugs'])} findings · capture {viewport['screenshot_width']} × {viewport['screenshot_height']} px</p>
          <ul class="warnings">{warnings}</ul><img alt="Annotated {escape(viewport['name'])} screenshot" src="data:image/png;base64,{encoded}">
          <table><thead><tr><th>#</th><th>Severity</th><th>Element / rule</th><th>Finding</th><th>Suggested remediation</th></tr></thead>
          <tbody>{''.join(rows) or '<tr><td colspan="5">No automated findings in this viewport.</td></tr>'}</tbody></table></section>''')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Visual UI Audit — {escape(audit['url'])}</title><style>
    *{{box-sizing:border-box}}body{{font:14px system-ui,sans-serif;color:#17202d;margin:40px auto;max-width:1100px;padding:0 24px}}
    h1{{font-size:34px}}h2{{border-top:2px solid #17202d;padding-top:24px}}p,li{{line-height:1.6;overflow-wrap:anywhere}}
    section{{break-before:page}}img{{display:block;max-width:100%;height:auto;border:1px solid #cdd2dd;margin:20px auto}}
    table{{border-collapse:collapse;width:100%;font-size:11px;table-layout:fixed}}td,th{{border:1px solid #d8dde5;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}
    th{{background:#eef2f8}}th:first-child{{width:35px}}th:nth-child(2){{width:65px}}tr{{break-inside:avoid}}pre,code{{font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere}}.warnings{{background:#fff3d9;padding:15px 32px}}
    @page{{size:A4 landscape;margin:12mm}}@media print{{body{{margin:0;padding:0;max-width:none}}img{{max-height:170mm;width:auto;max-width:100%;object-fit:contain}}}}
    </style></head><body><h1>Visual UI Audit</h1><p>{escape(audit['url'])}</p>
    <p>Created {escape(audit['created_at'])} · {report['duration_seconds']} seconds · {report['summary']['total']} findings</p>
    <p>Automated evidence, not an accessibility certification. Layout collisions are heuristic. Remediations are suggestions requiring review. Counts include repeat findings across viewports. Use your browser’s Print command to print this standalone report.</p>
    {''.join(sections)}</body></html>'''


async def render_pdf(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            context = await browser.new_context(java_script_enabled=False)
            page = await context.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html, wait_until="load", timeout=20000)
            return await page.pdf(format="A4", landscape=True, print_background=True, prefer_css_page_size=True)
        finally:
            await browser.close()
