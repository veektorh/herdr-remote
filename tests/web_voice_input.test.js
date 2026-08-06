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
  'speechRecognizerClass\\(\\)',
  'setupDictation\\(\\)',
  'setDictationButton\\(listening\\)',
  'toggleDictation\\(\\)',
  'startDictation\\(\\)',
  'handleDictationResult\\(event\\)',
  'dictationTranscript\\(\\)',
  'mergeSpeech\\(text, piece\\)',
  'tidySpeech\\(text\\)',
  'applyDictationTranscript\\(spoken\\)',
  'stopDictation\\(\\)',
  'failDictation\\(message\\)',
  'endDictation\\(\\)',
  'abortDictation\\(\\)',
  'microphoneIsPermittedHere\\(\\)',
  'dictationErrorMessage\\(code\\)',
].map(extract);

function makeElement(id) {
  const classes = new Set();
  const attributes = {};
  return {
    id, textContent: '', value: '', hidden: true, focusCount: 0,
    classList: {
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    },
    setAttribute: (name, value) => { attributes[name] = value; },
    getAttribute: (name) => attributes[name] ?? null,
    focus() { this.focusCount += 1; },
  };
}

const elements = new Map();
const el = (id) => {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
};

class FakeRecognition {
  constructor() {
    FakeRecognition.instances.push(this);
    this.started = 0;
    this.stopped = 0;
    this.aborted = 0;
    this.startThrows = false;
  }

  start() {
    if (this.startThrows) throw new Error('already started');
    this.started += 1;
  }

  stop() { this.stopped += 1; }

  abort() { this.aborted += 1; }

  // Test helpers that mimic the browser firing events. `chunks` is the whole
  // cumulative result list, as the real API delivers it.
  speak(chunks, resultIndex = 0) {
    this.onresult({resultIndex, results: chunks.map(([transcript, isFinal]) => ({
      0: {transcript}, isFinal,
    }))});
  }

  fail(code) { this.onerror({error: code}); }

  finish() { this.onend(); }
}
FakeRecognition.instances = [];

const originalDocument = global.document;
const originalWindow = global.window;
const originalNavigator = global.navigator;

let micPermittedByPolicy = true;
global.document = {
  getElementById: el,
  featurePolicy: {allowsFeature: (name) => (name === 'microphone' ? micPermittedByPolicy : false)},
};
global.window = {SpeechRecognition: FakeRecognition, isSecureContext: true};
global.navigator = {language: 'en-GB'};

let dictation = null;
let activePane = 'pane-1';
let statuses = [];

function setCommandStatus(message, isError = false) { statuses.push({message, isError}); }
const lastStatus = () => statuses.at(-1);

function reset(inputValue = '') {
  dictation = null;
  micPermittedByPolicy = true;
  statuses = [];
  FakeRecognition.instances = [];
  activePane = 'pane-1';
  el('termInput').value = inputValue;
  el('termInput').focusCount = 0;
  el('micButton').hidden = true;
}

try {
  for (const source of sources) eval(source);

  // --- The mic only appears where the API can actually work ---
  reset();
  setupDictation();
  assert.equal(el('micButton').hidden, false, 'a secure context with the API shows the mic');

  reset();
  global.window = {SpeechRecognition: FakeRecognition, isSecureContext: false};
  setupDictation();
  assert.equal(el('micButton').hidden, true, 'an insecure context must hide the mic');

  reset();
  global.window = {isSecureContext: true};
  setupDictation();
  assert.equal(el('micButton').hidden, true, 'a browser without the API must hide the mic');
  toggleDictation();
  assert.equal(dictation, null, 'dictation cannot start without the API');
  global.window = {SpeechRecognition: FakeRecognition, isSecureContext: true};

  // --- Speaking fills the message box and never sends ---
  reset();
  toggleDictation();
  const recognition = FakeRecognition.instances[0];
  assert.equal(recognition.started, 1);
  assert.equal(recognition.lang, 'en-GB', 'dictation follows the device language');
  assert.equal(recognition.continuous, true);
  assert.equal(recognition.interimResults, true);
  assert.equal(el('micButton').getAttribute('aria-pressed'), 'true');
  assert.equal(el('micButton').getAttribute('aria-label'), 'Stop dictating');
  assert.equal(el('termInput').classList.contains('dictating'), true);
  assert.match(lastStatus().message, /Listening/);

  recognition.speak([['run the tests', false]]);
  assert.equal(el('termInput').value, 'run the tests', 'interim speech previews live');
  recognition.speak([['run the tests again', true]]);
  assert.equal(el('termInput').value, 'run the tests again');

  // --- Re-delivered results must not duplicate words ---
  // The engine revises and re-sends results; the same phrase arriving twice
  // must overwrite its slot rather than append.
  recognition.speak([['run the tests again', true]]);
  assert.equal(el('termInput').value, 'run the tests again', 'a re-sent final must not repeat');
  recognition.speak([['run the tests again', true]], 0);
  recognition.speak([['run the tests again', true]], 0);
  assert.equal(el('termInput').value, 'run the tests again', 'repeated re-delivery stays stable');

  // A second phrase extends the first instead of replacing or doubling it.
  recognition.speak([['run the tests again', true], ['and show me the output', false]], 1);
  assert.equal(el('termInput').value, 'run the tests again and show me the output');
  recognition.speak([['run the tests again', true], ['and show me the output', true]], 1);
  assert.equal(el('termInput').value, 'run the tests again and show me the output');
  recognition.speak([['run the tests again', true], ['and show me the output', true]], 0);
  assert.equal(
    el('termInput').value, 'run the tests again and show me the output',
    'a full re-delivery of every final must not double the sentence',
  );

  // Chrome pads results with leading spaces; the box must not collect runs.
  recognition.speak([[' run the tests again ', true], ['  and show me the output', true]], 0);
  assert.equal(el('termInput').value, 'run the tests again and show me the output');

  toggleDictation();
  assert.equal(recognition.stopped, 1, 'tapping again stops listening');
  assert.match(lastStatus().message, /Finishing/);
  toggleDictation();
  assert.equal(recognition.stopped, 1, 'a second tap while stopping is ignored');

  recognition.finish();
  assert.equal(dictation, null);
  assert.equal(el('micButton').getAttribute('aria-pressed'), 'false');
  assert.equal(el('micButton').getAttribute('aria-label'), 'Dictate message');
  assert.equal(el('termInput').classList.contains('dictating'), false);
  assert.equal(
    el('termInput').value, 'run the tests again and show me the output',
    'the transcript stays for review',
  );
  assert.equal(el('termInput').focusCount, 1, 'focus returns for editing');
  assert.match(lastStatus().message, /Review, then send/);
  assert.equal(lastStatus().isError, false);

  // --- Engines that restate the whole utterance in every result ---
  // Saying "hello my name is Victor" once produced growing prefixes, each in its
  // own result slot; joining them repeated the sentence on every word.
  reset();
  toggleDictation();
  const restating = FakeRecognition.instances[0];
  const utterance = [
    'hello', 'hello my', 'hello my name', 'hello my name is',
    'hello my name is', 'hello my name is Victor', 'hello my name is Victor',
  ];
  for (let spoken = 1; spoken <= utterance.length; spoken += 1) {
    restating.speak(utterance.slice(0, spoken).map((text) => [text, true]), spoken - 1);
  }
  assert.equal(
    el('termInput').value, 'hello my name is Victor',
    'a restating engine must not stack growing prefixes',
  );
  restating.finish();
  assert.equal(el('termInput').value, 'hello my name is Victor');

  // The same restatement arriving as interim text must fold too.
  reset();
  toggleDictation();
  const restatingInterim = FakeRecognition.instances[0];
  for (let spoken = 1; spoken <= 4; spoken += 1) {
    restatingInterim.speak(utterance.slice(0, spoken).map((text) => [text, false]), 0);
  }
  assert.equal(el('termInput').value, 'hello my name is');

  // --- Segmented engines must still concatenate ---
  reset();
  toggleDictation();
  const segmenting = FakeRecognition.instances[0];
  segmenting.speak([['open the file', true]], 0);
  segmenting.speak([['open the file', true], ['and run it', true]], 1);
  assert.equal(
    el('termInput').value, 'open the file and run it',
    'distinct phrases must still join',
  );

  // Overlapping segments merge on the shared words rather than repeating them.
  reset();
  toggleDictation();
  const overlapping = FakeRecognition.instances[0];
  overlapping.speak([['deploy the service', true]], 0);
  overlapping.speak([['deploy the service', true], ['the service to staging', true]], 1);
  assert.equal(el('termInput').value, 'deploy the service to staging');

  // Folding is case-insensitive but keeps the engine's own capitalisation.
  assert.equal(mergeSpeech('hello my name is', 'Hello my name is Victor'), 'Hello my name is Victor');
  assert.equal(mergeSpeech('', 'first words'), 'first words');
  assert.equal(mergeSpeech('already said', ''), 'already said');
  assert.equal(mergeSpeech('one two', 'three four'), 'one two three four');

  // --- Dictation appends to text already typed ---
  reset('git status');
  toggleDictation();
  FakeRecognition.instances[0].speak([['and push', true]]);
  assert.equal(el('termInput').value, 'git status and push', 'a separator is added once');
  reset('git status ');
  toggleDictation();
  FakeRecognition.instances[0].speak([[' and push', true]]);
  assert.equal(el('termInput').value, 'git status and push', 'existing spacing is not doubled');

  // --- Silence is reported rather than sending an empty message ---
  reset();
  toggleDictation();
  FakeRecognition.instances[0].finish();
  assert.equal(el('termInput').value, '');
  assert.deepEqual(
    {message: lastStatus().message, isError: lastStatus().isError},
    {message: 'No speech detected.', isError: true},
  );

  // --- Errors surface with actionable text ---
  const expected = {
    'not-allowed': /Allow it for this site/,
    'service-not-allowed': /refused speech recognition/,
    'no-speech': /No speech detected/,
    'audio-capture': /No microphone was found/,
    network: /needs a network connection/,
    'made-up-code': /stopped unexpectedly/,
  };
  for (const [code, pattern] of Object.entries(expected)) {
    reset();
    toggleDictation();
    const failing = FakeRecognition.instances[0];
    failing.fail(code);
    assert.equal(dictation, null, `${code} must end the session`);
    assert.equal(failing.aborted, 1, `${code} must release the microphone`);
    assert.match(lastStatus().message, pattern);
    assert.equal(lastStatus().isError, true);
    assert.equal(el('micButton').getAttribute('aria-pressed'), 'false');
    failing.finish(); // A late onend must not double-report.
    assert.match(lastStatus().message, pattern);
  }

  // --- A Permissions-Policy block is named, not blamed on the user ---
  reset();
  micPermittedByPolicy = false;
  toggleDictation();
  assert.equal(dictation, null, 'a blocked page must not open a session');
  assert.equal(FakeRecognition.instances.length, 0, 'the mic is never touched when policy forbids it');
  assert.match(lastStatus().message, /not allowed to use the microphone/);
  assert.match(lastStatus().message, /Update the relay/, 'the fix points at the server, not settings');
  assert.equal(lastStatus().isError, true);
  assert.doesNotMatch(lastStatus().message, /browser settings/, 'settings advice cannot fix a policy block');
  // The same distinction applies if the engine reports the denial instead.
  assert.match(dictationErrorMessage('not-allowed'), /not allowed to use the microphone/);
  micPermittedByPolicy = true;
  assert.match(dictationErrorMessage('not-allowed'), /Allow it for this site/);

  // A user-initiated abort is not an error.
  reset();
  toggleDictation();
  FakeRecognition.instances[0].speak([['hello', true]]);
  FakeRecognition.instances[0].fail('aborted');
  assert.equal(lastStatus().isError, false);
  assert.match(lastStatus().message, /Review, then send/);

  // --- A failure to start is reported and leaves no stuck state ---
  reset();
  const blocked = new FakeRecognition();
  blocked.startThrows = true;
  FakeRecognition.instances = [];
  global.window.SpeechRecognition = function () { return blocked; };
  global.window.SpeechRecognition.prototype = FakeRecognition.prototype;
  toggleDictation();
  assert.equal(dictation, null);
  assert.match(lastStatus().message, /Could not start dictation/);
  assert.equal(lastStatus().isError, true);
  assert.equal(el('micButton').getAttribute('aria-pressed'), 'false');
  global.window.SpeechRecognition = FakeRecognition;

  // --- Sending or leaving discards an in-flight transcript ---
  reset();
  toggleDictation();
  const inFlight = FakeRecognition.instances[0];
  inFlight.speak([['partial', false]]);
  abortDictation();
  assert.equal(dictation, null);
  assert.equal(inFlight.aborted, 1, 'the microphone is released');
  assert.equal(el('micButton').getAttribute('aria-pressed'), 'false');
  el('termInput').value = '';
  inFlight.speak([['late result', true]]);
  assert.equal(el('termInput').value, '', 'a late result cannot refill a cleared box');
  inFlight.finish();
  assert.equal(dictation, null);

  // --- Dictation is scoped to an open terminal ---
  reset();
  activePane = null;
  toggleDictation();
  assert.equal(dictation, null, 'dictation needs an open pane');
  assert.equal(FakeRecognition.instances.length, 0);
} finally {
  global.document = originalDocument;
  global.window = originalWindow;
  global.navigator = originalNavigator;
}

console.log('web voice input tests passed');
