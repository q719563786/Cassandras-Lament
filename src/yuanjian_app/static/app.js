const token = new URLSearchParams(location.search).get('token') || '';
const content = document.querySelector('#content');
const title = document.querySelector('#page-title');
const caption = document.querySelector('#page-caption');
const runButton = document.querySelector('#run-cognition');
const connection = document.querySelector('#connection');
const unreadBadge = document.querySelector('#header-unread');
const toast = document.querySelector('#toast');
const UI = window.YuanJianUI;
const RiskUI = window.YuanJianRiskUI;

const state = {
  view: 'today', metric: 'all',
  clusters: {limit: 10, offset: 0, q: '', category: '', evidence: ''},
  world: {limit: 10, offset: 0, q: ''},
  notifications: {limit: 20, offset: 0, status: 'unread'},
  benefit: 'interests', system: 'settings'
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {'Content-Type': 'application/json', 'X-YuanJian-Token': token, ...(options.headers || {})}
  });
  let payload;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) throw new Error(payload.error?.message || '本地请求失败');
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function showToast(message, kind = 'success') {
  toast.textContent = message;
  toast.className = `toast ${kind}`;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 4200);
}

function showLoading(message = '正在读取本地账本…') {
  content.innerHTML = `<div class="loading-block"><span class="spinner"></span>${escapeHtml(message)}</div>`;
}

function showPageError(error) {
  content.innerHTML = `<div class="state-panel error-state"><h2>这次没有加载成功</h2><p>${escapeHtml(error.message)}</p><button class="button retry-current" type="button">重试</button></div>`;
  document.querySelector('.retry-current')?.addEventListener('click', () => setView(state.view));
}

async function withBusy(button, busyText, operation) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = busyText;
  try { return await operation(); }
  finally { button.disabled = false; button.textContent = original; }
}

function setHeader(heading, description, allowRun = false) {
  title.textContent = heading;
  caption.textContent = description;
  runButton.hidden = !allowRun;
}

function updateChrome(status) {
  unreadBadge.textContent = status.unread_notifications ?? 0;
  connection.innerHTML = `<span class="status-dot"></span><span>后台监控 · ${status.tasks ? '运行中' : '已连接'}</span>`;
}

function metricsHtml(status) {
  const cells = [
    ['all', status.clusters, '重要事件'],
    ['judge', status.needs_judgment, '等你判断'],
    ['unread', status.unread_notifications, '未读提醒']
  ];
  return `<div class="metrics" aria-label="快捷筛选">${cells.map(([key, value, label]) => `<button class="metric-button ${state.metric === key ? 'selected' : ''}" type="button" data-filter="${key}" aria-pressed="${state.metric === key}"><strong>${escapeHtml(value)}</strong><span>${label}</span></button>`).join('')}</div>`;
}

function paginationHtml(page, name) {
  const range = UI.pageRange(page.total, page.limit, page.offset);
  return `<div class="pagination"><span>${range.total ? `第 ${range.start}–${range.end} 条，共 ${range.total} 条` : '没有结果'}</span><div><button class="button page-button" data-page-name="${name}" data-direction="prev" data-total="${page.total}" ${page.offset <= 0 ? 'disabled' : ''}>上一页</button><button class="button page-button" data-page-name="${name}" data-direction="next" data-total="${page.total}" ${page.offset + page.limit >= page.total ? 'disabled' : ''}>下一页</button></div></div>`;
}

function clusterRow(item) {
  return `<button class="event-row cluster-open" type="button" data-id="${escapeHtml(item.cluster_id)}">
    <span class="event-dot evidence-${escapeHtml(item.evidence_level)}"></span>
    <span class="event-body"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.summary || '等待更多公开摘要')}</span><span class="meta"><em>${escapeHtml(UI.evidenceLabel(item.evidence_level))}</em><em>${escapeHtml(item.independent_domains)} 个独立来源</em><em>${item.needs_judgment ? '等待研判' : '研判完成'}</em></span></span>
    <time>${escapeHtml(UI.formatLocalTime(item.last_seen_at))}</time>
  </button>`;
}

function notificationRow(item) {
  return `<article class="notification-row ${item.status === 'unread' ? 'is-unread' : ''}" data-id="${escapeHtml(item.notification_id)}">
    <div><span class="badge">${escapeHtml(item.alert_level)}</span><strong>${escapeHtml(item.reason || '需要查看的本地提醒')}</strong><p>${escapeHtml(UI.statusLabel(item.status))} · ${escapeHtml(UI.formatLocalTime(item.created_at))}</p></div>
    <div class="row-actions">${item.cluster_id ? `<button class="button notification-open-cluster" data-cluster-id="${escapeHtml(item.cluster_id)}">查看事件</button>` : ''}${item.status === 'unread' ? `<button class="button notification-read" data-id="${escapeHtml(item.notification_id)}">标为已读</button>` : ''}</div>
  </article>`;
}

function bindMetricButtons() {
  document.querySelectorAll('.metric-button').forEach(button => button.addEventListener('click', () => {
    state.metric = button.dataset.filter;
    state.clusters = UI.applyMetricFilter(state.clusters, state.metric);
    state.notifications.offset = 0;
    renderRiskHome().catch(showPageError);
  }));
}

function bindPaging(page, render) {
  document.querySelectorAll(`.page-button[data-page-name="${page}"]`).forEach(button => button.addEventListener('click', () => {
    state[page] = UI.movePage(state[page], button.dataset.direction, Number(button.dataset.total));
    render().catch(showPageError);
  }));
}

function riskCard(item, index) {
  return `<article class="risk-card risk-${escapeHtml(item.mode)} ${index === 0 ? 'risk-first' : ''}">
    <div class="risk-card-head"><span>${index === 0 ? '最先做' : '接下来'}</span><span>${escapeHtml(item.interest_name)}</span></div>
    <div class="risk-advice"><strong>建议</strong><h2>${escapeHtml(item.advice || item.action)}</h2></div>
    <p class="risk-reason"><strong>为什么：</strong>${escapeHtml(item.reason || item.title)}</p>
    <div class="risk-footer"><span><strong>最迟：</strong>${escapeHtml(item.time_window)}</span><span class="risk-level">风险：${escapeHtml(item.risk_label || item.risk_level)}</span></div>
    <div class="row-actions"><button class="button risk-evidence" type="button" data-id="${escapeHtml(item.cluster_id)}">查看原因</button></div>
  </article>`;
}

async function renderRiskHome(runNotice = null) {
  state.view = 'today';
  setHeader('现在该做什么', '直接看建议；需要时再看原因', true);
  showLoading('正在整理你需要知道的风险…');
  const [status, dashboard] = await Promise.all([
    api('/api/cognition/status'), api('/api/risk-dashboard')
  ]);
  updateChrome(status);
  const risks = RiskUI.visibleRisks(dashboard.items);
  content.innerHTML = `
    ${runNotice ? `<div class="run-notice ${escapeHtml(runNotice.kind)}">${escapeHtml(runNotice.text)}</div>` : ''}
    <section class="risk-overview state-${escapeHtml(dashboard.state)}"><span>${escapeHtml(RiskUI.overviewLabel(dashboard.state))}</span><h2>${escapeHtml(dashboard.summary)}</h2><button id="tell-yuanjian" class="button button-primary" type="button">告诉远见新情况</button></section>
    <section class="risk-list" aria-label="行动建议">${risks.map(riskCard).join('') || '<div class="state-panel calm-state"><h2>目前没有需要你处理的高风险</h2><p>系统会继续在后台监控；发生新情况时，你也可以主动告诉远见。</p></div>'}</section>`;
  document.querySelector('#tell-yuanjian')?.addEventListener('click', () => setView('input').catch(showPageError));
  document.querySelectorAll('.risk-evidence').forEach(button => button.addEventListener('click', () => showClusterDetail(button.dataset.id, true).catch(showPageError)));
}

async function renderInput() {
  state.view = 'input';
  setHeader('告诉远见', '写发生的事实或你的行为，不需要先分析');
  content.innerHTML = `<section class="input-layout">
    <form id="event-form" class="panel input-card">
      <label for="event-text"><strong>发生了什么，或者你做了什么？</strong></label>
      <textarea id="event-text" name="text" required rows="7" placeholder="例如：今天工资到账 5000 元"></textarea>
      <div class="input-examples" aria-label="输入例子">
        <button class="example-chip" type="button" data-example="今天工资到账 5000 元">工资到账</button>
        <button class="example-chip" type="button" data-example="今天看病自付 800 元，票据已经留好">医疗支出</button>
        <button class="example-chip" type="button" data-example="下周工作安排发生变化，收入可能减少">工作变化</button>
      </div>
      <details class="optional-field"><summary>补充发生时间（选填）</summary><label>发生时间<input name="occurred_at" type="datetime-local"></label></details>
      <button class="button button-primary input-submit" type="submit">告诉远见并判断</button>
      <div id="event-result" aria-live="polite"></div>
    </form>
    <aside class="input-help"><strong>怎么写都可以</strong><p>只写你知道的事实。钱、健康、工作、家庭计划，或者你刚做的决定都能写。</p><p>系统会保存到本机，并给出大致行动建议。</p></aside>
  </section>`;
  document.querySelectorAll('.example-chip').forEach(button => button.addEventListener('click', () => {
    document.querySelector('#event-text').value = button.dataset.example;
    document.querySelector('#event-text').focus();
  }));
  document.querySelector('#event-form').addEventListener('submit', async event => {
    event.preventDefault();
    const resultNode = document.querySelector('#event-result');
    const values = Object.fromEntries(new FormData(event.target));
    if (!values.occurred_at) delete values.occurred_at;
    try {
      const data = await withBusy(event.submitter, '正在判断…', () => api('/api/events', {method:'POST', body:JSON.stringify(values)}));
      const result = RiskUI.inputResult(data.signal);
      resultNode.innerHTML = `<section class="input-result"><span>远见建议</span><h2>${escapeHtml(result.advice)}</h2><p>风险：<strong>${escapeHtml(result.risk)}</strong></p></section>`;
    } catch (error) {
      resultNode.innerHTML = `<div class="run-notice error">没有保存成功：${escapeHtml(error.message)}</div>`;
    }
  });
}

function externalRow(item) {
  const rule = item.matched_rules?.[0];
  const why = rule?.reasons?.join('；') || '命中本地关注规则';
  return `<article class="external-row"><div><span class="badge">${escapeHtml(item.alert_level)}</span><span class="meta-text">${escapeHtml(item.source_count)} 个独立来源</span></div><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.summary || '来源未提供摘要')}</p><p><strong>与你有关：</strong>${escapeHtml(why)}</p><div class="row-between"><time>${escapeHtml(UI.formatLocalTime(item.published_at || item.fetched_at))}</time><a class="button" href="${escapeHtml(item.canonical_url)}" target="_blank" rel="noopener noreferrer">打开原始来源</a></div></article>`;
}

async function renderWorld() {
  state.view = 'world';
  setHeader('外部世界', '原始信息在这里做证据源，行动中心只接收过滤后的结果');
  showLoading('正在读取外部来源状态…');
  const [page, sourceData, ruleData] = await Promise.all([
    api(`/api/external/radar${UI.buildQuery(state.world)}`), api('/api/external/sources'), api('/api/external/rules')
  ]);
  const failed = sourceData.sources.filter(item => item.last_status === 'error' || item.stale);
  const sources = sourceData.sources.map(source => `<article class="source-row"><div><strong>${escapeHtml(source.name)}</strong><span class="source-status ${source.last_status === 'error' ? 'warning' : ''}">${escapeHtml(UI.sourceHealthLabel(source))}</span></div><p>${escapeHtml(UI.sourceKindLabel(source.kind))} · 每 ${escapeHtml(source.refresh_minutes)} 分钟</p>${source.last_error ? `<p class="inline-error">${escapeHtml(source.last_error)}</p>` : ''}<div class="row-actions"><button class="button refresh-source" data-id="${escapeHtml(source.source_id)}" ${source.enabled ? '' : 'disabled'}>立即刷新</button><button class="button toggle-source" data-id="${escapeHtml(source.source_id)}" data-enabled="${source.enabled}">${source.enabled ? '暂停' : '恢复'}</button></div></article>`).join('');
  content.innerHTML = `${failed.length ? `<div class="source-warning"><strong>${failed.length} 个来源暂时不可用</strong><span>抓取失败不代表没有消息；系统会继续按计划重试。</span></div>` : ''}
    <section class="panel"><div class="panel-head"><div><h2>经过规则过滤的新变化</h2><p>每页只读取 10 条，避免一次加载几百条。</p></div></div><form id="world-search" class="filterbar"><label class="sr-only" for="world-q">搜索外部信息</label><input id="world-q" name="q" type="search" value="${escapeHtml(state.world.q)}" placeholder="搜索外部信息"><button class="button">搜索</button></form><div class="external-list">${page.items.map(externalRow).join('') || '<div class="state-panel"><h3>当前筛选没有结果</h3><p>这只表示没有命中规则，不代表互联网没有相关消息。</p></div>'}</div>${paginationHtml(page, 'world')}</section>
    <section class="panel"><div class="panel-head"><div><h2>来源健康状态</h2><p>${sourceData.sources.length} 个来源 · ${sourceData.sources.length - failed.length} 个正常</p></div></div><div class="source-grid">${sources || '<div class="state-panel">尚未添加来源。</div>'}</div></section>
    <section class="panel"><h2>关注规则</h2><div class="chip-list">${ruleData.rules.map(rule => `<span class="chip">${escapeHtml(rule.query)} · 重要度 ${escapeHtml(rule.importance)}/5</span>`).join('') || '还没有关注词。'}</div><form id="rule-form" class="inline-form"><input name="query" required maxlength="80" placeholder="例如：行业、政策、资产"><select name="importance"><option value="5">最高</option><option value="4">高</option><option value="3" selected>中</option></select><button class="button button-primary">添加关注词</button></form><details><summary>添加公开来源</summary><form id="source-form" class="form-grid"><label>来源名称<input name="name" required maxlength="100"></label><label>公开地址<input name="endpoint" required type="url" placeholder="https://..."></label><label>类型<select name="kind"><option value="rss">RSS / Atom</option><option value="html_list">公开网页列表</option><option value="gdelt">GDELT JSON</option></select></label><button class="button button-primary">添加来源</button></form></details></section>`;
  document.querySelector('#world-search').addEventListener('submit', event => { event.preventDefault(); state.world.q = new FormData(event.target).get('q'); state.world.offset = 0; renderWorld().catch(showPageError); });
  bindPaging('world', renderWorld);
  document.querySelector('#rule-form').addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); values.importance = Number(values.importance); await withBusy(event.submitter, '正在添加…', () => api('/api/external/rules', {method:'POST', body:JSON.stringify(values)})); showToast('关注规则已添加'); await renderWorld(); });
  document.querySelector('#source-form').addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); await withBusy(event.submitter, '正在添加…', () => api('/api/external/sources', {method:'POST', body:JSON.stringify(values)})); showToast('公开来源已添加'); await renderWorld(); });
  document.querySelectorAll('.refresh-source').forEach(button => button.addEventListener('click', () => withBusy(button, '刷新中…', async () => { const result = await api('/api/external/refresh', {method:'POST', body:JSON.stringify({source_id:button.dataset.id})}); showToast(result.status === 'ok' ? `刷新完成，新增 ${result.new_count} 条` : `刷新失败：${result.message}`, result.status === 'ok' ? 'success' : 'error'); await renderWorld(); }).catch(error => showToast(error.message, 'error'))));
  document.querySelectorAll('.toggle-source').forEach(button => button.addEventListener('click', async () => { await api(`/api/external/sources/${encodeURIComponent(button.dataset.id)}/enabled`, {method:'POST', body:JSON.stringify({enabled:button.dataset.enabled !== 'true'})}); showToast('来源状态已更新'); await renderWorld(); }));
}

function subnav(group, active, items) {
  return `<nav class="subnav" aria-label="${escapeHtml(group)}">${items.map(([key, label]) => `<button class="subnav-button ${active === key ? 'active' : ''}" data-subview="${key}" type="button">${label}</button>`).join('')}</nav>`;
}

function forecastCard(item) {
  return `<article class="summary-card"><div class="row-between"><span class="badge">${escapeHtml(item.alert_level)}</span><strong>${Math.round(item.probability * 100)}%</strong></div><h3>${escapeHtml(item.title)}</h3><p>截止 ${escapeHtml(item.window_end)} · ${escapeHtml(UI.statusLabel(item.status))}</p><button class="button forecast-detail" data-id="${escapeHtml(item.forecast_id)}">查看详情</button></article>`;
}

async function renderBenefit(subview = state.benefit) {
  state.view = 'benefit'; state.benefit = subview;
  setHeader('我的情况', '外部变化如何传导到现金流、家庭、工作和风险退路');
  showLoading();
  const items = [['interests','利益地图'],['forecasts','预测账本'],['create','新建预测'],['signals','信号收件箱'],['score','模型表现'],['event','录入事件']];
  let body = '';
  if (subview === 'interests') {
    const data = await api('/api/interests');
    const labels = {asset:'资产',liability:'负债',income:'收入',expense:'支出',protection:'保障',family:'家庭',health:'健康',work:'工作'};
    body = `<div class="summary-grid">${data.objects.map(item => `<article class="summary-card"><div class="row-between"><span>${escapeHtml(labels[item.category] || item.category)}</span><span>重要度 ${item.importance}/5</span></div><h3>${escapeHtml(item.name)}</h3><p>状态：${escapeHtml(UI.statusLabel(item.status))} · 隐私等级：${escapeHtml(item.privacy_level)}</p></article>`).join('') || '<div class="state-panel">尚未登记利益对象。</div>'}</div>`;
  } else if (subview === 'forecasts') {
    const data = await api('/api/forecasts');
    body = `<div class="summary-grid">${data.forecasts.map(forecastCard).join('') || '<div class="state-panel"><h3>还没有正式预测</h3><p>只有可结算、写清概率和期限的判断才进入账本。</p></div>'}</div>`;
  } else if (subview === 'signals') {
    const data = await api('/api/signals');
    body = `<div class="summary-grid">${data.signals.map(item => `<article class="summary-card"><span class="badge">${escapeHtml(item.alert_level)}</span><h3>${escapeHtml(item.summary)}</h3><p><strong>影响：</strong>${escapeHtml(item.why_it_matters)}</p><p><strong>建议：</strong>${escapeHtml(item.recommended_action)}</p></article>`).join('') || '<div class="state-panel">还没有保存的信号。</div>'}</div>`;
  } else if (subview === 'score') {
    const data = await api('/api/score');
    body = `<div class="metrics two"><article class="metric-static"><strong>${escapeHtml(data.resolved_binary)}</strong><span>已评分预测</span></article><article class="metric-static"><strong>${data.brier_score ?? '—'}</strong><span>Brier 分数（越低越准）</span></article></div>`;
  } else if (subview === 'create') {
    body = forecastFormHtml();
  } else {
    body = `<form id="event-form" class="panel form-grid"><h2>录入你刚知道的新事件</h2><label class="full">事件内容<textarea name="text" required rows="7" placeholder="只写事实、来源和时间，不先写结论"></textarea></label><label>发生时间<input name="occurred_at" type="datetime-local"></label><button class="button button-primary">保存并分析</button><div id="event-result" class="full"></div></form>`;
  }
  content.innerHTML = `${subnav('我的情况', subview, items)}<section class="panel">${body}</section>`;
  bindSubview('.subnav-button', value => renderBenefit(value));
  bindBenefitActions(subview);
}

function forecastFormHtml(initial = {}) {
  return `<form id="forecast-form" class="form-grid"><h2 class="full">写一条能结算的预测</h2><label class="full">预测标题<input name="title" required value="${escapeHtml(initial.title || '')}"></label><label class="full">结算标准<textarea name="resolution_criteria" required rows="3">${escapeHtml(initial.resolution_criteria || '')}</textarea></label><label>开始日期<input name="window_start" type="date" required value="${escapeHtml(initial.window_start || new Date().toISOString().slice(0,10))}"></label><label>截止日期<input name="window_end" type="date" required value="${escapeHtml(initial.window_end || '')}"></label><label>概率<select name="probability">${[.1,.2,.3,.4,.5,.6,.7,.8,.9].map(value => `<option value="${value}" ${Number(initial.probability || .5) === value ? 'selected' : ''}>${Math.round(value*100)}%</option>`).join('')}</select></label><label>可信度<select name="confidence"><option value="low">低</option><option value="medium" selected>中</option><option value="high">高</option></select></label><label>预警等级<select name="alert_level"><option>L1</option><option selected>L2</option><option>L3</option><option>L4</option></select></label><label>隐私等级<select name="privacy_level"><option>P1</option><option selected>P2</option><option>P3</option></select></label><button class="button button-primary">写入预测账本</button></form>`;
}

function bindSubview(selector, render) {
  document.querySelectorAll(selector).forEach(button => button.addEventListener('click', () => render(button.dataset.subview).catch(showPageError)));
}

function bindBenefitActions(subview) {
  document.querySelectorAll('.forecast-detail').forEach(button => button.addEventListener('click', () => showForecastDetail(button.dataset.id).catch(showPageError)));
  document.querySelector('#forecast-form')?.addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); values.probability = Number(values.probability); await withBusy(event.submitter, '正在保存…', () => api('/api/forecasts', {method:'POST', body:JSON.stringify(values)})); showToast('预测已写入不可变账本'); await renderBenefit('forecasts'); });
  document.querySelector('#event-form')?.addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); const data = await withBusy(event.submitter, '正在分析…', () => api('/api/events', {method:'POST', body:JSON.stringify(values)})); document.querySelector('#event-result').innerHTML = `<div class="run-notice success">已保存为 ${escapeHtml(data.signal.alert_level)} 信号：${escapeHtml(data.signal.recommended_action)}</div>`; });
}

async function showForecastDetail(id) {
  setHeader('预测详情', '不可变版本、结算标准和历史表现'); runButton.hidden = true; showLoading();
  const item = await api(`/api/forecasts/${encodeURIComponent(id)}`);
  content.innerHTML = `<button class="button back-benefit">← 返回预测账本</button><section class="panel detail-panel"><span class="badge">${escapeHtml(item.alert_level)}</span><h2>${escapeHtml(item.title)}</h2><p><strong>概率：</strong>${Math.round(item.probability*100)}%</p><p><strong>结算标准：</strong>${escapeHtml(item.resolution_criteria)}</p><p><strong>截止：</strong>${escapeHtml(item.window_end)}</p><h3>历史版本</h3>${item.versions.map(version => `<details><summary>版本 ${version.version} · ${Math.round(version.probability*100)}%</summary><pre>${escapeHtml(version.content)}</pre></details>`).join('')}</section>`;
  document.querySelector('.back-benefit').addEventListener('click', () => renderBenefit('forecasts').catch(showPageError));
}

async function renderSystem(subview = state.system) {
  state.view = 'system'; state.system = subview;
  setHeader('系统设置', '低频配置集中在这里，日常页面不再被参数淹没');
  showLoading();
  const items = [['settings','后台监控'],['intelligence','情报后台'],['notifications','通知中心'],['knowledge','知识库']];
  if (subview === 'intelligence') return renderIntelligence(items);
  if (subview === 'notifications') return renderNotificationCenter(items);
  if (subview === 'knowledge') return renderKnowledge(items);
  const [ai, startup, status] = await Promise.all([api('/api/settings/ai'), api('/api/settings/startup'), api('/api/cognition/status')]);
  updateChrome(status);
  content.innerHTML = `${subnav('系统设置', subview, items)}<div class="settings-grid"><section class="panel"><h2>后台监控</h2><p>事件 ${status.clusters} · 待判断 ${status.needs_judgment} · 未读 ${status.unread_notifications}</p><div class="setting-row"><div><strong>登录后自动运行</strong><p>${startup.available === false ? '源码运行时不可设置，独立程序中可用。' : '关闭窗口后缩到托盘，后台继续监控。'}</p></div>${startup.available === false ? '' : `<button id="startup-toggle" class="button">${startup.installed ? '关闭' : '开启'}</button>`}</div></section><form id="ai-settings" class="panel form-grid"><h2 class="full">可选外部 AI 研判</h2><p class="full panel-note">默认关闭。只发送公开标题、摘要、网址、时间和通用类别；私人利益、Obsidian 原文、地址、医疗、债务和账户不会发送。</p><label class="form-check full"><input name="enabled" type="checkbox" ${ai.enabled ? 'checked' : ''}><span>启用外部 AI</span></label><label class="full">Responses API 地址<input name="endpoint" type="url" required value="${escapeHtml(ai.endpoint)}"></label><label>模型编号<input name="model" value="${escapeHtml(ai.model)}" placeholder="明确填写模型编号"></label><label>API 密钥<input name="token" type="password" autocomplete="new-password" placeholder="${ai.configured ? '已安全保存；留空不更换' : '尚未配置'}"></label><button class="button button-primary">保存设置</button><span>密钥：${ai.configured ? '已加密保存' : '未配置'}</span></form></div>`;
  bindSubview('.subnav-button', value => renderSystem(value));
  document.querySelector('#startup-toggle')?.addEventListener('click', async event => { await withBusy(event.currentTarget, '正在更新…', () => api('/api/settings/startup', {method:'POST', body:JSON.stringify({enabled:!startup.installed})})); showToast('登录启动设置已更新'); await renderSystem(); });
  document.querySelector('#ai-settings').addEventListener('submit', async event => { event.preventDefault(); const form = new FormData(event.target); const values = {enabled:form.get('enabled') === 'on', endpoint:form.get('endpoint'), model:form.get('model')}; if (form.get('token')) values.token = form.get('token'); await withBusy(event.submitter, '正在保存…', () => api('/api/settings/ai', {method:'POST', body:JSON.stringify(values)})); showToast('AI 设置已保存'); await renderSystem(); });
}

async function renderIntelligence(navItems, mode = 'summary') {
  state.view = 'system'; state.system = 'intelligence';
  setHeader('情报后台', '系统在这里处理公开信息；日常不需要你阅读');
  showLoading('正在读取监控摘要…');
  const requests = RiskUI.intelligenceRequests(mode);
  const data = await Promise.all(requests.map(path => api(path)));
  if (mode === 'raw') {
    const page = data[0];
    content.innerHTML = `${subnav('设置', 'intelligence', navItems)}<section class="panel"><div class="panel-head"><div><h2>原始情报</h2><p>仅在核查系统判断时阅读。</p></div><button id="intelligence-summary" class="button">返回摘要</button></div><div class="external-list">${page.items.map(externalRow).join('') || '<div class="state-panel">目前没有命中关注规则的公开信息。</div>'}</div>${paginationHtml(page, 'world')}</section>`;
    bindSubview('.subnav-button', value => renderSystem(value));
    document.querySelector('#intelligence-summary').addEventListener('click', () => renderIntelligence(navItems).catch(showPageError));
    bindPaging('world', () => renderIntelligence(navItems, 'raw'));
    return;
  }
  if (mode === 'manage') {
    const [sourceData, ruleData] = data;
    const sources = sourceData.sources.map(source => `<article class="source-row"><div><strong>${escapeHtml(source.name)}</strong><span class="source-status ${source.last_status === 'error' ? 'warning' : ''}">${escapeHtml(UI.sourceHealthLabel(source))}</span></div><p>${escapeHtml(UI.sourceKindLabel(source.kind))} · 每 ${escapeHtml(source.refresh_minutes)} 分钟</p>${source.last_error ? `<details><summary>查看技术错误</summary><p class="inline-error">${escapeHtml(source.last_error)}</p></details>` : ''}<div class="row-actions"><button class="button refresh-source" data-id="${escapeHtml(source.source_id)}" ${source.enabled ? '' : 'disabled'}>立即刷新</button><button class="button toggle-source" data-id="${escapeHtml(source.source_id)}" data-enabled="${source.enabled}">${source.enabled ? '暂停' : '恢复'}</button></div></article>`).join('');
    content.innerHTML = `${subnav('设置', 'intelligence', navItems)}<section class="panel"><div class="panel-head"><div><h2>管理公开来源</h2><p>这些设置只影响系统的眼睛和耳朵。</p></div><button id="intelligence-summary" class="button">返回摘要</button></div><div class="source-grid">${sources || '<div class="state-panel">尚未添加来源。</div>'}</div></section><section class="panel"><details><summary>管理关注规则</summary><div class="chip-list">${ruleData.rules.map(rule => `<span class="chip">${escapeHtml(rule.query)} · 重要度 ${escapeHtml(rule.importance)}/5</span>`).join('') || '还没有关注词。'}</div><form id="rule-form" class="inline-form"><input name="query" required maxlength="80" placeholder="例如：行业、政策、资产"><select name="importance"><option value="5">最高</option><option value="4">高</option><option value="3" selected>中</option></select><button class="button button-primary">添加关注词</button></form></details><details><summary>添加公开来源</summary><form id="source-form" class="form-grid"><label>来源名称<input name="name" required maxlength="100"></label><label>公开地址<input name="endpoint" required type="url" placeholder="https://..."></label><label>类型<select name="kind"><option value="rss">RSS / Atom</option><option value="html_list">公开网页列表</option><option value="gdelt">GDELT JSON</option></select></label><button class="button button-primary">添加来源</button></form></details></section>`;
    bindSubview('.subnav-button', value => renderSystem(value));
    document.querySelector('#intelligence-summary').addEventListener('click', () => renderIntelligence(navItems).catch(showPageError));
    document.querySelector('#rule-form')?.addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); values.importance = Number(values.importance); await withBusy(event.submitter, '正在添加…', () => api('/api/external/rules', {method:'POST', body:JSON.stringify(values)})); showToast('关注规则已添加'); await renderIntelligence(navItems, 'manage'); });
    document.querySelector('#source-form')?.addEventListener('submit', async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); await withBusy(event.submitter, '正在添加…', () => api('/api/external/sources', {method:'POST', body:JSON.stringify(values)})); showToast('公开来源已添加'); await renderIntelligence(navItems, 'manage'); });
    document.querySelectorAll('.refresh-source').forEach(button => button.addEventListener('click', () => withBusy(button, '刷新中…', async () => { const result = await api('/api/external/refresh', {method:'POST', body:JSON.stringify({source_id:button.dataset.id})}); showToast(result.status === 'ok' ? `刷新完成，新增 ${result.new_count} 条` : '刷新失败，系统会自动重试', result.status === 'ok' ? 'success' : 'error'); await renderIntelligence(navItems, 'manage'); }).catch(error => showToast(error.message, 'error'))));
    document.querySelectorAll('.toggle-source').forEach(button => button.addEventListener('click', async () => { await api(`/api/external/sources/${encodeURIComponent(button.dataset.id)}/enabled`, {method:'POST', body:JSON.stringify({enabled:button.dataset.enabled !== 'true'})}); showToast('来源状态已更新'); await renderIntelligence(navItems, 'manage'); }));
    return;
  }
  const [status, sourceData] = data;
  updateChrome(status);
  const enabled = sourceData.sources.filter(source => source.enabled);
  const healthy = enabled.filter(source => source.last_status === 'ok' && !source.stale);
  content.innerHTML = `${subnav('设置', 'intelligence', navItems)}<section class="panel intelligence-summary"><h2>系统正在替你处理</h2><div class="risk-counts"><article class="risk-count"><strong>${escapeHtml(status.clusters)}</strong><span>已归并事件</span></article><article class="risk-count"><strong>${escapeHtml(healthy.length)}/${escapeHtml(enabled.length)}</strong><span>来源正常</span></article><article class="risk-count"><strong>${escapeHtml(status.needs_judgment)}</strong><span>仍在核实</span></article></div><p class="panel-note">只有形成个人风险结论的变化才会进入风险首页。</p><div class="row-actions"><button id="show-raw-intelligence" class="button">查看原始情报</button><button id="manage-intelligence" class="button">管理来源</button></div></section>`;
  bindSubview('.subnav-button', value => renderSystem(value));
  document.querySelector('#show-raw-intelligence').addEventListener('click', () => renderIntelligence(navItems, 'raw').catch(showPageError));
  document.querySelector('#manage-intelligence').addEventListener('click', () => renderIntelligence(navItems, 'manage').catch(showPageError));
}

async function renderNotificationCenter(navItems = [['settings','后台监控'],['notifications','通知中心'],['knowledge','知识库']]) {
  setHeader('通知中心', '处理需要你关注的提醒，已读状态只保存在本机');
  const page = await api(`/api/notifications${UI.buildQuery(state.notifications)}`);
  content.innerHTML = `${subnav('系统设置', 'notifications', navItems)}<section class="panel"><div class="panel-head"><div><h2>${state.notifications.status === 'unread' ? '未读提醒' : '全部提醒'}</h2><p>可以直接打开关联事件或标记已读。</p></div><div class="row-actions"><button id="notification-filter" class="button">${state.notifications.status === 'unread' ? '查看全部' : '只看未读'}</button><button id="read-all" class="button button-primary" ${page.items.some(item => item.status === 'unread') ? '' : 'disabled'}>全部标为已读</button></div></div><div>${page.items.map(notificationRow).join('') || '<div class="state-panel"><h3>没有需要处理的提醒</h3></div>'}</div>${paginationHtml(page, 'notifications')}</section>`;
  bindSubview('.subnav-button', value => renderSystem(value));
  bindPaging('notifications', () => renderNotificationCenter(navItems));
  bindNotificationActions(() => renderNotificationCenter(navItems));
  document.querySelector('#notification-filter').addEventListener('click', () => { state.notifications.status = state.notifications.status === 'unread' ? '' : 'unread'; state.notifications.offset = 0; renderNotificationCenter(navItems).catch(showPageError); });
  document.querySelector('#read-all').addEventListener('click', event => withBusy(event.currentTarget, '正在处理…', async () => { const result = await api('/api/notifications/read-all', {method:'POST', body:'{}'}); showToast(`已处理 ${result.updated} 条提醒`); await renderNotificationCenter(navItems); }).catch(error => showToast(error.message, 'error')));
}

function bindNotificationActions(refresh) {
  document.querySelectorAll('.notification-read').forEach(button => button.addEventListener('click', event => withBusy(event.currentTarget, '处理中…', async () => { await api(`/api/notifications/${encodeURIComponent(button.dataset.id)}/read`, {method:'POST', body:'{}'}); showToast('已标为已读'); await refresh(); }).catch(error => showToast(error.message, 'error'))));
  document.querySelectorAll('.notification-open-cluster').forEach(button => button.addEventListener('click', () => showClusterDetail(button.dataset.clusterId).catch(showPageError)));
}

async function renderKnowledge(navItems) {
  setHeader('知识库', '只读索引 Obsidian，不修改你的原文');
  const [vaults, documents] = await Promise.all([api('/api/knowledge/vaults'), api('/api/knowledge/documents')]);
  content.innerHTML = `${subnav('系统设置', 'knowledge', navItems)}<section class="panel"><h2>Obsidian 只读索引</h2><p class="panel-note">只保存相对路径、摘要和哈希，不会修改原文件。</p><form id="knowledge-search" class="filterbar"><input name="q" placeholder="搜索标题或摘要"><button class="button">搜索</button></form><form id="index-form" class="inline-form"><select name="path" required>${vaults.vaults.map(item => `<option value="${escapeHtml(item.path)}">${escapeHtml(item.name)}</option>`).join('')}</select><button class="button button-primary" ${vaults.vaults.length ? '' : 'disabled'}>建立只读索引</button></form><div id="knowledge-list">${documents.documents.map(item => `<article class="document-row"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.summary)}</p><span>${escapeHtml(item.relative_path)}</span></article>`).join('') || '<div class="state-panel">索引中还没有文档。</div>'}</div></section>`;
  bindSubview('.subnav-button', value => renderSystem(value));
  document.querySelector('#knowledge-search').addEventListener('submit', async event => { event.preventDefault(); const q = new FormData(event.target).get('q'); const data = await api(`/api/knowledge/documents${UI.buildQuery({q})}`); document.querySelector('#knowledge-list').innerHTML = data.documents.map(item => `<article class="document-row"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.summary)}</p><span>${escapeHtml(item.relative_path)}</span></article>`).join('') || '<div class="state-panel">没有匹配文档。</div>'; });
  document.querySelector('#index-form').addEventListener('submit', async event => { event.preventDefault(); const path = new FormData(event.target).get('path'); const result = await withBusy(event.submitter, '正在索引…', () => api('/api/knowledge/index', {method:'POST', body:JSON.stringify({path})})); showToast(`索引完成：${result.indexed} 篇`); await renderKnowledge(navItems); });
}

async function showClusterDetail(id, evidenceOpen = false) {
  setHeader('风险详情', '先看怎么做；只有需要核查时再看证据'); runButton.hidden = true; showLoading();
  const item = await api(`/api/cognition/clusters/${encodeURIComponent(id)}`);
  const judgment = item.judgment;
  const detail = RiskUI.detailSummary(item);
  const evidence = item.items.map(source => `<li><a href="${escapeHtml(source.canonical_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title)}</a><span>${escapeHtml(source.source_domain)}</span></li>`).join('');
  const impacts = item.impacts.map(impact => `<article class="impact-row"><div class="row-between"><span class="badge">${escapeHtml(impact.alert_level)}</span><span>${Math.round(impact.impact_score*100)} 分</span></div><h3>${escapeHtml(impact.interest_name || impact.interest_id)}</h3><p>${escapeHtml(impact.reason)}</p>${impact.candidate ? `<div class="candidate"><strong>候选预测：${escapeHtml(impact.candidate.title)}</strong><label>人工概率<select class="candidate-probability">${[.1,.2,.3,.4,.5,.6,.7,.8,.9].map(value => `<option value="${value}" ${value === .5 ? 'selected' : ''}>${Math.round(value*100)}%</option>`).join('')}</select></label><button class="button button-primary confirm-candidate" data-id="${escapeHtml(impact.impact_id)}">确认进入预测账本</button></div>` : ''}</article>`).join('');
  const triggers = detail.triggers.map(value => `<li>${escapeHtml(value)}</li>`).join('') || '<li>系统尚未识别明确的升级或解除条件。</li>';
  content.innerHTML = `<button class="button back-today">← 返回风险首页</button><div class="detail-grid"><section class="panel decision-panel"><span class="badge">${escapeHtml(detail.interest)}</span><h2>对你的影响</h2><p>${escapeHtml(detail.impact)}</p><div class="risk-action"><strong>现在怎么做</strong><p>${escapeHtml(detail.action)}</p></div><h3>最迟决定时间</h3><p>${escapeHtml(detail.decisionBy)}</p><h3>升级或解除条件</h3><ul>${triggers}</ul><details class="evidence-drawer" ${evidenceOpen ? 'open' : ''}><summary>为什么这样判断</summary><h3>事实判断</h3><p>${escapeHtml(judgment?.fact_summary || '等待研判')}</p><h3>因果链</h3><ol>${(judgment?.causal_chain || []).map(value => `<li>${escapeHtml(value)}</li>`).join('') || '<li>等待研判</li>'}</ol><h3>反对证据与不确定性</h3><ul>${(judgment?.uncertainties || []).map(value => `<li>${escapeHtml(value)}</li>`).join('') || '<li>尚未形成</li>'}</ul><h3>公开证据</h3><ul class="evidence-list">${evidence}</ul><details><summary>高级操作：进入正式预测账本</summary>${impacts || '<div class="state-panel">没有命中已登记利益。</div>'}</details></details></section><aside><section class="panel feedback"><h3>这个提醒不准确？</h3><div class="row-actions"><button class="button" data-action="mute">7 天内不再提醒</button><button class="button" data-action="lower_importance">降低提醒</button><button class="button" data-action="false_positive">与我无关</button></div></section></aside></div>`;
  document.querySelector('.back-today').addEventListener('click', () => renderRiskHome().catch(showPageError));
  document.querySelectorAll('.confirm-candidate').forEach(button => button.addEventListener('click', event => withBusy(event.currentTarget, '正在写入…', async () => { const probability = Number(button.closest('.candidate').querySelector('.candidate-probability').value); await api(`/api/cognition/candidates/${encodeURIComponent(button.dataset.id)}/confirm`, {method:'POST', body:JSON.stringify({probability})}); showToast('已写入不可变预测账本'); await showClusterDetail(id); }).catch(error => showToast(error.message, 'error'))));
  document.querySelectorAll('.feedback button').forEach(button => button.addEventListener('click', event => withBusy(event.currentTarget, '保存中…', async () => { await api(`/api/cognition/clusters/${encodeURIComponent(id)}/feedback`, {method:'POST', body:JSON.stringify({action:button.dataset.action})}); showToast('本地反馈已保存，不会发送给外部 AI'); }).catch(error => showToast(error.message, 'error'))));
}

async function setView(view) {
  state.view = view;
  document.querySelectorAll('.primary-nav').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  showLoading();
  if (view === 'input') return renderInput();
  if (view === 'system') return renderSystem();
  return renderRiskHome();
}

document.querySelectorAll('.primary-nav').forEach(button => button.addEventListener('click', () => setView(button.dataset.view).catch(showPageError)));
document.querySelector('#open-notifications').addEventListener('click', () => { state.system = 'notifications'; setView('system').catch(showPageError); });
runButton.addEventListener('click', event => CognitionUI.runCognitionWithFeedback({
  apiCall: () => api('/api/cognition/run', {method:'POST', body:'{}'}),
  button: event.currentTarget,
  status: {textContent: '', className: ''},
  onComplete: notice => renderRiskHome(notice),
  onFailure: notice => showToast(notice.text, 'error')
}).catch(error => showToast(error.message, 'error')));
document.querySelector('#shutdown').addEventListener('click', async () => {
  if (!confirm('确定要完全退出远见吗？关闭窗口会继续在托盘监控。')) return;
  try { await api('/api/shutdown', {method:'POST', body:'{}'}); document.body.innerHTML = '<main class="closed"><section class="panel"><h1>远见已安全退出</h1><p>后台监控已经停止。</p></section></main>'; }
  catch (error) { showToast(error.message, 'error'); }
});

renderRiskHome().catch(showPageError);
