from pathlib import Path

from playwright.async_api import Page

AXE_PATH = Path(__file__).resolve().parents[2] / "node_modules" / "axe-core" / "axe.min.js"


async def scan_accessibility(page: Page) -> dict:
    if not AXE_PATH.is_file():
        raise RuntimeError("axe-core is missing. Run npm ci in the project directory.")
    await page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    return await page.evaluate("""async () => {
      const result = await axe.run(document, {
        runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21aa']},
        resultTypes: ['violations','incomplete']
      });
      const bugs = [], warnings = [];
      for (const violation of result.violations) {
        for (const node of violation.nodes) {
          if (bugs.length >= 250) break;
          const target = node.target.length === 1 && typeof node.target[0] === 'string' ? node.target[0] : null;
          let rect = null;
          try {
            const el = target && document.querySelector(target);
            if (el) {
              const r = el.getBoundingClientRect();
              if (r.width && r.height) rect = {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height};
            }
          } catch (_) {}
          bugs.push({type: violation.id, pillar: 'accessibility', selector: target || JSON.stringify(node.target),
            severity: ['critical','serious'].includes(violation.impact) ? 'high' : 'medium',
            message: violation.help + ': ' + (node.failureSummary || violation.description), rect,
            suggestion: violation.id === 'color-contrast'
              ? 'Adjust foreground/background colors to at least 4.5:1 for normal text or 3:1 for large text. Verify actual composited colors.'
              : violation.id === 'button-name'
                ? 'Add visible descriptive button text, or an accessible aria-label for an icon-only button.'
                : violation.description + ' Consult the linked rule for context-specific remediation.',
            help_url: violation.helpUrl});
        }
      }
      if (bugs.length >= 250) warnings.push('Accessibility findings capped at 250.');
      if (result.incomplete.length) warnings.push(`${result.incomplete.length} accessibility rules need manual review: ${result.incomplete.map(r=>r.id).join(', ')}.`);
      return {bugs, warnings};
    }""")
