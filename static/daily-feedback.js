(() => {
  'use strict';
  const labels = {talisman: '花合战今日经验', collective: '寮集体任务'};
  const taskLabels = {Dokan: '道馆', CollectiveMissions: '寮集体任务', AbyssShadows: '峡间暗域', GuildBanquet: '寮宴会', DemonRetreat: '首领退治'};
  const outcomeLabels = {expired: '等待开场超时', failed: '执行失败', skipped: '已跳过', unconfirmed: '未核验', blocked_by_dokan: '道馆待核验，联动任务未执行'};
  const notificationLabels = {pending: '尚未发送', sending: '正在发送', accepted: '推送服务已接收', failed: '推送失败', disabled: '未启用推送'};
  const completionLabels = {completed: '已完成', pending: '待开始', unverified: '待核验', disabled: '未开启', not_scheduled: '今日无需运行'};
  const visibleAccounts = new Set([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]);
  function visibleData(data) {
    const accounts = data.accounts.filter(row => {
      const match = /^(\d{1,2})(?:[-_\s]|$)/.exec(row.account);
      return match && visibleAccounts.has(Number(match[1]));
    });
    const counts = Object.fromEntries(['attention', 'verified', 'pending', 'running', 'unverified', 'disabled'].map(status =>
      [status, accounts.filter(row => row.status === status).length]));
    return {...data, accounts, counts};
  }
  const state = {day: '', today: '', data: null, selected: null, loading: false, detailSignature: '', requestId: 0, request: null};
  function localDay() {
    const parts = new Intl.DateTimeFormat('en', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(new Date());
    return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type).value).join('-');
  }
  function clock(value) {
    if (!value) return '尚未采集';
    const match = String(value).match(/T(\d{2}:\d{2})(?::\d{2})?/);
    return match ? `${match[1]} 采集` : '时间未核验';
  }
  function metric(item, kind) {
    if (item && item.verified && Number.isInteger(item.current)) return kind === 'collective' ? `${item.current}/${item.total}` : String(item.current);
    if (item && !item.expected && item.status !== 'captured') return '今日无需验收';
    return '未核验';
  }
  function imageUrl(account, day, image) {
    return `/api/daily-feedback/${encodeURIComponent(account)}/images/${encodeURIComponent(image)}?date=${encodeURIComponent(day)}`;
  }
  function targetSummary(item) {
    if (!item || !item.verified) return '';
    if (!Number.isInteger(item.target)) return '经验已读取 · 目标未核验';
    return `目标 ${item.target} · ${item.current >= item.target ? '已达标' : '未达标'}`;
  }
  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }
  function displayText(value, fallback = '') {
    return typeof value === 'string' && value.trim() ? value.trim() : fallback;
  }
  function taskCompletion(row) {
    const source = row && row.task_completion && (row.task_completion.service_checklist || row.task_completion);
    const unavailable = note => ({available: false, tasks: [], counts: {completed: 0, incomplete: 0, unverified: 0}, note});
    if (!source || typeof source !== 'object') return unavailable('任务完成情况待核验');
    if (!['poster_checklist', 'enabled_today', 'executed_today', 'scheduled_today', 'historical_planned', 'historical_executed'].includes(source.scope)) return unavailable('任务完成情况待核验');
    if (source.date !== row.date) return unavailable('任务完成情况与所选日期不一致，待核验');
    if (source.available !== true) return unavailable(displayText(source.note, '该日期任务完成情况待核验'));
    if (!Array.isArray(source.tasks)) return unavailable('任务完成情况待核验');
    const counts = Object.fromEntries(Object.keys(completionLabels).map(status => [status, 0]));
    const tasks = source.tasks.map((entry, index) => {
      const item = entry && typeof entry === 'object' ? entry : {};
      const task = displayText(item.task, `任务 ${index + 1}`);
      const legacyStatus = item.status === 'incomplete' ? 'unverified' : item.status;
      const status = typeof legacyStatus === 'string' && Object.prototype.hasOwnProperty.call(completionLabels, legacyStatus) ? legacyStatus : 'unverified';
      counts[status]++;
      return {task, group: item.group === 'weekly' ? 'weekly' : 'daily', label: displayText(item.label, Object.prototype.hasOwnProperty.call(taskLabels, task) ? taskLabels[task] : task), status,
        status_label: task === 'CourtyardAffairs' && status === 'unverified' &&
          ['没找到入口', '已无可领取', '未确认完成'].includes(item.status_label) ? item.status_label : completionLabels[status], reason: displayText(item.reason, '未提供核验说明'),
        weekly_progress: item.weekly_progress || null,
        evidence_at: displayText(item.evidence_at) || null,
        completion_count: task === 'KekkaiUtilize' && Number.isInteger(item.completion_count) && item.completion_count >= 0 && item.completion_count <= 1000 ? item.completion_count : null};
    });
    return {available: true, scope: source.scope, tasks, counts, note: displayText(source.note)};
  }
  function taskEvidenceTime(value) {
    if (!value) return '未记录证据时间';
    let stamp = String(value).trim().replace(' ', 'T');
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/.test(stamp)) return '证据时间待核验';
    if (!/(?:Z|[+-]\d{2}:?\d{2})$/.test(stamp)) stamp += '+08:00';
    const date = new Date(stamp);
    if (Number.isNaN(date.getTime())) return '证据时间待核验';
    const parts = new Intl.DateTimeFormat('en', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).formatToParts(date);
    const part = type => parts.find(item => item.type === type).value;
    return `证据时间：${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}`;
  }
  const weeklyTasks = new Set(['Dokan', 'WeeklyTrifles']);
  function taskResult(item) {
    const weekly = item.weekly_progress;
    if (weekly && ['SixRealms', 'Chess', 'Dokan', 'Kirin', 'AbyssShadows', 'Netherworld'].includes(item.task)) {
      const value = Number.isInteger(weekly.value) && weekly.value >= 0 ? weekly.value : null;
      const label = item.task === 'Chess' ? `${value === null ? '—' : value}/600` : `${value === null ? '—' : value}次`;
      return label;
    }
    if (item.status === 'disabled' || item.status === 'not_scheduled' || item.status === 'pending') return item.status_label;
    if (item.task === 'KekkaiUtilize') {
      return Number.isInteger(item.completion_count) ? `${item.completion_count}次` : '待核验';
    }
    if (item.task === 'AreaBoss') {
      const match = /(?:已成功处理|已核验)\s+(\d{1,3})\/([1-3])\s*只/.exec(item.reason);
      if (match) return `${match[1]}/${match[2]}`;
    }
    return item.status_label;
  }
  function renderTaskCompletion(row, detailed = false) {
    const report = taskCompletion(row);
    const section = element(detailed ? 'section' : 'div', detailed ? 'detail-section task-completion' : 'task-overview');
    section.append(element(detailed ? 'h3' : 'span', 'task-heading', report.scope && report.scope.startsWith('historical_') ? '当日任务完成情况' : '今日任务完成情况'));
    if (!report.available) {
      section.append(element('p', 'task-note', report.note));
      return section;
    }
    if (!report.tasks.length) {
      section.append(element('p', 'task-note', report.scope && report.scope.startsWith('historical_') ? '当日没有可恢复的其它任务记录' : report.scope === 'scheduled_today' ? '今天没有安排其它任务' : report.scope === 'executed_today' ? '今天尚无实际运行的其它任务' : '今天没有启用其它任务'));
      if (report.note) section.append(element('p', 'task-note', report.note));
      return section;
    }
    const counts = element('div', 'task-counts');
    Object.keys(completionLabels).filter(status => report.scope === 'poster_checklist' ? report.counts[status] : ['completed', 'pending', 'unverified'].includes(status)).forEach(status => counts.append(element('span', `task-state ${status}`, `${completionLabels[status]} ${report.counts[status]}`)));
    if (detailed || report.scope !== 'poster_checklist') section.append(counts);
    const poster = report.scope === 'poster_checklist';
    const isWeekly = item => poster ? item.group === 'weekly' : weeklyTasks.has(item.task);
    const groups = [[poster ? '每日任务：' : '日常', report.tasks.filter(item => !isWeekly(item))],
      [poster ? '每周任务：' : '周常', report.tasks.filter(isWeekly)]];
    groups.forEach(([name, tasks]) => {
      if (!tasks.length) return;
      const group = element('div', 'task-group');
      group.append(element('h4', 'task-group-heading', name));
      const list = element('ul', detailed ? 'task-list' : 'task-chips');
      tasks.forEach((item, index) => {
        const entry = element('li', detailed ? 'task-item' : 'task-chip');
        const line = detailed ? element('div', 'task-line') : entry;
        line.append(element(detailed ? 'strong' : 'span', detailed ? 'task-name' : 'task-chip-label', `${poster ? `${index + 1}、` : ''}${item.label}`),
          element('span', `task-state ${item.status}`, taskResult(item)));
        if (detailed) entry.append(line, element('p', 'task-reason', item.reason),
          element('p', 'task-time', taskEvidenceTime(item.evidence_at)));
        list.append(entry);
      });
      group.append(list); section.append(group);
    });
    if (detailed && report.note) section.append(element('p', 'task-note', report.note));
    return section;
  }
  function screenshotStatusLabel(row) { return `截图验收：${displayText(row.status_label, '待核验')}`; }
  function badge(row) { return element('span', `status ${row.status}`, screenshotStatusLabel(row)); }
  function renderSummary(data) {
    const target = document.getElementById('summary');
    target.replaceChildren();
    const items = [['attention', '截图需关注', data.counts.attention], ['verified', '截图已核验', data.counts.verified], ['pending', data.date === data.today ? '截图待收尾' : '截图未核验', data.counts.pending + data.counts.running + data.counts.unverified], ['disabled', '截图未启用', data.counts.disabled]];
    items.forEach(([key, label, count]) => {
      const card = element('div', `summary-item ${key}`);
      card.append(element('span', '', label), element('strong', '', count));
      target.append(card);
    });
  }
  function renderRows(data) {
    const target = document.getElementById('account-list');
    target.replaceChildren();
    if (!data.accounts.length) { target.append(element('div', 'empty', '没有可读取的账号配置。')); return; }
    [...data.accounts].sort((a, b) => a.account.localeCompare(b.account, 'zh-CN', {numeric: true})).forEach(row => {
      const card = element('button', `account-card ${row.status}`);
      card.type = 'button';
      card.setAttribute('aria-label', `${row.account}，${screenshotStatusLabel(row)}，查看任务完成情况与验收截图`);
      const identity = element('div', 'account-identity');
      identity.append(element('strong', 'account-name', row.account), badge(row));
      card.append(identity);
      Object.keys(labels).forEach(kind => {
        const item = row.evidence[kind];
        const value = metric(item, kind);
        const metricNode = element('div', 'metric');
        metricNode.append(element('span', 'metric-label', labels[kind]), element('strong', `metric-value${item.verified ? '' : ' neutral'}`, value));
        if (kind === 'talisman' && item.verified) metricNode.append(element('span', 'metric-target', targetSummary(item)));
        metricNode.append(element('span', 'metric-time', clock(item.captured_at)));
        card.append(metricNode);
      });
      card.append(renderTaskCompletion(row), element('span', 'card-action', '查看详情与截图 ›'));
      card.addEventListener('click', () => openDetails(row.account));
      target.append(card);
    });
  }
  function openImage(row, kind) {
    const item = row.evidence[kind];
    if (!item.image) return;
    const source = imageUrl(row.account, row.date, item.image);
    const image = document.getElementById('image-preview');
    image.src = source;
    image.alt = `${row.account} ${row.date} ${labels[kind]}游戏截图`;
    document.getElementById('image-label').textContent = `${row.account} · ${labels[kind]}`;
    const download = document.getElementById('image-download');
    download.href = source;
    download.download = `${row.date}-${row.account}-${kind}.png`;
    document.getElementById('image-dialog').showModal();
  }
  function renderDetails(row) {
    document.getElementById('detail-date').textContent = row.date;
    document.getElementById('detail-account').textContent = row.account;
    const target = document.getElementById('detail-body');
    target.replaceChildren();
    const overview = element('div', 'detail-status');
    overview.append(badge(row), element('p', '', row.reason));
    target.append(overview);
    target.append(renderTaskCompletion(row, true));
    const grid = element('div', 'evidence-grid');
    Object.keys(labels).forEach(kind => {
      const item = row.evidence[kind];
      const card = element('section', 'evidence-card');
      const heading = element('div', 'evidence-heading');
      heading.append(element('h3', '', labels[kind]), element('strong', '', metric(item, kind)));
      card.append(heading);
      if (item.image) {
        const button = element('button', 'evidence-photo');
        button.type = 'button';
        button.setAttribute('aria-label', `放大${labels[kind]}截图`);
        const image = element('img');
        image.src = imageUrl(row.account, row.date, item.image);
        image.alt = `${row.account} ${labels[kind]}截图`;
        image.loading = 'lazy';
        image.addEventListener('error', () => { button.replaceWith(element('div', 'evidence-empty', '截图暂时无法读取，请刷新重试。')); });
        button.append(image);
        button.addEventListener('click', () => openImage(row, kind));
        card.append(button);
      } else card.append(element('div', 'evidence-empty', item.expected ? '尚无当天游戏画面，不能确认完成情况。' : '今天未启用这项验收任务。'));
      const information = [clock(item.captured_at), item.final ? '收尾截图' : (item.image ? '阶段截图，尚未收尾' : ''), item.detail].filter(Boolean).join(' · ');
      if (kind === 'talisman') card.append(element('p', 'evidence-info', `花合战活跃进度（今日获得经验）${item.verified ? ' · ' + targetSummary(item) : ''}`));
      card.append(element('p', 'evidence-info', information));
      if (item.capture_error) {
        const failedAt = item.last_attempt_at ? `${clock(item.last_attempt_at).replace(' 采集', '')} ` : '';
        card.append(element('p', 'evidence-info warning', `${failedAt}最新采集失败：${item.capture_error}${item.image ? '。上方保留之前的截图，请留意采集时间。' : ''}`));
      }
      grid.append(card);
    });
    target.append(grid);
    const closeout = element('section', 'detail-section');
    closeout.append(element('h3', '', '当天收尾'));
    closeout.append(element('p', '', row.closeout.phase === 'final' ? '已生成当天收尾记录。' : '尚未生成最终收尾记录。'));
    if (row.closeout.waiting.length) closeout.append(element('p', '', `${row.closeout.phase === 'final' ? '未核验活动' : '等待'}：${row.closeout.waiting.map(task => taskLabels[task] || task).join('、')}`));
    if (row.closeout.completed.length) closeout.append(element('p', '', `已核实活动结果：${row.closeout.completed.map(task => taskLabels[task] || task).join('、')}`));
    if (row.closeout.incomplete.length) {
      const list = element('ul');
      row.closeout.incomplete.forEach(item => list.append(element('li', 'warning', `${item.label}：${outcomeLabels[item.outcome] || '待核验'}`)));
      closeout.append(list);
    }
    if (row.settings.talisman_enabled && !row.settings.dynamic_closeout_enabled) closeout.append(element('p', 'warning', '动态收尾尚未启用，花合战将按现有排程执行。'));
    target.append(closeout);
    const notification = element('section', 'detail-section');
    notification.append(element('h3', '', '手机通知'), element('p', row.notification.status === 'failed' ? 'warning' : '', notificationLabels[row.notification.status] || '尚未发送'));
    if (row.notification.at) notification.append(element('p', '', `通知时间：${row.notification.at.replace('T', ' ')}`));
    target.append(notification);
  }
  function openDetails(account) {
    const row = state.data && state.data.accounts.find(item => item.account === account);
    if (!row) return;
    state.selected = account;
    state.detailSignature = JSON.stringify(row);
    renderDetails(row);
    const dialog = document.getElementById('detail-dialog');
    if (!dialog.open) dialog.showModal();
    updateUrl();
  }
  function updateUrl() {
    const params = new URLSearchParams({date: state.day});
    if (state.selected) params.set('account', state.selected);
    history.replaceState(null, '', `/daily-feedback?${params}`);
  }
  async function requestReport(day, signal, timeoutMs = 15000) {
    const controller = new AbortController();
    let timer, cancel;
    const deadline = new Promise((resolve, reject) => {
      cancel = () => { const error = new Error('请求已取消。'); error.name = 'AbortError'; reject(error); controller.abort(); };
      timer = setTimeout(() => { reject(new Error('读取超时，请检查网络后刷新。')); controller.abort(); }, timeoutMs);
      if (signal.aborted) cancel(); else signal.addEventListener('abort', cancel, {once: true});
    });
    try {
      return await Promise.race([deadline, (async () => {
        const response = await fetch(`/api/daily-feedback?date=${encodeURIComponent(day)}`, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
        const data = response.status === 401 ? null : await response.json();
        return {response, data};
      })()]);
    } finally { clearTimeout(timer); signal.removeEventListener('abort', cancel); }
  }
  function cancelRefresh() {
    ++state.requestId;
    state.request?.abort(); state.request = null; state.loading = false;
    document.getElementById('refresh').disabled = false;
  }
  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    const requestId = ++state.requestId;
    const day = state.day;
    const request = new AbortController(); state.request = request;
    document.getElementById('refresh').disabled = true;
    try {
      let {response, data} = await requestReport(day, request.signal);
      if (requestId !== state.requestId || day !== state.day) return;
      if (response.status === 401) { document.getElementById('login-notice').hidden = false; return; }
      if (!response.ok) throw new Error(data.detail || '读取验收记录失败');
      if (requestId !== state.requestId || day !== state.day) return;
      data = visibleData(data);
      state.data = data;
      state.today = data.today;
      document.getElementById('error').hidden = true;
      document.getElementById('login-notice').hidden = true;
      document.getElementById('report-date').max = state.today;
      document.getElementById('next-day').disabled = state.day >= state.today;
      renderSummary(data);
      renderRows(data);
      document.getElementById('refresh-time').textContent = `更新于 ${new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit'}).format(new Date())}`;
      if (state.selected) {
        const row = data.accounts.find(item => item.account === state.selected);
        if (row && JSON.stringify(row) !== state.detailSignature) openDetails(row.account);
        else if (!row) document.getElementById('detail-dialog').close();
      }
    } catch (error) {
      if (requestId !== state.requestId || day !== state.day || error.name === 'AbortError') return;
      const notice = document.getElementById('error');
      notice.textContent = error.message || '读取失败，请稍后刷新。';
      notice.hidden = false;
    } finally {
      if (requestId === state.requestId) {
        state.loading = false; state.request = null;
        document.getElementById('refresh').disabled = false;
      }
    }
  }
  function setDay(day) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || day > state.today) return;
    cancelRefresh();
    state.day = day;
    state.selected = null;
    state.detailSignature = '';
    document.getElementById('detail-dialog').close();
    document.getElementById('report-date').value = day;
    updateUrl();
    refresh();
  }
  function moveDay(direction) {
    const value = new Date(`${state.day}T12:00:00Z`);
    value.setUTCDate(value.getUTCDate() + direction);
    setDay(value.toISOString().slice(0, 10));
  }
  function initialize() {
    state.today = localDay();
    const params = new URLSearchParams(location.search);
    const requestedDay = params.get('date');
    state.day = requestedDay && /^\d{4}-\d{2}-\d{2}$/.test(requestedDay) && requestedDay <= state.today ? requestedDay : state.today;
    state.selected = params.get('account');
    document.getElementById('report-date').value = state.day;
    document.getElementById('report-date').max = state.today;
    document.getElementById('report-date').addEventListener('change', event => setDay(event.target.value));
    document.getElementById('previous-day').addEventListener('click', () => moveDay(-1));
    document.getElementById('next-day').addEventListener('click', () => moveDay(1));
    document.getElementById('today').addEventListener('click', () => setDay(localDay()));
    document.getElementById('refresh').addEventListener('click', refresh);
    document.getElementById('detail-close').addEventListener('click', () => document.getElementById('detail-dialog').close());
    document.getElementById('detail-dialog').addEventListener('close', () => { state.selected = null; state.detailSignature = ''; updateUrl(); });
    document.getElementById('image-close').addEventListener('click', () => document.getElementById('image-dialog').close());
    document.getElementById('image-dialog').addEventListener('close', () => { document.getElementById('image-preview').removeAttribute('src'); });
    document.addEventListener('visibilitychange', () => { if (document.hidden) cancelRefresh(); else refresh(); });
    window.addEventListener('pagehide', cancelRefresh);
    setInterval(() => { if (!document.hidden) refresh(); }, 30000);
    refresh();
  }
  // Pure display helpers are exported for offline tests; no UI or OAS actions.
  if (typeof module !== 'undefined' && module.exports) module.exports = {metric, clock, imageUrl, localDay, targetSummary, taskCompletion, taskEvidenceTime, taskResult, visibleData};
  if (typeof document !== 'undefined') initialize();
})();
