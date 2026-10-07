'use strict';

// Exercise actual shipped selection/error functions with delayed replies. No
// network requests, game tasks, OAS services or simulator operations occur here.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
class Node {
  constructor(tag = 'div') { this.tag = tag; this.hidden = false; this.open = false; this.disabled = false; this.textContent = ''; this.children = []; this.dataset = {}; this.listeners = {}; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'src') delete this.src; }
  close() { this.open = false; }
  showModal() { this.open = true; }
}
const nodes = new Map();
const $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
const state = { authenticated: true, account: '01-测试', accountEpoch: 1, bootEpoch: 1, errorEpoch: 0, taskEpoch: 0,
  errors: [], errorDetail: null, snapshots: new Map(), reconnectAttempt: 0 };
const requests = [], toasts = [];
const context = {
  state, $, window: { location: { href: 'https://panel.example.com:4443/', origin: 'https://panel.example.com:4443' } },
  URL, Intl, encoder: encodeURIComponent, taskLabel: (name) => name === 'Chess' ? '百鬼棋局' : name,
  el: (tag, className, text) => { const node = new Node(tag); node.className = className; node.textContent = text || ''; return node; },
  empty: (node, title, text) => { node.replaceChildren(); node.textContent = `${title}: ${text}`; },
  api: (url) => new Promise((resolve, reject) => requests.push({ url, resolve, reject })),
  toast: (...args) => toasts.push(args), mayDiscard: async () => true,
  storeSnapshot: (name, snapshot) => state.snapshots.set(name, snapshot),
};
for (const name of ['stopSnapshotRefresh', 'disconnectSocket', 'resetStatistics', 'renderLogs', 'renderAccounts', 'renderSelectedAccount',
  'renderCategories', 'renderTasks', 'showWorkTab', 'showMobile', 'updateSaveBar', 'updateConnection', 'connectEvents', 'syncSnapshotVisibility']) context[name] = () => {};
vm.createContext(context);
function functions(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end);
  assert(first >= 0 && last > first);
  vm.runInContext(source.slice(first, last), context);
}
functions('  function resetErrors(', '  function openPassword(');
functions('  async function selectAccount(', '  function connectEvents(');
const flush = () => new Promise(setImmediate);
const record = (id, account) => ({ id, config_name: account, task: 'Chess', created_at: '2026-10-06T04:00:00Z' });
function nextRequest(part) { const item = requests.find((request) => !request.used && request.url.includes(part)); assert(item, `Missing request ${part}`); item.used = true; return item; }
async function switchTo(account) {
  const pending = context.selectAccount(account); await flush();
  nextRequest('/snapshot').resolve({ connected: true, state: 0 });
  await pending;
}
async function verify() {
  const oldList = context.loadErrors();
  const first = nextRequest('/api/errors?');
  assert.equal(new URL(first.url, context.window.location.href).searchParams.get('config'), '01-测试');
  state.errors = [record('old', '01-测试')]; state.errorDetail = { log: 'old account log' };
  $('error-log').textContent = 'old account log'; $('error-images').append(new Node('img'));
  $('image-dialog').open = true; $('image-preview').src = 'old account image';
  await switchTo('02');
  assert.equal(state.errors.length, 0, 'Switching must immediately clear the previous list');
  assert.equal(state.errorDetail, null); assert.equal($('error-log').textContent, '');
  assert.equal($('error-images').children.length, 0); assert.equal($('image-dialog').open, false);
  assert.equal($('image-preview').src, undefined, 'A previous full-screen screenshot must be cleared');
  const second = nextRequest('/api/errors?');
  assert.equal(new URL(second.url, context.window.location.href).searchParams.get('config'), '02');
  second.resolve({ records: [record('new', '02'), record('foreign', '01-测试')], scope: '全部配置' }); await flush();
  first.resolve({ records: [record('old', '01-测试')] }); await oldList;
  assert.equal(state.errors.length, 1); assert.equal(state.errors[0].id, 'new');
  assert.equal($('errors-scope').textContent, '02', 'The toolbar must name the selected account');

  const oldDetail = context.openError('new'); const detailRequest = nextRequest('/api/errors/new?');
  await switchTo('01-测试');
  nextRequest('/api/errors?').resolve({ records: [] }); await flush();
  detailRequest.resolve({ ...record('new', '02'), log: 'foreign late log', images: [] }); await oldDetail;
  assert.equal(state.errorDetail, null); assert.equal($('error-log').textContent, '');
  assert.equal($('error-detail').hidden, true, 'A late previous-account detail must not reopen its panel');

  const selectedDetail = context.openError('one'); const selectedRequest = nextRequest('/api/errors/one?');
  assert.equal(new URL(selectedRequest.url, context.window.location.href).searchParams.get('config'), '01-测试');
  selectedRequest.resolve({ ...record('one', '01-测试'), log: 'selected log', images: [{ name: 'screen.png', url: '/api/errors/one/images/screen.png' }] }); await selectedDetail;
  const wrap = $('error-images').children[0], image = wrap.children[0].children[0];
  assert.equal(new URL(image.src).searchParams.get('config'), '01-测试', 'Image display/download must carry the exact account');
  assert.equal(wrap.children[1].href, image.src);
  const oldImageButton = wrap.children[0];
  state.accountEpoch++; state.account = '02'; context.resetErrors();
  oldImageButton.listeners.click(); assert.equal($('image-dialog').open, false, 'Detached old screenshot controls must not reopen');

  const staleFailure = context.openError('failing'); const failure = nextRequest('/api/errors/failing?');
  state.accountEpoch++; state.account = '01-测试'; context.resetErrors();
  failure.reject(new Error('old account error')); await staleFailure;
  assert.equal(toasts.length, 0); assert.equal($('error-log').textContent, '');

  const mismatch = context.openError('wrong'); nextRequest('/api/errors/wrong?').resolve({ ...record('wrong', '02'), log: 'foreign log', images: [] }); await mismatch;
  assert.equal(state.errorDetail, null); assert(!$('error-log').textContent.includes('foreign log'));
  assert.equal(toasts.length, 1, 'A mismatched detail response must be rejected');

  state.account = ''; state.accountEpoch++; const before = requests.length;
  await context.loadErrors(); assert.equal(requests.length, before, 'No selection must never fall back to a global errors API');
  assert.equal($('errors-scope').textContent, '请选择配置');
  console.log('PASS: account-scoped list/detail/images, immediate selection reset, stale list/detail/failure guards, screenshot dismissal and empty selection.');
}
verify().catch((error) => { console.error(error); process.exitCode = 1; });
