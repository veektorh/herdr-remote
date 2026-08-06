const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

function extract(signature) {
  const pattern = new RegExp(`function ${signature} \\{[\\s\\S]*?\\n\\}`);
  const source = html.match(pattern)?.[0];
  assert.ok(source, `${signature} should be present in web/index.html`);
  return source;
}

const sources = [
  'h\\(value\\)',
  'agentLabelFor\\(agentKind\\)',
  'projectBusy\\(\\)',
  'agentStartCard\\(project, agent, startNew\\)',
  'renderActiveProject\\(project\\)',
  'activateProjectFromPhone\\(projectId, agentKind, startNew=false\\)',
  'requestProjectClose\\(projectId\\)',
  'setProjectCloseStatus\\(message, isError=false\\)',
  'setProjectCloseButtons\\(busy\\)',
  'cancelProjectClose\\(\\)',
  'confirmProjectClose\\(\\)',
  'finishProjectClose\\(message\\)',
].map(extract);

function makeElement(id) {
  const classes = new Set();
  return {
    id, textContent: '', innerHTML: '', disabled: false, value: '', focusCount: 0,
    style: {},
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    },
    focus() { this.focusCount += 1; },
  };
}

const elements = new Map();
const el = (id) => {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
};

const originalDocument = global.document;
const originalWebSocket = global.WebSocket;
const originalSetTimeout = global.setTimeout;
const originalClearTimeout = global.clearTimeout;

global.document = {getElementById: el};
global.WebSocket = {OPEN: 1};

let timers = [];
global.setTimeout = (callback) => { timers.push(callback); return timers.length; };
global.clearTimeout = (id) => { cleared.push(id); };

// State mirrored from the web app.
let projects = [];
let projectAgentChoices = [{id: 'claude', label: 'Claude'}, {id: 'codex', label: 'Codex'}];
let projectActivationPending = null, projectClosePending = null, projectCloseConfirmId = null;
let selectedProjectId = null;
let activeWorkspace = null, activeTab = null;
let sent = [], cleared = [], statuses = [], launcherClosed = 0, rendered = 0, catalogRefreshes = 0;
let ws = {readyState: 1, send: (raw) => sent.push(JSON.parse(raw))};

function setProjectLauncherStatus(message, isError = false) { statuses.push({message, isError}); }
function renderProjectLauncher() { rendered += 1; }
function closeProjectLauncher() { launcherClosed += 1; }
function requestProjectCatalog() { catalogRefreshes += 1; }
function render() {}

function resetState() {
  projects = [{
    id: 'alpha', name: 'Alpha', description: 'Alpha workspace', group: 'Personal',
    tabs: ['terminal'], active: true, workspace_id: 'w-existing',
    has_agent: true, agent: 'claude',
  }];
  projectActivationPending = null;
  projectClosePending = null;
  projectCloseConfirmId = null;
  activeWorkspace = null;
  activeTab = null;
  sent = []; cleared = []; statuses = []; timers = [];
  launcherClosed = 0; rendered = 0; catalogRefreshes = 0;
  ws = {readyState: 1, send: (raw) => sent.push(JSON.parse(raw))};
  el('projectCloseConfirm').classList.remove('active');
  el('projectCloseSubmit').disabled = false;
}

try {
  for (const source of sources) eval(source);

  // --- Active project offers resume, start-another, and close ---
  resetState();
  const markup = renderActiveProject(projects[0]);
  assert.match(markup, /Current agent/, 'the running agent should have its own section');
  assert.match(markup, /Resume ›/, 'the current agent must be resumable');
  assert.match(markup, /project-current-pill">Current/, 'the current agent must be visibly marked');
  assert.match(markup, /Start another agent/, 'another installed agent can be started');
  assert.match(markup, /aria-label="Start Codex in Alpha"/, 'other agents are offered by label');
  assert.match(markup, /class="project-card danger-card"/, 'close must be visually distinct');
  assert.match(markup, /aria-haspopup="dialog" aria-controls="projectCloseConfirm"/, 'close opens a dialog');
  assert.match(markup, /Existing agents keep running\./, 'starting another agent is described as additive');
  assert.doesNotMatch(markup, /Replace|Kill|Stop the current/, 'the current agent is never replaced');

  // A project without a running agent still offers a start and a close.
  projects[0].has_agent = false;
  projects[0].agent = '';
  const idleMarkup = renderActiveProject(projects[0]);
  assert.match(idleMarkup, /No agent is running in this workspace yet\./);
  assert.match(idleMarkup, /Start an agent</);
  assert.match(idleMarkup, /danger-card/);

  // --- Starting another agent is non-destructive and guarded ---
  resetState();
  activateProjectFromPhone('alpha', 'codex', true);
  assert.equal(sent.length, 1);
  assert.deepEqual(
    {type: sent[0].type, project_id: sent[0].project_id, agent: sent[0].agent, start_new: sent[0].start_new},
    {type: 'activate_project', project_id: 'alpha', agent: 'codex', start_new: true},
  );
  assert.ok(!('cwd' in sent[0]) && !('workspace_id' in sent[0]), 'no cwd or workspace may be sent');
  assert.equal(projectActivationPending.startNew, true);
  activateProjectFromPhone('alpha', 'claude', true);
  assert.equal(sent.length, 1, 'a second submission must be blocked while startup is pending');
  requestProjectClose('alpha');
  assert.equal(el('projectCloseConfirm').classList.contains('active'), false, 'closing is blocked while busy');

  // The pending card reports loading and the timeout reports a clear failure.
  const pendingMarkup = renderActiveProject(projects[0]);
  assert.match(pendingMarkup, /Starting and waiting for Herdr…/);
  assert.match(pendingMarkup, /Starting…/, 'the pending agent card reports progress');
  assert.match(pendingMarkup, /danger-card" onclick="requestProjectClose[^>]*disabled/, 'close is disabled while a start is pending');
  timers[0]();
  assert.equal(projectActivationPending, null);
  assert.match(statuses.at(-1).message, /timed out/);
  assert.equal(statuses.at(-1).isError, true);

  // --- Closing requires explicit confirmation ---
  resetState();
  requestProjectClose('alpha');
  assert.equal(sent.length, 0, 'opening the dialog must not close anything');
  assert.equal(el('projectCloseConfirm').classList.contains('active'), true);
  assert.match(el('projectCloseBody').textContent, /Every pane and running process in the Alpha Herdr workspace will be terminated/);
  assert.match(el('projectCloseBody').textContent, /cannot be undone/);
  assert.equal(el('projectCloseSubmit').focusCount, 1, 'the dialog should take focus');

  cancelProjectClose();
  assert.equal(el('projectCloseConfirm').classList.contains('active'), false);
  assert.equal(sent.length, 0, 'cancelling must never close the workspace');

  // --- Confirming sends only the project id ---
  requestProjectClose('alpha');
  confirmProjectClose();
  assert.equal(sent.length, 1);
  assert.deepEqual(Object.keys(sent[0]).sort(), ['project_id', 'request_id', 'type']);
  assert.equal(sent[0].type, 'close_project');
  assert.equal(sent[0].project_id, 'alpha');
  assert.equal(el('projectCloseSubmit').disabled, true, 'duplicate confirmations must be blocked');
  confirmProjectClose();
  assert.equal(sent.length, 1);

  // A response for another request is ignored.
  finishProjectClose({type: 'project_closed', ok: true, request_id: 'stale', closed: true});
  assert.ok(projectClosePending, 'an uncorrelated response must not resolve the request');

  // --- Success clears local state and refreshes the lists ---
  activeWorkspace = 'w-existing';
  finishProjectClose({
    type: 'project_closed', ok: true, project_id: 'alpha',
    request_id: sent[0].request_id, workspace_id: 'w-existing',
    closed: true, already_closed: false,
  });
  assert.equal(projectClosePending, null);
  assert.equal(projects[0].active, false);
  assert.equal(projects[0].has_agent, false);
  assert.equal(projects[0].workspace_id, '');
  assert.equal(activeWorkspace, null, 'the closed workspace must not stay selected');
  assert.equal(el('projectCloseStatus').textContent, 'Project closed.');
  timers.at(-1)();
  assert.equal(launcherClosed, 1, 'the launcher closes on success');
  assert.equal(catalogRefreshes, 1, 'the project list is refreshed');

  // --- An already-closed workspace is handled gracefully ---
  resetState();
  requestProjectClose('alpha');
  confirmProjectClose();
  finishProjectClose({
    type: 'project_closed', ok: true, project_id: 'alpha',
    request_id: sent[0].request_id, workspace_id: '', closed: false, already_closed: true,
  });
  assert.equal(el('projectCloseStatus').textContent, 'This project was already closed.');
  assert.equal(el('projectCloseStatus').classList.contains('error'), false);
  assert.equal(projects[0].active, false);

  // --- Failures and timeouts surface without clearing the project ---
  resetState();
  requestProjectClose('alpha');
  confirmProjectClose();
  finishProjectClose({
    type: 'project_closed', ok: false, project_id: 'alpha',
    request_id: sent[0].request_id, error: 'Unknown project',
  });
  assert.equal(el('projectCloseStatus').textContent, 'Unknown project');
  assert.equal(el('projectCloseStatus').classList.contains('error'), true);
  assert.equal(el('projectCloseSubmit').disabled, false, 'the dialog stays usable after a failure');
  assert.equal(projects[0].active, true, 'a rejected close must not mark the project closed');

  resetState();
  requestProjectClose('alpha');
  confirmProjectClose();
  const closeTimeout = timers.at(-1);
  closeTimeout();
  assert.equal(projectClosePending, null);
  assert.match(el('projectCloseStatus').textContent, /timed out/);
  assert.equal(el('projectCloseStatus').classList.contains('error'), true);
  assert.equal(projects[0].active, true);

  // --- A disconnected relay reports rather than silently dropping the close ---
  resetState();
  ws = {readyState: 3, send: () => assert.fail('a closed socket must not be used')};
  requestProjectClose('alpha');
  confirmProjectClose();
  assert.equal(sent.length, 0);
  assert.match(el('projectCloseStatus').textContent, /Connect to the relay/);
  assert.equal(el('projectCloseStatus').classList.contains('error'), true);
} finally {
  global.document = originalDocument;
  global.WebSocket = originalWebSocket;
  global.setTimeout = originalSetTimeout;
  global.clearTimeout = originalClearTimeout;
}

console.log('web project lifecycle tests passed');
