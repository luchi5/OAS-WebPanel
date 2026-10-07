'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    csrf: '', username: '', authenticated: false, mustChangePassword: false, loginRequired: true,
    backendOnline: false, backendName: 'OAS 后端', domain: '',
    accounts: [], labels: {}, menu: [], snapshots: new Map(), snapshotVersions: new Map(),
    snapshotRefresh: { epoch: 0, timer: null, request: null, busy: false, cursor: 0, full: false },
    account: '', accountEpoch: 0, task: '', taskEpoch: 0, category: '', taskScope: 'schedule',
    openCategories: new Set(['Script']), focusTask: '', logFilter: 'all', logLevel: 'ALL', accountFilter: 'all', taskFilter: 'all', activity: 'logs',
    fields: [], saving: false, actionBusy: false, loadingSettings: false, taskEnableOverrides: new Map(),
    socket: null, reconnectTimer: null, reconnectAttempt: 0,
    logs: [], pendingLogs: [], followLogs: true, logFrame: 0, errors: [], errorEpoch: 0,
    errorDetail: null, bootEpoch: 0, confirmResolve: null,
    transfer: { epoch: 0, busy: false, supported: false, request: null },
    statistics: { account: '', epoch: 0, active: false, request: null, timer: null, loading: false, refreshPending: false, dates: [], datesUpdatedAt: null, date: '', data: null, source: null, complete: true, error: '', unsupported: false, updatedAt: null, openTasks: new Set() },
  };
  const MAX_LOGS = 3000;
  const icons = { power: 0xf00b8, more: 0xe404, bolt: 0xf5ca, layers: 0xf847, clock: 0xf012b, flash: 0xf76d, tune: 0xf0258 };
  const icon = (name) => { const node = el('span', 'mi', String.fromCodePoint(icons[name])); node.setAttribute('aria-hidden', 'true'); return node; };
  const iconButton = (name, title, action) => {
    const button = el('button', 'icon-button'); button.type = 'button'; button.title = title; button.setAttribute('aria-label', title); button.append(icon(name));
    if (action) button.addEventListener('click', action); return button;
  };
  const encoder = (value) => encodeURIComponent(String(value));
  const clone = (value) => value === undefined ? null : JSON.parse(JSON.stringify(value));
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const clean = (value) => typeof value === 'string' && !/^[\w.:-]+_help$/i.test(value.trim()) ? value : '';
  const label = (key, fallback) => clean(state.labels[key]) || clean(state.labels[fallback]) || clean(fallback) || String(key || '');
  const taskLabel = (task) => label(task, state.menu.find((entry) => entry.name === task)?.title);
  const dirtyFields = () => state.fields.filter((field) => field.dirty);

  class ApiError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }

  function responseMessage(data, fallback) {
    if (typeof data?.detail === 'string') return data.detail;
    if (typeof data?.message === 'string') return data.message;
    if (typeof data?.error === 'string') return data.error;
    if (Array.isArray(data?.detail)) return data.detail.map((item) => item.msg || '').filter(Boolean).join('；') || fallback;
    return fallback;
  }

  function transferBusy(busy) {
    state.transfer.busy = busy;
    ['transfer-kind', 'transfer-account', 'transfer-task', 'transfer-name', 'transfer-file', 'transfer-close'].forEach((id) => { $(id).disabled = busy; });
    ['transfer-backup', 'transfer-share', 'transfer-import'].forEach((id) => { $(id).disabled = busy || !state.transfer.supported; });
  }

  function renderTransferScope() {
    const task = $('transfer-kind').value === 'task';
    $('transfer-task-row').hidden = !task;
    $('transfer-name-row').hidden = task;
    $('transfer-import').textContent = task ? '导入任务参数' : '导入为新配置';
    $('transfer-import-hint').textContent = task ? '只替换所选任务的参数，保留启用状态；请先停止目标配置。文件最大 2 MiB。' : '不覆盖已有配置。文件最大 2 MiB。';
  }

  async function openTransfer(taskScope = false) {
    if (!state.authenticated || state.mustChangePassword) return;
    if ($('preferences-dialog').open) $('preferences-dialog').close();
    const transfer = state.transfer, epoch = ++transfer.epoch;
    transfer.request?.abort(); transfer.request = new AbortController(); transfer.supported = false;
    $('transfer-error').hidden = true; $('transfer-status').textContent = '正在检查备份功能…';
    $('transfer-kind').value = taskScope ? 'task' : 'config';
    $('transfer-account').replaceChildren();
    state.accounts.forEach((name) => { const option = el('option', '', name); option.value = name; $('transfer-account').append(option); });
    $('transfer-account').value = state.account || state.accounts[0] || '';
    $('transfer-task').replaceChildren();
    state.menu.forEach((entry) => { const option = el('option', '', taskLabel(entry.name)); option.value = entry.name; $('transfer-task').append(option); });
    $('transfer-task').value = state.task || state.menu[0]?.name || '';
    $('transfer-name').value = ''; $('transfer-file').value = '';
    renderTransferScope(); transferBusy(false); $('transfer-dialog').showModal();
    try {
      const capability = await api('/api/transfer/capabilities', { signal: transfer.request.signal });
      if (epoch !== transfer.epoch || !$('transfer-dialog').open) return;
      transfer.supported = capability?.version === 1 && capability?.config_import === true && capability?.task_import === true && capability?.modes?.includes('backup') && capability?.modes?.includes('share');
      if (!transfer.supported) throw new Error('当前后台暂不支持备份与导入。');
      $('transfer-status').textContent = '备份功能已就绪。';
    } catch (error) {
      if (epoch !== transfer.epoch || error.name === 'AbortError') return;
      $('transfer-status').textContent = ''; $('transfer-error').textContent = error.message; $('transfer-error').hidden = false;
    } finally { if (epoch === transfer.epoch) transferBusy(false); }
  }

  async function exportTransfer(mode) {
    const transfer = state.transfer;
    if (transfer.busy || !transfer.supported) return;
    const epoch = transfer.epoch, account = $('transfer-account').value;
    const taskScope = $('transfer-kind').value === 'task';
    const task = taskScope ? $('transfer-task').value : '';
    if (!account || (taskScope && !task) || (task && !state.menu.some((entry) => entry.name === task))) return;
    const path = `/api/accounts/${encoder(account)}${task ? `/tasks/${encoder(task)}` : ''}/export?mode=${mode}`;
    transfer.request?.abort(); transfer.request = new AbortController(); transferBusy(true);
    $('transfer-error').hidden = true; $('transfer-status').textContent = '正在准备导出文件…';
    try {
      const data = await api(path, { signal: transfer.request.signal });
      if (epoch !== transfer.epoch || !$('transfer-dialog').open) return;
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
      const url = URL.createObjectURL(blob), link = el('a');
      link.href = url; link.download = `${account}${task ? `-${task}` : ''}-${mode}.json`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      $('transfer-status').textContent = '文件已下载。';
    } catch (error) {
      if (epoch !== transfer.epoch || error.name === 'AbortError') return;
      $('transfer-status').textContent = ''; $('transfer-error').textContent = error.message; $('transfer-error').hidden = false;
    } finally { if (epoch === transfer.epoch) transferBusy(false); }
  }

  async function importTransfer() {
    const transfer = state.transfer;
    if (transfer.busy || !transfer.supported) return;
    const epoch = transfer.epoch, file = $('transfer-file').files?.[0];
    const taskScope = $('transfer-kind').value === 'task';
    const task = taskScope ? $('transfer-task').value : '';
    const account = task ? $('transfer-account').value : $('transfer-name').value.trim();
    let importSucceeded = false;
    $('transfer-error').hidden = true;
    try {
      if (!file) throw new Error('请先选择 JSON 文件。');
      if (file.size > 2 * 1024 * 1024) throw new Error('文件不能超过 2 MiB。');
      if (taskScope && (!task || !state.menu.some((entry) => entry.name === task))) throw new Error('请选择有效的目标任务。');
      if (!account) throw new Error(task ? '请选择目标配置。' : '请输入新配置名称。');
      if (!task && state.accounts.includes(account)) throw new Error('名称已存在，请使用新名称。');
      if (task && dirtyFields().length) throw new Error('请先保存或取消当前参数修改，再导入任务。');
      transferBusy(true);
      const text = (await file.text()).replace(/^\uFEFF/, '');
      if (epoch !== transfer.epoch || !$('transfer-dialog').open) return;
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('文件必须包含 JSON 配置对象。');
      const accepted = await confirmAction(task ? '导入任务参数' : '导入新配置', task ? `将文件“${file.name}”导入 ${account} 的“${taskLabel(task)}”；不会启动任务。` : `将文件“${file.name}”导入为新配置“${account}”；所有任务默认停止。`, '导入');
      if (!accepted || epoch !== transfer.epoch || !$('transfer-dialog').open) return;
      transfer.request?.abort(); transfer.request = new AbortController();
      $('transfer-status').textContent = '正在导入…';
      const path = task ? `/api/accounts/${encoder(account)}/tasks/${encoder(task)}/import` : '/api/config/import';
      const result = await api(path, { method: 'POST', body: task ? { json_text: text } : { name: account, json_text: text }, signal: transfer.request.signal });
      importSucceeded = true;
      if (epoch !== transfer.epoch || !$('transfer-dialog').open) return;
      $('transfer-file').value = '';
      $('transfer-status').textContent = task ? '任务参数已导入，启用状态保持原样。' : '新配置已导入，所有任务保持停止。';
      if (result?.warnings?.length) toast(result.warnings.join('；'), 'error');
      await bootstrap({ keepAccount: true });
      if (task && account === state.account && task === state.task) await selectTask(task, true);
    } catch (error) {
      if (epoch !== transfer.epoch || error.name === 'AbortError') return;
      $('transfer-status').textContent = importSucceeded ? '文件已导入。' : '';
      $('transfer-error').textContent = importSucceeded ? '导入已保存，但页面刷新失败，请刷新面板查看，无需再次导入。' : error instanceof SyntaxError ? 'JSON 文件格式不正确。' : error.message;
      $('transfer-error').hidden = false;
    } finally { if (epoch === transfer.epoch) transferBusy(false); }
  }

  async function api(path, options = {}) {
    const method = options.method || 'GET';
    const headers = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (method !== 'GET' && method !== 'HEAD' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
    let response;
    try {
      response = await fetch(path, {
        method, headers, credentials: 'same-origin', cache: 'no-store',
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: options.signal,
      });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new ApiError('连接失败，请检查网络后重试。');
    }
    let data = null;
    if (response.status !== 204) {
      try { data = await response.json(); } catch (_) { /* Handled below for unsuccessful responses. */ }
    }
    if (!response.ok) {
      if (response.status === 401 && path !== '/api/login') resetSession();
      const message = responseMessage(data, response.status === 409
        ? '内容或状态已改变，请刷新后重试。'
        : response.status === 401 ? '登录已失效，请重新登录。' : `请求未成功（${response.status}），请重试。`);
      throw new ApiError(message, response.status);
    }
    return data;
  }

  function toast(message, kind = 'success', retry) {
    const node = el('div', `toast ${kind}`);
    node.append(el('span', '', message));
    if (retry) {
      const button = el('button', '', '重试');
      button.type = 'button';
      button.addEventListener('click', () => { node.remove(); retry(); });
      node.append(button);
    }
    const close = el('button', 'toast-close', '×');
    close.type = 'button'; close.setAttribute('aria-label', '关闭提示');
    close.addEventListener('click', () => node.remove()); node.append(close);
    $('toasts').append(node);
    while ($('toasts').children.length > 4) $('toasts').firstElementChild.remove();
    setTimeout(() => node.remove(), kind === 'error' ? 14000 : 5000);
  }

  function empty(container, title, message, retry) {
    const wrap = el('div', 'empty-state');
    wrap.append(el('span', 'empty-symbol', '◇'), el('h3', '', title), el('p', '', message));
    if (retry) {
      const button = el('button', 'button small', '重试'); button.type = 'button';
      button.addEventListener('click', retry); wrap.append(button);
    }
    container.replaceChildren(wrap);
  }

  function confirmAction(title, message, accept = '确定', danger = false) {
    if (state.confirmResolve) return Promise.resolve(false);
    $('confirm-title').textContent = title;
    $('confirm-message').textContent = message;
    $('confirm-accept').textContent = accept;
    $('confirm-accept').className = `button ${danger ? 'danger' : 'primary'}`;
    $('confirm-dialog').returnValue = '';
    $('confirm-dialog').showModal();
    $('confirm-cancel').focus();
    return new Promise((resolve) => { state.confirmResolve = resolve; });
  }
  $('confirm-dialog').addEventListener('close', () => {
    const resolve = state.confirmResolve; state.confirmResolve = null;
    if (resolve) resolve($('confirm-dialog').returnValue === 'confirm');
  });

  async function mayDiscard() {
    if (state.saving) { toast('正在保存，请稍候。', 'error'); return false; }
    if (!dirtyFields().length) return true;
    return confirmAction('放弃未保存的修改？', `「${taskLabel(state.task)}」有 ${dirtyFields().length} 项修改尚未保存。离开后，这些修改将被放弃。`, '放弃修改');
  }

  function showMobile(panel) {
    $('app').dataset.mobilePanel = panel;
    document.querySelectorAll('[data-mobile-target]').forEach((button) => {
      button.classList.toggle('active', button.dataset.mobileTarget === panel);
      button.setAttribute('aria-current', button.dataset.mobileTarget === panel ? 'page' : 'false');
    });
    syncStatisticsVisibility();
  }

  function applySession(session) {
    state.authenticated = Boolean(session?.authenticated ?? true);
    state.username = session?.username || state.username;
    if (typeof session?.csrf === 'string') state.csrf = session.csrf;
    if (typeof session?.must_change_password === 'boolean') state.mustChangePassword = session.must_change_password;
    if (typeof session?.login_required === 'boolean') state.loginRequired = session.login_required;
    if (!state.loginRequired) state.mustChangePassword = false;
    $('current-user').textContent = state.username;
    ['password-open', 'logout', 'mobile-password-open', 'account-preferences'].forEach((id) => {
      const node = $(id); if (node) node.hidden = !state.loginRequired;
    });
    if (!state.loginRequired) $('login-screen').hidden = true;
  }

  function storeSnapshot(name, snapshot) {
    const changed = !equal(state.snapshots.get(name), snapshot);
    state.snapshots.set(name, snapshot);
    state.snapshotVersions.set(name, (state.snapshotVersions.get(name) || 0) + 1);
    return changed;
  }

  function snapshotsVisible() {
    return state.authenticated && !document.hidden && !$('app').hidden;
  }

  function stopSnapshotRefresh() {
    const refresh = state.snapshotRefresh;
    refresh.epoch++;
    clearTimeout(refresh.timer); refresh.timer = null;
    refresh.request?.abort(); refresh.request = null; refresh.busy = false;
  }

  function syncSnapshotVisibility({ force = false, full = false } = {}) {
    if (!snapshotsVisible()) { stopSnapshotRefresh(); return; }
    if (force) stopSnapshotRefresh();
    const refresh = state.snapshotRefresh;
    refresh.full ||= full;
    if (!refresh.busy) {
      clearTimeout(refresh.timer); refresh.timer = null;
      refreshSnapshots();
    }
  }

  async function refreshSnapshots() {
    const refresh = state.snapshotRefresh;
    if (!snapshotsVisible() || refresh.busy) return;
    clearTimeout(refresh.timer); refresh.timer = null;
    if (state.actionBusy || state.saving) { refresh.timer = setTimeout(refreshSnapshots, 2000); return; }
    const epoch = refresh.epoch, bootEpoch = state.bootEpoch, accountEpoch = state.accountEpoch;
    const selected = state.account, request = new AbortController();
    const current = () => epoch === refresh.epoch && bootEpoch === state.bootEpoch && accountEpoch === state.accountEpoch && snapshotsVisible();
    refresh.request = request; refresh.busy = true;
    const others = state.accounts.filter((name) => name !== selected);
    // A snapshot briefly opens an upstream connection. Rotate two other accounts
    // per cycle instead of repeatedly opening one connection for every account.
    const queue = selected ? [selected] : [];
    if (refresh.full) { queue.push(...others); refresh.full = false; }
    else if (others.length) {
      const count = Math.min(2, others.length);
      for (let index = 0; index < count; index++) queue.push(others[(refresh.cursor + index) % others.length]);
      refresh.cursor = (refresh.cursor + count) % others.length;
    }
    const workers = Array.from({ length: Math.min(2, queue.length) }, async () => {
      while (queue.length && current()) {
        const name = queue.shift(), version = state.snapshotVersions.get(name) || 0;
        try {
          const snapshot = await api(`/api/accounts/${encoder(name)}/snapshot`, { signal: request.signal });
          if (!current() || version !== (state.snapshotVersions.get(name) || 0)) continue;
          const changed = storeSnapshot(name, snapshot);
          if (name === selected) {
            const connectionChanged = state.backendOnline !== Boolean(snapshot.connected);
            state.backendOnline = Boolean(snapshot.connected);
            if (changed || connectionChanged) { renderTasks(); updateConnection(); }
          }
          if (changed) renderAccounts();
        } catch (error) {
          if (!current() || error.name === 'AbortError' || version !== (state.snapshotVersions.get(name) || 0)) continue;
          const changed = storeSnapshot(name, { ...(state.snapshots.get(name) || {}), connected: false });
          if (changed) renderAccounts();
          if (name === selected) {
            state.backendOnline = false; updateConnection(error.message);
          }
          // An unavailable account keeps its last known state until a later cycle.
        }
      }
    });
    try { await Promise.all(workers); }
    finally {
      if (epoch === refresh.epoch) {
        refresh.busy = false; refresh.request = null;
        if (current()) refresh.timer = setTimeout(refreshSnapshots, refresh.full ? 0 : 2000);
      }
    }
  }

  function disconnectSocket() {
    clearTimeout(state.reconnectTimer); state.reconnectTimer = null;
    if (state.socket) { const socket = state.socket; state.socket = null; socket.onclose = null; socket.close(); }
  }

  function resetSession() {
    stopSnapshotRefresh();
    state.transfer.epoch++; state.transfer.request?.abort(); state.transfer.supported = false;
    if ($('transfer-dialog').open) $('transfer-dialog').close();
    resetStatistics();
    state.accountEpoch++; state.taskEpoch++; state.bootEpoch++; state.errorEpoch++;
    disconnectSocket();
    state.authenticated = false; state.csrf = ''; state.username = '';
    state.account = ''; state.task = ''; state.fields = []; state.logs = []; state.pendingLogs = [];
    state.snapshots.clear(); state.snapshotVersions.clear(); state.accounts = []; state.errorDetail = null;
    state.taskEnableOverrides.clear();
    state.errors = []; state.saving = false; state.actionBusy = false;
    resetErrors();
    document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
    $('password-form').reset(); $('login-password').value = '';
    $('app').hidden = true; $('loading-screen').hidden = state.loginRequired; $('login-screen').hidden = !state.loginRequired;
  }

  function normaliseMenu(menu) {
    const entries = [];
    Object.entries(menu || {}).forEach(([category, tasks]) => {
      if (!Array.isArray(tasks)) return;
      tasks.forEach((task) => {
        const name = typeof task === 'string' ? task : task?.name || task?.task || task?.key;
        if (name && !entries.some((entry) => entry.name === name)) entries.push({ name, category, title: task?.title || '' });
      });
    });
    return entries;
  }

  async function bootstrap({ keepAccount = true } = {}) {
    stopSnapshotRefresh();
    const epoch = ++state.bootEpoch;
    const data = await api('/api/bootstrap');
    if (epoch !== state.bootEpoch || !state.authenticated) return;
    applySession(data);
    state.accounts = (data.accounts || []).map((name) => String(name));
    state.labels = data.labels || {};
    state.menu = normaliseMenu(data.menu);
    state.backendName = data.backend_name || 'OAS 后端'; state.domain = data.domain || '';
    state.backendOnline = Boolean(data.backend_online);
    $('backend-name').textContent = state.backendName;
    $('backend-domain').textContent = state.domain;
    $('loading-screen').hidden = true; $('login-screen').hidden = true; $('app').hidden = false;
    renderAccounts(); renderCategories(); renderTasks(); updateConnection();
    if (state.mustChangePassword) openPassword(true);
    if (!keepAccount || !state.accounts.includes(state.account)) {
      if (state.accounts.length) await selectAccount(state.accounts[0], true);
      else {
        state.account = ''; state.fields = []; state.task = ''; disconnectSocket();
        resetStatistics();
        resetErrors();
        renderSelectedAccount();
        empty($('task-list'), '还没有配置', '请先在 OAS 中创建一个配置，再刷新这里。');
      }
    }
    syncSnapshotVisibility({ full: true });
  }

  const stateInfo = (value) => ({
    0: { name: '已停止', className: 'neutral' },
    1: { name: '运行中', className: 'running' },
    2: { name: '需要关注', className: 'warning' },
    3: { name: '更新中', className: 'warning' },
  }[value] || { name: '等待连接', className: 'neutral' });

  function renderAccounts() {
    $('account-count').textContent = state.accounts.length;
    const counts = { running: 0, stopped: 0, attention: 0 };
    state.accounts.forEach((name) => { const snapshot = state.snapshots.get(name); if (!snapshot) return; if (snapshot.state === 1) counts.running++; else if (snapshot.state === 0) counts.stopped++; else if ([2, 3].includes(snapshot.state)) counts.attention++; });
    ['running', 'stopped', 'attention'].forEach((kind) => { $(`${kind}-count`).textContent = counts[kind]; });
    const search = $('account-search').value.trim().toLowerCase();
    const matches = state.accounts.filter((name) => name.toLowerCase().includes(search) && (state.accountFilter === 'all' || (state.accountFilter === 'running' ? state.snapshots.get(name)?.state === 1 : state.accountFilter === 'stopped' ? state.snapshots.get(name)?.state === 0 : [2, 3].includes(state.snapshots.get(name)?.state))));
    $('account-list').replaceChildren();
    if (!matches.length) { empty($('account-list'), '暂无配置', '试试其他名称或状态。'); return; }
    matches.forEach((name) => {
      const snapshot = state.snapshots.get(name), info = stateInfo(snapshot?.state);
      const row = el('div', `account-row${state.account === name ? ' selected' : ''}`); row.dataset.account = name;
      const button = el('button', 'account-select'); button.type = 'button'; button.dataset.testid = 'account-row';
      button.setAttribute('aria-pressed', String(state.account === name)); button.setAttribute('aria-label', `${name} ${snapshot ? info.name : '点击查看状态'}`);
      button.append(el('span', `account-accent ${snapshot?.state === 1 ? 'running' : [2, 3].includes(snapshot?.state) ? 'attention' : ''}`));
      const text = el('span', 'account-info'); text.append(el('span', 'account-name', name));
      const firstTask = scheduledTasks(snapshot)[0], status = firstTask ? scheduleForTask(firstTask, snapshot) : null;
      const meta = el('span', `account-meta ${status?.kind || ''}`);
      if (firstTask) meta.append(icon(status.kind === 'running' ? 'bolt' : status.kind === 'pending' ? 'layers' : 'clock'), document.createTextNode(`${taskLabel(firstTask)}${status.time ? ` ${status.time.split(' ').pop()}` : ''}`));
      else meta.textContent = snapshot ? (snapshot.state === 0 ? '暂无任务' : info.name) : '正在读取…';
      text.append(meta); button.append(text); button.addEventListener('click', () => selectAccount(name)); row.append(button);
      const actions = el('span', 'account-actions');
      const power = iconButton('power', `${name} ${snapshot && snapshot.state !== 0 ? '停止' : '启动'}`, async () => { await selectAccount(name); if (state.account === name) performAction(state.snapshots.get(name)?.state === 0 ? 'start' : 'stop'); });
      power.disabled = !snapshot?.connected || state.actionBusy || state.mustChangePassword || snapshot.state === 3;
      actions.append(power, iconButton('more', `${name} 更多操作`, () => openConfigMenu(name))); row.append(actions); $('account-list').append(row);
    });
  }

  function renderSelectedAccount() {
    $('selected-account').textContent = state.account || '请选择配置';
    const snapshot = state.snapshots.get(state.account);
    const info = stateInfo(snapshot?.state);
    const currentTask = runningTask(snapshot);
    $('selected-status').textContent = state.account ? info.name : '尚未选择';
    $('selected-status').className = `config-indicator ${info.className}`;
    $('selected-status').title = info.name; $('selected-status').setAttribute('aria-label', info.name);
    const connected = Boolean(snapshot?.connected && state.backendOnline);
    $('start-task').disabled = !state.account || !connected || state.actionBusy || state.mustChangePassword || snapshot?.state === 1;
    $('stop-task').disabled = !state.account || !connected || state.actionBusy || state.mustChangePassword || snapshot?.state === 0;
    $('power-task').disabled = !state.account || !connected || state.actionBusy || state.mustChangePassword || snapshot?.state === 3;
    $('power-task').title = snapshot && snapshot.state !== 0 ? '停止' : '启动'; $('power-task').setAttribute('aria-label', snapshot && snapshot.state !== 0 ? '停止配置' : '启动配置');
    const scheduled = scheduledTasks(snapshot);
    const queued = scheduled.filter((name) => name !== currentTask).length;
    state.focusTask = currentTask || scheduled[0] || '';
    $('task-focus').classList.toggle('is-running', Boolean(currentTask));
    $('focus-label').textContent = currentTask ? '正在执行' : '排程状态';
    $('focus-title').textContent = currentTask ? taskLabel(currentTask) : state.account ? (snapshot?.state === 0 ? '当前配置已停止' : info.name) : '从一个配置开始';
    $('focus-description').textContent = currentTask ? (queued ? `后续还有 ${queued} 项任务` : '任务进行中，日志会自动更新') : scheduled.length ? `已安排 ${scheduled.length} 项任务，启动排程后执行` : '在“全部功能”中选择任务并调整参数';
    $('focus-settings').disabled = !state.focusTask;
    $('schedule-summary').textContent = queued ? `${queued} 项待执行` : '';
    $('open-global-settings').disabled = !state.account || !state.menu.some((item) => item.name === 'GlobalGame');
    $('open-script-settings').disabled = !state.account || !state.menu.some((item) => item.name === 'Script');
  }

  function updateConnection(message) {
    const snapshot = state.snapshots.get(state.account);
    const connected = state.backendOnline && (!state.account || Boolean(snapshot?.connected));
    $('connection-dot').className = `status-dot ${connected ? '' : 'offline'}`;
    $('connection-banner').hidden = connected;
    $('connection-message').textContent = message || (state.backendOnline ? '当前配置连接已断开，正在尝试恢复。' : '暂时无法连接 OAS 服务，任务状态可能不是最新。');
    $('live-label').replaceChildren(el('span', `status-dot ${connected ? '' : 'neutral'}`), document.createTextNode(connected ? '实时接收' : '等待连接'));
    renderSelectedAccount(); updateSaveBar();
  }

  async function selectAccount(name, force = false) {
    if (name === state.account && !force) { showMobile('tasks'); return; }
    if (!force && !(await mayDiscard())) return;
    const epoch = ++state.accountEpoch; state.taskEpoch++;
    stopSnapshotRefresh();
    disconnectSocket(); state.reconnectAttempt = 0;
    state.account = name; state.task = ''; state.fields = []; state.logs = []; state.pendingLogs = [];
    resetStatistics();
    resetErrors();
    state.loadingSettings = false; state.category = ''; state.taskFilter = 'all'; state.taskScope = 'schedule'; state.errorDetail = null;
    $('task-search').value = ''; $('settings-tab').disabled = true;
    storeSnapshot(name, { ...(state.snapshots.get(name) || {}), connected: false });
    renderLogs(); renderAccounts(); renderSelectedAccount(); renderCategories(); renderTasks();
    showWorkTab('tasks'); showMobile('tasks'); updateSaveBar(); updateConnection('正在连接所选配置…');
    if (!$('errors-view').hidden) loadErrors();
    try {
      const snapshot = await api(`/api/accounts/${encoder(name)}/snapshot`);
      if (epoch !== state.accountEpoch || state.account !== name) return;
      storeSnapshot(name, snapshot); state.backendOnline = Boolean(snapshot.connected);
      renderAccounts(); renderSelectedAccount(); renderTasks(); updateConnection();
    } catch (error) {
      if (epoch !== state.accountEpoch) return;
      updateConnection(error.message);
      toast(error.message, 'error', reconnectCurrent);
    }
    if (epoch === state.accountEpoch && state.authenticated) {
      connectEvents(name, epoch); syncSnapshotVisibility();
    }
  }

  function connectEvents(name, epoch) {
    if (epoch !== state.accountEpoch || !state.authenticated) return;
    disconnectSocket();
    const url = new URL(`/api/accounts/${encoder(name)}/events`, window.location.href);
    url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    state.socket = socket;
    const current = () => epoch === state.accountEpoch && state.account === name && state.socket === socket;
    socket.onopen = () => { if (current()) state.reconnectAttempt = 0; };
    socket.onmessage = (event) => {
      if (!current()) return;
      let message; try { message = JSON.parse(event.data); } catch (_) { return; }
      const snapshot = state.snapshots.get(name) || { state: null, schedule: {}, connected: false };
      if (message.type === 'state') {
        snapshot.state = message.state; storeSnapshot(name, snapshot);
        renderAccounts(); renderSelectedAccount(); renderTasks();
      } else if (message.type === 'schedule') {
        snapshot.schedule = message.schedule || {}; storeSnapshot(name, snapshot); renderAccounts(); renderSelectedAccount(); renderTasks();
      } else if (message.type === 'log') {
        appendLog(typeof message.line === 'string' ? message.line : JSON.stringify(message.line ?? ''));
      } else if (message.type === 'connection') {
        snapshot.connected = Boolean(message.connected); state.backendOnline = Boolean(message.connected);
        storeSnapshot(name, snapshot); renderAccounts(); updateConnection(message.error ? String(message.error) : undefined);
      }
    };
    socket.onerror = () => { /* onclose performs the bounded reconnect. */ };
    socket.onclose = () => {
      if (!current()) return;
      state.socket = null;
      const snapshot = state.snapshots.get(name) || {};
      snapshot.connected = false; storeSnapshot(name, snapshot); renderAccounts(); updateConnection();
      const wait = Math.min(20000, 1500 * (2 ** Math.min(state.reconnectAttempt++, 4)));
      state.reconnectTimer = setTimeout(() => connectEvents(name, epoch), wait);
    };
  }

  async function reconnectCurrent() {
    if (!state.authenticated) return;
    const name = state.account, epoch = state.accountEpoch;
    stopSnapshotRefresh();
    $('reconnect').disabled = true;
    try {
      if (name) {
        const version = state.snapshotVersions.get(name) || 0;
        const snapshot = await api(`/api/accounts/${encoder(name)}/snapshot`);
        if (name !== state.account || epoch !== state.accountEpoch) return;
        if (version === (state.snapshotVersions.get(name) || 0)) {
          storeSnapshot(name, snapshot); state.backendOnline = Boolean(snapshot.connected);
        }
        renderAccounts(); renderTasks(); updateConnection(); connectEvents(name, epoch);
      } else await bootstrap();
    } catch (error) { if (epoch === state.accountEpoch) { updateConnection(error.message); toast(error.message, 'error', reconnectCurrent); } }
    finally { $('reconnect').disabled = false; syncSnapshotVisibility(); }
  }

  function renderCategories() {
    $('category-tabs').replaceChildren();
    $('category-tabs').hidden = state.taskScope !== 'catalog';
    ['schedule', 'catalog'].forEach((scope) => {
      $(`scope-${scope}`).classList.toggle('selected', state.taskScope === scope);
      $(`scope-${scope}`).setAttribute('aria-pressed', String(state.taskScope === scope));
    });
    const select = el('select'); select.setAttribute('aria-label', '筛选任务');
    [['all', '全部'], ['enabled', '已启用'], ['disabled', '未启用']].forEach(([value, title]) => {
      const option = el('option', '', title); option.value = value; option.selected = value === state.taskFilter; select.append(option);
    });
    select.addEventListener('change', () => { state.taskFilter = select.value; $('category-tabs').classList.toggle('is-filtered', state.taskFilter !== 'all'); renderTasks(); });
    const filterIcon = el('span', 'mi', String.fromCodePoint(0xf75b)); filterIcon.setAttribute('aria-hidden', 'true'); $('category-tabs').append(filterIcon, select);
  }

  async function closeTaskParameters() {
    if (!(await mayDiscard())) return false;
    state.taskEpoch++; state.task = ''; state.fields = []; state.loadingSettings = false;
    $('settings-tab').disabled = true; showWorkTab('tasks'); updateSaveBar(); return true;
  }

  async function setTaskScope(scope) {
    if (!(await closeTaskParameters())) return;
    state.taskScope = scope; state.category = ''; state.taskFilter = 'all'; $('task-search').value = '';
    renderCategories(); renderTasks(); showMobile('tasks');
  }

  function scheduleItems(source) {
    return Array.isArray(source) ? source : source && typeof source === 'object' ? [source] : typeof source === 'string' ? [source] : [];
  }

  function scheduleTaskName(item) {
    return typeof item === 'string' ? item : item?.name || item?.task || item?.task_name || item?.command || '';
  }

  function runningTask(snapshot) {
    if (snapshot?.state !== 1) return '';
    const schedule = snapshot.schedule || {};
    for (const key of ['running', 'run', 'Running', 'Run']) {
      for (const item of scheduleItems(schedule[key])) {
        const name = scheduleTaskName(item);
        if (name) return name;
      }
    }
    return '';
  }

  function scheduledTasks(snapshot) {
    const names = [], current = runningTask(snapshot);
    if (current) names.push(current);
    for (const key of ['pending', 'waiting', 'Pending', 'Waiting']) {
      for (const item of scheduleItems(snapshot?.schedule?.[key])) {
        const name = scheduleTaskName(item);
        if (name && !names.includes(name)) names.push(name);
      }
    }
    return names;
  }

  function scheduleForTask(task, snapshot = state.snapshots.get(state.account)) {
    const schedule = snapshot?.schedule || {};
    const kinds = [['running', '运行中'], ['run', '运行中'], ['pending', '待执行'], ['waiting', '等待中']];
    for (const [key, title] of kinds) {
      if (title === '运行中' && snapshot?.state !== 1) continue;
      const source = schedule[key] || schedule[key[0].toUpperCase() + key.slice(1)];
      const list = scheduleItems(source);
      for (const item of list) {
        const name = scheduleTaskName(item);
        if (name === task) return { kind: title === '运行中' ? 'running' : key === 'pending' ? 'pending' : 'waiting', running: title === '运行中', time: title === '运行中' ? '' : item.next_run || '', text: title === '运行中' ? '正在执行' : item.next_run || '' };
      }
    }
    const direct = schedule[task];
    if (direct && typeof direct === 'object') return { kind: 'waiting', running: false, time: direct.next_run || '', text: direct.next_run || '' };
    return { kind: '', running: false, time: '', text: '' };
  }

  function taskIsEnabled(task, schedule) {
    const confirmed = state.taskEnableOverrides?.get(state.account)?.get(task);
    const scheduled = schedule.includes(task);
    if (!confirmed) return scheduled;
    // A disabled running task still appears in the worker's schedule until it
    // finishes. Other local confirmations expire if polling never catches up.
    if (!confirmed.enabled && runningTask(state.snapshots.get(state.account)) === task) return false;
    if (scheduled === confirmed.enabled || Date.now() >= confirmed.until) {
      state.taskEnableOverrides.get(state.account).delete(task); return scheduled;
    }
    return confirmed.enabled;
  }

  function canToggleTask(task) {
    return Boolean(state.authenticated && state.account && state.backendOnline && !state.mustChangePassword
      && !state.saving && !state.actionBusy && !state.loadingSettings
      && !['Script', 'GlobalGame'].includes(task) && state.menu.some((entry) => entry.name === task));
  }

  function rememberTaskEnabled(account, task, enabled) {
    if (!state.taskEnableOverrides) state.taskEnableOverrides = new Map();
    if (!state.taskEnableOverrides.has(account)) state.taskEnableOverrides.set(account, new Map());
    state.taskEnableOverrides.get(account).set(task, { enabled, until: Date.now() + 15000 });
  }

  function renderTasks() {
    if (!state.account) return;
    const search = $('task-search').value.trim().toLowerCase();
    const schedule = scheduledTasks(state.snapshots.get(state.account));
    const entries = state.menu.filter((entry) => (state.taskScope === 'catalog' || (schedule.includes(entry.name) && taskIsEnabled(entry.name, schedule)) || runningTask(state.snapshots.get(state.account)) === entry.name)
      && (state.taskScope !== 'catalog' || state.taskFilter === 'all' || (state.taskFilter === 'enabled') === taskIsEnabled(entry.name, schedule))
      && (!search || `${entry.name} ${taskLabel(entry.name)}`.toLowerCase().includes(search)));
    $('task-list').replaceChildren();
    if (!entries.length) {
      empty($('task-list'), state.taskScope === 'schedule' ? '暂无任务' : '没有匹配的任务', search ? '试试其他关键词。' : '');
      return;
    }
    const taskButton = (entry) => {
      const status = scheduleForTask(entry.name);
      const row = el('div', `task-row ${status.kind}${state.task === entry.name ? ' selected' : ''}`); row.dataset.task = entry.name;
      if (!['Script', 'GlobalGame'].includes(entry.name)) {
        const control = el('label', 'task-enable-control'); control.title = `${taskLabel(entry.name)}：勾选启用，取消停用，立即保存`;
        const toggle = el('input', 'task-enabled'); toggle.type = 'checkbox'; toggle.checked = taskIsEnabled(entry.name, schedule);
        toggle.setAttribute('role', 'switch'); toggle.setAttribute('aria-label', `${taskLabel(entry.name)} 启用状态，修改立即保存`); toggle.setAttribute('aria-describedby', 'task-toggle-hint');
        toggle.disabled = !canToggleTask(entry.name);
        toggle.addEventListener('change', () => stageTaskEnabled(entry.name, toggle.checked)); control.append(toggle); row.append(control);
      }
      const button = el('button', 'task-open'); button.type = 'button'; button.dataset.testid = 'task-row'; button.setAttribute('aria-label', `${taskLabel(entry.name)} 参数`);
      const symbol = el('span', 'task-symbol'); symbol.append(icon(status.kind === 'running' ? 'bolt' : status.kind === 'pending' ? 'layers' : 'clock')); button.append(symbol);
      const info = el('span', 'task-info'); info.append(el('span', 'task-name', taskLabel(entry.name)));
      if (status.time) info.append(el('span', 'task-meta', status.time)); button.append(info); button.addEventListener('click', () => selectTask(entry.name)); row.append(button);
      const actions = el('div', 'task-actions');
      const quick = iconButton('flash', taskIsEnabled(entry.name, schedule) ? `${taskLabel(entry.name)} 立即执行` : `${taskLabel(entry.name)}：请先启用该任务`, () => runTaskNow(entry.name));
      const clock = iconButton('clock', `${taskLabel(entry.name)} 调整运行时间`, () => openScheduleSettings(entry.name));
      quick.disabled = !canRunTaskNow(entry.name);
      clock.disabled = ['Script', 'GlobalGame'].includes(entry.name) || state.mustChangePassword;
      actions.append(quick, clock, iconButton('tune', `${taskLabel(entry.name)} 参数设置`, () => selectTask(entry.name))); row.append(actions); return row;
    };
    if (state.taskScope === 'schedule') {
      entries.sort((a, b) => schedule.indexOf(a.name) - schedule.indexOf(b.name)).forEach((entry) => $('task-list').append(taskButton(entry)));
      return;
    }
    const groups = new Map();
    entries.forEach((entry) => { if (!groups.has(entry.category)) groups.set(entry.category, []); groups.get(entry.category).push(entry); });
    groups.forEach((items, category) => {
      const group = el('details', 'task-category');
      group.open = Boolean(search || state.category || state.openCategories.has(category));
      const heading = el('summary');
      heading.append(el('span', 'category-title', label(category)), el('span', 'category-count', `${items.length} 项`));
      group.append(heading);
      const content = el('div', 'category-content'); items.forEach((entry) => content.append(taskButton(entry))); group.append(content);
      group.addEventListener('toggle', () => { if (group.open) state.openCategories.add(category); else state.openCategories.delete(category); });
      $('task-list').append(group);
    });
  }

  function showWorkTab(tab) {
    if (tab === 'settings' && !state.task) return;
    $('tasks-view').hidden = tab !== 'tasks'; $('settings-view').hidden = tab !== 'settings';
    ['tasks', 'settings'].forEach((name) => {
      $(`${name}-tab`).classList.toggle('active', name === tab);
      $(`${name}-tab`).classList.toggle('selected', name === tab);
      $(`${name}-tab`).setAttribute('aria-selected', String(name === tab));
    });
  }

  function openConfigMenu(name) {
    let dialog = $('config-menu');
    if (!dialog) { dialog = el('dialog', 'dialog config-menu'); dialog.id = 'config-menu'; document.body.append(dialog); }
    dialog.replaceChildren(el('h2', '', name));
    [['全局配置', 'GlobalGame'], ['脚本', 'Script']].forEach(([title, task]) => {
      const button = el('button', '', title); button.type = 'button';
      button.disabled = !state.menu.some((entry) => entry.name === task);
      button.addEventListener('click', async () => { dialog.close(); await selectAccount(name); if (state.account === name) selectTask(task); }); dialog.append(button);
    });
    const close = el('button', '', '关闭'); close.type = 'button'; close.addEventListener('click', () => dialog.close()); dialog.append(close); dialog.showModal();
  }

  function canRunTaskNow(task, snapshot = state.snapshots.get(state.account)) {
    return Boolean(state.authenticated && state.account && state.backendOnline && snapshot?.connected
      && [0, 1].includes(snapshot.state) && !state.actionBusy && !state.saving && !state.mustChangePassword
      && !['Script', 'GlobalGame'].includes(task) && state.menu.some((entry) => entry.name === task)
      && runningTask(snapshot) !== task);
  }

  async function runTaskNow(task) {
    if (!canRunTaskNow(task)) return;
    if (dirtyFields().length) { toast('当前参数尚未保存，请先保存或放弃修改。', 'error'); showWorkTab('settings'); return; }
    const account = state.account, accountEpoch = state.accountEpoch, taskEpoch = state.taskEpoch, bootEpoch = state.bootEpoch;
    const current = () => state.authenticated && state.account === account && state.accountEpoch === accountEpoch && state.bootEpoch === bootEpoch;
    stopSnapshotRefresh(); state.actionBusy = true; renderSelectedAccount(); renderTasks();
    let queued = false;
    try {
      // Read the actual worker state before writing. Queueing keeps its current
      // task intact and does not enable tasks; only a stopped account is started.
      const version = state.snapshotVersions.get(account) || 0;
      const snapshot = await api(`/api/accounts/${encoder(account)}/snapshot`);
      if (!current()) return;
      if (version === (state.snapshotVersions.get(account) || 0)) storeSnapshot(account, snapshot);
      if (!snapshot.connected || ![0, 1].includes(snapshot.state)) throw new Error('当前配置不可执行，请刷新状态后重试。');
      if (runningTask(snapshot) === task || runningTask(state.snapshots.get(account)) === task) { toast('这个任务已经在执行，无需重复提交。'); return; }
      const settings = await api(`/api/accounts/${encoder(account)}/settings/${encoder(task)}`);
      if (!current()) return;
      const fields = Array.isArray(settings?.scheduler) ? settings.scheduler : [];
      const enabled = fields.find((field) => field.name === 'enable');
      const next = fields.find((field) => field.name === 'next_run');
      if (enabled?.value !== true || !next || typeof next.value !== 'string' || readOnly(next)) throw new Error('请先启用这个任务并保存，才能立即执行。');
      if (runningTask(state.snapshots.get(account)) === task) { toast('这个任务已经在执行，无需重复提交。'); return; }
      const target = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(new Date(Date.now() - 86400000));
      const result = await api(`/api/accounts/${encoder(account)}/settings/${encoder(task)}/scheduler/next_run`, {
        method: 'PUT', body: { value: target, expected_value: clone(next.value) },
      });
      if (!current()) return;
      if (result?.ok !== true || result.value !== target) throw new Error('未确认排程修改，请刷新检查，勿重复提交。');
      queued = true;
      const latest = state.snapshots.get(account) || snapshot;
      if (latest.state === 0) {
        const started = await api(`/api/accounts/${encoder(account)}/actions`, { method: 'POST', body: { action: 'start' } });
        if (!current()) return;
        if (started?.ok !== true || started.state !== 1) throw new Error('未确认当前配置启动成功。');
        toast(`「${taskLabel(task)}」已加入待执行，当前配置已启动。`);
      } else toast(runningTask(latest) ? `「${taskLabel(task)}」已加入待执行，当前任务结束后按排程执行。`
        : `「${taskLabel(task)}」已加入待执行，后台将自动执行。`);
      if (state.task === task && state.taskEpoch === taskEpoch && !dirtyFields().length && !state.loadingSettings) await selectTask(task, true);
    } catch (error) {
      if (current()) toast(queued ? `任务已加入排程；${error.message} 请刷新查看运行状态，勿重复提交。` : error.message, 'error');
    } finally {
      if (state.bootEpoch === bootEpoch) {
        state.actionBusy = false; renderSelectedAccount(); renderTasks(); syncSnapshotVisibility({ force: true, full: true });
      }
    }
  }

  async function openScheduleSettings(task) {
    await selectTask(task);
    if (state.task !== task || state.loadingSettings) return;
    const field = state.fields.find((item) => item.group === 'scheduler' && item.name === 'next_run');
    if (!field) return;
    field.input.closest('.settings-group').open = true;
    field.input.focus(); field.input.scrollIntoView({ block: 'nearest' });
  }

  async function stageTaskEnabled(task, enabled) {
    if (!canToggleTask(task) || typeof enabled !== 'boolean') { renderTasks(); return; }
    if (dirtyFields().length) { toast('当前参数尚未保存，请先保存或放弃修改。', 'error'); showWorkTab('settings'); renderTasks(); return; }
    const account = state.account, accountEpoch = state.accountEpoch, taskEpoch = state.taskEpoch, bootEpoch = state.bootEpoch;
    const current = () => state.authenticated && state.account === account && state.accountEpoch === accountEpoch && state.bootEpoch === bootEpoch;
    stopSnapshotRefresh(); state.actionBusy = true; renderSelectedAccount(); renderTasks();
    try {
      const settings = await api(`/api/accounts/${encoder(account)}/settings/${encoder(task)}`);
      if (!current()) return;
      const fields = Array.isArray(settings?.scheduler) ? settings.scheduler : settings?.scheduler?.fields;
      const field = fields?.find((item) => item.name === 'enable');
      if (!field || typeof field.value !== 'boolean' || readOnly(field)) throw new Error('这个任务没有可修改的启用开关。');
      if (field.value !== enabled) {
        const result = await api(`/api/accounts/${encoder(account)}/settings/${encoder(task)}/scheduler/enable`, {
          method: 'PUT', body: { value: enabled, expected_value: clone(field.value) },
        });
        if (!current()) return;
        if (result?.ok !== true || result.value !== enabled) throw new Error('未确认启用状态已保存，请刷新参数后检查。');
      }
      rememberTaskEnabled(account, task, enabled);
      toast(enabled ? `「${taskLabel(task)}」已启用并保存。`
        : runningTask(state.snapshots.get(account)) === task ? `「${taskLabel(task)}」已停用并保存；当前任务执行完后不再调度。`
          : `「${taskLabel(task)}」已停用并保存，不再自动执行。`);
      if (state.task === task && state.taskEpoch === taskEpoch && !dirtyFields().length && !state.loadingSettings) await selectTask(task, true);
    } catch (error) { if (current()) toast(error.message, 'error'); }
    finally {
      if (state.bootEpoch === bootEpoch) {
        state.actionBusy = false; renderSelectedAccount(); renderTasks(); syncSnapshotVisibility({ force: true, full: true });
      }
    }
  }

  async function selectTask(task, force = false) {
    if (!state.account) return;
    if (state.task === task && !force) { showWorkTab('settings'); return; }
    if (!(await mayDiscard())) return;
    const account = state.account, accountEpoch = state.accountEpoch, epoch = ++state.taskEpoch;
    state.task = task; state.fields = []; state.loadingSettings = true;
    $('selected-task').textContent = taskLabel(task);
    const category = state.menu.find((entry) => entry.name === task)?.category;
    $('selected-category').textContent = category ? label(category) : '';
    $('settings-tab').disabled = false; showWorkTab('settings'); renderTasks(); updateSaveBar();
    empty($('settings-content'), '正在读取参数', '请稍候…');
    try {
      const data = await api(`/api/accounts/${encoder(account)}/settings/${encoder(task)}`);
      if (epoch !== state.taskEpoch || accountEpoch !== state.accountEpoch || account !== state.account) return;
      renderSettings(data || {});
    } catch (error) {
      if (epoch !== state.taskEpoch || accountEpoch !== state.accountEpoch) return;
      empty($('settings-content'), '暂时无法读取参数', error.message, () => selectTask(task, true));
      toast(error.message, 'error');
    } finally {
      if (epoch === state.taskEpoch && accountEpoch === state.accountEpoch) { state.loadingSettings = false; updateSaveBar(); }
    }
  }

  function fieldType(field) {
    if (field.type === 'multi_enum') return 'multi_enum';
    if (field.enumEnum || field.enum || field.options || field.type === 'enum') return 'enum';
    const type = String(field.type || '').toLowerCase();
    if (['bool', 'boolean'].includes(type) || typeof field.value === 'boolean') return 'boolean';
    if (['int', 'integer'].includes(type)) return 'integer';
    if (['float', 'double', 'number'].includes(type) || typeof field.value === 'number') return 'number';
    if (['datetime', 'date_time', 'datetime.datetime'].includes(type)) return 'date_time';
    if (['time', 'datetime.time'].includes(type)) return 'time';
    if (['timedelta', 'time_delta', 'datetime.timedelta'].includes(type)) return 'time_delta';
    return 'string';
  }

  function enumOptions(field) {
    const source = field.enumEnum || field.enum || field.options || [];
    if (Array.isArray(source)) return source.map((value) => typeof value === 'object' && value !== null
      ? { value: value.value ?? value.name ?? value.key, title: value.title || value.label || value.name || String(value.value) }
      : { value, title: label(String(value)) });
    if (source && typeof source === 'object') return Object.entries(source).map(([key, value]) => {
      if (typeof value === 'object' && value !== null) return { value: value.value ?? key, title: value.title || value.label || key };
      return { value: key, title: label(key, typeof value === 'string' ? value : key) };
    });
    return [];
  }

  function descriptionFor(group, field) {
    return clean(state.labels[`${group}.${field.name}_help`]) || clean(state.labels[`${field.name}_help`]) || clean(state.labels[field.description]) || clean(field.description) || '';
  }

  function readOnly(field) {
    return Boolean(field.readOnly || field.readonly || field.read_only || field.disabled);
  }

  function renderSettings(groups) {
    state.fields = []; $('settings-content').replaceChildren();
    const schedulerFields = Array.isArray(groups?.scheduler) ? groups.scheduler : groups?.scheduler?.fields;
    const enabled = schedulerFields?.find((field) => field.name === 'enable');
    if (typeof enabled?.value === 'boolean') rememberTaskEnabled(state.account, state.task, enabled.value);
    const groupNames = Object.entries(groups).filter(([, source]) => {
      const fields = Array.isArray(source) ? source : source?.fields;
      return Array.isArray(fields) && fields.length;
    }).map(([name]) => name);
    const taskKey = state.task.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
    const preferred = state.task === 'GlobalGame' ? 'costume_config' : state.task === 'Script' ? 'device' : `${taskKey}_config`;
      const defaultGroup = groupNames.includes(preferred) ? preferred : groupNames.find((name) => name !== 'scheduler') || groupNames[0];
    let index = 0;
    Object.entries(groups).forEach(([groupName, source]) => {
      const fields = Array.isArray(source) ? source : source?.fields;
      if (!Array.isArray(fields) || !fields.length) return;
      const section = el('details', 'settings-group'); section.open = groupName === defaultGroup;
      const groupHeading = el('summary');
      groupHeading.append(el('span', '', label(groupName, source?.title)), el('span', 'category-count', `${fields.length} 项`));
      section.append(groupHeading);
      fields.forEach((definition) => {
        if (!definition || !definition.name) return;
        const type = fieldType(definition);
        const item = { group: groupName, name: definition.name, type, original: clone(definition.value), dirty: false, error: '', definition };
        const row = el('div', 'field-row'); row.dataset.testid = 'setting-field';
        const head = el('div', 'field-head');
        const title = label(`${groupName}.${definition.name}`, clean(state.labels[definition.name]) || definition.title || definition.name);
        const fieldLabel = el('label', '', title); fieldLabel.htmlFor = `setting-field-${index++}`;
        const status = el('span', 'field-status'); head.append(fieldLabel, status); row.append(head);
        const description = descriptionFor(groupName, definition);
        if (description) {
          row.append(el('p', 'field-description', description));
        }
        let input;
        if (type === 'boolean') {
          input = el('input', 'toggle'); input.type = 'checkbox'; input.checked = definition.value === true;
          input.setAttribute('role', 'switch'); head.append(input);
        } else if (type === 'multi_enum') {
          input = el('fieldset', 'multi-enum'); item.options = enumOptions(definition);
          input.setAttribute('aria-label', title);
          item.options.forEach((option, optionIndex) => {
            const choice = el('label', 'multi-enum-option');
            const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.value = String(optionIndex);
            checkbox.checked = Array.isArray(definition.value) && definition.value.includes(option.value);
            choice.append(checkbox, el('span', '', label(String(option.value), option.title)));
            input.append(choice);
          });
          row.append(input);
        } else if (type === 'enum') {
          input = el('select'); item.options = enumOptions(definition);
          if (!item.options.some((option) => equal(option.value, definition.value))) {
            item.options.unshift({ value: clone(definition.value), title: definition.value === null ? '未设置' : `${label(String(definition.value))}（当前值）` });
          }
          item.options.forEach((option, optionIndex) => {
            const node = el('option', '', label(String(option.value), option.title)); node.value = String(optionIndex);
            node.selected = equal(option.value, definition.value); input.append(node);
          });
          row.append(input);
        } else {
          const multiLine = definition.type === 'multi_line' || typeof definition.value === 'string' && (definition.value.includes('\n') || definition.value.length > 100);
          input = el(multiLine ? 'textarea' : 'input');
          if (!multiLine) {
            input.type = type === 'date_time' ? 'datetime-local' : type === 'time' ? 'time' : ['integer', 'number'].includes(type) ? 'number' : 'text';
          }
          if (type === 'date_time') { input.value = String(definition.value || '').replace(' ', 'T'); input.step = '1'; }
          else if (type === 'time') { input.value = String(definition.value || ''); input.step = '1'; }
          else input.value = definition.value === null || definition.value === undefined ? '' : typeof definition.value === 'object' ? JSON.stringify(definition.value) : String(definition.value);
          if (type === 'time_delta') input.placeholder = '00 00:00:00';
          if (type === 'integer' || type === 'number') {
            input.step = type === 'integer' ? '1' : 'any';
            const min = definition.min ?? definition.minimum, max = definition.max ?? definition.maximum;
            if (min !== undefined && min !== null) input.min = min;
            if (max !== undefined && max !== null) input.max = max;
          }
          row.append(input);
        }
        input.id = fieldLabel.htmlFor; input.dataset.field = `${groupName}.${definition.name}`;
        input.autocomplete = 'off'; input.disabled = readOnly(definition);
        const errorNode = el('p', 'field-error'); errorNode.hidden = true; errorNode.setAttribute('role', 'status'); row.append(errorNode);
        item.input = input; item.row = row; item.status = status; item.errorNode = errorNode;
        item.initialInput = inputSnapshot(item);
        input.addEventListener('input', () => updateField(item));
        input.addEventListener('change', () => updateField(item));
        state.fields.push(item); section.append(row);
      });
      $('settings-content').append(section);
    });
    if (!state.fields.length) empty($('settings-content'), '暂无可调整参数', '这个任务没有提供参数设置。');
    updateSaveBar();
  }

  function readField(item) {
    const input = item.input, text = input.value;
    if (item.type === 'boolean') return input.checked;
    if (item.type === 'multi_enum') {
      const values = Array.from(input.querySelectorAll('input:checked')).map((node) => clone(item.options[Number(node.value)].value));
      if (values.length < Math.max(1, item.definition.minItems || 1)) throw new Error('请至少选择一个庭院皮肤。');
      return values;
    }
    if (item.type === 'enum') return clone(item.options[Number(text)]?.value);
    if (item.type === 'integer' || item.type === 'number') {
      if (!text.trim()) { if (item.original === null) return null; throw new Error('请填写数值。'); }
      const value = Number(text);
      if (!Number.isFinite(value) || (item.type === 'integer' && !Number.isInteger(value))) throw new Error(item.type === 'integer' ? '请输入整数。' : '请输入有效数字。');
      if (input.min !== '' && value < Number(input.min)) throw new Error(`不能小于 ${input.min}。`);
      if (input.max !== '' && value > Number(input.max)) throw new Error(`不能大于 ${input.max}。`);
      return value;
    }
    if (item.type === 'date_time') {
      if (!text) { if (item.original === null || item.original === '') return item.original; throw new Error('请填写日期与时间。'); }
      return `${text.replace('T', ' ')}${text.length === 16 ? ':00' : ''}`;
    }
    if (item.type === 'time') {
      if (!text) { if (item.original === null || item.original === '') return item.original; throw new Error('请填写时间。'); }
      return text.length === 5 ? `${text}:00` : text;
    }
    if (item.type === 'time_delta') {
      if (!/^\d{2}\s(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(text.trim()) || Number(text.trim().slice(0, 2)) > 31) {
        throw new Error('时长格式为：DD HH:MM:SS，例如 00 02:30:00；天数为 00–31。');
      }
      return text.trim();
    }
    if (typeof item.original === 'object' && item.original !== null) {
      try { return JSON.parse(text); } catch (_) { throw new Error('请输入有效的 JSON 内容。'); }
    }
    return text;
  }

  function inputSnapshot(item) {
    if (item.type === 'multi_enum') return Array.from(item.input.querySelectorAll('input')).map((node) => node.checked);
    return item.type === 'boolean' ? item.input.checked : item.input.value;
  }

  function updateField(item) {
    const raw = inputSnapshot(item);
    item.error = '';
    if (equal(raw, item.initialInput)) item.dirty = false;
    else {
      try { item.dirty = !equal(readField(item), item.original); }
      catch (error) { item.dirty = true; item.error = error.message; }
    }
    item.row.classList.toggle('is-dirty', item.dirty);
    item.row.classList.toggle('has-error', Boolean(item.error));
    item.status.textContent = item.dirty ? '待保存' : '';
    item.errorNode.textContent = item.error; item.errorNode.hidden = !item.error;
    updateSaveBar();
  }

  function updateSaveBar() {
    const dirty = dirtyFields().length;
    $('dirty-count').textContent = dirty; $('dirty-count').hidden = !dirty;
    $('save-status').textContent = state.saving ? '正在保存…' : dirty ? `${dirty} 项修改待保存` : '未作修改';
    $('save-settings').disabled = !dirty || state.saving || state.loadingSettings || state.mustChangePassword || !state.backendOnline;
    $('save-settings').textContent = state.saving ? '保存中…' : '保存';
    $('discard-settings').disabled = !dirty || state.saving;
    $('refresh-settings').disabled = state.saving || state.loadingSettings;
  }

  async function saveSettings() {
    if (state.saving || !dirtyFields().length || state.mustChangePassword) return;
    const items = dirtyFields(); let invalid = false;
    items.forEach((item) => { updateField(item); if (item.error) invalid = true; });
    if (invalid) { toast('请先修正标出的参数，再保存。', 'error'); return; }
    const account = state.account, task = state.task, epoch = state.taskEpoch, accountEpoch = state.accountEpoch;
    stopSnapshotRefresh();
    state.saving = true; updateSaveBar(); state.fields.forEach((item) => { item.input.disabled = true; });
    let saved = 0, failure = null;
    try {
      for (const item of items) {
        const value = readField(item);
        try {
          await api(`/api/accounts/${encoder(account)}/settings/${encoder(task)}/${encoder(item.group)}/${encoder(item.name)}`, {
            method: 'PUT', body: { value, expected_value: clone(item.original) },
          });
        } catch (error) {
          failure = error;
          if (epoch === state.taskEpoch && accountEpoch === state.accountEpoch) {
            item.error = error.message; item.errorNode.textContent = error.message; item.errorNode.hidden = false; item.row.classList.add('has-error');
          }
          break;
        }
        if (epoch !== state.taskEpoch || accountEpoch !== state.accountEpoch) return;
        item.original = clone(value); item.initialInput = inputSnapshot(item);
        if (item.group === 'scheduler' && item.name === 'enable') rememberTaskEnabled(account, task, value);
        item.dirty = false; item.error = ''; item.status.textContent = '已保存';
        item.row.classList.remove('is-dirty', 'has-error'); item.errorNode.hidden = true; saved++;
      }
    } finally {
      if (epoch === state.taskEpoch && accountEpoch === state.accountEpoch) {
        state.saving = false;
        state.fields.forEach((item) => { item.input.disabled = readOnly(item.definition); });
        updateSaveBar();
        syncSnapshotVisibility({ force: true });
        if (saved) requestStatisticsRefresh();
      }
    }
    if (failure) {
      toast(`${saved ? `已保存 ${saved} 项；` : ''}${failure.message}`, 'error', failure.status === 409 ? undefined : saveSettings);
    } else if (saved) {
      $('save-status').textContent = `已保存 ${saved} 项修改`; toast('参数已保存。');
    }
  }

  async function discardSettings() {
    if (!dirtyFields().length || !(await mayDiscard())) return;
    state.fields.forEach((item) => {
      if (item.type === 'boolean') item.input.checked = item.initialInput;
      else if (item.type === 'multi_enum') item.input.querySelectorAll('input').forEach((node, index) => { node.checked = item.initialInput[index]; });
      else item.input.value = item.initialInput;
      updateField(item);
    });
  }

  async function performAction(action) {
    if (!state.account || state.actionBusy || state.mustChangePassword) return;
    if (dirtyFields().length) { toast('当前参数尚未保存，请先保存或放弃修改。', 'error'); showWorkTab('settings'); return; }
    const name = state.account, epoch = state.accountEpoch;
    const start = action === 'start';
    const confirmed = await confirmAction(start ? '启动这个配置？' : '停止这个配置？', start
      ? `将启动「${name}」的任务排程，OAS 会开始控制对应模拟器。`
      : `将停止「${name}」当前执行的任务与排程。`, start ? '确认启动' : '确认停止', !start);
    if (!confirmed || state.account !== name || epoch !== state.accountEpoch) return;
    stopSnapshotRefresh();
    state.actionBusy = true; renderSelectedAccount();
    let commandSent = false;
    try {
      await api(`/api/accounts/${encoder(name)}/actions`, { method: 'POST', body: { action } });
      commandSent = true;
      toast(`已发送${start ? '启动' : '停止'}指令，运行状态以服务返回为准。`);
    } catch (error) { toast(commandSent ? `指令已发送，但暂时无法刷新状态：${error.message}` : error.message, 'error'); }
    finally {
      state.actionBusy = false; renderSelectedAccount();
      syncSnapshotVisibility({ force: true, full: true });
      if (commandSent && epoch === state.accountEpoch) requestStatisticsRefresh();
    }
  }

  function appendLog(line) {
    const lines = String(line).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').split(/\r?\n/);
    if (lines.at(-1) === '') lines.pop();
    state.logs.push(...lines);
    state.pendingLogs.push(...lines);
    if (state.logs.length > MAX_LOGS) state.logs.splice(0, state.logs.length - MAX_LOGS);
    if (state.pendingLogs.length > MAX_LOGS) state.pendingLogs.splice(0, state.pendingLogs.length - MAX_LOGS);
    if (!state.logFrame) state.logFrame = requestAnimationFrame(() => { state.logFrame = 0; renderLogs(); });
  }

  function visibleLog(line) {
    if (state.logLevel !== 'ALL' && !(new RegExp(`\\b${state.logLevel}\\b`)).test(line)) return false;
    if (state.logFilter === 'all' || /\b(?:ERROR|CRITICAL|FATAL|WARN(?:ING)?)\b/.test(line)) return true;
    return !/\bDEBUG\b|\[[^\]]+\s\d+(?:\.\d+)?s\]|Try to detect vertically|No text detected in ROI|^[\s─━═_=-]{5,}$/.test(line);
  }

  function setLogFilter(value) {
    state.logFilter = value;
    ['key', 'all'].forEach((kind) => {
      $(`log-filter-${kind}`).classList.toggle('selected', kind === value);
      $(`log-filter-${kind}`).setAttribute('aria-pressed', String(kind === value));
    });
    renderLogs(true);
  }

  function renderLogs(force = false) {
    const container = $('log-content');
    const visible = state.logs.filter(visibleLog);
    $('log-count').textContent = `${visible.length} 行${visible.length !== state.logs.length ? ` / 共 ${state.logs.length} 行` : ''}`;
    $('log-count').title = state.logFilter === 'key' ? `已收起 ${state.logs.length - visible.length} 行调试信息，可切换“全部日志”查看` : '显示全部原始日志';
    if (!visible.length) {
      state.pendingLogs = [];
      empty(container, '暂无日志', '');
      return;
    }
    const previousTop = container.scrollTop;
    const incremental = !force && container.firstElementChild?.classList.contains('log-line');
    const lines = incremental ? state.pendingLogs.filter(visibleLog) : visible;
    const fragment = document.createDocumentFragment();
    lines.forEach((line) => {
      let kind = /\b(ERROR|CRITICAL|FATAL)\b/.test(line) ? 'error' : /\bWARN(?:ING)?\b/.test(line) ? 'warning' : /\bDEBUG\b/.test(line) ? 'debug' : '';
      const row = el('div', `log-line ${kind}`); let offset = 0;
      for (const match of line.matchAll(/(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}:\d{2}(?:\.\d+)?|\bINFO\b/g)) {
        row.append(document.createTextNode(line.slice(offset, match.index)), el('span', match[0] === 'INFO' ? 'log-level-info' : 'log-time', match[0])); offset = match.index + match[0].length;
      }
      row.append(document.createTextNode(line.slice(offset))); fragment.append(row);
    });
    state.pendingLogs = [];
    if (incremental) container.append(fragment);
    else container.replaceChildren(fragment);
    let removedHeight = 0;
    while (container.children.length > visible.length) {
      removedHeight += container.firstElementChild.getBoundingClientRect().height;
      container.firstElementChild.remove();
    }
    if (state.followLogs) container.scrollTop = container.scrollHeight;
    else container.scrollTop = Math.max(0, previousTop - removedHeight);
  }

  function setLogFollow(value) {
    state.followLogs = value; $('log-follow').setAttribute('aria-pressed', String(value));
    $('log-follow').replaceChildren(icon('flash')); $('log-follow').title = value ? '自动滚动' : '滚动已暂停'; $('log-paused').hidden = value;
    if (value) $('log-content').scrollTop = $('log-content').scrollHeight;
  }

  async function copyText(text) {
    if (!text) { toast('暂无可复制的日志。', 'error'); return; }
    try { await navigator.clipboard.writeText(text); toast('日志已复制。'); }
    catch (_) {
      const input = el('textarea'); input.value = text; input.style.position = 'fixed'; input.style.opacity = '0';
      document.body.append(input); input.select();
      const copied = document.execCommand('copy'); input.remove();
      toast(copied ? '日志已复制。' : '无法自动复制，请选择日志文本手动复制。', copied ? 'success' : 'error');
    }
  }

  function showLogTab(tab) {
    ['logs', 'errors'].forEach((name) => {
      $(`${name}-view`).hidden = name !== tab;
      $(`${name}-tab`).classList.toggle('active', name === tab);
      $(`${name}-tab`).classList.toggle('selected', name === tab);
      $(`${name}-tab`).setAttribute('aria-selected', String(name === tab));
    });
    if (tab === 'errors') loadErrors();
  }

  function resetErrors() {
    state.errorEpoch++; state.errors = []; state.errorDetail = null;
    $('error-detail').hidden = true; $('error-list').hidden = false;
    $('error-date').textContent = ''; $('error-log').textContent = '';
    $('error-images').replaceChildren(); $('error-truncated').hidden = true;
    $('error-copy').disabled = true; $('refresh-errors').disabled = false;
    $('errors-scope').textContent = state.account || '请选择配置';
    if ($('image-dialog').open) $('image-dialog').close();
    $('image-preview').removeAttribute('src');
    renderErrors();
  }

  async function loadErrors() {
    resetErrors();
    const account = state.account, accountEpoch = state.accountEpoch, bootEpoch = state.bootEpoch, epoch = state.errorEpoch;
    const current = () => state.authenticated && epoch === state.errorEpoch && account === state.account && accountEpoch === state.accountEpoch && bootEpoch === state.bootEpoch;
    if (!account || !state.authenticated) return;
    empty($('error-list'), '正在读取错误档案', '请稍候…');
    $('refresh-errors').disabled = true;
    try {
      const data = await api(`/api/errors?config=${encoder(account)}`);
      if (!current()) return;
      state.errors = (data?.records || []).filter((record) => record.config_name === account);
      $('errors-scope').textContent = account;
      renderErrors();
    } catch (error) {
      if (current()) empty($('error-list'), '暂时无法读取记录', error.message, loadErrors);
    } finally { if (current()) $('refresh-errors').disabled = false; }
  }

  function displayDate(value) {
    if (!value) return '时间未知';
    const source = String(value);
    const date = new Date(source);
    if (Number.isNaN(date.getTime())) return source;
    const parts = new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    }).formatToParts(date);
    const part = (type) => parts.find((entry) => entry.type === type)?.value || '';
    return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}`;
  }

  function renderErrors() {
    $('error-list').replaceChildren();
    if (!state.account) { empty($('error-list'), '请选择配置', '选择账号后查看该账号的错误档案。'); return; }
    if (!state.errors.length) { empty($('error-list'), '当前账号没有错误记录', '这里只显示所选账号的现场截图与日志。'); return; }
    state.errors.forEach((record) => {
      const button = el('button', 'error-record'); button.type = 'button'; button.dataset.testid = 'error-record';
      const text = el('span'); text.append(el('strong', '', displayDate(record.created_at)), el('small', '', record.task ? `${taskLabel(record.task)} · 查看错误日志` : '查看现场截图与错误日志'));
      button.append(el('span', 'error-icon', '!'), text, el('span', 'muted', '›'));
      button.addEventListener('click', () => openError(record.id)); $('error-list').append(button);
    });
  }

  async function openError(id) {
    if (!state.account || !state.authenticated) return;
    const account = state.account, accountEpoch = state.accountEpoch, bootEpoch = state.bootEpoch;
    const epoch = ++state.errorEpoch;
    const current = () => state.authenticated && epoch === state.errorEpoch && account === state.account && accountEpoch === state.accountEpoch && bootEpoch === state.bootEpoch;
    $('error-list').hidden = true; $('error-detail').hidden = false;
    $('error-date').textContent = '正在读取…'; $('error-log').textContent = '';
    $('error-images').replaceChildren(); $('error-truncated').hidden = true;
    $('error-copy').disabled = true; state.errorDetail = null;
    try {
      const data = await api(`/api/errors/${encoder(id)}?config=${encoder(account)}`);
      if (!current()) return;
      if (data.config_name !== account) throw new Error('此错误记录不属于当前账号。');
      state.errorDetail = data; $('error-date').textContent = displayDate(data.created_at);
      $('error-log').textContent = typeof data.log === 'string' ? data.log : '';
      $('error-truncated').hidden = !data.truncated; $('error-copy').disabled = !data.log;
      (data.images || []).forEach((image) => {
        let url; try { url = new URL(image.url, window.location.href); } catch (_) { return; }
        if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol)) return;
        url.searchParams.set('config', account);
        const wrap = el('div', 'error-image'), button = el('button'); button.type = 'button';
        const img = el('img'); img.src = url.href; img.alt = image.name || '错误现场截图'; img.loading = 'lazy';
        button.append(img); button.addEventListener('click', () => {
          if (!current()) return;
          $('image-preview').src = url.href; $('image-preview').alt = image.name || '错误现场截图'; $('image-dialog').showModal();
        });
        const link = el('a', '', `↓ ${image.name || '保存截图'}`); link.href = url.href; link.download = image.name || 'oas-error.png';
        wrap.append(button, link); $('error-images').append(wrap);
      });
    } catch (error) { if (current()) { $('error-date').textContent = '读取失败'; $('error-log').textContent = error.message; toast(error.message, 'error', () => { if (current()) openError(id); }); } }
  }

  function openPassword(required = false) {
    if (!state.loginRequired) return;
    $('password-form').reset(); $('password-error').hidden = true;
    $('password-title').textContent = required ? '设置你的新密码' : '修改密码';
    $('password-hint').textContent = required ? '首次登录请先更换初始密码，完成后即可操作任务与参数。' : '更新用于登录工作台的密码。';
    $('password-cancel').textContent = required ? '退出登录' : '取消';
    if (!$('password-dialog').open) $('password-dialog').showModal();
  }

  async function logout() {
    if (!state.loginRequired) return;
    if (!(await mayDiscard())) return;
    try { await api('/api/logout', { method: 'POST' }); resetSession(); toast('已退出登录。'); }
    catch (error) { toast(error.message, 'error', logout); }
  }

  function showActivity(value) {
    state.activity = value;
    $('log-center').hidden = value !== 'logs'; $('statistics-view').hidden = value !== 'stats';
    ['logs', 'stats'].forEach((kind) => { $(`activity-${kind}-tab`).classList.toggle('selected', kind === value); $(`activity-${kind}-tab`).setAttribute('aria-pressed', String(kind === value)); });
    syncStatisticsVisibility();
  }

  function statisticsVisible() {
    return state.authenticated && Boolean(state.account) && state.activity === 'stats' && !document.hidden && !$('app').hidden && document.querySelector('.logs-panel').getClientRects().length > 0;
  }

  function stopStatistics() {
    const stats = state.statistics;
    stats.active = false; stats.epoch++;
    clearTimeout(stats.timer); stats.timer = null;
    stats.request?.abort(); stats.request = null; stats.loading = false; stats.refreshPending = false;
  }

  function resetStatistics() {
    stopStatistics();
    Object.assign(state.statistics, { account: '', dates: [], datesUpdatedAt: null, date: '', data: null, source: null, complete: true, error: '', unsupported: false, updatedAt: null, openTasks: new Set() });
    renderStatistics();
  }

  function syncStatisticsVisibility() {
    const stats = state.statistics;
    if (!statisticsVisible()) { if (stats.active) { stopStatistics(); renderStatistics(); } return; }
    if (stats.account !== state.account) {
      resetStatistics(); stats.account = state.account;
    }
    if (!stats.active) { stats.active = true; loadStatistics(); }
  }

  function requestStatisticsRefresh() {
    if (!statisticsVisible()) return;
    const stats = state.statistics;
    if (stats.loading) { stats.refreshPending = true; return; }
    loadStatistics({ quiet: true });
  }

  async function loadStatistics({ quiet = false } = {}) {
    if (!statisticsVisible()) return;
    const stats = state.statistics;
    if (quiet && stats.loading) { stats.refreshPending = true; return; }
    clearTimeout(stats.timer); stats.timer = null; stats.request?.abort();
    const epoch = ++stats.epoch, account = state.account, accountEpoch = state.accountEpoch;
    const request = new AbortController(); stats.request = request; stats.active = true; stats.loading = true; stats.error = '';
    const current = () => stats.epoch === epoch && account === state.account && accountEpoch === state.accountEpoch && statisticsVisible();
    if (!quiet || !stats.data) renderStatistics();
    try {
      if (!quiet || !stats.data || !stats.datesUpdatedAt || Date.now() - stats.datesUpdatedAt >= 60000) {
        const dates = await api(`/api/accounts/${encoder(account)}/statistics/dates`, { signal: request.signal });
        if (!current()) return;
        if (!dates || !Array.isArray(dates.dates)) throw new ApiError('统计日期响应格式不正确，请重试。');
        stats.account = account; stats.source = dates.source || null; stats.unsupported = dates.supported === false || dates.status === 'unsupported';
        stats.complete = dates.statistics_complete !== false && !dates.source?.backfill_pending;
        stats.dates = [...new Set(dates.dates.filter((date) => typeof date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(date)))].sort().reverse();
        stats.datesUpdatedAt = Date.now();
      }
      const date = stats.dates.includes(stats.date) ? stats.date : stats.dates[0] || '';
      if (date !== stats.date) { stats.data = null; stats.openTasks.clear(); }
      stats.date = date;
      if (!date || stats.unsupported) { stats.data = null; stats.updatedAt = new Date(); return; }
      const day = await api(`/api/accounts/${encoder(account)}/statistics?date=${encoder(date)}`, { signal: request.signal });
      if (!current()) return;
      if (!day || !day.tasks || typeof day.tasks !== 'object' || Array.isArray(day.tasks)) throw new ApiError('统计内容响应格式不正确，请重试。');
      if (day.script_name && day.script_name !== account) throw new ApiError('统计配置与所选配置不一致，请刷新后重试。');
      stats.data = day; stats.source = day.source || stats.source; stats.complete = day.statistics_complete !== false && !day.source?.backfill_pending; stats.updatedAt = new Date();
    } catch (error) {
      if (current() && error.name !== 'AbortError') stats.error = error.message;
    } finally {
      if (current()) {
        stats.loading = false; stats.request = null; renderStatistics();
        const delay = stats.refreshPending ? 0 : 3000;
        stats.refreshPending = false;
        stats.timer = setTimeout(() => loadStatistics({ quiet: true }), delay);
      }
    }
  }

  function statisticsNumber(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
  }

  function statisticsMetric(record, key, value = record?.[key]) {
    const aliases = {
      total_runtime_seconds: ['runtime', 'total_duration', 'duration', 'duration_seconds'],
      total_task_run_count: ['run_count', 'task_runs'], total_battle_count: ['battle_count', 'battle'],
      total_duration_seconds: ['total_duration', 'duration', 'duration_seconds'], duration_seconds: ['duration'],
      run_count: ['task_runs'], battle_count: ['battle', 'total_battle_count'],
    };
    const available = record?.available_metrics;
    if (Array.isArray(available) && ![key, ...(aliases[key] || [])].some((metric) => available.includes(metric))) return null;
    return statisticsNumber(value);
  }

  function statisticsDuration(value) {
    if (value === null) return '未记录';
    const seconds = Math.round(value), hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    if (hours) return `${hours}小时 ${minutes}分`;
    if (minutes) return `${minutes}分 ${seconds % 60}秒`;
    return `${seconds}秒`;
  }

  function statisticsCount(value, suffix = '次') { return value === null ? '未记录' : `${new Intl.NumberFormat('zh-CN').format(value)}${suffix}`; }

  function statisticsTime(value, date) {
    if (typeof value !== 'string' || !value.trim()) return '未记录';
    const text = value.trim();
    return text.startsWith(date) ? text.substring(date.length).trim().replace(/^T/, '').replace(/\.\d+(?:Z)?$/, '') : text;
  }

  function statisticsRunGroups(runs, date) {
    const groups = [];
    for (const run of runs) {
      const count = statisticsMetric(run, 'battle_count', run.battle?.count);
      const start = typeof run.start_time === 'string' ? Date.parse(run.start_time.replace(' ', 'T')) : NaN;
      const end = typeof run.end_time === 'string' ? Date.parse(run.end_time.replace(' ', 'T')) : NaN;
      const mergeable = !(count > 0) && Number.isFinite(start) && Number.isFinite(end)
        && end >= start && run.start_time.startsWith(date);
      const previous = groups.at(-1);
      if (mergeable && previous?.mergeable && start >= previous.end) {
        previous.runs.push(run); previous.end = end;
      } else {
        groups.push({ runs: [run], mergeable, end });
      }
    }
    return groups.reverse();
  }

  function statisticsRunRow(run, date) {
    const row = el('div', 'statistics-run');
    const status = [run.continues_from_previous_date ? '承接前日' : '', run.continues_to_next_date ? '跨日继续' : '', { completed: '已结束', running: '进行中', interrupted: '已中断', incomplete: '未记录结束' }[run.status] || ''].filter(Boolean).join(' · ');
    const endTime = run.end_time ? `${run.status === 'incomplete' || run.status === 'running' ? '观测至 ' : ''}${statisticsTime(run.end_time, date)}` : run.status === 'running' ? '进行中' : '结束未记录';
    row.append(el('span', 'statistics-run-time', `${statisticsTime(run.start_time, date)} → ${endTime}`), el('span', 'statistics-run-duration', statisticsDuration(statisticsMetric(run, 'duration_seconds'))));
    if (status) row.append(el('span', `statistics-run-status ${run.status === 'running' ? 'is-running' : ''}`, status));
    const count = statisticsMetric(run, 'battle_count', run.battle?.count);
    row.append(el('span', 'statistics-run-detail', count !== null ? `确认结算 ${count} 次` : '结算数未记录'));
    return row;
  }

  function statisticsMergedRunRow(runs, date) {
    const first = runs[0], last = runs.at(-1), row = el('div', 'statistics-run');
    const durations = runs.map((run) => statisticsMetric(run, 'duration_seconds'));
    const duration = durations.some((value) => value === null) ? null : durations.reduce((sum, value) => sum + value, 0);
    const unknownCount = runs.some((run) => statisticsMetric(run, 'battle_count', run.battle?.count) === null);
    const statuses = [first.continues_from_previous_date ? '承接前日' : '', last.continues_to_next_date ? '跨日继续' : '', `合并 ${runs.length} 段`];
    for (const [status, label] of Object.entries({ completed: '已结束', running: '进行中', interrupted: '中断', incomplete: '未记录结束' })) {
      const count = runs.filter((run) => run.status === status).length;
      if (count) statuses.push(`${label} ${count} 段`);
    }
    row.append(el('span', 'statistics-run-time', `${statisticsTime(first.start_time, date)} → 观测至 ${statisticsTime(last.end_time, date)}`), el('span', 'statistics-run-duration', `累计 ${statisticsDuration(duration)}`));
    row.append(el('span', `statistics-run-status ${runs.some((run) => run.status === 'running') ? 'is-running' : ''}`, statuses.filter(Boolean).join(' · ')), el('span', 'statistics-run-detail', unknownCount ? '结算数未记录' : '确认结算 0 次'));
    return row;
  }

  function renderStatistics() {
    const stats = state.statistics, content = $('statistics-content'), select = $('statistics-date');
    select.replaceChildren();
    if (!stats.dates.length) { const option = el('option', '', stats.loading ? '正在读取…' : '暂无日期'); option.value = ''; select.append(option); }
    stats.dates.forEach((date) => { const option = el('option', '', date); option.value = date; select.append(option); });
    select.value = stats.date; select.disabled = !stats.dates.length || stats.unsupported;
    $('statistics-refresh').disabled = stats.loading || !state.account;
    $('statistics-view').setAttribute('aria-busy', String(stats.loading));
    const source = stats.source;
    const sourceLabel = typeof source === 'string' ? source : source?.label || (source?.kind === 'local_logs' ? '本地日志解析' : '运行统计');
    const detail = typeof source?.detail === 'string' ? source.detail : '';
    $('statistics-source').textContent = state.account ? `${state.account} · ${sourceLabel}` : '';
    $('statistics-method').hidden = !detail;
    $('statistics-method-detail').textContent = detail;
    $('statistics-status').textContent = stats.loading ? '正在读取统计…' : stats.updatedAt ? `${stats.complete ? '' : '部分日志仍在读取 · '}更新于 ${stats.updatedAt.toLocaleTimeString('zh-CN', { hour12: false })} · 可见时每 3 秒刷新` : '';
    $('statistics-error').hidden = !stats.error;
    $('statistics-error').textContent = stats.error ? `${stats.error}${stats.data ? ' 当前保留上次成功读取的统计。' : ''}` : '';
    if (!state.account) { empty(content, '请选择配置', '选择配置后查看已记录的运行统计。'); return; }
    if (!stats.data) {
      if (stats.loading) empty(content, '正在读取统计', '读取所选配置的日期与运行记录。');
      else if (stats.error) empty(content, '统计读取失败', '请使用上方刷新按钮重试。');
      else if (stats.unsupported) empty(content, '当前统计来源不可用', '当前后台未提供统计，也未绑定可读取的本地统计来源。');
      else if (!stats.complete) empty(content, '正在整理历史统计', '日志仍在分批读取，统计页保持打开后会继续更新。');
      else empty(content, '暂无历史统计', '此配置的可用日志中尚未发现统计记录。');
      return;
    }
    const day = stats.data, scrollTop = content.scrollTop, fragment = document.createDocumentFragment();
    const summary = el('div', 'statistics-summary');
    [['已记录耗时', statisticsDuration(statisticsMetric(day, 'total_runtime_seconds'))], ['运行记录', statisticsCount(statisticsMetric(day, 'total_task_run_count'), '段')], ['确认结算', statisticsCount(statisticsMetric(day, 'total_battle_count'))]].forEach(([title, value]) => {
      const item = el('div', 'statistics-total'); item.append(el('span', '', title), el('strong', '', value)); summary.append(item);
    });
    fragment.append(summary, el('p', 'statistics-note', source?.kind === 'local_logs' ? '按日志统计；无结束记录不代表仍在运行。“未记录”表示缺少依据。' : '“未记录”表示统计来源缺少对应数值。'));
    if (Array.isArray(day.warnings) && day.warnings.length) fragment.append(el('p', 'statistics-note', day.warnings.filter((warning) => typeof warning === 'string').join('；')));
    const tasks = Object.entries(day.tasks).filter(([, task]) => task && typeof task === 'object');
    tasks.sort((a, b) => (statisticsNumber(b[1].total_duration_seconds) ?? -1) - (statisticsNumber(a[1].total_duration_seconds) ?? -1) || a[0].localeCompare(b[0], 'zh-CN'));
    if (!tasks.length) fragment.append(el('p', 'statistics-note', '此日期没有可展示的任务记录。'));
    tasks.forEach(([name, task]) => {
      const card = el('details', 'statistics-task'); card.dataset.task = name; card.open = stats.openTasks.has(name);
      card.addEventListener('toggle', () => { if (card.open) stats.openTasks.add(name); else stats.openTasks.delete(name); });
      const heading = el('summary'), taskHeading = el('span', 'statistics-task-heading');
      taskHeading.append(el('strong', '', taskLabel(name) || name));
      const count = statisticsMetric(task, 'run_count');
      taskHeading.append(el('span', 'statistics-task-meta', `${count === null ? '运行次数未记录' : `${statisticsCount(count, '段')}运行`} · ${statisticsDuration(statisticsMetric(task, 'total_duration_seconds'))}`));
      heading.append(taskHeading, el('span', 'statistics-task-chevron', '⌄')); card.append(heading);
      const body = el('div', 'statistics-task-body');
      const completed = statisticsNumber(task.completed_run_count), running = statisticsNumber(task.running_run_count), interrupted = statisticsNumber(task.interrupted_run_count), incomplete = statisticsNumber(task.incomplete_run_count);
      const statuses = stats.complete ? [completed !== null ? `已结束 ${completed} 段` : '', running ? `进行中 ${running} 段` : '', interrupted ? `中断 ${interrupted} 段` : '', incomplete ? `结束未记录 ${incomplete} 段` : ''].filter(Boolean) : ['累计值等待日志读取完成'];
      const battles = statisticsMetric(task, 'battle_count', task.battle?.count);
      body.append(el('p', 'statistics-note', [...statuses, battles !== null ? `确认结算 ${battles} 次` : '结算数未记录'].join(' · ')));
      if (task.runs_truncated) body.append(el('p', 'statistics-note', '部分运行明细已省略，任务汇总包含全部已读取的记录。'));
      const runs = Array.isArray(task.runs) ? task.runs.filter((run) => run && typeof run === 'object') : [];
      if (!runs.length) body.append(el('p', 'statistics-note', '未记录具体运行时间。'));
      const groups = statisticsRunGroups(runs, stats.date);
      if (groups.some((group) => group.runs.length > 1)) body.append(el('p', 'statistics-note', '连续无结算记录已合并；耗时仅累加已记录的运行时间。'));
      groups.forEach((group) => {
        body.append(group.runs.length > 1 ? statisticsMergedRunRow(group.runs, stats.date) : statisticsRunRow(group.runs[0], stats.date));
      });
      card.append(body); fragment.append(card);
    });
    content.replaceChildren(fragment); content.scrollTop = scrollTop;
  }

  function setTheme(value) {
    const light = value === 'light'; document.body.classList.toggle('theme-light', light);
    ['dark', 'light'].forEach((kind) => { $(`theme-${kind}`).classList.toggle('selected', kind === value); $(`theme-${kind}`).setAttribute('aria-pressed', String(kind === value)); });
    document.querySelector('meta[name="theme-color"]').content = light ? '#fffbff' : '#313033';
    try { localStorage.setItem('oasx-web-theme', value); } catch (_) { }
  }

  function initialisePaneDividers() {
    const workspace = document.querySelector('.workspace');
    const apply = (kind, delta) => {
      if (window.innerWidth < 1120) return;
      const total = workspace.clientWidth - 24, account = document.querySelector('.accounts-panel').getBoundingClientRect().width + 8;
      if (kind === 'accounts') {
        const width = Math.max(260, Math.min(520, total - 24 - 720, account + delta)); workspace.style.setProperty('--account-width', `${width}px`);
      } else {
        const available = total - account - 24, details = document.querySelector('.tasks-panel').getBoundingClientRect().width + 8;
        const ratio = Math.max(360 / available, Math.min(1 - 360 / available, (details + delta) / available));
        workspace.style.setProperty('--detail-fr', `${ratio}fr`); workspace.style.setProperty('--log-fr', `${1 - ratio}fr`);
      }
    };
    document.querySelectorAll('[data-divider]').forEach((divider) => {
      let lastX = null;
      divider.addEventListener('pointerdown', (event) => { if (event.button !== 0) return; lastX = event.clientX; divider.setPointerCapture(event.pointerId); });
      divider.addEventListener('pointermove', (event) => { if (lastX === null) return; apply(divider.dataset.divider, event.clientX - lastX); lastX = event.clientX; });
      const finish = () => { lastX = null; }; divider.addEventListener('pointerup', finish); divider.addEventListener('pointercancel', finish);
      divider.addEventListener('keydown', (event) => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); apply(divider.dataset.divider, event.key === 'ArrowLeft' ? -10 : 10); } });
    });
  }

  $('preferences-open').addEventListener('click', () => $('preferences-dialog').showModal());
  const transferOpen = el('button', 'button subtle small', '配置备份与导入'); transferOpen.type = 'button';
  transferOpen.addEventListener('click', () => openTransfer());
  const transferRow = el('div', 'preference-row'); transferRow.append(el('span', '', '配置管理'), transferOpen);
  $('preferences-dialog').querySelectorAll('.preference-card')[1].append(transferRow);
  const taskTransferOpen = el('button', 'icon-button', '备份'); taskTransferOpen.type = 'button'; taskTransferOpen.title = '备份与导入任务参数';
  taskTransferOpen.addEventListener('click', () => openTransfer(true)); $('settings-view').querySelector('.settings-heading').append(taskTransferOpen);
  $('transfer-kind').addEventListener('change', renderTransferScope);
  $('transfer-backup').addEventListener('click', () => exportTransfer('backup')); $('transfer-share').addEventListener('click', () => exportTransfer('share'));
  $('transfer-import').addEventListener('click', importTransfer);
  $('transfer-close').addEventListener('click', () => { if (!state.transfer.busy) $('transfer-dialog').close(); });
  $('transfer-dialog').addEventListener('cancel', (event) => { if (state.transfer.busy) event.preventDefault(); });
  $('transfer-dialog').addEventListener('close', () => { state.transfer.epoch++; state.transfer.request?.abort(); });
  $('preferences-close').addEventListener('click', () => $('preferences-dialog').close());
  $('theme-dark').addEventListener('click', () => setTheme('dark')); $('theme-light').addEventListener('click', () => setTheme('light'));
  $('preferences-log-filter').addEventListener('change', () => setLogFilter($('preferences-log-filter').value));
  $('activity-logs-tab').addEventListener('click', () => showActivity('logs'));
  $('activity-stats-tab').addEventListener('click', () => showActivity('stats'));
  $('statistics-refresh').addEventListener('click', () => loadStatistics());
  $('statistics-date').addEventListener('change', () => {
    state.statistics.date = $('statistics-date').value; state.statistics.data = null; state.statistics.openTasks.clear();
    loadStatistics();
  });
  $('compact-overview-tab').addEventListener('click', () => setTaskScope('schedule'));
  $('compact-catalog-tab').addEventListener('click', () => setTaskScope('catalog'));
  $('compact-stats-tab').addEventListener('click', () => { showActivity('stats'); showMobile('logs'); });
  $('log-level').addEventListener('change', () => { state.logLevel = $('log-level').value; $('log-level').title = state.logLevel; $('log-level').parentElement.classList.toggle('is-filtered', state.logLevel !== 'ALL'); renderLogs(true); });
  $('account-state-filter').addEventListener('change', () => { state.accountFilter = $('account-state-filter').value; $('account-state-filter').parentElement.classList.toggle('is-filtered', state.accountFilter !== 'all'); renderAccounts(); });
  $('power-task').addEventListener('click', () => performAction(state.snapshots.get(state.account)?.state === 0 ? 'start' : 'stop'));
  initialisePaneDividers();
  try { setTheme(localStorage.getItem('oasx-web-theme') === 'light' ? 'light' : 'dark'); } catch (_) { setTheme('dark'); }

  $('login-form').addEventListener('submit', async (event) => {
    if (!state.loginRequired) { event.preventDefault(); return; }
    event.preventDefault(); $('login-error').hidden = true; $('login-submit').disabled = true;
    try {
      const session = await api('/api/login', { method: 'POST', body: { username: $('login-username').value.trim(), password: $('login-password').value } });
      applySession(session); $('login-password').value = ''; await bootstrap({ keepAccount: false });
    } catch (error) {
      $('login-error').textContent = error.message; $('login-error').hidden = false;
      if (state.authenticated) { $('loading-screen').hidden = true; $('login-screen').hidden = false; }
    } finally { $('login-submit').disabled = false; }
  });
  $('password-form').addEventListener('submit', async (event) => {
    if (!state.loginRequired) { event.preventDefault(); return; }
    event.preventDefault(); $('password-error').hidden = true;
    if ($('new-password').value !== $('confirm-password').value) { $('password-error').textContent = '两次输入的新密码不一致。'; $('password-error').hidden = false; return; }
    $('password-submit').disabled = true; $('password-cancel').disabled = true;
    try {
      const session = await api('/api/password', { method: 'POST', body: { current_password: $('current-password').value, new_password: $('new-password').value } });
      applySession(session || {}); state.mustChangePassword = false;
      $('password-form').reset(); $('password-dialog').close(); renderSelectedAccount(); updateSaveBar(); toast('密码已更新。');
    } catch (error) { $('password-error').textContent = error.message; $('password-error').hidden = false; }
    finally { $('password-submit').disabled = false; $('password-cancel').disabled = false; }
  });
  $('password-dialog').addEventListener('cancel', (event) => { if (state.mustChangePassword) event.preventDefault(); });
  $('password-cancel').addEventListener('click', () => state.mustChangePassword ? logout() : $('password-dialog').close());
  $('password-open').addEventListener('click', () => { $('preferences-dialog').close(); openPassword(state.mustChangePassword); });
  $('mobile-password-open').addEventListener('click', () => openPassword(state.mustChangePassword));
  $('logout').addEventListener('click', () => { $('preferences-dialog').close(); logout(); });
  $('account-search').addEventListener('input', renderAccounts);
  $('task-search').addEventListener('input', renderTasks);
  $('scope-schedule').addEventListener('click', () => setTaskScope('schedule'));
  $('scope-catalog').addEventListener('click', () => setTaskScope('catalog'));
  $('focus-settings').addEventListener('click', () => { if (state.focusTask) selectTask(state.focusTask); });
  $('open-global-settings').addEventListener('click', () => { showMobile('tasks'); selectTask('GlobalGame'); });
  $('open-script-settings').addEventListener('click', () => { showMobile('tasks'); selectTask('Script'); });
  $('refresh-accounts').addEventListener('click', async () => {
    if (!(await mayDiscard())) return;
    $('refresh-accounts').disabled = true;
    try { await bootstrap({ keepAccount: false }); } catch (error) { toast(error.message, 'error'); }
    finally { $('refresh-accounts').disabled = false; }
  });
  $('tasks-tab').addEventListener('click', () => closeTaskParameters().then((closed) => { if (closed) renderTasks(); }));
  $('settings-tab').addEventListener('click', () => showWorkTab('settings'));
  $('refresh-settings').addEventListener('click', () => { if (state.task) selectTask(state.task, true); });
  $('save-settings').addEventListener('click', saveSettings);
  $('discard-settings').addEventListener('click', discardSettings);
  $('start-task').addEventListener('click', () => performAction('start'));
  $('stop-task').addEventListener('click', () => performAction('stop'));
  $('reconnect').addEventListener('click', reconnectCurrent);
  $('logs-tab').addEventListener('click', () => showLogTab('logs'));
  $('errors-tab').addEventListener('click', () => showLogTab('errors'));
  $('log-follow').addEventListener('click', () => setLogFollow(!state.followLogs));
  $('log-resume').addEventListener('click', () => setLogFollow(true));
  $('log-filter-key').addEventListener('click', () => setLogFilter('key'));
  $('log-filter-all').addEventListener('click', () => setLogFilter('all'));
  $('log-copy').addEventListener('click', () => copyText(state.logs.filter(visibleLog).join('\n')));
  $('log-clear').addEventListener('click', () => { state.logs = []; state.pendingLogs = []; renderLogs(); });
  $('log-content').addEventListener('wheel', (event) => { if (event.deltaY < 0 && state.followLogs) setLogFollow(false); }, { passive: true });
  $('refresh-errors').addEventListener('click', loadErrors);
  $('error-back').addEventListener('click', () => { state.errorEpoch++; $('error-detail').hidden = true; $('error-list').hidden = false; renderErrors(); });
  $('error-copy').addEventListener('click', () => copyText(state.errorDetail?.log || ''));
  $('image-close').addEventListener('click', () => $('image-dialog').close());
  document.querySelectorAll('[data-mobile-target]').forEach((button) => button.addEventListener('click', () => showMobile(button.dataset.mobileTarget)));
  window.addEventListener('beforeunload', (event) => { if (dirtyFields().length || state.saving) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('offline', () => { if (state.authenticated) { state.backendOnline = false; updateConnection('网络已断开。恢复连接后会继续接收状态与日志。'); } });
  window.addEventListener('online', () => { if (state.authenticated) { reconnectCurrent(); syncSnapshotVisibility({ force: true, full: true }); requestStatisticsRefresh(); } });
  document.addEventListener('visibilitychange', () => {
    syncSnapshotVisibility({ force: true, full: true }); syncStatisticsVisibility();
    if (document.visibilityState === 'visible' && state.authenticated && !state.socket) reconnectCurrent();
  });
  window.addEventListener('resize', syncStatisticsVisibility);
  window.addEventListener('pagehide', () => { stopSnapshotRefresh(); stopStatistics(); });
  window.addEventListener('pageshow', () => { syncSnapshotVisibility({ force: true, full: true }); syncStatisticsVisibility(); });

  (async () => {
    try {
      const session = await api('/api/session');
      if (!session?.authenticated) { resetSession(); return; }
      applySession(session); await bootstrap({ keepAccount: false });
    } catch (error) {
      resetSession(); toast(error.message, 'error', () => window.location.reload());
    }
  })();
})();
