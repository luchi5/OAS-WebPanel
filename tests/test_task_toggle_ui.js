'use strict';

// Invoke the shipped row switch with a fake backend. No real account settings,
// worker actions, emulator operations or notification requests are made.
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
const state = {}, calls = [], notices = [], refreshes = [], selections = [];
let responder, dirty = false, storedEnabled = true;
const el = (tag, className, text) => { const node = new Node(tag); node.className = className; node.textContent = text || ''; return node; };
const context = {
  state, $, encoder: encodeURIComponent, clone: (value) => JSON.parse(JSON.stringify(value)),
  taskLabel: (name) => name === 'DuelBet' ? '对弈竞猜' : name, label: (text) => text,
  el, icon: (name) => el('span', '', name),
  iconButton: (name, title, action) => { const node = el('button', 'icon-button'); node.name = name; node.title = title; node.addEventListener('click', action); return node; },
  empty: (node) => node.replaceChildren(), readOnly: (field) => Boolean(field.readOnly || field.readonly || field.read_only || field.disabled),
  dirtyFields: () => dirty ? [{}] : [], toast: (...args) => notices.push(args), showWorkTab() {}, stopSnapshotRefresh() {}, renderSelectedAccount() {},
  selectTask: async (...args) => selections.push(args),
  runTaskNow() { throw new Error('An enable switch must not submit a quick run'); },
  canRunTaskNow: () => true, syncSnapshotVisibility: (options) => refreshes.push(options),
  api: async (url, options = {}) => { calls.push({ url, ...options }); return responder(url, options); },
};
vm.createContext(context);
function load(first, last) { const begin = source.indexOf(first), end = source.indexOf(last, begin); assert(begin >= 0 && end > begin); vm.runInContext(source.slice(begin, end), context); }
load('  function scheduleItems(', '  function showWorkTab(');
load('  async function stageTaskEnabled(', '  async function selectTask(');
const task = 'DuelBet', account = '05-测试';
function snapshot(running = '', queued = true) {
  return { connected: true, state: 1, schedule: { running: running ? { name: running } : {}, pending: [], waiting: queued ? [{ name: task, next_run: '2026-10-07 17:00:00' }] : [] } };
}
function settings(enabled = storedEnabled) { return { scheduler: [{ name: 'enable', type: 'boolean', value: enabled }] }; }
function reset(running = '', queued = true) {
  Object.assign(state, { authenticated: true, account, accountEpoch: 1, taskEpoch: 1, bootEpoch: 1, task: '', actionBusy: false, saving: false, loadingSettings: false, mustChangePassword: false, backendOnline: true,
    taskScope: 'schedule', taskFilter: 'all', menu: [{ name: task, category: 'weekly' }, { name: 'Script', category: 'script' }], snapshots: new Map([[account, snapshot(running, queued)]]), openCategories: new Set(['weekly']), taskEnableOverrides: new Map() });
  calls.length = 0; notices.length = 0; refreshes.length = 0; selections.length = 0; dirty = false; storedEnabled = queued || running === task;
  responder = async (url, options) => {
    if (!options.method) return settings();
    assert.equal(options.method, 'PUT'); assert.equal(options.body.expected_value, storedEnabled);
    storedEnabled = options.body.value; return { ok: true, value: storedEnabled };
  };
}
function row(taskName = task) {
  context.renderTasks(); let children = $('task-list').children;
  if (state.taskScope === 'catalog') children = children.flatMap((node) => node.children.at(-1).children);
  return children.find((node) => node.dataset.task === taskName);
}
function toggle() { const taskRow = row(); assert(taskRow, 'Task row must be visible'); return taskRow.children[0].children[0]; }
const writes = () => calls.filter((call) => call.method);
async function change(enabled) { const control = toggle(); control.checked = enabled; await control.listeners.change(); }
async function verify() {
  reset(); const control = toggle();
  assert.equal(control.disabled, false); assert.equal(control.checked, true);
  assert.equal(control.attributes.role, 'switch'); assert(control.attributes['aria-label'].includes('立即保存'));
  await change(false); assert.equal(storedEnabled, false); assert.equal(writes().length, 1);
  assert(writes()[0].url.endsWith('/settings/DuelBet/scheduler/enable')); assert.equal(writes()[0].body.value, false);
  assert.equal(row(), undefined, 'A successfully disabled queued task leaves the overview');
  assert(notices.at(-1)[0].includes('已停用并保存')); assert.equal(refreshes.length, 1); assert.equal(selections.length, 0, 'Switching does not navigate into collapsed settings');
  state.taskScope = 'catalog'; assert.equal(toggle().checked, false, 'Catalogue preserves the confirmed disabled setting while the old snapshot is in flight');
  await change(true); assert.equal(storedEnabled, true); assert.equal(writes().length, 2); assert.equal(toggle().checked, true);

  reset(task); await change(false); assert.equal(toggle().checked, false, 'The disabled current task remains visible until it ends');
  assert(notices.at(-1)[0].includes('当前任务执行完后')); assert(calls.every((call) => !call.url.endsWith('/actions')), 'Never stop/restart the account to change an enable flag');
  state.taskEnableOverrides.get(account).get(task).until = 0;
  assert.equal(toggle().checked, false, 'A still running disabled task cannot look enabled when a temporary confirmation expires');

  reset(); await change(false); state.taskEnableOverrides.get(account).get(task).until = 0;
  assert(row(), 'If the backend never reconciles a queued flag, polling must eventually expose the real schedule instead of hiding it indefinitely');
  assert.equal(toggle().checked, true);

  reset(); state.taskScope = 'catalog'; state.taskFilter = 'enabled'; await change(false); assert.equal(row(), undefined);
  state.taskFilter = 'disabled'; assert(row()); assert.equal(toggle().checked, false);

  reset(); dirty = true; await change(false); assert.equal(calls.length, 0); assert.equal(toggle().checked, true); assert(notices.at(-1)[0].includes('尚未保存'));
  reset(); responder = async () => settings(false); await change(false); assert.equal(writes().length, 0, 'An already saved value does not make a redundant PUT');

  reset(); responder = async (url, options) => { if (!options.method) return settings(); throw Object.assign(new Error('配置已改变，请刷新'), { status: 409 }); };
  await change(false); assert.equal(toggle().checked, true, 'A conflicting save restores the confirmed visual state'); assert(notices.at(-1)[0].includes('配置已改变'));
  reset(); responder = async (url, options) => !options.method ? settings() : { ok: true, value: true };
  await change(false); assert.equal(toggle().checked, true); assert(notices.at(-1)[0].includes('未确认'));

  reset(); responder = async () => ({ scheduler: [{ name: 'enable', value: true, readOnly: true }] });
  await change(false); assert.equal(writes().length, 0); assert(notices.at(-1)[0].includes('没有可修改'));
  reset(); responder = async (url, options) => !options.method ? { scheduler: { fields: settings().scheduler } } : { ok: true, value: false };
  await change(false); assert.equal(writes().length, 1, 'Accept grouped field response shape too');

  reset(); let release; responder = async (url, options) => !options.method ? new Promise((resolve) => { release = resolve; }) : { ok: true, value: false };
  const first = context.stageTaskEnabled(task, false); await context.stageTaskEnabled(task, false);
  assert.equal(calls.length, 1, 'Two taps share one in-flight save'); release(settings()); await first; assert.equal(writes().length, 1); assert.equal(state.actionBusy, false);

  reset(); responder = async () => { state.accountEpoch++; state.account = '06'; return settings(); };
  await context.stageTaskEnabled(task, false); assert.equal(writes().length, 0, 'Account switch before GET returns must cancel the write');
  assert.equal(state.actionBusy, false);

  reset(); state.task = task; await change(false); assert.deepEqual(selections, [[task, true]], 'An already open clean settings view is refreshed after save');
  for (const unavailable of ['backendOnline', 'authenticated']) { reset(); state[unavailable] = false; assert.equal(toggle().disabled, true); await context.stageTaskEnabled(task, false); assert.equal(calls.length, 0); }
  for (const busy of ['actionBusy', 'saving', 'loadingSettings', 'mustChangePassword']) { reset(); state[busy] = true; assert.equal(toggle().disabled, true); await context.stageTaskEnabled(task, false); assert.equal(calls.length, 0); }
  reset(); await context.stageTaskEnabled('Script', false); await context.stageTaskEnabled('unknown', false); assert.equal(calls.length, 0);
  assert.equal(state.taskEnableOverrides.size, 0);
  console.log('PASS: shipped overview/catalogue switches save immediately; active task remains intact; only enable is written; disabled filters, stale snapshots, conflicts, read-only, dirty edits, repeated taps, account races and unavailable backend are handled. No live settings or game tasks changed.');
}
verify().catch((error) => { console.error(error); process.exitCode = 1; });
