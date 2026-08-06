const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const refreshSource = html.match(/function refreshPane\(\{periodic = false\} = \{\}\) \{[\s\S]*?\n\}/)?.[0];
const jumpSource = html.match(/function jumpToLive\(\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(refreshSource, 'refreshPane should take a periodic flag');
assert.ok(jumpSource, 'jumpToLive function should be present');

{
  // Re-reading the whole scrollback on every tick hung Herdr and got the CLI
  // killed mid-snapshot; periodic polling must stand down while reading history.
  const sent = [];
  const globals = {
    ws: {readyState: 1, send: (raw) => sent.push(JSON.parse(raw))},
    WebSocket: {OPEN: 1},
    activePane: 'pane-1',
    paneReadInFlight: null,
    paneHistoryLoaded: true,
    userScrolledUp: false,
    paneLines: 5000,
    DEFAULT_PANE_LINES: 200,
    PANE_READ_TIMEOUT_MS: 20000,
    paneRefreshQueued: false,
    paneHistoryLoadPending: false,
    paneUnreadUpdates: 3,
    document: {getElementById: () => ({scrollTop: 0, scrollHeight: 100, textContent: '', classList: {add(){}, remove(){}, toggle(){}, contains: () => false}})},
    setTimeout: () => 1,
    clearTimeout: () => {},
    showPaneLoadError: () => {},
    updateLivePill: () => {},
  };
  const run = new Function(...Object.keys(globals), [
    refreshSource,
    jumpSource,
    'refreshPane({periodic: true});',            // parked in history: must not read
    'jumpToLive();',
    'refreshPane({periodic: true});',            // back at live: must read cheaply
    'return {historyLoaded: paneHistoryLoaded, lines: paneLines, scrolledUp: userScrolledUp};',
  ].join('\n'));
  const after = run(...Object.values(globals));
  assert.equal(sent.length, 1, 'the timer never requests history, even sitting at the live edge');
  assert.equal(sent[0].source, 'visible', 'the resumed poll uses the cheap viewport read');
  assert.equal(sent[0].lines, 200, 'and drops back to the default line count');
  assert.equal(after.historyLoaded, false, 'jumping to live clears the history mode');
  assert.equal(after.lines, 200);
}

const source = html.match(/function finishPaneRead\(requestId\) \{[\s\S]*?\n\}/)?.[0];
const resolverSource = html.match(/function resolvePaneResponseRequestId\(message\) \{[\s\S]*?\n\}/)?.[0];
const sanitizerSource = html.match(/function sanitizePaneContent\(content\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(source, 'finishPaneRead function should be present');
assert.ok(resolverSource, 'resolvePaneResponseRequestId function should be present');
assert.ok(sanitizerSource, 'sanitizePaneContent function should be present');

let paneReadInFlight = {requestId: 'slow_read', paneId: 'pane-1', timeout: 42};
let paneRefreshQueued = true;
let activePane = 'pane-1';
let queuedRefresh = null;
let clearedTimeout = null;
let refreshCount = 0;
const originalClearTimeout = global.clearTimeout;
const originalSetTimeout = global.setTimeout;

global.clearTimeout = (timeout) => { clearedTimeout = timeout; };
global.setTimeout = (callback) => { queuedRefresh = callback; return 1; };
function refreshPane() { refreshCount += 1; }

try {
  eval(sanitizerSource);
  const upperBlockRow = ` ${'\u2580'.repeat(80)}`;
  const lowerBlockRow = ` ${'\u2584'.repeat(80)}`;
  const claudePromptBorder = '\u2500'.repeat(120);
  assert.equal(
    sanitizePaneContent(`before\n${lowerBlockRow}\n  \u2192 Add a follow-up\n${upperBlockRow}\n${claudePromptBorder}\n\u276f\n${claudePromptBorder}\nafter`),
    'before\n  \u2192 Add a follow-up\n\u276f\nafter',
    'Cursor and Claude prompt borders should not wrap into tall bands on mobile',
  );
  assert.equal(
    sanitizePaneContent('progress \u2584\u2584\u2584\n\u2500\u2500\u2500\n\u251c\u2500\u2500 table border \u2500\u2500\u2524\n--------------------\nnormal output'),
    'progress \u2584\u2584\u2584\n\u2500\u2500\u2500\n\u251c\u2500\u2500 table border \u2500\u2500\u2524\n--------------------\nnormal output',
    'short block characters, table borders, and ordinary separators should remain visible',
  );

  eval(resolverSource);
  assert.equal(resolvePaneResponseRequestId({pane_id: 'pane-1', request_id: 'current'}), 'current');
  assert.equal(resolvePaneResponseRequestId({pane_id: 'pane-1'}), 'slow_read', 'a matching legacy response should use the one in-flight request');
  assert.equal(resolvePaneResponseRequestId({pane_id: 'another-pane'}), null, 'an uncorrelated response for another pane must be ignored');

  eval(source);
  assert.equal(finishPaneRead('slow_read'), true, 'a valid slow response must still render');
  assert.equal(clearedTimeout, 42);
  assert.equal(paneReadInFlight, null);
  assert.equal(paneRefreshQueued, false);
  assert.equal(typeof queuedRefresh, 'function', 'the queued refresh should run after rendering');
  queuedRefresh();
  assert.equal(refreshCount, 1);
} finally {
  global.clearTimeout = originalClearTimeout;
  global.setTimeout = originalSetTimeout;
}
