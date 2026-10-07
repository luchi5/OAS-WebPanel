'use strict';

// Exercise the shipped refresh controller with delayed responses. No OAS process,
// browser, simulator or real network connection is used by these checks.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const state = {
  authenticated: true, bootEpoch: 1, accountEpoch: 1, backendOnline: true,
  account: '01', accounts: Array.from({ length: 9 }, (_, index) => String(index + 1).padStart(2, '0')),
  snapshots: new Map(), snapshotVersions: new Map(),
  snapshotRefresh: { epoch: 0, timer: null, request: null, busy: false, cursor: 0, full: false },
};
const document = { hidden: false }, app = { hidden: false };
const timers = new Map();
let nextTimer = 0, calls = [], renders = 0, active = 0, peak = 0, respond;
const snapshot = (value = 0) => ({ state: value, schedule: {}, connected: true });
const context = {
  state, document, $: () => app, AbortController, encoder: encodeURIComponent,
  equal: (first, second) => JSON.stringify(first) === JSON.stringify(second),
  renderAccounts: () => { renders++; }, renderTasks: () => { renders++; }, updateConnection: () => { renders++; },
  clearTimeout: (id) => timers.delete(id),
  setTimeout: (callback, delay) => { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
  api: async (requestPath, options) => {
    calls.push({ path: requestPath, ...options }); active++; peak = Math.max(peak, active);
    try { return await respond(decodeURIComponent(requestPath.split('/')[3]), options); }
    finally { active--; }
  },
};
vm.createContext(context);
const start = source.indexOf('  function storeSnapshot('), end = source.indexOf('  function disconnectSocket(');
assert(start >= 0 && end > start);
vm.runInContext(source.slice(start, end), context);
const flush = () => new Promise(setImmediate);
const names = () => calls.map((call) => decodeURIComponent(call.path.split('/')[3]));
async function tick() {
  assert.equal(timers.size, 1, 'Only one state timer may exist');
  const [id, timer] = [...timers.entries()][0]; timers.delete(id);
  await timer.callback(); await flush();
}

async function verify() {
  state.accounts.forEach((name) => state.snapshots.set(name, snapshot()));
  respond = async () => { await Promise.resolve(); return snapshot(); };
  context.syncSnapshotVisibility({ full: true }); await flush();
  assert.deepEqual(names().sort(), [...state.accounts]);
  assert.equal(peak, 2, 'The initial account list must limit upstream reads to two at a time');
  assert.equal([...timers.values()][0].delay, 2000);
  assert.equal(renders, 0, 'Unchanged snapshots must not rebuild account and task lists');

  calls = [];
  for (let cycle = 0; cycle < 4; cycle++) await tick();
  assert.equal(calls.length, 12, 'Each normal cycle reads the selected account and two other accounts');
  assert.equal(names().filter((name) => name === '01').length, 4);
  assert.equal(new Set(names().filter((name) => name !== '01')).size, 8,
    'Every other account must be refreshed within four normal cycles');
  assert(calls.every((call) => call.path.endsWith('/snapshot')), 'The refresh loop must not request logs or bootstrap/menu');

  context.stopSnapshotRefresh(); calls = [];
  let resolveSelected;
  respond = async (name) => name === '01' ? new Promise((resolve) => { resolveSelected = resolve; }) : snapshot();
  context.syncSnapshotVisibility(); await flush();
  const countWhilePending = calls.length;
  context.syncSnapshotVisibility(); context.syncSnapshotVisibility(); await context.refreshSnapshots();
  assert.equal(calls.length, countWhilePending, 'A slow cycle must not spawn an overlapping cycle');
  context.storeSnapshot('01', snapshot(1));
  resolveSelected(snapshot(0)); await flush();
  assert.equal(state.snapshots.get('01').state, 1,
    'A delayed snapshot must not replace a newer WebSocket state or schedule');

  context.stopSnapshotRefresh(); calls = [];
  context.syncSnapshotVisibility(); await flush();
  const hiddenSignal = calls[0].signal;
  document.hidden = true; context.syncSnapshotVisibility({ force: true });
  assert(hiddenSignal.aborted && timers.size === 0 && !state.snapshotRefresh.busy);
  resolveSelected(snapshot(0)); await flush();
  assert.equal(state.snapshots.get('01').state, 1, 'Hidden-page responses must be discarded');
  const hiddenRequests = calls.length;
  await context.refreshSnapshots(); assert.equal(calls.length, hiddenRequests);

  calls = []; document.hidden = false; respond = async () => snapshot(1);
  context.syncSnapshotVisibility({ force: true, full: true }); await flush();
  assert.deepEqual(names().sort(), [...state.accounts], 'Returning to the page must immediately reconcile all accounts');
  assert.equal(timers.size, 1);

  context.stopSnapshotRefresh(); calls = [];
  state.actionBusy = true; context.syncSnapshotVisibility({ full: true }); await flush();
  assert.equal(calls.length, 0, 'Changing page visibility during a control operation must not re-enable status reads');
  state.actionBusy = false; await tick();
  assert.deepEqual(names().sort(), [...state.accounts], 'The deferred full refresh must survive a control operation');

  context.stopSnapshotRefresh(); calls = [];
  respond = async (name) => name === '01' ? new Promise((resolve) => { resolveSelected = resolve; }) : snapshot(1);
  context.syncSnapshotVisibility(); await flush();
  const replacedSignal = calls[0].signal, previousResolve = resolveSelected;
  respond = async () => snapshot(1);
  context.syncSnapshotVisibility({ force: true, full: true }); await flush();
  assert(replacedSignal.aborted, 'An immediate visibility refresh must cancel the previous request group');
  previousResolve(snapshot(0)); await flush();
  assert.equal(state.snapshots.get('01').state, 1, 'Late replies from a cancelled cycle must not replace the resumed page');
  assert.equal(timers.size, 1, 'Cancelled and resumed cycles must not each retain a polling timer');

  context.stopSnapshotRefresh(); calls = [];
  respond = async (name) => name === '01' ? new Promise((resolve) => { resolveSelected = resolve; }) : snapshot(1);
  context.syncSnapshotVisibility(); await flush();
  state.account = '09'; state.accountEpoch++;
  resolveSelected(snapshot(0)); await flush();
  assert.equal(state.snapshots.get('01').state, 1, 'A response from the previous selected account must not write back');
  assert.equal(timers.size, 0, 'The previous selection must not schedule another cycle');

  state.account = '01'; context.stopSnapshotRefresh(); calls = [];
  context.syncSnapshotVisibility(); await flush();
  state.bootEpoch++;
  resolveSelected(snapshot(0)); await flush();
  assert.equal(state.snapshots.get('01').state, 1, 'A previous bootstrap generation must not write back');
  assert.equal(timers.size, 0);

  state.authenticated = false; const loggedOutRequests = calls.length;
  context.syncSnapshotVisibility(); await context.refreshSnapshots();
  assert.equal(calls.length, loggedOutRequests, 'Logged-out pages must not poll accounts');

  // Run the actual action completion and statistics queue hook together, with
  // every API mocked, so no task or simulator can be started by this test.
  state.authenticated = true; state.mustChangePassword = false;
  state.statistics = { loading: true, refreshPending: false };
  Object.assign(context, {
    dirtyFields: () => [], confirmAction: async () => true, renderSelectedAccount: () => {}, toast: () => {},
    showWorkTab: () => {}, statisticsVisible: () => true,
    loadStatistics: () => { throw new Error('A pending statistics read must not be replaced'); },
  });
  const actionStart = source.indexOf('  async function performAction('), actionEnd = source.indexOf('  function appendLog(');
  const queueStart = source.indexOf('  function requestStatisticsRefresh('), queueEnd = source.indexOf('  async function loadStatistics(');
  assert(actionStart >= 0 && actionEnd > actionStart && queueStart >= 0 && queueEnd > queueStart);
  vm.runInContext(source.slice(actionStart, actionEnd) + source.slice(queueStart, queueEnd), context);
  calls = []; respond = async () => snapshot(1);
  await context.performAction('start'); await flush();
  assert.equal(calls.filter((call) => call.method === 'POST').length, 1);
  assert.equal(state.statistics.refreshPending, true, 'Action completion must queue statistics when its read is pending');
  assert.equal(state.actionBusy, false); assert.equal(timers.size, 1);
  assert.deepEqual(names().filter((name, index) => calls[index].method !== 'POST').sort(), [...state.accounts],
    'Action completion must immediately reconcile all account states');
  context.stopSnapshotRefresh();
  console.log('PASS: initial/full reconciliation, selected-account cadence, bounded rotating reads, unchanged rendering, no overlap, WebSocket revision guard, hidden/session/selection/bootstrap races and immediate resume.');
}
verify().catch((error) => { console.error(error); process.exitCode = 1; });
