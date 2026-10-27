'use strict';
// Fake network and clock: no HTTP calls, account writes or browser required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const dashboard = fs.readFileSync('static/daily-feedback.js', 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
const response = (data, status = 200) => ({status, ok: status >= 200 && status < 300, json: async () => data});

function harness() {
  const timers = new Map(), calls = []; let timerId = 0;
  const context = {
    AbortController, state: {csrf: 'test', bootEpoch: 1}, calls,
    clone: value => JSON.parse(JSON.stringify(value)), equal: (a, b) => JSON.stringify(a) === JSON.stringify(b),
    resetSession() { context.resets = (context.resets || 0) + 1; },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); },
    fetch(path, options) { const result = deferred(); calls.push({path, options, ...result}); return result.promise; },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  class ApiError'), source.indexOf('  function transferBusy')), context);
  vm.runInContext(source.slice(source.indexOf('  async function requestJson('), source.indexOf('  function toast(')), context);
  return {context, calls, timers, expire() { const [id, fn] = timers.entries().next().value; timers.delete(id); fn(); }};
}

async function main() {
  {
    const h = harness(), pending = h.context.api('/api/read');
    const check = assert.rejects(pending, /超时/);
    h.expire(); await check;
    assert.equal(h.calls[0].options.signal.aborted, true);
    h.calls[0].resolve(response({}, 401)); await flush();
    assert.equal(h.context.resets, undefined, 'late unauthorized response must not reset the page');
    assert.equal(h.timers.size, 0);
  }
  {
    const h = harness(), pending = h.context.api('/api/read');
    h.calls[0].resolve({status: 200, ok: true, json: () => new Promise(() => {})});
    await flush(); const check = assert.rejects(pending, /超时/); h.expire(); await check;
  }
  {
    const h = harness(), controller = new AbortController();
    const pending = h.context.api('/api/read', {signal: controller.signal});
    const check = assert.rejects(pending, error => error.name === 'AbortError');
    controller.abort(); await check; assert.equal(h.timers.size, 0);
    await assert.rejects(h.context.api('/api/write', {method: 'POST', signal: controller.signal}), error => error.name === 'AbortError');
    assert.equal(h.calls.length, 1, 'pre-cancelled mutation must never be sent');
  }
  {
    const h = harness(), path = '/api/accounts/02/settings/DailyTrifles/scheduler/next_run';
    const pending = h.context.api(path, {method: 'PUT', body: {value: '2026-10-14 08:00:00'}});
    h.expire(); await flush();
    assert.equal(h.calls.length, 2);
    assert.equal(h.calls[1].options.method, 'GET');
    assert.equal(h.calls[1].path, '/api/accounts/02/settings/DailyTrifles');
    h.calls[1].resolve(response({scheduler: [{name: 'next_run', value: '2026-10-14 08:00:00'}]}));
    assert.equal((await pending).confirmed_by_readback, true);
    h.calls[0].resolve(response({ok: false})); await flush();
    assert.equal(h.calls.filter(call => call.options.method === 'PUT').length, 1);
  }
  {
    const h = harness(), pending = h.context.api('/api/accounts/02/settings/Task/config/value', {method: 'PUT', body: {value: 2}});
    const check = assert.rejects(pending, error => error.uncertain && /勿重复提交/.test(error.message));
    h.calls[0].reject(new Error('network changed')); await flush();
    h.calls[1].resolve(response({config: [{name: 'value', value: 1}]})); await check;
    assert.equal(h.calls.length, 2);
  }
  {
    const h = harness(), pending = h.context.api('/api/accounts/02/actions', {method: 'POST', body: {action: 'start'}});
    h.calls[0].resolve(response({}, 502)); await flush();
    h.calls[1].resolve(response({connected: true, state: 1}));
    assert.equal((await pending).state, 1);
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  }
  {
    const h = harness(), pending = h.context.api('/api/accounts/02/actions', {method: 'POST', body: {action: 'stop'}});
    const check = assert.rejects(pending, error => error.uncertain === true);
    h.expire(); await flush(); h.expire(); await check;
    assert.equal(h.calls.length, 2, 'timed-out readback must not trigger another read or write');
    assert.equal(h.timers.size, 0);
  }
  {
    const h = harness(), pending = h.context.api('/api/config/import', {method: 'POST', body: {name: 'new'}});
    const check = assert.rejects(pending, error => error.uncertain === true);
    h.expire(); await flush(); assert.equal(h.calls[1].path, '/api/bootstrap');
    h.calls[1].resolve(response({accounts: ['new']})); await check;
  }
  {
    const h = harness(), notices = [];
    Object.assign(h.context.state, {authenticated: true, account: '02', accountEpoch: 1, taskEpoch: 1, task: '', actionBusy: false});
    Object.assign(h.context, {
      canWaitTaskNow: () => true, dirtyFields: () => [], stopSnapshotRefresh() {}, renderSelectedAccount() {},
      renderTasks() {}, syncSnapshotVisibility() {}, toast: message => notices.push(message),
      encoder: encodeURIComponent, taskLabel: value => value, readOnly: () => false,
      chooseWaitTime: async () => '2026-10-14 08:00:00',
    });
    vm.runInContext(source.slice(source.indexOf('  async function waitTaskNow('), source.indexOf('  function chooseWaitTime(')), h.context);
    const first = h.context.waitTaskNow('Task');
    assert.equal(h.context.state.actionBusy, true); h.expire(); await first;
    assert.equal(h.context.state.actionBusy, false); assert.match(notices.at(-1), /超时/);
    const second = h.context.waitTaskNow('Task');
    h.calls[1].resolve(response({scheduler: [{name: 'next_run', value: 'old'}]})); await flush();
    assert.equal(h.calls[2].options.method, 'PUT'); h.expire(); await flush();
    h.calls[3].resolve(response({scheduler: [{name: 'next_run', value: '2026-10-14 08:00:00'}]})); await second;
    assert.equal(h.context.state.actionBusy, false); assert.match(notices.at(-1), /已调整为等待/);
    assert.equal(h.calls.filter(call => call.options.method === 'PUT').length, 1);
  }
  {
    const h = harness(), nodes = new Map(), rendered = [];
    h.context.document = {getElementById(id) { if (!nodes.has(id)) nodes.set(id, {close() {}}); return nodes.get(id); }};
    Object.assign(h.context, {
      state: {day: '2026-10-07', today: '2026-10-08', loading: false, requestId: 0, selected: null},
      visibleData: data => data, renderSummary() {}, renderRows: data => rendered.push(data.date), updateUrl() {},
    });
    vm.runInContext(dashboard.slice(dashboard.indexOf('  async function requestReport('), dashboard.indexOf('  function moveDay(')), h.context);
    const pending = h.context.refresh(); h.expire(); await pending;
    assert.equal(h.context.state.loading, false);
    assert.equal(nodes.get('refresh').disabled, false);
    assert.match(nodes.get('error').textContent, /超时/);
    h.calls[0].resolve(response({date: '2026-10-07', today: '2026-10-08', accounts: []})); await flush();
    assert.deepEqual(rendered, []);
    const second = h.context.refresh(); h.context.setDay('2026-10-08'); await flush();
    assert.equal(h.calls[1].options.signal.aborted, true);
    h.calls[1].resolve(response({date: '2026-10-07', today: '2026-10-08', accounts: []}));
    h.calls[2].resolve(response({date: '2026-10-08', today: '2026-10-08', accounts: []}));
    await second; await flush();
    assert.deepEqual(rendered, ['2026-10-08']);
    assert.equal(h.context.state.loading, false);
    assert.equal(h.timers.size, 0);
  }
  console.log('PASS: 11 bounded network scenarios, write readback without resubmission, busy cleanup, stale response and dashboard cancellation.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
