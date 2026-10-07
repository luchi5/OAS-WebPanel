'use strict';
// Offline checks of the actual transfer controller; no browser or OAS connection.
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(new Set(ids).size, ids.length);
class Element {
  constructor(tag = '', cls = '', text = '') {
    Object.assign(this, {tagName: tag, className: cls, textContent: text, children: [], value: '',
      files: [], open: false, hidden: false, disabled: false});
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  click() { downloads.push({href: this.href, name: this.download}); }
  remove() {}
}
const nodes = Object.fromEntries(ids.map((id) => [id, new Element()]));
const state = {authenticated: true, mustChangePassword: false, accounts: ['演示一', '演示二'],
  account: '演示一', task: 'Chess', menu: [{name: 'Chess'}, {name: 'CourtyardAffairs'}],
  fields: [], transfer: {epoch: 0, busy: false, supported: false, request: null}};
let calls = [], downloads = [], blobs = [], confirmations = [], refreshes = [], respond;
let accept = true, refreshError = false;
const capability = {version: 1, modes: ['backup', 'share'], config_import: true, task_import: true};
const context = {state, $: (id) => nodes[id], encoder: encodeURIComponent, AbortController,
  el: (...args) => new Element(...args), taskLabel: (name) => name,
  dirtyFields: () => state.fields.filter((field) => field.dirty),
  document: {body: new Element()}, Blob,
  URL: {createObjectURL: (blob) => {blobs.push(blob); return 'blob:offline';}, revokeObjectURL: () => {}},
  setTimeout: () => 1, toast: () => {},
  api: async (requestPath, options = {}) => {calls.push({path: requestPath, ...options}); return respond(requestPath, options);},
  confirmAction: async (...args) => {confirmations.push(args); return accept;},
  bootstrap: async (options) => {refreshes.push(options); if (refreshError) throw new Error('refresh fixture failed');},
  selectTask: async (...args) => {refreshes.push(args);},
};
vm.createContext(context);
const start = source.indexOf('  function transferBusy('), end = source.indexOf('  async function api(');
assert(start >= 0 && end > start);
vm.runInContext(source.slice(start, end), context);
const flush = () => new Promise(setImmediate);
function file(text = '{"chess":{}}', size = Buffer.byteLength(text)) {
  return {name: 'fixture.json', size, text: async () => text};
}
async function reset(taskScope = false) {
  calls = []; downloads = []; blobs = []; confirmations = []; refreshes = [];
  state.fields = []; accept = true; refreshError = false;
  nodes['transfer-file'].files = []; nodes['transfer-name'].value = '';
  respond = async (requestPath) => requestPath.endsWith('/capabilities') ? capability : {warnings: []};
  await context.openTransfer(taskScope);
  assert.equal(state.transfer.supported, true);
  calls = [];
}
async function verify() {
  await reset();
  assert.equal(nodes['transfer-task-row'].hidden, true);
  assert.equal(nodes['transfer-name-row'].hidden, false);
  respond = async () => {throw new Error('后台尚未加载接口');};
  await context.openTransfer();
  assert.equal(state.transfer.supported, false);
  assert(nodes['transfer-error'].textContent.includes('尚未加载'));
  assert(nodes['transfer-backup'].disabled);

  let resolveOld;
  respond = async () => new Promise((resolve) => {resolveOld = resolve;});
  const old = context.openTransfer(); await flush();
  const oldSignal = calls.at(-1).signal;
  respond = async () => capability;
  await context.openTransfer(); assert(oldSignal.aborted);
  resolveOld({version: 99}); await old;
  assert.equal(state.transfer.supported, true, 'Late capability must not overwrite a newer dialog');

  await reset();
  respond = async () => ({fixture: '完整内容'});
  await context.exportTransfer('backup'); await context.exportTransfer('share');
  assert(calls[0].path.endsWith('/export?mode=backup'));
  assert(calls[1].path.endsWith('/export?mode=share'));
  assert.equal(downloads[0].name, '演示一-backup.json');
  assert((await blobs[0].text()).includes('完整内容'));

  await reset(); nodes['transfer-name'].value = '新配置';
  nodes['transfer-file'].files = [{name: 'large.json', size: 2097153, text: async () => {throw new Error('Must not read');}}];
  await context.importTransfer(); assert.equal(calls.length, 0);
  assert(nodes['transfer-error'].textContent.includes('2 MiB'));
  nodes['transfer-file'].files = [file('[]')];
  await context.importTransfer(); assert.equal(calls.length, 0);
  assert(nodes['transfer-error'].textContent.includes('JSON 配置对象'));
  nodes['transfer-file'].files = [file()]; nodes['transfer-name'].value = '演示一';
  await context.importTransfer(); assert.equal(calls.length, 0);
  assert(nodes['transfer-error'].textContent.includes('名称已存在'));

  await reset(true); nodes['transfer-file'].files = [file()];
  state.fields = [{dirty: true}]; await context.importTransfer();
  assert.equal(calls.length, 0); assert(nodes['transfer-error'].textContent.includes('保存或取消'));
  state.fields = []; accept = false; await context.importTransfer();
  assert.equal(calls.length, 0); assert.equal(state.transfer.busy, false);
  accept = true; await context.importTransfer();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/accounts/%E6%BC%94%E7%A4%BA%E4%B8%80/tasks/Chess/import');
  assert.equal(calls[0].method, 'POST');
  assert.equal(JSON.parse(calls[0].body.json_text).chess.constructor.name, 'Object');
  assert.equal(refreshes.length, 2); assert.deepEqual(refreshes[1], ['Chess', true]);
  assert(calls.every((call) => !/start|stop/.test(call.path)));

  await reset(true); nodes['transfer-file'].files = [file()]; nodes['transfer-task'].value = '';
  nodes['transfer-name'].value = '不应新建'; await context.importTransfer();
  assert.equal(calls.length, 0); assert(nodes['transfer-error'].textContent.includes('目标任务'));

  await reset(); nodes['transfer-name'].value = '新配置'; nodes['transfer-file'].files = [file()];
  refreshError = true; await context.importTransfer();
  assert.equal(calls.length, 1); assert.equal(calls[0].body.name, '新配置');
  assert(nodes['transfer-error'].textContent.includes('导入已保存'));
  assert(nodes['transfer-error'].textContent.includes('无需再次导入'));

  await reset(); nodes['transfer-name'].value = '新配置';
  let resolveFile; nodes['transfer-file'].files = [{name: 'fixture.json', size: 16,
    text: () => new Promise((resolve) => {resolveFile = resolve;})}];
  const pending = context.importTransfer(); await flush();
  state.transfer.epoch++; nodes['transfer-dialog'].close();
  resolveFile('{"chess":{}}'); await pending;
  assert.equal(calls.length, 0, 'Closing/session change during file read must prevent import');

  console.log('PASS: transfer capabilities, stale responses, explicit export modes, size/type/duplicate guards, dirty settings, cancellation, exact task target, saved-refresh error and session race.');
}
verify().catch((error) => {console.error(error); process.exitCode = 1;});
