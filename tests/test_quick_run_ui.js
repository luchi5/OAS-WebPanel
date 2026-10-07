'use strict';

// Exercise the shipped lightning button and its asynchronous controller without
// opening OAS connections or starting any real game/account task.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
class Node {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.value = ''; this.hidden = false; this.disabled = false; this.classList = { toggle() {} }; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
const nodes = new Map(), $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
const state = {}, calls = [], notices = [], settingsRefreshes = [], visibilityRefreshes = [];
let responder, dirty = false;
const el = (tag, className, text) => { const node = new Node(tag); node.className = className; node.textContent = text || ''; return node; };
const context = {
  state, $, Intl, Date, encoder: encodeURIComponent, clone: (value) => JSON.parse(JSON.stringify(value)),
  taskLabel: (task) => task === 'DailyTrifles' ? '每日琐事' : task,
  el, icon: (name) => el('span', '', name), iconButton: (name, title, action) => { const node = el('button', 'icon-button'); node.name = name; node.title = title; node.addEventListener('click', action); return node; },
  document: { createTextNode: (text) => text }, label: (text) => text,
  empty: (node) => node.replaceChildren(), readOnly: (field) => Boolean(field.readOnly || field.readonly || field.read_only || field.disabled),
  dirtyFields: () => dirty ? [{}] : [], toast: (...args) => notices.push(args), showWorkTab() {}, stopSnapshotRefresh() {}, renderSelectedAccount() {},
  storeSnapshot: (name, data) => { state.snapshots.set(name, data); state.snapshotVersions.set(name, (state.snapshotVersions.get(name) || 0) + 1); },
  selectTask: async (...args) => settingsRefreshes.push(args),
  syncSnapshotVisibility: (options) => visibilityRefreshes.push(options),
  api: async (url, options = {}) => { calls.push({ url, ...options }); return responder(url, options); },
};
vm.createContext(context);
function load(first, last) { const begin = source.indexOf(first), end = source.indexOf(last, begin); assert(begin >= 0 && end > begin); vm.runInContext(source.slice(begin, end), context); }
load('  function scheduleItems(', '  function showWorkTab(');
load('  function canRunTaskNow(', '  async function openScheduleSettings(');
load('  function applySession(', '  function storeSnapshot(');
load('  function openPassword(', '  function showActivity(');
const task = 'DailyTrifles', account = '02-测试';
function snapshot(value = 1, running = '') { return { connected: true, state: value, schedule: { running: running ? { name: running } : {}, pending: [], waiting: [{ name: task, next_run: '2099-01-01 00:00:00' }] } }; }
function settings(enabled = true) { return { scheduler: [{ name: 'enable', type: 'boolean', value: enabled }, { name: 'next_run', type: 'date_time', value: '2099-01-01 00:00:00' }] }; }
function reset(value = 1, running = '') {
  Object.assign(state, { authenticated: true, loginRequired: true, account, accountEpoch: 1, taskEpoch: 1, bootEpoch: 1, task: '', actionBusy: false, saving: false, loadingSettings: false, mustChangePassword: false, backendOnline: true,
    taskScope: 'schedule', taskFilter: 'all', menu: [{ name: task, category: 'daily' }, { name: 'Orochi', category: 'souls' }, { name: 'Script', category: 'script' }], snapshots: new Map([[account, snapshot(value, running)]]), snapshotVersions: new Map(), openCategories: new Set() });
  calls.length = 0; notices.length = 0; settingsRefreshes.length = 0; visibilityRefreshes.length = 0; dirty = false;
  responder = async (url, options) => url.endsWith('/snapshot') ? snapshot(value, running) : url.endsWith('/next_run') ? { ok: true, value: options.body.value } : url.endsWith('/actions') ? { ok: true, state: 1 } : settings();
}
function lightning() { context.renderTasks(); const row = $('task-list').children.find((node) => node.dataset.task === task); assert(row); return row.children.at(-1).children[0]; }
const writes = () => calls.filter((call) => call.method);
const assertQueue = () => {
  const call = writes()[0]; assert(call.url.endsWith('/settings/DailyTrifles/scheduler/next_run')); assert.equal(call.method, 'PUT');
  assert.equal(call.body.expected_value, '2099-01-01 00:00:00'); assert(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(call.body.value));
  assert(new Date(call.body.value.replace(' ', 'T') + '+08:00').getTime() < Date.now() - 23 * 3600000);
};
async function verify() {
  reset(); let button = lightning(); assert.equal(button.disabled, false, 'An idle running account must allow the lightning button');
  await button.listeners.click(); assertQueue(); assert.equal(writes().length, 1); assert(notices.at(-1)[0].includes('后台将自动执行')); assert.equal(state.actionBusy, false);

  reset(1, 'Orochi'); await lightning().listeners.click(); assertQueue(); assert.equal(writes().length, 1, 'Queue a different task without stopping/restarting the current account'); assert(notices.at(-1)[0].includes('当前任务结束后'));

  reset(0); await lightning().listeners.click(); assertQueue(); assert.equal(writes().length, 2); assert.equal(writes()[1].body.action, 'start'); assert(notices.at(-1)[0].includes('已启动'));

  reset(0); responder = async (url) => url.endsWith('/snapshot') ? snapshot(0) : settings(false);
  await lightning().listeners.click(); assert.equal(writes().length, 0); assert(notices.at(-1)[0].includes('请先启用'), 'A disabled task must explain how to proceed and never enable itself');

  reset(); dirty = true; await lightning().listeners.click(); assert.equal(calls.length, 0); assert(notices.at(-1)[0].includes('尚未保存'));
  reset(1, task); assert.equal(lightning().disabled, true); await context.runTaskNow(task); assert.equal(calls.length, 0);
  reset(2); assert.equal(lightning().disabled, true); await context.runTaskNow(task); assert.equal(calls.length, 0);
  reset(); state.snapshots.get(account).connected = false; assert.equal(lightning().disabled, true); await context.runTaskNow(task); assert.equal(calls.length, 0);
  reset(); await context.runTaskNow('Script'); await context.runTaskNow('not-a-task'); assert.equal(calls.length, 0);

  reset(); responder = async (url) => url.endsWith('/snapshot') ? snapshot(1, task) : settings();
  await context.runTaskNow(task); assert.equal(writes().length, 0); assert(notices.at(-1)[0].includes('已经在执行'));

  reset(); let release; responder = async (url, options) => url.endsWith('/snapshot') ? new Promise((resolve) => { release = resolve; }) : url.endsWith('/next_run') ? { ok: true, value: options.body.value } : settings();
  const first = context.runTaskNow(task); await context.runTaskNow(task); assert.equal(calls.length, 1, 'Double taps must share one operation'); release(snapshot()); await first; assert.equal(writes().length, 1);

  reset(); responder = async (url) => { state.accountEpoch++; state.account = '03'; return snapshot(); };
  await context.runTaskNow(task); assert.equal(calls.length, 1); assert.equal(writes().length, 0, 'Switching accounts during snapshot must cancel the write');
  reset(); responder = async (url) => { if (url.endsWith('/snapshot')) return snapshot(); state.accountEpoch++; state.account = '03'; return settings(); };
  await context.runTaskNow(task); assert.equal(writes().length, 0, 'Switching accounts while loading settings must cancel the write');

  reset(0); responder = async (url) => { if (url.endsWith('/snapshot')) return snapshot(0); if (url.endsWith('/next_run')) throw Object.assign(new Error('内容已改变'), { status: 409 }); return settings(); };
  await context.runTaskNow(task); assert.equal(writes().length, 1); assert(notices.at(-1)[0].includes('内容已改变')); assert(!calls.some((call) => call.url.endsWith('/actions')), 'Conflicting saves must never start an account');
  reset(0); responder = async (url, options) => { if (url.endsWith('/snapshot')) return snapshot(0); if (url.endsWith('/next_run')) return { ok: true, value: options.body.value }; if (url.endsWith('/actions')) throw new Error('连接超时'); return settings(); };
  await context.runTaskNow(task); assert.equal(writes().length, 2); assert(notices.at(-1)[0].includes('任务已加入排程')); assert(notices.at(-1)[0].includes('勿重复提交'));

  reset(); state.task = task; await context.runTaskNow(task); assert.deepEqual(settingsRefreshes, [[task, true]], 'Refresh a clean currently open task after a confirmed queue update');

  reset(); context.applySession({ authenticated: true, username: '免登录', login_required: false, csrf: 'anonymous-test-csrf', must_change_password: false });
  assert.equal(state.loginRequired, false); assert.equal(state.csrf, 'anonymous-test-csrf'); assert.equal($('login-screen').hidden, true);
  for (const id of ['password-open', 'logout', 'mobile-password-open', 'account-preferences']) assert.equal($(id).hidden, true);
  context.openPassword(); await context.logout(); assert.equal(calls.length, 0, 'Anonymous UI must not submit authentication actions');
  context.applySession({ authenticated: true, username: 'admin', login_required: true, csrf: 'session-test-csrf' });
  assert.equal(state.loginRequired, true); assert.equal(state.csrf, 'session-test-csrf'); assert.equal($('account-preferences').hidden, false);
  assert(calls.every((call) => !call.url.endsWith('/stop')));
  console.log('PASS: actual lightning button, idle/busy/stopped accounts, enabled guard, optimistic save, double taps, account races, failed start, clean settings refresh and anonymous/authenticated UI. No game task was started.');
}
verify().catch((error) => { console.error(error); process.exitCode = 1; });
