'use strict';

// Offline controller/rendering checks. This DOM shim does not verify visual layout.
// Run with: node tests/test_statistics_ui.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const ids = ['app', 'statistics-date', 'statistics-refresh', 'statistics-view', 'statistics-source',
  'statistics-method', 'statistics-method-detail', 'statistics-status', 'statistics-error', 'statistics-content', 'logs-panel'];
const htmlIds = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(new Set(htmlIds).size, htmlIds.length, 'HTML IDs must remain unique');
ids.filter((id) => id !== 'logs-panel').forEach((id) => assert(htmlIds.includes(id), `Missing HTML element: ${id}`));

class Element {
  constructor(tag = '', className = '', text = '') {
    Object.assign(this, { tagName: tag, className, children: [], dataset: {}, hidden: false,
      scrollTop: 0, value: '', _text: String(text) });
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener() {}
  get textContent() { return this._text + this.children.map((node) => node.textContent || '').join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  getClientRects() { return this.hidden ? [] : [{}]; }
}

const nodes = Object.fromEntries(ids.map((id) => [id, new Element()]));
const timers = new Map();
let timerId = 0, respond, calls = [];
const stats = { account: '', epoch: 0, active: false, request: null, timer: null, loading: false,
  dates: [], date: '', data: null, source: null, complete: true, error: '', unsupported: false,
  updatedAt: null, openTasks: new Set() };
const state = { authenticated: true, account: '演示一', accountEpoch: 1, activity: 'stats', statistics: stats };
const document = { hidden: false, querySelector: () => nodes['logs-panel'], createDocumentFragment: () => new Element() };
const context = {
  state, document, $: (id) => nodes[id], encoder: encodeURIComponent, AbortController, Intl, Date, Set,
  el: (...args) => new Element(...args), taskLabel: (name) => name,
  empty: (container, title, message) => container.replaceChildren(new Element('h3', '', title), new Element('p', '', message)),
  setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
  clearTimeout: (id) => timers.delete(id), ApiError: class extends Error {},
  api: async (requestPath, options) => { calls.push({ path: requestPath, signal: options.signal }); return respond(requestPath, options); },
};
const start = source.indexOf('  function statisticsVisible()');
const end = source.indexOf('  function setTheme(value)');
assert(start >= 0 && end > start, 'Statistics controller functions must be present');
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

const dates = { dates: ['2026-10-04', '2026-10-05'],
  source: { kind: 'local_logs', label: '本地日志解析', detail: '合成统计口径，只用于测试。' } };
const day = {
  script_name: '演示一', total_runtime_seconds: 0, total_task_run_count: 1, total_battle_count: 3,
  available_metrics: ['total_task_run_count', 'total_battle_count'],
  tasks: { Chess: { run_count: 1, total_duration_seconds: 0, available_metrics: ['run_count', 'battle_count'],
    battle: { count: 3 }, completed_run_count: 0, incomplete_run_count: 1,
    runs: [{ start_time: '2026-10-05 16:40:00', end_time: '2026-10-05 16:45:00', duration_seconds: null,
      status: 'incomplete', available_metrics: ['battle_count'], battle: { count: 3 } }] } },
};
const flush = () => new Promise(setImmediate);

const selectedDate = '2026-10-05';
function runRecord(start, end, count, duration = 10, overrides = {}) {
  const stamp = (time) => time.includes(' ') ? time : `${selectedDate} ${time}`;
  return { start_time: stamp(start), end_time: stamp(end), duration_seconds: duration,
    status: 'completed', available_metrics: ['duration_seconds', ...(count === null ? [] : ['battle_count'])],
    battle: count === null ? null : { count }, ...overrides };
}
const descendants = (node, predicate) => [
  ...(predicate(node) ? [node] : []),
  ...node.children.flatMap((child) => descendants(child, predicate)),
];
const freezeDeep = (value) => {
  if (value && typeof value === 'object') { Object.values(value).forEach(freezeDeep); Object.freeze(value); }
  return value;
};

function verifyRunGrouping() {
  const runs = [
    runRecord('08:00:00', '08:01:00', 0, 10),
    runRecord('08:11:00', '08:12:00', 0, 20),
    runRecord('08:20:00', '08:21:00', 3),
    runRecord('08:30:00', '08:31:00', 0),
    runRecord('08:40:00', '08:41:00', null),
    runRecord('08:50:00', '08:51:00', 0),
    runRecord('09:00:00', '09:01:00', 2),
  ];
  const before = JSON.stringify(runs);
  freezeDeep(runs);
  const groups = context.statisticsRunGroups(runs, selectedDate);
  assert.deepEqual(Array.from(groups, (group) => group.runs.length), [1, 3, 1, 2],
    'Positive settlements must stay independent and separate empty stretches on both sides');
  assert.strictEqual(groups[0].runs[0], runs[6], 'Display groups must show the latest interval first');
  assert.strictEqual(groups[2].runs[0], runs[2]);
  const emptyRow = context.statisticsMergedRunRow(groups[3].runs, selectedDate);
  assert(emptyRow.textContent.includes('08:00:00 → 观测至 08:12:00'));
  assert(emptyRow.textContent.includes('累计 30秒'), 'Idle gaps must not be counted as task runtime');
  assert(emptyRow.textContent.includes('确认结算 0 次') && emptyRow.textContent.includes('合并 2 段'));
  const unknownRow = context.statisticsMergedRunRow(groups[1].runs, selectedDate);
  assert(unknownRow.textContent.includes('结算数未记录'));
  assert(!unknownRow.textContent.includes('确认结算 0 次'), 'An unknown count must not become a confirmed zero');
  assert(context.statisticsRunRow(groups[0].runs[0], selectedDate).textContent.includes('确认结算 2 次'));

  const unavailable = runRecord('10:10:00', '10:11:00', 0, 20, { available_metrics: ['duration_seconds'] });
  const unavailableRow = context.statisticsMergedRunRow([
    runRecord('10:00:00', '10:01:00', 0), unavailable,
  ], selectedDate);
  assert(unavailableRow.textContent.includes('结算数未记录'), 'Metric availability must take precedence over raw zero');
  for (const missing of [
    runRecord('10:10:00', '10:11:00', 0, null),
    runRecord('10:10:00', '10:11:00', 0, 20, { available_metrics: ['battle_count'] }),
  ]) {
    const row = context.statisticsMergedRunRow([runRecord('10:00:00', '10:01:00', 0), missing], selectedDate);
    assert(row.textContent.includes('累计 未记录'), 'Partial duration evidence must remain unknown');
  }

  const crossDate = [
    runRecord('00:00:00', '00:01:00', 0, 10, { continues_from_previous_date: true }),
    runRecord('23:50:00', '2026-10-06 00:00:00', 0, 20, {
      status: 'incomplete', continues_to_next_date: true,
    }),
  ];
  const crossGroups = context.statisticsRunGroups(crossDate, selectedDate);
  assert.equal(crossGroups.length, 1, 'A midnight end clipped to the selected date remains a valid same-day interval');
  const crossRow = context.statisticsMergedRunRow(crossGroups[0].runs, selectedDate);
  assert(crossRow.textContent.includes('承接前日') && crossRow.textContent.includes('跨日继续'));
  assert(crossRow.textContent.includes('已结束 1 段') && crossRow.textContent.includes('未记录结束 1 段'));
  const runningRow = context.statisticsMergedRunRow([
    runRecord('10:00:00', '10:01:00', 0, 10, { status: 'interrupted' }),
    runRecord('10:10:00', '10:11:00', 0, 10, { status: 'running' }),
  ], selectedDate);
  assert(runningRow.textContent.includes('中断 1 段') && runningRow.textContent.includes('进行中 1 段'));

  for (const boundary of [
    runRecord('2026-10-04 23:58:00', '2026-10-04 23:59:00', 0),
    runRecord('invalid', '10:09:00', 0),
    runRecord('10:02:00', '10:01:00', 0),
  ]) {
    const separated = context.statisticsRunGroups([
      runRecord('10:00:00', '10:02:00', 0), boundary, runRecord('10:10:00', '10:11:00', 0),
    ], selectedDate);
    assert.equal(separated.length, 3, 'Wrong-day, malformed or reversed intervals must not be bridged');
  }
  const overlapping = [runRecord('10:00:00', '10:02:00', 0), runRecord('10:01:30', '10:02:30', 0)];
  assert.equal(context.statisticsRunGroups(overlapping, selectedDate).length, 2,
    'Overlapping intervals must stay in separate display groups');
  assert.equal(JSON.stringify(runs), before, 'Grouping and rendering must not mutate original run records');

  const otherRuns = [runRecord('11:00:00', '11:01:00', 0), runRecord('11:10:00', '11:11:00', 0)];
  const makeTask = (records, battleCount) => ({ run_count: records.length,
    total_duration_seconds: records.reduce((total, record) => total + record.duration_seconds, 0),
    available_metrics: ['run_count', 'total_duration_seconds', 'battle_count'], battle: { count: battleCount },
    completed_run_count: records.length, runs: records });
  const fixture = freezeDeep({ script_name: '演示一', date: selectedDate, total_runtime_seconds: 100,
    total_task_run_count: 9, total_battle_count: 5,
    available_metrics: ['total_runtime_seconds', 'total_task_run_count', 'total_battle_count'],
    tasks: { Chess: { ...makeTask(runs, 5), runs_truncated: true }, Other: makeTask(otherRuns, 0) } });
  const fixtureBefore = JSON.stringify(fixture);
  Object.assign(stats, { date: selectedDate, dates: [selectedDate], data: fixture, complete: true,
    source: dates.source, openTasks: new Set(['Chess', 'Other']) });
  nodes['statistics-content'].scrollTop = 127;
  context.renderStatistics();
  const content = nodes['statistics-content'];
  const cards = descendants(content, (node) => node.className === 'statistics-task');
  assert.equal(cards.length, 2);
  const chess = cards.find((card) => card.dataset.task === 'Chess');
  const other = cards.find((card) => card.dataset.task === 'Other');
  assert.equal(descendants(chess, (node) => node.className === 'statistics-run').length, 4);
  assert.equal(descendants(other, (node) => node.className === 'statistics-run').length, 1,
    'Separate task cards must never share a merged interval');
  assert(chess.textContent.includes('7段运行') && chess.textContent.includes('确认结算 5 次'));
  assert(chess.textContent.includes('部分运行明细已省略'));
  assert(content.textContent.includes('运行记录9段') && content.textContent.includes('确认结算5次'),
    'Summary counts must describe original intervals, not merged display rows');
  assert.equal(content.scrollTop, 127); assert(chess.open && other.open);
  assert.equal(JSON.stringify(fixture), fixtureBefore, 'Rendering must not alter totals or underlying data');
  Object.assign(stats, { date: '', dates: [], data: null, source: null, openTasks: new Set() });
}

async function verify() {
  verifyRunGrouping();
  respond = async (requestPath) => requestPath.endsWith('/dates') ? dates : day;
  context.syncStatisticsVisibility(); await flush();
  assert.equal(stats.date, '2026-10-05');
  let text = nodes['statistics-content'].textContent;
  assert(text.includes('已记录耗时未记录'));
  assert(text.includes('1段运行 · 未记录'));
  assert(text.includes('未记录结束') && text.includes('观测至 16:45:00'));
  assert(!text.includes('0秒') && !text.includes('进行中'));
  assert(nodes['statistics-source'].textContent.includes('本地日志解析'));
  assert(!nodes['statistics-source'].textContent.includes('合成统计口径'));
  assert(nodes['statistics-method-detail'].textContent.includes('合成统计口径'));
  assert.equal(timers.size, 1); assert.equal([...timers.values()][0].delay, 3000);
  const requestsBefore = calls.length;
  context.syncStatisticsVisibility(); await flush();
  assert.equal(calls.length, requestsBefore, 'Already visible panel must not spawn duplicate requests');
  assert.equal(context.statisticsMetric({ available_metrics: ['duration_seconds'], duration_seconds: 0 }, 'duration_seconds'), 0);

  // A completed control operation must not lose its statistics refresh just
  // because an earlier read is still pending. Collapse multiple requests into
  // one follow-up rather than cancelling/restarting each read.
  let resolveSlow;
  const beforeSlow = calls.length;
  respond = async () => new Promise((resolve) => { resolveSlow = resolve; });
  const slow = context.loadStatistics({ quiet: true }); await flush();
  context.requestStatisticsRefresh(); context.requestStatisticsRefresh();
  await context.loadStatistics({ quiet: true });
  assert.equal(calls.length, beforeSlow + 1, 'Cached dates and one in-flight day request avoid duplicate reads');
  assert(!calls.at(-1).path.endsWith('/dates'), 'Automatic updates need not re-read unchanged dates each cycle');
  assert(!calls.at(-1).signal.aborted, 'Queued operation refresh must allow the pending read to finish');
  resolveSlow({ ...day, total_battle_count: 4 }); await slow;
  assert.equal(stats.data.total_battle_count, 4);
  assert.equal([...timers.values()][0].delay, 0, 'A queued operation refresh must run immediately after the pending read');
  const [followId, follow] = [...timers.entries()][0]; timers.delete(followId);
  respond = async () => ({ ...day, total_battle_count: 5 });
  await follow.callback();
  assert.equal(calls.length, beforeSlow + 2, 'Repeated refresh triggers must collapse into one follow-up');
  assert.equal(stats.data.total_battle_count, 5);
  assert.equal([...timers.values()][0].delay, 3000);
  assert(nodes['statistics-status'].textContent.includes('每 3 秒刷新'));

  const previous = stats.data;
  respond = async () => { throw new Error('故障测试'); };
  await context.loadStatistics({ quiet: true });
  assert.strictEqual(stats.data, previous);
  assert(nodes['statistics-error'].textContent.includes('上次成功读取'));

  respond = async () => ({ ...dates, dates: [], statistics_complete: false, source: { ...dates.source, backfill_pending: true } });
  await context.loadStatistics();
  assert(nodes['statistics-content'].textContent.includes('正在整理历史统计'));
  assert(!nodes['statistics-content'].textContent.includes('暂无历史统计'));

  respond = async () => ({ ...dates, dates: [] });
  await context.loadStatistics();
  assert.equal(stats.data, null); assert.equal(stats.error, '');
  assert(nodes['statistics-content'].textContent.includes('暂无历史统计'));
  respond = async () => ({ ...dates, dates: [], supported: false });
  await context.loadStatistics();
  assert(nodes['statistics-content'].textContent.includes('当前统计来源不可用'));

  respond = async (requestPath) => requestPath.endsWith('/dates') ? dates : { ...day, script_name: '别的配置' };
  await context.loadStatistics();
  assert(stats.error.includes('统计配置与所选配置不一致'));
  respond = async () => ({ dates: 'invalid' });
  await context.loadStatistics();
  assert(stats.error.includes('响应格式不正确'));

  respond = async (requestPath) => requestPath.endsWith('/dates') ? dates : day;
  await context.loadStatistics(); stats.date = '2026-10-04'; stats.data = null;
  await context.loadStatistics();
  assert(calls.at(-1).path.endsWith('date=2026-10-04'));

  let resolveOld;
  respond = async (requestPath) => requestPath.endsWith('/dates') ? dates : new Promise((resolve) => { resolveOld = resolve; });
  const pending = context.loadStatistics(); await flush();
  const signal = calls.at(-1).signal;
  document.hidden = true; context.syncStatisticsVisibility();
  assert(signal.aborted); assert.equal(timers.size, 0); assert.equal(stats.active, false);
  resolveOld({ ...day, total_task_run_count: 999 }); await pending;
  assert.notEqual(stats.data.total_task_run_count, 999, 'Late hidden-page result must be discarded');

  document.hidden = false;
  respond = async (requestPath) => requestPath.endsWith('/dates') ? dates : new Promise((resolve) => { resolveOld = resolve; });
  context.syncStatisticsVisibility(); await flush();
  const late = resolveOld, oldSignal = calls.at(-1).signal;
  state.account = '演示二'; state.accountEpoch++;
  respond = async () => ({ script_name: '演示二', dates: [] });
  context.syncStatisticsVisibility(); await flush();
  assert(oldSignal.aborted);
  late({ ...day, total_task_run_count: 888 }); await flush();
  assert.equal(stats.account, '演示二'); assert.equal(stats.data, null);
  assert(nodes['statistics-source'].textContent.startsWith('演示二'));

  const beforeHidden = calls.length;
  nodes['logs-panel'].hidden = true; context.syncStatisticsVisibility();
  assert.equal(timers.size, 0);
  await context.loadStatistics();
  assert.equal(calls.length, beforeHidden, 'Compact layout must stop polling when statistics is hidden');
  console.log('PASS: empty-interval grouping, positive boundaries, unknown metrics, durations, ordering, date/status safeguards, unchanged summaries and data; statistics controller regressions.');
}

verify().catch((error) => { console.error(error); process.exitCode = 1; });
