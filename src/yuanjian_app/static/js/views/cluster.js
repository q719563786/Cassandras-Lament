// 远见 v1.1 · 事件详情页
// 展示GYW六步分析、多路径推演、领先指标、个人影响、证据来源
import { api, escapeHtml, showLoading, showPageError } from '../api.js';

function alertBadge(level) {
  const labels = { L4: 'L4 · 高风险', L3: 'L3 · 中风险', L2: 'L2 · 低风险', L1: 'L1 · 信号' };
  return `<span class="badge badge-alert-${level || 'L2'}">${labels[level] || level || ''}</span>`;
}

function evidenceBadge(level) {
  const labels = { E3: 'E3 · 多源互证', E2: 'E2 · 双源', E1: 'E1 · 单源待证' };
  return `<span class="badge badge-evidence">${labels[level] || level || ''}</span>`;
}

function renderGywSection(gyw) {
  if (!gyw || !gyw.stakeholders) return '';
  return `<section class="card u-mb-md">
    <h3 class="section-title">🔍 登高望远 · 六步分析</h3>
    <div class="gyw-grid">
      <div class="gyw-item">
        <h4>① 权力结构</h4>
        <p>${escapeHtml(gyw.stakeholders || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>② 利益方向</h4>
        <p>${escapeHtml(gyw.interests || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>③ 结构约束</h4>
        <p>${escapeHtml(gyw.constraints || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>④ 最小阻力路径</h4>
        <p>${escapeHtml(gyw.least_resistance_path || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>⑤ 反面证据</h4>
        <p class="text-warn">${escapeHtml(gyw.counter_evidence || '-')}</p>
      </div>
      <div class="gyw-item">
        <h4>⑥ 领先指标</h4>
        <p>${escapeHtml(gyw.leading_indicators || '-')}</p>
      </div>
    </div>
  </section>`;
}

function renderScenarios(scenarios) {
  if (!scenarios || !scenarios.length) return '';
  const labels = { most_likely: '最可能路径', secondary: '次可能路径', black_swan: '黑天鹅路径' };
  const classes = { most_likely: 'scenario-most', secondary: 'scenario-secondary', black_swan: 'scenario-swan' };
  return `<section class="card u-mb-md">
    <h3 class="section-title">🛤️ 多路径推演</h3>
    <div class="scenario-list">
      ${scenarios.map(s => `
        <div class="scenario-item ${classes[s.path_type] || ''}">
          <div class="scenario-header">
            <strong>${escapeHtml(labels[s.path_type] || s.path_type || '路径')}</strong>
            <span class="scenario-prob">概率 ${Math.round((s.probability || 0) * 100)}%</span>
          </div>
          <p class="scenario-desc">${escapeHtml(s.description || '')}</p>
          ${s.implications ? `<div class="scenario-impl"><strong>对你的影响：</strong>${escapeHtml(s.implications)}</div>` : ''}
          ${s.watch_signals ? `<div class="scenario-watch"><strong>观察信号：</strong>${escapeHtml(s.watch_signals)}</div>` : ''}
        </div>
      `).join('')}
    </div>
  </section>`;
}

function renderImpacts(impacts, clusterId) {
  if (!impacts || !impacts.length) return '';
  return `<section class="card u-mb-md">
    <h3 class="section-title">🎯 对你的个人影响</h3>
    <div class="impact-list">
      ${impacts.map(imp => {
        const c = imp.candidate || {};
        const confirmed = !!c.confirmed_forecast_id;
        return `<div class="impact-row" data-impact="${imp.impact_id}">
          <div class="impact-main">
            <div class="impact-header">
              ${alertBadge(imp.alert_level)}
              <span class="impact-name">${escapeHtml(imp.interest_name || '已登记利益')}</span>
              ${confirmed ? '<span class="badge badge-confirmed">✓ 已记录预测</span>' : '<span class="badge badge-pending">待确认</span>'}
            </div>
            <div class="impact-title">${escapeHtml(c.title || '')}</div>
            ${c.recommended_action ? `<div class="impact-action">💡 ${escapeHtml(c.recommended_action)}</div>` : ''}
            <div class="impact-meta">
              ${c.window_end ? `<span>窗口期截止：${escapeHtml(c.window_end)}</span>` : ''}
              ${!confirmed ? `<span>系统估计概率：${Math.round((c.probability_low||0)*100)}% — ${Math.round((c.probability_high||0)*100)}%</span>` : ''}
            </div>
          </div>
          ${!confirmed ? `<button class="btn-primary btn-sm" data-action="confirm-from-detail" data-impact="${imp.impact_id}" data-cluster="${clusterId}">记录预测</button>` : ''}
        </div>`;
      }).join('')}
    </div>
  </section>`;
}

function renderSources(items) {
  if (!items || !items.length) return '';
  return `<section class="card u-mb-md">
    <h3 class="section-title">📎 证据来源（${items.length}条）</h3>
    <div class="source-evidence-list">
      ${items.slice(0, 20).map(item => `
        <div class="evidence-item">
          <div class="evidence-title">
            <a href="${escapeHtml(item.canonical_url || '#')}" target="_blank" rel="noopener">${escapeHtml(item.title || '(无标题)')}</a>
          </div>
          <div class="evidence-meta">
            <span>${escapeHtml(item.source_domain || '')}</span>
            ${item.published_at ? `<span>${escapeHtml(String(item.published_at).slice(0,10))}</span>` : ''}
          </div>
          ${item.summary ? `<div class="evidence-summary">${escapeHtml(item.summary.slice(0, 300))}</div>` : ''}
        </div>
      `).join('')}
      ${items.length > 20 ? `<div class="evidence-more">还有 ${items.length - 20} 条来源未显示</div>` : ''}
    </div>
  </section>`;
}

function renderFactChain(judgment) {
  if (!judgment) return '';
  const actors = judgment.actors || [];
  const chain = judgment.causal_chain || [];
  const up = judgment.up_triggers || [];
  const down = judgment.down_triggers || [];
  return `<section class="card u-mb-md">
    <h3 class="section-title">📋 事实摘要与因果链</h3>
    <div class="fact-summary">${escapeHtml(judgment.fact_summary || '暂无摘要')}</div>
    ${actors.length ? `<div class="fact-actors"><strong>相关方：</strong>${actors.map(a => `<span class="actor-tag">${escapeHtml(typeof a === 'string' ? a : a.name || JSON.stringify(a))}</span>`).join('')}</div>` : ''}
    ${chain.length ? `<div class="fact-chain">
      <h4>因果链条：</h4>
      <ol>${chain.map(step => `<li>${escapeHtml(typeof step === 'string' ? step : step.description || JSON.stringify(step))}</li>`).join('')}</ol>
    </div>` : ''}
    ${up.length || down.length ? `<div class="triggers">
      ${up.length ? `<div class="trigger-up"><strong>⬆ 风险上升信号：</strong>${escapeHtml(up.join('；'))}</div>` : ''}
      ${down.length ? `<div class="trigger-down"><strong>⬇ 风险缓解信号：</strong>${escapeHtml(down.join('；'))}</div>` : ''}
    </div>` : ''}
  </section>`;
}

export async function render(root) {
  // 从hash解析cluster_id: #/cluster/XXX
  const hash = location.hash || '';
  const clusterId = hash.replace(/^#\/cluster\//, '').split('?')[0];
  if (!clusterId) {
    showPageError(root, '事件ID无效', () => location.hash = '#/today');
    return;
  }

  showLoading(root, '正在加载事件详情…');

  try {
    const data = await api(`/api/cognition/clusters/${clusterId}`);
    const j = data.judgment || {};
    const gyw = j.gyw || {};
    const scenarios = j.scenario_paths || [];
    const confidence = j.confidence != null ? `${Math.round(Number(j.confidence) * 100)}%` : '待评估';
    const horizon = Array.isArray(j.horizons) ? j.horizons.join('，') : (j.horizons || '');

    root.innerHTML = `<div class="u-max">
      <div class="page-nav">
        <a href="#/today" class="btn-text">← 返回行动雷达</a>
      </div>
      <section class="card u-mb-md">
        <div class="detail-header">
          <div class="detail-badges">
            ${alertBadge(data.impacts?.[0]?.alert_level || 'L3')}
            ${evidenceBadge(data.evidence_level)}
            <span class="badge badge-conf">置信度 ${escapeHtml(confidence)}</span>
            ${horizon ? `<span class="badge badge-window">窗口：${escapeHtml(horizon)}</span>` : ''}
          </div>
          <h2 class="detail-title">${escapeHtml(data.title || '(无标题事件)')}</h2>
          <p class="detail-summary">${escapeHtml(data.summary || '')}</p>
          <div class="detail-meta">
            <span>首次发现：${escapeHtml(String(data.first_seen_at || '').slice(0, 16).replace('T', ' '))}</span>
            <span>最近更新：${escapeHtml(String(data.last_seen_at || '').slice(0, 16).replace('T', ' '))}</span>
            ${j.provider ? `<span>研判来源：${j.provider === 'local' ? '本地启发式' : escapeHtml(j.provider)}</span>` : ''}
          </div>
        </div>
      </section>
      ${renderFactChain(j)}
      ${renderGywSection(gyw)}
      ${renderScenarios(scenarios)}
      ${renderImpacts(data.impacts, clusterId)}
      ${renderSources(data.items)}
      <div class="page-nav u-mt-md">
        <a href="#/today" class="btn-text">← 返回行动雷达</a>
      </div>
    </div>`;

    // 绑定详情页的确认预测按钮
    bindDetailActions(root, clusterId);
  } catch (err) {
    showPageError(root, `加载失败：${err.message || '未知错误'}`, () => render(root));
  }
}

function bindDetailActions(root, clusterId) {
  root.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-action="confirm-from-detail"]');
    if (!btn) return;
    const impactId = btn.dataset.impact;
    // 简化：跳转到校准面板进行确认
    location.hash = '#/calib';
  });
}
