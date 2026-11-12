({touch = false, maxElements = 5000, maxFindings = 350} = {}) => {
  const bugs = [], warnings = [];
  const clickable = 'button,a[href],input:not([type="hidden"]),select,textarea,[role="button"],[role="link"],[tabindex]:not([tabindex="-1"])';
  const selector = el => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (n.id && document.querySelectorAll(`#${CSS.escape(n.id)}`).length === 1) {
        parts.unshift(`#${CSS.escape(n.id)}`); break;
      }
      let part = n.localName;
      if (n.parentElement) {
        const siblings = [...n.parentElement.children].filter(s => s.localName === n.localName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(n) + 1})`;
      }
      parts.unshift(part);
    }
    return parts.join(' > ');
  };
  const documentRect = r => ({x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height});
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
    let left = r.left, right = r.right, top = r.top, bottom = r.bottom;
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const s = getComputedStyle(p), b = p.getBoundingClientRect();
      if (/(hidden|clip|scroll|auto)/.test(s.overflowX)) { left = Math.max(left,b.left); right = Math.min(right,b.right); }
      if (/(hidden|clip|scroll|auto)/.test(s.overflowY)) { top = Math.max(top,b.top); bottom = Math.min(bottom,b.bottom); }
    }
    return right > left && bottom > top;
  };
  const clippedX = el => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (/(hidden|clip|auto|scroll)/.test(getComputedStyle(p).overflowX)) return true;
    }
    return false;
  };
  const add = (type, pillar, el, message, suggestion, severity = 'medium', other = null) => {
    if (bugs.length >= maxFindings) return;
    bugs.push({type, pillar, selector: selector(el), severity, message,
      rect: documentRect(el.getBoundingClientRect()), suggestion,
      related_selector: other ? selector(other) : null});
  };
  const all = [...document.querySelectorAll('body *')];
  if (all.length > maxElements) warnings.push(`DOM scan limited to ${maxElements} of ${all.length} elements.`);
  const elements = all.slice(0, maxElements).filter(visible);
  const controls = [], content = [];
  for (const el of elements) {
    const r = el.getBoundingClientRect();
    if ((r.right > document.documentElement.clientWidth + 1 || r.left < -1) && !clippedX(el)) {
      add('overflow', 'overflow', el, 'Element extends beyond the horizontal viewport.',
        `${selector(el)} { max-width: 100%; min-width: 0; overflow-wrap: anywhere; } /* Review fixed widths and parent flex/grid sizing. */`, 'high');
    }
    if (el.matches('img') && el.complete && el.naturalWidth === 0) {
      add('broken_image', 'runtime', el, 'Image finished loading without decodable image data.', 'Verify the image src/srcset URL, response status and file format; provide meaningful alt text.', 'high');
    }
    const isControl = el.matches(clickable) && !el.matches(':disabled');
    if (isControl) {
      controls.push(el);
      if (touch && (r.width < 44 || r.height < 44)) add('small_tap_target', 'touch', el,
        `Touch target is ${Math.round(r.width)} × ${Math.round(r.height)} CSS px; recommended minimum is 44 × 44.`,
        `${selector(el)} { min-width: 44px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; }`);
    }
    const hasText = !el.children.length && el.textContent.trim() && !el.matches('script,style,option,noscript');
    if (isControl || hasText) content.push(el);
  }
  let comparisons = 0;
  const compare = (list, margin, callback) => {
    const sorted = list.map(el => ({el, r: el.getBoundingClientRect()})).sort((a,b) => a.r.top - b.r.top);
    for (let i = 0; i < sorted.length; i++) {
      const a = sorted[i];
      for (let j = i + 1; j < sorted.length && sorted[j].r.top < a.r.bottom + margin; j++) {
        if (++comparisons > 100000) return;
        const b = sorted[j];
        if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
        callback(a, b);
      }
    }
  };
  compare(content, 0, (a,b) => {
    const dx = Math.min(a.r.right,b.r.right) - Math.max(a.r.left,b.r.left);
    const dy = Math.min(a.r.bottom,b.r.bottom) - Math.max(a.r.top,b.r.top);
    if (dx > 2 && dy > 2) add('overlap', 'collision', a.el,
      `Potential collision with ${selector(b.el)} (${Math.round(dx)} × ${Math.round(dy)} CSS px intersection). Intentional overlays need review.`,
      'Review absolute positioning and fixed heights. Prefer a flex/grid layout with gap and wrapping; verify the intended stacking order.', 'high', b.el);
  });
  if (touch) compare(controls, 4, (a,b) => {
    const dx = Math.max(0, Math.max(a.r.left,b.r.left) - Math.min(a.r.right,b.r.right));
    const dy = Math.max(0, Math.max(a.r.top,b.r.top) - Math.min(a.r.bottom,b.r.bottom));
    const distance = Math.hypot(dx,dy);
    if (distance < 4) add('crowded_tap_target', 'touch', a.el,
      `Only ${distance.toFixed(1)} CSS px from ${selector(b.el)}; recommended separation is 4 px.`,
      'On the shared layout container, use display: flex (or grid); gap: 4px or greater; flex-wrap: wrap. Review overlapping positioning.', 'medium', b.el);
  });
  if (comparisons > 100000) warnings.push('Pair comparison budget reached; some collisions or crowded controls may be unexamined.');
  if (bugs.length >= maxFindings) warnings.push(`DOM findings capped at ${maxFindings}; additional issues may exist.`);
  return {bugs, warnings, metrics: {elements_scanned: elements.length, comparisons}};
}
