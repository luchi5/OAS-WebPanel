'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {metric, clock, imageUrl, localDay, targetSummary, taskCompletion, taskEvidenceTime} = require('../static/daily-feedback.js');
assert.equal(metric({expected:true, verified:false, current:0}, 'talisman'), '未核验');
assert.equal(metric({expected:true, verified:false, current:30, total:30}, 'collective'), '未核验');
assert.equal(metric({expected:true, verified:true, current:0}, 'talisman'), '0');
assert.equal(metric({expected:true, verified:true, current:113, target:100, total:null}, 'talisman'), '113');
assert.equal(targetSummary({verified:true, current:113, target:100}), '目标 100 · 已达标');
assert.equal(targetSummary({verified:true, current:80, target:100}), '目标 100 · 未达标');
assert.equal(targetSummary({verified:true, current:113, target:null}), '经验已读取 · 目标未核验');
assert.equal(targetSummary({verified:false, current:100, target:100}), '');
assert.equal(metric({expected:true, verified:true, current:25, total:30}, 'collective'), '25/30');
assert.equal(metric({expected:false, verified:false}, 'collective'), '今日无需验收');
assert.equal(clock(null), '尚未采集');
assert.equal(clock('2026-10-07T21:10:00+08:00'), '21:10 采集');
assert.equal(imageUrl('05-托管10-20', '2026-10-07', 'talisman-a.png'), '/api/daily-feedback/05-%E6%89%98%E7%AE%A110-20/images/talisman-a.png?date=2026-10-07');
assert.match(localDay(), /^\d{4}-\d{2}-\d{2}$/);
const source = fs.readFileSync(path.join(__dirname, '../static/daily-feedback.js'), 'utf8');
assert(!source.includes('innerHTML'), 'Evidence/account labels must render through textContent');
assert(!source.includes("method: 'POST'"), 'Verification page must not start, stop, or change tasks');
assert(source.includes("document.hidden"), 'Polling pauses while phone page is hidden');
assert(source.includes('推送服务已接收'), 'Accepted delivery must not claim iPhone receipt');
const html = fs.readFileSync(path.join(__dirname, '../static/daily-feedback.html'), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length, 'HTML IDs must remain unique');
const assetVersions = [...html.matchAll(/daily-feedback\.(?:css|js)\?v=([A-Za-z0-9_-]+)/g)].map(match => match[1]);
assert.equal(assetVersions.length, 2, 'Both assets need cache version keys');
assert.equal(assetVersions[0], assetVersions[1], 'Styles and scripts must use a coherent page version');
assert.equal(taskEvidenceTime(null), '未记录证据时间');
assert.equal(taskEvidenceTime('not-a-time'), '证据时间待核验');
assert.equal(taskEvidenceTime('2026-10-08T09:12:00Z'), '证据时间：2026-10-08 17:12');
assert.equal(taskEvidenceTime('2026-10-08 17:12:00'), '证据时间：2026-10-08 17:12');

// A small offline DOM executes the real initialization, card/detail rendering and
// asynchronous refresh handlers. It deliberately does not claim visual-layout QA.
class Element {
  constructor(tag = '') {
    Object.assign(this, {tagName: tag, className: '', children: [], listeners: {}, attributes: {}, _text: '', hidden: false, open: false, showCount: 0});
  }
  append(...nodes) { this.children.push(...nodes); nodes.forEach(node => { node.parent = this; }); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  removeAttribute(key) { delete this.attributes[key]; delete this[key]; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  dispatch(name, event = {}) { return Promise.all((this.listeners[name] || []).map(handler => handler({target: this, ...event}))); }
  showModal() { this.open = true; this.showCount++; }
  close() { if (this.open) { this.open = false; this.dispatch('close'); } }
  replaceWith(node) { const index = this.parent.children.indexOf(this); this.parent.children[index] = node; node.parent = this.parent; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
}
const descendants = (node, predicate) => [...(predicate(node) ? [node] : []), ...node.children.flatMap(child => descendants(child, predicate))];
const hasClass = (node, name) => node.className.split(/\s+/).includes(name);
const flush = () => new Promise(setImmediate);
const copy = value => JSON.parse(JSON.stringify(value));
const day = '2026-10-08';
const report = tasks => ({scope: 'enabled_today', date: day, available: true, tasks,
  // Counts are intentionally inconsistent: unknown statuses must not inherit an
  // alleged completion from server counts or the legacy account screenshot badge.
  counts: {completed: 999, incomplete: 0, unverified: 0}, note: '根据今天启用任务的完成证据核验'});
const tasks = [
  {task: 'DailyTrifles', label: '日常琐事', status: 'completed', status_label: '已完成', reason: '记录到任务结束', evidence_at: `${day}T17:12:00+08:00`},
  {task: 'Dokan', label: '道馆', status: 'incomplete', status_label: '未完成', reason: '开场超时未执行', evidence_at: `${day}T17:20:00+08:00`},
  {task: 'AreaBoss', label: '地域鬼王', status: 'unverified', status_label: '待核验', reason: '尚无可核验的结束记录', evidence_at: null},
];
function row(completion = report(tasks)) {
  return {account: '05-合成账号', date: day, status: 'verified', status_label: '已核验', reason: '仅说明花合战及寮30截图', task_completion: completion,
    evidence: {talisman: {expected: true, verified: true, current: 113, target: 100, image: 'talisman.png', captured_at: `${day}T17:00:00+08:00`, final: true, detail: '花合战截图'},
      collective: {expected: true, verified: true, current: 30, total: 30, image: 'collective.png', captured_at: `${day}T17:01:00+08:00`, final: true, detail: '寮30截图'},
      unrelated: {image: 'must-not-render.png'}},
    closeout: {phase: 'final', waiting: ['Dokan'], completed: ['CollectiveMissions'], incomplete: [{label: '道馆', outcome: 'expired'}]},
    settings: {talisman_enabled: true, dynamic_closeout_enabled: true}, notification: {status: 'accepted', at: `${day}T17:30:00+08:00`}};
}
function data(account) { return {date: day, today: day, counts: {attention: 0, verified: 1, pending: 0, running: 0, unverified: 0, disabled: 0}, accounts: [account]}; }
class FixedDate extends Date { constructor(...args) { super(...(args.length ? args : [`${day}T08:00:00Z`])); } }
function harness(initial) {
  const nodes = Object.fromEntries(ids.map(id => [id, new Element()]));
  const timers = [], calls = [], historyCalls = [];
  let respond = async () => initial;
  const document = {hidden: false, createElement: tag => new Element(tag), getElementById: id => { assert(nodes[id], `Missing DOM node ${id}`); return nodes[id]; }, addEventListener() {}};
  const context = {document, Intl, Date: FixedDate, URLSearchParams, AbortController, setTimeout, clearTimeout,
    window: {addEventListener() {}}, location: {search: `?date=${day}`},
    history: {replaceState: (...args) => historyCalls.push(args)}, setInterval: (callback, delay) => timers.push({callback, delay}),
    fetch: async (url, options) => { calls.push({url, options}); const value = await respond(); return {ok: true, status: 200, json: async () => value}; }};
  vm.createContext(context); vm.runInContext(source, context);
  return {nodes, timers, calls, historyCalls, respond: handler => { respond = handler; }};
}
function completionSection(body) { return descendants(body, node => hasClass(node, 'task-completion'))[0]; }
function preview(body) { return descendants(body, node => hasClass(node, 'task-overview'))[0]; }
async function rendered(account) {
  const ui = harness(data(account)); await flush();
  await ui.nodes['account-list'].children[0].dispatch('click');
  return ui;
}
async function verifyRendering() {
  const {visibleData, taskResult} = require('../static/daily-feedback.js');
  assert.equal(taskResult({task:'Chess', status:'not_scheduled', weekly_progress:{value:530, verified:true}}), '530/600');
  assert.equal(taskResult({task:'Dokan', weekly_progress:{value:2, verified:true}}), '2次');
  assert.equal(taskResult({task:'SixRealms', weekly_progress:{value:1, verified:false}}), '1次');
  const all = {...data(row()), accounts: Array.from({length: 15}, (_, index) => ({...row(), account: `${String(index + 1).padStart(2, '0')}-合成账号`}))};
  const visible = visibleData(all);
  assert.deepEqual(visible.accounts.map(item => Number(item.account.slice(0, 2))), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]);
  assert.equal(visible.counts.verified, 14, 'Summary counts must only count visible accounts');
  assert.equal(all.accounts.length, 15, 'Filtering must not remove backend accounts');
  const fixed = {...report(Array.from({length: 24}, (_, index) => ({task: `Fixed${index}`, label: `固定项目${index + 1}`,
    group: index < 13 ? 'daily' : 'weekly', status: index === 0 ? 'disabled' : index === 13 ? 'not_scheduled' : 'incomplete', reason: '合成配置状态'}))), scope: 'poster_checklist'};
  fixed.tasks[1] = {task: 'KekkaiUtilize', label: '结界蹭卡', group: 'daily', status: 'completed', completion_count: 2, reason: '寄养证据'};
  fixed.tasks[4] = {task: 'AreaBoss', label: '地域鬼王', group: 'daily', status: 'incomplete', reason: '今日已成功处理 1/2 只地域鬼王'};
  const fixedAccount = row(); fixedAccount.task_completion.service_checklist = fixed;
  const fixedUi = await rendered(fixedAccount);
  const fixedCard = fixedUi.nodes['account-list'].children[0];
  const fixedEntries = descendants(fixedCard, node => hasClass(node, 'task-chip'));
  assert.equal(fixedEntries.length, 24);
  assert(fixedEntries[0].textContent.includes('1、固定项目1未开启'));
  assert(fixedEntries[1].textContent.includes('2、结界蹭卡2次'));
  assert(fixedEntries[4].textContent.includes('5、地域鬼王1/2'));
  assert(fixedEntries[13].textContent.includes('1、固定项目14今日无需运行'));
  assert(fixedCard.textContent.includes('每日任务：') && fixedCard.textContent.includes('每周任务：'));
  assert.equal(descendants(fixedUi.nodes['detail-body'], node => hasClass(node, 'task-item')).length, 24);
  assert.equal(descendants(fixedUi.nodes['detail-body'], node => node.tagName === 'img').length, 2);
  assert.equal(taskResult({task: 'KekkaiUtilize', status: 'disabled', status_label: '未开启', completion_count: 2}), '未开启');
  const multi = harness(all); await flush();
  assert.equal(multi.nodes['account-list'].children.length, 14);
  assert(multi.nodes['account-list'].textContent.includes('09-合成账号'));
  assert(multi.nodes['account-list'].textContent.includes('11-合成账号'));
  assert(multi.nodes['account-list'].textContent.includes('12-合成账号'));
  assert(!multi.nodes['account-list'].textContent.includes('15-合成账号'));
  const fixture = row(); const before = JSON.stringify(fixture);
  const ui = await rendered(fixture);
  const card = ui.nodes['account-list'].children[0];
  const detail = ui.nodes['detail-body']; const completion = completionSection(detail);
  assert(card.textContent.includes('截图验收：已核验') && ui.nodes.summary.textContent.includes('截图已核验'), 'Legacy badges and summary must visibly state their screenshot-only scope');
  for (const status of ['completed', 'unverified']) {
    assert.equal(descendants(preview(card), node => hasClass(node, 'task-state') && hasClass(node, status)).length, status === 'completed' ? 2 : 3, 'Each status needs its count and its task badge on the card');
    assert.equal(descendants(completion, node => hasClass(node, 'task-state') && hasClass(node, status)).length, status === 'completed' ? 2 : 3, 'Each status needs its count and its task badge in details');
  }
  assert(preview(card).textContent.includes('已完成 1') && preview(card).textContent.includes('待核验 2') && preview(card).textContent.includes('待核验 2'));
  assert(!preview(card).textContent.includes('999'), 'Displayed counts must follow normalized tasks');
  assert(completion.textContent.includes('开场超时未执行') && completion.textContent.includes('未记录证据时间'));
  assert(completion.textContent.includes('证据时间：2026-10-08 17:12'));
  assert.equal(descendants(detail, node => node.tagName === 'img').length, 2, 'Task completion must not add screenshots');
  assert.equal(descendants(detail, node => hasClass(node, 'evidence-photo')).length, 2);
  assert(!detail.textContent.includes('must-not-render.png'));
  assert(detail.textContent.includes('当天收尾') && detail.textContent.includes('等待开场超时') && detail.textContent.includes('手机通知') && detail.textContent.includes('推送服务已接收'), 'Original closeout and notification meanings remain visible');
  assert.equal(JSON.stringify(fixture), before, 'Rendering must not mutate evidence or completion data');
  const imageButtons = descendants(detail, node => hasClass(node, 'evidence-photo'));
  await imageButtons[0].dispatch('click');
  assert(ui.nodes['image-dialog'].open && ui.nodes['image-preview'].src.includes('talisman.png'));
  assert(ui.nodes['image-download'].download.endsWith('-talisman.png'), 'Original image enlargement/download remains available');

  const legacy = row(); delete legacy.task_completion;
  const legacyUi = await rendered(legacy);
  assert(preview(legacyUi.nodes['account-list']).textContent.includes('任务完成情况待核验'));
  assert(completionSection(legacyUi.nodes['detail-body']).textContent.includes('任务完成情况待核验'));
  assert.equal(descendants(completionSection(legacyUi.nodes['detail-body']), node => hasClass(node, 'completed')).length, 0, 'Legacy verified screenshot status cannot complete all tasks');
  const disabledImages = row(); disabledImages.status = 'disabled'; disabledImages.status_label = '未启用';
  const disabledUi = await rendered(disabledImages);
  assert(disabledUi.nodes['account-list'].textContent.includes('截图验收：未启用'));
  assert(preview(disabledUi.nodes['account-list']).textContent.includes('已完成 1') && preview(disabledUi.nodes['account-list']).textContent.includes('待核验 2'), 'Disabled screenshots cannot hide other enabled tasks');

  const unknown = row(report([{task: '<img src=x onerror=alert(1)>', label: '<script>unsafe</script>', status: 'future-status', status_label: '已完成', reason: '<b>未经确认</b>', evidence_at: null}]));
  const unknownUi = await rendered(unknown);
  const unknownSection = completionSection(unknownUi.nodes['detail-body']);
  assert(unknownSection.textContent.includes('待核验 1') && unknownSection.textContent.includes('<script>unsafe</script>') && unknownSection.textContent.includes('<b>未经确认</b>'));
  const unknownItems = descendants(unknownSection, node => hasClass(node, 'task-list'))[0];
  assert.equal(descendants(unknownItems, node => node.tagName === 'script' || node.tagName === 'img' || hasClass(node, 'completed') || hasClass(node, 'future-status')).length, 0, 'Unknown statuses and text must not create trusted completion or HTML');
  assert.equal(taskCompletion(row(report([{task: '__proto__', status: {toString: null}}]))).tasks[0].status, 'unverified', 'Malformed statuses normalize without throwing');

  const emptyUi = await rendered(row(report([])));
  assert(completionSection(emptyUi.nodes['detail-body']).textContent.includes('今天没有启用其它任务'));
  assert.equal(descendants(completionSection(emptyUi.nodes['detail-body']), node => hasClass(node, 'task-state')).length, 0, 'No enabled tasks must not imply completed tasks');
  const historyUi = await rendered(row({scope: 'enabled_today', date: day, available: false, tasks: [], note: '历史日期的启用配置无法还原'}));
  assert(completionSection(historyUi.nodes['detail-body']).textContent.includes('历史日期的启用配置无法还原'));
  assert(!completionSection(historyUi.nodes['detail-body']).textContent.includes('今天没有启用其它任务'));
  const mismatchUi = await rendered(row({...report(tasks), date: '2026-10-07'}));
  assert(completionSection(mismatchUi.nodes['detail-body']).textContent.includes('所选日期不一致'));

  const many = row(report([...tasks, ...Array.from({length: 5}, (_, index) => ({task: `Task${index}`, label: `其它任务${index}`, status: 'unverified', reason: '等待记录'}))]));
  const manyUi = await rendered(many);
  assert.equal(descendants(manyUi.nodes['account-list'], node => hasClass(node, 'task-chip')).length, 8);
  assert(!preview(manyUi.nodes['account-list']).textContent.includes('另有'));
  assert.equal(descendants(manyUi.nodes['detail-body'], node => hasClass(node, 'task-item')).length, 8, 'Full details must include every enabled task');

  let resolveRefresh;
  ui.respond(() => new Promise(resolve => { resolveRefresh = resolve; }));
  const refreshing = ui.nodes.refresh.dispatch('click'); await flush();
  assert(ui.nodes['detail-dialog'].open && completionSection(ui.nodes['detail-body']).textContent.includes('待核验 2'), 'In-flight reads must retain the selected account and previous evidence');
  const updated = copy(fixture);
  updated.task_completion.tasks[1] = {...updated.task_completion.tasks[1], status: 'completed', reason: '新增道馆完成记录', evidence_at: `${day}T18:00:00+08:00`};
  resolveRefresh(data(updated)); await refreshing; await flush();
  assert(ui.nodes['detail-dialog'].open && ui.nodes['detail-dialog'].showCount === 1, 'Updating an open account must not reopen or close its dialog');
  const refreshed = completionSection(ui.nodes['detail-body']);
  assert(refreshed.textContent.includes('已完成 2') && refreshed.textContent.includes('待开始 0') && refreshed.textContent.includes('新增道馆完成记录'));
  assert(preview(ui.nodes['account-list']).textContent.includes('已完成 2'));
  assert.equal(descendants(ui.nodes['detail-body'], node => node.tagName === 'img').length, 2);
  assert(ui.nodes['image-dialog'].open && ui.nodes['image-preview'].src.includes('talisman.png'), 'Refreshing task states must retain an already open image');
  assert(ui.historyCalls.at(-1)[2].includes('account='), 'Selected account remains in the URL');

  const retained = ui.nodes['detail-body'].textContent;
  ui.respond(async () => { throw new Error('合成网络异常'); });
  await ui.nodes.refresh.dispatch('click'); await flush();
  assert.equal(ui.nodes['detail-body'].textContent, retained, 'Failed refreshes must retain the last successful task information');
  assert(!ui.nodes.error.hidden && ui.nodes.error.textContent.includes('合成网络异常'));
  assert.equal(ui.timers[0].delay, 30000, 'Existing read-only refresh interval remains unchanged');
  assert(ui.calls.every(call => call.options.cache === 'no-store' && !call.options.method), 'Rendering tests must use only read requests');
  console.log('PASS: daily feedback helpers; three-state cards/details; legacy/malformed/empty/historical data; all-task list; two-image closeout/notification regressions; safe text rendering; asynchronous refresh and failure retention.');
}
verifyRendering().catch(error => { console.error(error); process.exitCode = 1; });
