import { CanvasInspector } from './canvas_inspector.js';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const names = ['desktop', 'tablet', 'mobile'];
const pillars = ['overflow', 'collision', 'touch', 'runtime', 'accessibility'];
const terminal = status => status === 'completed' || status === 'failed';
const text = (value, max = 1800) => String(value ?? '').slice(0, max);
const number = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
const titleCase = value => text(value, 100).replace(/[_-]/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase());
const setText = (selector, value) => { $(selector).textContent = text(value, 6000); };
const element = (tag, className, content) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = text(content, 6000);
  return node;
};

let epoch = 0;
let controller = new AbortController();
let socket = null;
let reconnectTimer;
let heartbeatTimer;
let pollTimer;
let historyEpoch = 0;
let historyController;
let elapsedTimer;
let refreshBusy = false;
let refreshAgain = false;
let reconnectCount = 0;
let lastSeq = 0;
let logs = 0;
let latestLog = '';
let listLimit = 200;
let zoom = 1;
let selectedView = 'all';
let selectedPillar = 'all';
let selected = null;
let exporting = false;
let startedAt = 0;
let active = { id: null, status: 'idle', progress: 0, report: null, url: '' };
let allItems = [];
const inspectors = new Map();

for (const name of names) {
  inspectors.set(name, new CanvasInspector($(`canvas[data-viewport='${name}']`), {
    onSelect: item => selectFinding(item, false),
    onError: (viewport, message) => {
      addLog(`${titleCase(viewport)}: ${message}`, 'error');
      setText(`[data-capture-label='${viewport}']`, 'CAPTURE UNAVAILABLE');
      if (selected?.viewport === viewport) renderInspector();
    },
    onReady: viewport => {
      setText(`[data-capture-label='${viewport}']`, 'CAPTURE READY · SCROLL TO EXPLORE');
      updateZoomButtons();
      if (selected?.viewport === viewport) {
        inspectors.get(viewport).select(selected, true);
        renderInspector();
      }
    },
  }));
}

function storage(action, value) {
  try {
    if (action === 'read') return localStorage.getItem('frame.activeAudit');
    if (action === 'write') localStorage.setItem('frame.activeAudit', value);
    if (action === 'remove') localStorage.removeItem('frame.activeAudit');
  } catch { /* Storage may be disabled; the current audit remains usable. */ }
  return null;
}

function feedback(message) {
  setText('#feedback', message);
  $('#feedback').hidden = !message;
}

function addLog(message, kind = '') {
  const safeMessage = text(message, 1400);
  if (!safeMessage || safeMessage === latestLog) return;
  latestLog = safeMessage;
  const container = $('#audit-logs');
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 45;
  if (!logs) container.replaceChildren();
  const row = element('div', `log-row ${kind}`);
  const now = new Date();
  const time = element('time', '', now.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }));
  time.dateTime = now.toISOString();
  row.append(time, element('span', 'log-message', safeMessage));
  container.append(row);
  logs += 1;
  while (container.children.length > 150) container.firstElementChild.remove();
  if (nearBottom) container.scrollTop = container.scrollHeight;
}

async function request(url, options = {}, timeout = 25000) {
  const requestController = new AbortController();
  const parentSignal = options.signal;
  const abort = () => requestController.abort();
  if (parentSignal?.aborted) abort();
  else parentSignal?.addEventListener('abort', abort, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; requestController.abort(); }, timeout);
  try {
    const response = await fetch(url, { ...options, signal: requestController.signal, cache: 'no-store' });
    if (!response.ok) {
      let reason = '';
      try {
        const payload = await response.json();
        reason = typeof payload.detail === 'string' ? payload.detail : typeof payload.error === 'string' ? payload.error : '';
      } catch { /* Non-JSON errors must not be displayed as arbitrary markup. */ }
      throw new Error(reason ? text(reason, 500) : `Request failed (${response.status}).`);
    }
    // Read the body before clearing the timeout; downloads can take several seconds.
    return options.asBlob ? await response.blob() : await response.json();
  } catch (error) {
    if (timedOut) throw new Error('The request timed out. Please try again.');
    throw error;
  } finally {
    clearTimeout(timer);
    parentSignal?.removeEventListener('abort', abort);
  }
}

function stopSocket() {
  clearTimeout(reconnectTimer);
  clearTimeout(heartbeatTimer);
  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    socket.close();
    socket = null;
  }
}

function resetRun() {
  epoch += 1;
  controller.abort();
  controller = new AbortController();
  stopSocket();
  clearTimeout(pollTimer);
  clearInterval(elapsedTimer);
  refreshBusy = false;
  refreshAgain = false;
  reconnectCount = 0;
  lastSeq = 0;
  logs = 0;
  latestLog = '';
  exporting = false;
  active = { id: null, status: 'idle', progress: 0, report: null, url: '' };
  allItems = [];
  selected = null;
  selectedPillar = 'all';
  listLimit = 200;
  zoom = 1;
  startedAt = Date.now();
  $('#audit-logs').replaceChildren(element('p', 'log-empty', 'Preparing audit workspace…'));
  $('#inspector-detail').hidden = true;
  $('#inspector-empty').hidden = false;
  setText('#inspector-selector', '');
  $('#warnings-panel').hidden = true;
  $('#warnings-panel').open = false;
  $('#warnings-list').replaceChildren();
  for (const name of names) {
    const inspector = inspectors.get(name);
    inspector.clear('Waiting for the audit capture.');
    inspector.setZoom(1);
    setText(`[data-count='${name}']`, '—');
    $(`[data-count='${name}']`).classList.remove('has-bugs');
    $(`[data-card='${name}']`).classList.remove('active');
    setText(`[data-capture-label='${name}']`, 'AWAITING CAPTURE');
  }
  for (const id of ['total-count', 'high-count', 'medium-count', 'warning-count']) setText(`#${id}`, '—');
  setText('#summary-caption', 'Awaiting results');
  setText('#warning-caption', 'Capture & coverage');
  setText('#duration', '—');
  setText('#export-status', 'Reports unlock when the audit is complete.');
  setText('#connection-status', 'Connecting');
  setText('#zoom-value', '100%');
  feedback('');
  updateExports();
  updateZoomButtons();
  setView('all');
  renderFindings();
  return epoch;
}

function updateState(status, progress, error) {
  if (!['idle', 'submitting', 'queued', 'running', 'completed', 'failed'].includes(status)) return;
  active.status = status;
  active.progress = Math.max(active.progress, Math.min(100, number(progress)));
  const warnings = number(active.report?.summary?.warning_count);
  let label = 'Ready for your first audit';
  if (status === 'submitting') label = 'Preparing your audit…';
  if (status === 'queued') label = 'Audit queued';
  if (status === 'running') label = 'Visual audit in progress';
  if (status === 'completed') label = active.report ? (warnings ? `Audit complete · ${warnings} warning${warnings === 1 ? '' : 's'}` : 'Audit complete · no capture warnings') : 'Audit complete · loading report';
  if (status === 'failed') label = 'Audit failed';
  setText('#audit-status', label);
  setText('#audit-target', active.url || 'Your next quality check starts here.');
  $('#audit-target').title = text(active.url);
  const value = status === 'completed' ? 100 : Math.round(active.progress);
  $('#progress').value = value;
  setText('#progress-value', `${value}%`);
  setText('#progress-label', status === 'completed' ? 'AUDIT COMPLETE' : status === 'failed' ? 'AUDIT STOPPED' : status === 'idle' ? 'AWAITING TARGET' : 'AUDIT PIPELINE');
  $('#mission-dot').className = `status-dot ${status === 'failed' ? 'failed' : status === 'completed' ? (warnings ? 'warning' : '') : status === 'idle' ? 'muted' : 'running'}`;
  $('#run-audit').disabled = status === 'submitting';
  $('#try-demo').disabled = status === 'submitting';
  if (terminal(status)) {
    stopSocket();
    clearInterval(elapsedTimer);
    setText('#connection-status', status === 'failed' ? 'Stopped' : 'Finished');
    if (status === 'failed') {
      clearTimeout(pollTimer);
      if (error) {
        feedback(`Audit failed: ${text(error, 1000)}`);
        addLog(error, 'error');
      }
      setText('#export-status', 'No completed report is available. Review the activity log and try again.');
      for (const inspector of inspectors.values()) if (!inspector.loaded) inspector.clear('Audit stopped before a capture was available.');
    }
  }
  updateExports();
}

function startClock(createdAt) {
  const parsed = Date.parse(createdAt);
  startedAt = Number.isFinite(parsed) ? Math.min(Date.now(), parsed) : Date.now();
  clearInterval(elapsedTimer);
  const tick = () => setText('#duration', `${Math.max(0, Math.floor((Date.now() - startedAt) / 1000))}s`);
  tick();
  elapsedTimer = setInterval(tick, 1000);
}

function updateExports() {
  for (const button of $$('[data-export]')) button.disabled = active.status !== 'completed' || !active.report || exporting;
}

function updateZoomButtons() {
  const ready = [...inspectors.values()].some(inspector => inspector.loaded);
  $('#zoom-out').disabled = !ready || zoom <= .5;
  $('#zoom-in').disabled = !ready || zoom >= 4;
  $('#zoom-fit').disabled = !ready;
  $('#zoom-reset').disabled = !ready;
  $('#zoom-reset').setAttribute('aria-pressed', String(zoom === 1));
}

function changeZoom(value) {
  zoom = Math.max(.5, Math.min(4, value));
  for (const inspector of inspectors.values()) inspector.setZoom(zoom);
  setText('#zoom-value', `${Math.round(zoom * 100)}%`);
  updateZoomButtons();
}

async function startAudit() {
  if ($('#run-audit').disabled) return;
  let url;
  try {
    url = new URL($('#audit-url').value.trim());
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid URL');
  } catch {
    feedback('Enter a valid http:// or https:// URL without embedded credentials.');
    $('#audit-url').focus();
    return;
  }
  const current = resetRun();
  storage('remove');
  active.url = url.href;
  updateState('submitting', 0);
  addLog('Submitting URL for a three-viewport visual audit.');
  startClock();
  try {
    const data = await request('/api/audit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Audit-Client': 'dashboard' },
      body: JSON.stringify({ url: url.href }),
      signal: controller.signal,
    });
    if (current !== epoch) return;
    if (typeof data.audit_id !== 'string' || !data.audit_id) throw new Error('The server did not return an audit identifier.');
    active.id = data.audit_id;
    storage('write', active.id);
    updateState('queued', 0);
    addLog('Audit accepted. Waiting for the capture workers.');
    connectSocket(current);
    refreshAudit(current);
    loadHistory();
  } catch (error) {
    if (current !== epoch || error.name === 'AbortError') return;
    updateState('failed', 0, `Could not start the audit. ${text(error.message, 700)}`);
    setText('#connection-status', 'Not connected');
    renderFindings();
  }
}

async function selectAudit(id) {
  if (!id) return;
  const current = resetRun();
  active.id = text(id, 200);
  storage('write', active.id);
  updateState('queued', 0);
  addLog('Loading saved audit and available report.');
  markHistory();
  await refreshAudit(current, true);
  if (current === epoch && !terminal(active.status)) connectSocket(current);
}

function schedulePoll(current, delay = 6000) {
  clearTimeout(pollTimer);
  if (current !== epoch || !active.id || active.status === 'failed' || (active.status === 'completed' && active.report)) return;
  pollTimer = setTimeout(() => refreshAudit(current), delay);
}

async function refreshAudit(current, initial = false) {
  if (current !== epoch || !active.id) return;
  if (refreshBusy) { refreshAgain = true; return; }
  refreshBusy = true;
  const id = active.id;
  let retry = false;
  try {
    const data = await request(`/api/audit/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (current !== epoch || active.id !== id) return;
    if (!data || !['queued', 'running', 'completed', 'failed'].includes(data.status)) throw new Error('The server returned an invalid audit response.');
    active.url = text(data.url, 3000) || active.url;
    if (initial) {
      $('#audit-url').value = active.url;
      if (!terminal(data.status)) startClock(data.created_at);
      addLog(`Saved audit loaded · ${data.status}.`);
    }
    if (data.report && data.status === 'completed' && !active.report) renderReport(data.report);
    // A snapshot requested before a terminal socket event must not regress the UI.
    if (!terminal(active.status) || terminal(data.status)) updateState(data.status, data.progress, data.error);
    if (data.status === 'failed') {
      addLog(data.error || 'The audit failed without further details.', 'error');
      renderFindings();
    }
    if (data.status === 'completed' && data.report) {
      addLog(number(data.report.summary?.warning_count) ? 'Audit complete with capture warnings. Review coverage before using this report.' : 'Audit complete. All available findings are ready to inspect.', 'success');
      feedback('');
      loadHistory();
    }
    markHistory();
  } catch (error) {
    if (current !== epoch || error.name === 'AbortError') return;
    retry = true;
    addLog(`Unable to retrieve audit: ${text(error.message, 700)} Retrying…`, 'error');
    feedback(`Unable to load the latest audit state. Retrying automatically. ${text(error.message, 500)}`);
    setText('#connection-status', 'Retrying');
  } finally {
    if (current === epoch) {
      refreshBusy = false;
      if (refreshAgain) {
        refreshAgain = false;
        schedulePoll(current, 150);
      } else schedulePoll(current, retry || reconnectCount >= 3 || active.status === 'completed' ? 2500 : 6000);
    }
  }
}

function connectSocket(current) {
  if (current !== epoch || !active.id || terminal(active.status) || reconnectCount >= 3) return;
  stopSocket();
  const url = new URL(`/ws/audit/${encodeURIComponent(active.id)}`, location.origin);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('after', String(lastSeq));
  let ws;
  try { ws = new WebSocket(url.href); } catch { retryConnection(current); return; }
  socket = ws;
  const watchdog = () => {
    clearTimeout(heartbeatTimer);
    heartbeatTimer = setTimeout(() => {
      if (socket === ws && current === epoch) ws.close();
    }, 45000);
  };
  watchdog();
  ws.onopen = () => {
    if (current !== epoch || socket !== ws) return;
    setText('#connection-status', 'Live');
    addLog(reconnectCount ? `Live connection restored after event ${lastSeq}.` : 'Live progress connected.');
    watchdog();
  };
  ws.onmessage = event => {
    if (current !== epoch || socket !== ws) return;
    watchdog();
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (!data || typeof data !== 'object' || data.heartbeat) return;
    if (Number.isFinite(data.seq)) {
      if (data.seq <= lastSeq && !terminal(data.status)) return;
      lastSeq = Math.max(lastSeq, data.seq);
    }
    if (data.message) addLog(data.message, data.status === 'failed' ? 'error' : data.status === 'completed' ? 'success' : '');
    if (data.status === 'queued' && active.status === 'running') return;
    updateState(data.status, data.progress, data.status === 'failed' ? data.message : undefined);
    if (terminal(data.status)) {
      // Terminal events may omit seq, and the report is always fetched separately.
      refreshAudit(current);
      loadHistory();
    }
  };
  ws.onerror = () => { if (current === epoch && socket === ws) setText('#connection-status', 'Reconnecting'); };
  ws.onclose = () => {
    if (current !== epoch || socket !== ws || terminal(active.status)) return;
    socket = null;
    clearTimeout(heartbeatTimer);
    retryConnection(current);
  };
}

function retryConnection(current) {
  if (current !== epoch || terminal(active.status)) return;
  reconnectCount += 1;
  if (reconnectCount >= 3) {
    setText('#connection-status', 'Polling');
    addLog('Live connection unavailable. Continuing with automatic status polling.');
    schedulePoll(current, 500);
    return;
  }
  setText('#connection-status', 'Reconnecting');
  addLog(`Live connection interrupted. Reconnecting from event ${lastSeq}…`);
  reconnectTimer = setTimeout(() => connectSocket(current), 1000 * 2 ** (reconnectCount - 1));
}

function renderReport(report) {
  active.report = report;
  allItems = [];
  selected = null;
  const viewports = Array.isArray(report.viewports) ? report.viewports : [];
  const warnings = [];
  for (const name of names) {
    const viewport = viewports.find(item => item.name === name);
    if (!viewport) {
      inspectors.get(name).clear('This viewport is not included in the report.');
      setText(`[data-capture-label='${name}']`, 'NO CAPTURE RETURNED');
      continue;
    }
    const bugs = Array.isArray(viewport.bugs) ? viewport.bugs : [];
    const items = bugs.filter(bug => bug && typeof bug === 'object').map((bug, index) => ({ bug, viewport: name, number: allItems.length + index + 1, key: `${name}:${index}` }));
    allItems.push(...items);
    inspectors.get(name).load(viewport, items);
    const count = $(`[data-count='${name}']`);
    count.textContent = String(items.length);
    count.classList.toggle('has-bugs', items.length > 0);
    const size = $(`[data-card='${name}'] .viewport-size`);
    size.textContent = `${number(viewport.width)} × ${number(viewport.height)}`;
    for (const warning of Array.isArray(viewport.warnings) ? viewport.warnings : []) warnings.push(`${titleCase(name)} · ${text(warning, 1200)}`);
  }
  const summary = report.summary || {};
  const total = summary.total == null ? allItems.length : number(summary.total);
  const high = summary.by_severity?.high ?? allItems.filter(item => item.bug.severity === 'high').length;
  const medium = summary.by_severity?.medium ?? allItems.filter(item => item.bug.severity === 'medium').length;
  const warningCount = number(summary.warning_count);
  setText('#total-count', total);
  setText('#high-count', number(high));
  setText('#medium-count', number(medium));
  setText('#warning-count', warningCount);
  setText('#summary-caption', total ? 'Across all viewports' : warningCount ? 'Check capture coverage' : 'No findings detected');
  setText('#warning-caption', warningCount ? 'Coverage needs review' : 'No capture warnings');
  setText('#duration', `${number(report.duration_seconds).toFixed(1)}s`);
  const warningList = $('#warnings-list');
  warningList.replaceChildren();
  for (const warning of warnings) warningList.append(element('li', '', warning));
  if (warningCount && !warnings.length) warningList.append(element('li', '', 'The audit reported coverage warnings without additional details. Results may be incomplete.'));
  $('#warnings-panel').hidden = !warnings.length && !warningCount;
  setText('#warnings-title', `Capture & coverage warnings (${warningCount || warnings.length})`);
  setText('#export-status', 'Completed report ready. Choose a format to download.');
  renderFindings();
  if (allItems.length) selectFinding(allItems[0], false);
}

function setView(view) {
  selectedView = names.includes(view) ? view : 'all';
  $('#canvas-grid').dataset.view = selectedView;
  for (const button of $$('button[data-view]')) {
    const chosen = button.dataset.view === selectedView;
    button.classList.toggle('active', chosen);
    button.setAttribute('aria-pressed', String(chosen));
  }
  for (const name of names) $(`[data-card='${name}']`).hidden = selectedView !== 'all' && selectedView !== name;
  setText('#finding-scope', selectedView === 'all' ? 'All viewports' : `${titleCase(selectedView)} viewport`);
  listLimit = 200;
  renderFindings();
  for (const inspector of inspectors.values()) inspector.layout();
}

function renderFindings() {
  const scoped = allItems.filter(item => selectedView === 'all' || item.viewport === selectedView);
  for (const button of $$('[data-pillar]')) {
    const pillar = button.dataset.pillar;
    const count = pillar === 'all' ? scoped.length : scoped.filter(item => item.bug.pillar === pillar).length;
    button.querySelector('span').textContent = String(count);
    const chosen = pillar === selectedPillar;
    button.classList.toggle('active', chosen);
    button.setAttribute('aria-pressed', String(chosen));
  }
  const filtered = scoped.filter(item => selectedPillar === 'all' || item.bug.pillar === selectedPillar);
  setText('#visible-count', filtered.length);
  const list = $('#bug-list');
  list.replaceChildren();
  if (!filtered.length) {
    const empty = element('div', 'findings-empty');
    let heading = 'Clarity, not clutter.';
    let message = 'Run an audit to build your actionable quality checklist.';
    if (active.report) {
      heading = allItems.length ? 'Nothing in this view.' : number(active.report.summary?.warning_count) ? 'No findings returned. Coverage has warnings.' : 'A clear result.';
      message = allItems.length ? 'Try a different viewport or pillar filter.' : number(active.report.summary?.warning_count) ? 'Review the capture warnings before drawing conclusions.' : 'No issues detected by these checks. Manual review is still recommended.';
    } else if (active.status === 'failed') {
      heading = 'The audit could not finish.';
      message = 'Your activity log is preserved. Check the target URL and try again.';
    } else if (active.status !== 'idle') {
      heading = 'Looking at the details…';
      message = 'Findings will appear when all viewport checks finish.';
    }
    empty.append(element('span', '', active.report && !allItems.length ? '✓' : '◎'), element('h3', '', heading), element('p', '', message));
    list.append(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of filtered.slice(0, listLimit)) {
    const button = element('button', `bug-button${selected?.key === item.key ? ' selected' : ''}`);
    button.type = 'button';
    button.dataset.finding = item.key;
    button.setAttribute('aria-pressed', String(selected?.key === item.key));
    const index = element('span', `bug-index ${item.bug.severity === 'high' ? 'high' : 'medium'}`, item.number);
    const content = element('span');
    const name = text(item.bug.message, 500) || titleCase(item.bug.type);
    const issueTitle = element('span', 'bug-title', name);
    issueTitle.title = name;
    const offCapture = item.bug.rect && !inspectors.get(item.viewport).visibleRect(item);
    const subtitle = `${titleCase(item.viewport)} / ${titleCase(item.bug.pillar)} · ${text(item.bug.selector, 300) || 'Page runtime'}${offCapture ? ' · Not shown in capture' : !item.bug.rect ? ' · No visual box' : ''}`;
    content.append(issueTitle, element('span', 'bug-subtitle', subtitle));
    const end = element('span', 'bug-end');
    end.append(element('span', `severity-badge ${item.bug.severity === 'high' ? 'high' : 'medium'}`, item.bug.severity === 'high' ? 'High' : 'Medium'), element('span', '', '↗'));
    button.append(index, content, end);
    button.addEventListener('click', () => selectFinding(item, true));
    fragment.append(button);
  }
  if (filtered.length > listLimit) {
    const more = element('button', 'bug-button', `Show more findings (${filtered.length - listLimit} remaining)`);
    more.type = 'button';
    more.addEventListener('click', () => { listLimit += 200; renderFindings(); });
    fragment.append(more);
  }
  list.append(fragment);
}

function selectFinding(item, focusImage) {
  selected = item;
  if (selectedView !== 'all' && selectedView !== item.viewport) setView(item.viewport);
  for (const button of $$('#bug-list [data-finding]')) {
    const chosen = button.dataset.finding === item.key;
    button.classList.toggle('selected', chosen);
    button.setAttribute('aria-pressed', String(chosen));
  }
  // Preserve the user's zoom; list navigation moves the capture, canvas clicks do not.
  for (const [name, inspector] of inspectors) {
    inspector.select(name === item.viewport ? item : null, focusImage && name === item.viewport);
    $(`[data-card='${name}']`).classList.toggle('active', name === item.viewport);
  }
  renderInspector();
  if (focusImage) {
    const card = $(`[data-card='${item.viewport}']`);
    const grid = $('#canvas-grid');
    if (grid.scrollWidth > grid.clientWidth) {
      const cardBounds = card.getBoundingClientRect();
      const gridBounds = grid.getBoundingClientRect();
      grid.scrollLeft += cardBounds.left - gridBounds.left - 10;
    }
    window.scrollTo({ top: window.scrollY, behavior: 'instant' });
    $('#workspace').scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }
  setText('#canvas-hint', `#${item.number} selected · ${titleCase(item.viewport)}${$('#overlay-toggle').checked ? '' : ' · Raw image, overlays hidden'}`);
}

function renderInspector() {
  if (!selected) return;
  const bug = selected.bug;
  $('#inspector-empty').hidden = true;
  $('#inspector-detail').hidden = false;
  $('#inspector-panel').scrollTop = 0;
  setText('#inspector-severity', bug.severity === 'high' ? 'High priority' : 'Medium priority');
  $('#inspector-severity').className = `severity-badge ${bug.severity === 'high' ? 'high' : 'medium'}`;
  setText('#inspector-viewport', selected.viewport);
  setText('#inspector-number', `#${selected.number}`);
  setText('#inspector-type', titleCase(bug.type || bug.pillar || 'Finding'));
  setText('#inspector-selector', text(bug.selector, 2000) || 'No element selector (page-level issue)');
  setText('#related-selector', bug.related_selector ? `Related: ${text(bug.related_selector, 1000)}` : '');
  $('#related-selector').hidden = !bug.related_selector;
  setText('#inspector-reason', text(bug.message, 3000) || 'No additional reason was provided.');
  setText('#inspector-suggestion', text(bug.suggestion, 5000) || 'No automated suggestion is available. Review this finding in the context of your layout.');
  setText('#inspector-location', inspectors.get(selected.viewport).locationDescription(selected));
  const help = $('#inspector-help');
  help.hidden = true;
  help.removeAttribute('href');
  if (bug.help_url) {
    try {
      const url = new URL(bug.help_url);
      if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) {
        help.href = url.href;
        help.hidden = false;
      }
    } catch { /* Invalid documentation links are not interactive. */ }
  }
}

async function loadHistory() {
  const current = ++historyEpoch;
  historyController?.abort();
  historyController = new AbortController();
  $('#refresh-history').disabled = true;
  try {
    const data = await request('/api/audits', { signal: historyController.signal });
    if (current !== historyEpoch) return;
    if (!Array.isArray(data)) throw new Error('Invalid history response.');
    const container = $('#audit-history');
    container.replaceChildren();
    for (const entry of data.slice(0, 20)) {
      if (!entry || typeof entry.id !== 'string') continue;
      const button = element('button', 'history-button');
      button.type = 'button';
      button.dataset.audit = text(entry.id, 200);
      const status = ['queued', 'running', 'completed', 'failed'].includes(entry.status) ? entry.status : 'unknown';
      const date = new Date(entry.created_at);
      const dateText = Number.isNaN(date.getTime()) ? 'Date unavailable' : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      const url = element('span', 'history-url', text(entry.url, 1800));
      url.title = text(entry.url, 1800);
      button.append(element('span', `status-dot ${status === 'failed' ? 'failed' : terminal(status) ? '' : 'muted'}`), url, element('span', 'history-date', dateText), element('span', `history-status ${status}`, status === 'completed' ? 'Complete' : status), element('span', '', '↗'));
      button.addEventListener('click', () => selectAudit(entry.id));
      container.append(button);
    }
    if (!container.children.length) container.append(element('p', 'muted-text', 'No previous audits. Your completed and in-progress runs will appear here.'));
    markHistory();
  } catch (error) {
    if (current !== historyEpoch || error.name === 'AbortError') return;
    $('#audit-history').replaceChildren(element('p', 'muted-text', `History unavailable. ${text(error.message, 300)} Use Refresh history to retry.`));
  } finally {
    if (current === historyEpoch) $('#refresh-history').disabled = false;
  }
}

function markHistory() {
  for (const button of $$('#audit-history [data-audit]')) {
    const chosen = button.dataset.audit === active.id;
    button.classList.toggle('active', chosen);
    button.setAttribute('aria-pressed', String(chosen));
  }
}

async function downloadReport(format) {
  if (!['json', 'html', 'pdf'].includes(format) || active.status !== 'completed' || !active.report || exporting) return;
  const current = epoch;
  const id = active.id;
  exporting = true;
  updateExports();
  setText('#export-status', `Preparing ${format.toUpperCase()} download${format === 'pdf' ? ' — PDF generation may take a few seconds' : ''}…`);
  try {
    const blob = await request(`/api/audit/${encodeURIComponent(id)}/export/${format}`, { asBlob: true, signal: controller.signal }, 120000);
    if (current !== epoch) return;
    if (!blob.size) throw new Error('The server returned an empty export.');
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = `visual-audit-${id.replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 80)}.${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(href), 60000);
    setText('#export-status', `${format.toUpperCase()} download started.`);
    addLog(`${format.toUpperCase()} report download prepared.`);
  } catch (error) {
    if (current !== epoch || error.name === 'AbortError') return;
    setText('#export-status', `${format.toUpperCase()} export unavailable. ${text(error.message, 500)} Please try again.`);
    addLog(`${format.toUpperCase()} export failed: ${text(error.message, 500)}`, 'error');
  } finally {
    if (current === epoch) { exporting = false; updateExports(); }
  }
}

$('#audit-form').addEventListener('submit', event => { event.preventDefault(); startAudit(); });
$('#try-demo').addEventListener('click', () => { $('#audit-url').value = `${location.origin}/demo`; startAudit(); });
$('#refresh-history').addEventListener('click', loadHistory);
$('#overlay-toggle').addEventListener('change', event => {
  for (const inspector of inspectors.values()) inspector.setOverlay(event.target.checked);
  setText('#canvas-hint', event.target.checked ? 'Heatmap on · select a region or finding to inspect.' : 'Raw screenshots · all overlays hidden.');
});
for (const button of $$('button[data-view]')) button.addEventListener('click', () => setView(button.dataset.view));
for (const button of $$('[data-pillar]')) button.addEventListener('click', () => {
  selectedPillar = pillars.includes(button.dataset.pillar) ? button.dataset.pillar : 'all';
  listLimit = 200;
  renderFindings();
});
for (const button of $$('[data-export]')) button.addEventListener('click', () => downloadReport(button.dataset.export));
$('#zoom-out').addEventListener('click', () => changeZoom(zoom - .25));
$('#zoom-in').addEventListener('click', () => changeZoom(zoom + .25));
$('#zoom-reset').addEventListener('click', () => changeZoom(1));
$('#zoom-fit').addEventListener('click', () => {
  changeZoom(1);
  for (const inspector of inspectors.values()) inspector.scroller.scrollTo({ top: 0, left: 0 });
});
window.addEventListener('pagehide', () => {
  stopSocket();
  clearTimeout(pollTimer);
  clearInterval(elapsedTimer);
  controller.abort();
  historyController?.abort();
});
window.addEventListener('pageshow', event => {
  if (event.persisted && active.id) selectAudit(active.id);
});

loadHistory();
const restored = storage('read');
if (restored && restored.length <= 200) selectAudit(restored);
