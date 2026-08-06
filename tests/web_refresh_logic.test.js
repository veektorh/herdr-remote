const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const source = html.match(/function finishPaneRead\(requestId\) \{[\s\S]*?\n\}/)?.[0];
const resolverSource = html.match(/function resolvePaneResponseRequestId\(message\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(source, 'finishPaneRead function should be present');
assert.ok(resolverSource, 'resolvePaneResponseRequestId function should be present');

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
