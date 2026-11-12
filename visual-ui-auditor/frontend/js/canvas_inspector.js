const MAX_DIMENSION = 16000;
const MAX_PIXELS = 24_000_000;
const finite = value => typeof value === 'number' && Number.isFinite(value);

/** Raw capture renderer. All hit testing uses document coordinates, never CSS pixels. */
export class CanvasInspector {
  constructor(canvas, { onSelect, onError, onReady } = {}) {
    this.canvas = canvas;
    this.scroller = canvas.parentElement;
    this.placeholder = this.scroller.querySelector('.capture-placeholder');
    this.context = canvas.getContext('2d');
    this.onSelect = onSelect;
    this.onError = onError;
    this.onReady = onReady;
    this.overlay = true;
    this.zoom = 1;
    this.items = [];
    this.selected = null;
    this.generation = 0;
    this.loaded = false;
    this.initialPlaceholder = this.placeholder.cloneNode(true);
    this.regions = [];
    this.lastHit = null;
    canvas.addEventListener('click', event => this.handleClick(event));
    const updatePointer = () => {
      this.canvas.style.cursor = this.pointer && this.hitsAt(this.pointer).length ? 'pointer' : 'default';
    };
    canvas.addEventListener('pointermove', event => {
      this.pointer = { clientX: event.clientX, clientY: event.clientY };
      updatePointer();
    });
    canvas.addEventListener('pointerleave', () => { this.pointer = null; updatePointer(); });
    const onScroll = () => { this.lastHit = null; updatePointer(); };
    this.scroller.addEventListener('scroll', onScroll);
    window.addEventListener('scroll', onScroll);
    this.resizeObserver = new ResizeObserver(() => this.layout());
    this.resizeObserver.observe(this.scroller);
  }

  clear(message) {
    this.generation += 1;
    if (this.image) {
      this.image.onload = null;
      this.image.onerror = null;
      this.image.src = '';
    }
    this.image = null;
    this.loaded = false;
    this.items = [];
    this.regions = [];
    this.lastHit = null;
    this.canvas.style.cursor = 'default';
    this.selected = null;
    this.viewport = null;
    this.documentWidth = 0;
    this.documentHeight = 0;
    this.canvas.hidden = true;
    this.canvas.width = 1;
    this.canvas.height = 1;
    this.scroller.scrollTop = 0;
    this.scroller.scrollLeft = 0;
    this.placeholder.replaceChildren(...[...this.initialPlaceholder.childNodes].map(node => node.cloneNode(true)));
    this.placeholder.hidden = false;
    if (message) this.placeholder.querySelector('p').textContent = message;
  }

  load(viewport, items) {
    this.clear('Loading screenshot…');
    this.viewport = viewport;
    this.items = items;
    const generation = this.generation;
    let source;
    try {
      source = new URL(viewport.screenshot, location.origin);
      if (source.origin !== location.origin || !source.pathname.startsWith('/screenshots/')) throw new Error('Invalid screenshot path');
    } catch {
      this.fail('Screenshot is unavailable.', generation);
      return;
    }
    const image = new Image();
    this.image = image;
    image.onload = () => {
      if (this.generation !== generation) return;
      if (!image.naturalWidth || !image.naturalHeight || !this.context) {
        this.fail('This screenshot could not be rendered.', generation);
        return;
      }
      this.loaded = true;
      this.documentWidth = Number(viewport.screenshot_width) > 0 ? Number(viewport.screenshot_width) : image.naturalWidth;
      this.documentHeight = Number(viewport.screenshot_height) > 0 ? Number(viewport.screenshot_height) : image.naturalHeight;
      const ratio = Math.min(1, MAX_DIMENSION / image.naturalWidth, MAX_DIMENSION / image.naturalHeight, Math.sqrt(MAX_PIXELS / (image.naturalWidth * image.naturalHeight)));
      this.canvas.width = Math.max(1, Math.round(image.naturalWidth * ratio));
      this.canvas.height = Math.max(1, Math.round(image.naturalHeight * ratio));
      this.placeholder.hidden = true;
      this.canvas.hidden = false;
      this.layout();
      this.onReady?.(viewport.name);
    };
    image.onerror = () => this.fail('Screenshot could not load. The capture may have expired.', generation);
    image.src = source.href;
  }

  fail(message, generation) {
    if (generation !== this.generation) return;
    this.loaded = false;
    this.canvas.hidden = true;
    this.placeholder.hidden = false;
    this.placeholder.replaceChildren();
    const title = document.createElement('strong');
    title.textContent = 'Capture unavailable';
    const description = document.createElement('p');
    description.textContent = message;
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'subtle-button';
    retry.textContent = 'Retry screenshot';
    retry.addEventListener('click', () => this.load(this.viewport, this.items));
    this.placeholder.append(title, description, retry);
    this.onError?.(this.viewport?.name, message);
  }

  setOverlay(enabled) {
    this.overlay = enabled;
    this.lastHit = null;
    this.canvas.style.cursor = 'default';
    this.draw();
  }

  setZoom(zoom) {
    this.zoom = Math.max(.5, Math.min(4, zoom));
    this.lastHit = null;
    this.layout();
  }

  layout() {
    if (!this.loaded || !this.scroller.clientWidth) return;
    // clientWidth rounds fractional grid columns and can clip right-edge findings.
    const fit = parseFloat(getComputedStyle(this.scroller).width) - (this.scroller.offsetWidth - this.scroller.clientWidth);
    const width = Math.max(1, fit * this.zoom);
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${width * this.image.naturalHeight / this.image.naturalWidth}px`;
    this.draw();
  }

  rectFor(item) {
    const rect = item?.bug?.rect;
    if (!rect || !['x', 'y', 'width', 'height'].every(key => finite(rect[key])) || rect.width <= 0 || rect.height <= 0) return null;
    return rect;
  }

  captureSize() {
    return {
      width: this.documentWidth || Number(this.viewport?.screenshot_width) || Number(this.viewport?.width) || 0,
      height: this.documentHeight || Number(this.viewport?.screenshot_height) || Number(this.viewport?.height) || 0,
    };
  }

  visibleRect(item) {
    const rect = this.rectFor(item);
    if (!rect) return null;
    const size = this.captureSize();
    const x = Math.max(0, rect.x);
    const y = Math.max(0, rect.y);
    const right = Math.min(size.width, rect.x + rect.width);
    const bottom = Math.min(size.height, rect.y + rect.height);
    return right > x && bottom > y ? { x, y, width: right - x, height: bottom - y } : null;
  }

  locationDescription(item) {
    if (!this.rectFor(item)) return 'No visual bounding box. Review the finding details.';
    if (!this.visibleRect(item)) return 'Not shown in capture — this element is outside the captured image.';
    if (!this.loaded) return 'Screenshot unavailable. Finding details are still accessible.';
    const rect = this.rectFor(item);
    return `Document position: ${Math.round(rect.x)}, ${Math.round(rect.y)} · ${Math.round(rect.width)} × ${Math.round(rect.height)} px`;
  }

  draw() {
    if (!this.loaded) return;
    const ctx = this.context;
    const canvas = this.canvas;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(this.image, 0, 0, canvas.width, canvas.height);
    this.regions = [];
    if (!this.overlay) return;
    const scaleX = canvas.width / this.documentWidth;
    const scaleY = canvas.height / this.documentHeight;
    const cssWidth = parseFloat(canvas.style.width) || canvas.width;
    const unit = canvas.width / cssWidth;
    ctx.font = `bold ${10 * unit}px ui-monospace, monospace`;
    for (const item of this.items) {
      const visible = this.visibleRect(item);
      if (!visible) continue;
      const rect = { x: visible.x * scaleX, y: visible.y * scaleY, width: visible.width * scaleX, height: visible.height * scaleY };
      const width = Math.min(canvas.width, Math.max(19 * unit, ctx.measureText(String(item.number)).width + 10 * unit));
      const height = Math.min(canvas.height, 18 * unit);
      const badge = { x: Math.max(0, Math.min(rect.x, canvas.width - width)), y: Math.max(0, Math.min(rect.y, canvas.height - height)), width, height };
      // Keep coincident findings' labels distinct, independent of selection order.
      for (let attempt = 0; attempt < this.regions.length; attempt += 1) {
        const collision = this.regions.find(region => this.intersects(badge, region.badge));
        if (!collision) break;
        const nextX = collision.badge.x + collision.badge.width + 2 * unit;
        if (nextX + width <= canvas.width) badge.x = nextX;
        else {
          badge.x = Math.max(0, Math.min(rect.x, canvas.width - width));
          const nextY = collision.badge.y + collision.badge.height + 2 * unit;
          if (nextY + height > canvas.height) break;
          badge.y = nextY;
        }
      }
      this.regions.push({ item, rect, badge });
    }
    const ordered = [...this.regions].sort((a, b) => Number(a.item.key === this.selected) - Number(b.item.key === this.selected));
    for (const { item, rect, badge } of ordered) {
      const active = item.key === this.selected;
      const color = item.bug.severity === 'high' ? '#ff526b' : '#ffb84e';
      const { x, y, width: w, height: h } = rect;
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, y, w, h);
      ctx.clip();
      ctx.fillStyle = item.bug.severity === 'high' ? (active ? '#ff526b30' : '#ff526b13') : (active ? '#ffb84e30' : '#ffb84e13');
      ctx.fillRect(x, y, w, h);
      ctx.restore();
      ctx.save();
      ctx.strokeStyle = active ? '#8cffe0' : color;
      ctx.lineWidth = (active ? 2.8 : 1.6) * unit;
      ctx.shadowColor = active ? '#65e4bd' : color;
      ctx.shadowBlur = (active ? 10 : 5) * unit;
      const inset = ctx.lineWidth / 2;
      ctx.strokeRect(x + inset, y + inset, Math.max(0, w - inset * 2), Math.max(0, h - inset * 2));
      ctx.shadowBlur = 0;
      const label = String(item.number);
      ctx.font = `bold ${10 * unit}px ui-monospace, monospace`;
      const { x: badgeX, y: badgeY, width: badgeWidth, height: badgeHeight } = badge;
      ctx.fillStyle = active ? '#8cffe0' : color;
      ctx.fillRect(badgeX, badgeY, badgeWidth, badgeHeight);
      ctx.fillStyle = '#131a22';
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'center';
      ctx.fillText(label, badgeX + badgeWidth / 2, badgeY + badgeHeight / 2);
      ctx.restore();
    }
  }

  intersects(a, b) {
    return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
  }

  hitsAt(event) {
    if (!this.loaded || !this.overlay) return [];
    const bounds = this.canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return [];
    // Client coordinates and bounds already include page AND inner-container scrolling.
    const x = (event.clientX - bounds.left) / bounds.width * this.canvas.width;
    const y = (event.clientY - bounds.top) / bounds.height * this.canvas.height;
    const contains = rect => x >= rect.x && x <= rect.x + rect.width && y >= rect.y && y <= rect.y + rect.height;
    const badges = this.regions.filter(region => contains(region.badge));
    const hits = badges.length ? badges : this.regions.filter(region => contains(region.rect));
    return hits.sort((a, b) => a.rect.width * a.rect.height - b.rect.width * b.rect.height || a.item.number - b.item.number).map(region => region.item);
  }

  handleClick(event) {
    const hits = this.hitsAt(event);
    if (!hits.length) { this.lastHit = null; return; }
    const signature = hits.map(item => item.key).join('|');
    const repeated = this.lastHit?.signature === signature && Math.hypot(event.clientX - this.lastHit.x, event.clientY - this.lastHit.y) < 5;
    const index = repeated ? (hits.findIndex(item => item.key === this.selected) + 1) % hits.length : 0;
    this.lastHit = { signature, x: event.clientX, y: event.clientY };
    this.onSelect?.(hits[index]);
  }

  select(item, focus = false) {
    this.selected = item?.key || null;
    this.draw();
    if (!focus || !item || !this.loaded) return;
    const rect = this.visibleRect(item);
    if (!rect) return;
    this.layout();
    const width = parseFloat(this.canvas.style.width);
    const height = parseFloat(this.canvas.style.height);
    this.lastHit = null;
    const centerX = this.canvas.offsetLeft + (rect.x + rect.width / 2) / this.documentWidth * width;
    // For oversized elements, show the beginning instead of an uninformative middle.
    const visibleDocumentHeight = this.scroller.clientHeight / height * this.documentHeight;
    const centerY = (rect.y + Math.min(rect.height / 2, visibleDocumentHeight / 2)) / this.documentHeight * height;
    this.scroller.scrollTo({ left: Math.max(0, centerX - this.scroller.clientWidth / 2), top: Math.max(0, centerY - this.scroller.clientHeight / 2), behavior: 'instant' });
  }
}
