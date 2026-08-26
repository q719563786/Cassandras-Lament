// 远见 v1.1 · 今日远见 · 行动雷达模式
// 首页 = L4 立即行动 + L3 准备观察 + 今日低后悔动作 + 预测进度 KPI + 告诉远见
import { api, escapeHtml } from '../api.js';
import { tellBoxHtml, bindTellBox } from './tell.js';

const PROBABILITY_OPTIONS = [
  { value: 0.05, label: '5% · 几乎不可能' },
  { value: 0.20, label: '20% · 不太可能' },
  { value: 0.35, label: '35% · 可能性较低' },
  { value: 0.50, label: '50% · 五五开' },
  { value: 0.65, label: '65% · 较有可能' },
  { value: 0.80, label: '80% · 很可能' },
  { value: 0.95, label: '95% · 几乎确定' }
];

function directionClass(direction) {
  if (direction === '风险上升') return 'up';
  if (direction === '风险缓解') return 'down';
  return 'flat';
}

function closestProbability(low, high) {
  const mid = (Number(low) + Number(high)) / 2;
  return PROBABILITY_OPTIONS.reduce((best, opt) =>
    Math.abs(opt.value - mid) < Math.abs(best.value - mid) ? opt : best
  );
}

// 行动卡（L4立即行动 / L3准备观察通用）
function actionCardHtml(item, isL4) {
  const title = escapeHtml(item.title || item.interest_name || '');
  const window = escapeHtml(item.time_window || '');
  const action = escapeHtml(item.action || item.advice || '');
  const direction = item.direction || '没有明显变化';
  const dirClass = directionClass(direction);
  const riskLabel = escapeHtml(item.risk_label || '');
  const confirmed = item.candidate_confirmed;

  // 未确认候选预测时显示"记录预测"按钮
  const predictBtn = !confirmed ? `
    <button class="ac-btn ac-btn-predict" data-action="predict" data-impact="${item.impact_id}"
            data-prob-low="${item.candidate_prob_low}" data-prob-high="${item.candidate_prob_high}"
            title="选择你认为的概率，记入预测账本">
      📊 记录预测
    </button>` : '';

  return `<div class="action-card ${isL4 ? 'l4' : 'l3'}" data-cluster="${item.cluster_id}" data-impact="${item.impact_id}">
    <div class="ac-main" data-action="open-detail">
      <div class="ac-header">
        <span class="badge badge-alert-${isL4 ? 'l4' : 'l3'}">${riskLabel}</span>
        <span class="ac-window">窗口：${window}</span>
        <span class="ac-direction ${dirClass}">${escapeHtml(direction)}</span>
      </div>
      <div class="ac-title">${title}</div>
      ${action ? `<div class="ac-action">${action}</div>` : ''}
    </div>
    <div class="ac-actions">
      ${predictBtn}
      <button class="ac-btn" data-action="dismiss" title="已处理，不再提醒">✓ 已处理</button>
      <button class="ac-btn" data-action="mute" title="静音7天">🔇 静音7天</button>
      <button class="ac-btn ac-btn-warn" data-action="false_positive" title="这是误报">⚠ 误报</button>
    </div>
  </div>`;
}

// 概率选择弹窗
function probabilityPickerHtml(impactId, low, high) {
  const suggested = closestProbability(low, high);
  return `<div class="modal-backdrop" data-role="modal-backdrop">
    <div class="modal-card" role="dialog" aria-modal="true">
      <h3>记录你的预测</h3>
      <p class="modal-hint">远见给出的概率区间：${Math.round(Number(low)*100)}% — ${Math.round(Number(high)*100)}%（建议选 <strong>${Math.round(suggested.value*100)}%</strong>）</p>
      <p class="modal-hint">你认为此事在窗口期内发生的概率是多少？</p>
      <div class="prob-grid">
        ${PROBABILITY_OPTIONS.map(opt => `
          <button class="prob-btn ${opt.value === suggested.value ? 'prob-suggested' : ''}"
                  data-action="confirm-prob" data-impact="${impactId}" data-prob="${opt.value}">
            ${escapeHtml(opt.label)}
          </button>
        `).join('')}
      </div>
      <div class="modal-footer">
        <button class="btn-text" data-action="cancel-predict">取消</button>
      </div>
    </div>
  </div>`;
}

// 今日低后悔动作区
function safeListHtml() {
  const items = [
    '保留票据和结算材料',
    '不把信用额度算作家庭资产',
    '新事实出现后更新预测，不覆盖旧版本',
    '暂缓不可逆决定，等更多信息'
  ];
  return `<section class="radar-section">
    <h2><span class="dot-safe"></span> 今日低后悔动作</h2>
    <ul class="safe-list">${items.map(t => `<li>${escapeHtml(t)}</li>`).join('')}</ul>
  </section>`;
}

// 预测进度 KPI
function progressHtml(progress) {
  if (!progress) return '';
  const { resolved_total, hit_total, miss_total, due_this_week, overdue_total } = progress;
  const overdue = Number(overdue_total ?? 0);
  const overdueBanner = overdue > 0
    ? `<div class="overdue-banner"><span class="overdue-count">${overdue}</span> 条预测已到期待结算 — <a href="#/calib">去结算</a></div>`
    : '';
  return `<section class="card u-mb-md">
    ${overdueBanner}
    <div class="u-row">
      <div class="kpi-block"><div class="kpi-num">${String(resolved_total ?? 0)}</div><div class="kpi-label">已结算预测</div></div>
      <div class="kpi-block"><div class="kpi-num">${String(hit_total ?? 0)}</div><div class="kpi-label">历史命中</div></div>
      <div class="kpi-block"><div class="kpi-num">${String(miss_total ?? 0)}</div><div class="kpi-label">历史失误</div></div>
      <div class="kpi-block"><div class="kpi-num">${String(due_this_week ?? 0)}</div><div class="kpi-label">本周到期</div></div>
    </div>
  </section>`;
}

// 绑定行动卡交互事件
function bindCardActions(root) {
  // 关闭已有弹窗
  const existingModal = root.querySelector('.modal-backdrop');
  if (existingModal) existingModal.remove();

  root.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) {
      // 点击卡片主体区域，跳转到详情页
      const main = e.target.closest('.ac-main[data-action="open-detail"]');
      if (main) {
        const card = main.closest('.action-card');
        const clusterId = card?.dataset.cluster;
        if (clusterId) location.hash = `#/cluster/${clusterId}`;
      }
      return;
    }

    e.stopPropagation();
    const action = btn.dataset.action;
    const card = btn.closest('.action-card');
    const clusterId = card?.dataset.cluster;
    const impactId = btn.dataset.impact || card?.dataset.impact;

    if (action === 'open-detail' && clusterId) {
      location.hash = `#/cluster/${clusterId}`;
      return;
    }

    if (action === 'predict') {
      // 显示概率选择弹窗
      const low = Number(btn.dataset.probLow || 0.3);
      const high = Number(btn.dataset.probHigh || 0.7);
      const modal = document.createElement('div');
      modal.innerHTML = probabilityPickerHtml(impactId, low, high);
      root.appendChild(modal.firstElementChild);
      return;
    }

    if (action === 'cancel-predict') {
      btn.closest('.modal-backdrop')?.remove();
      return;
    }

    if (action === 'confirm-prob') {
      const prob = Number(btn.dataset.prob);
      btn.disabled = true;
      btn.textContent = '记录中…';
      try {
        await api(`/api/cognition/candidates/${impactId}/confirm`, {
          method: 'POST',
          body: { probability: prob }
        });
        btn.closest('.modal-backdrop')?.remove();
        // 刷新当前视图
        const { renderView } = await import('../router.js');
        renderView();
      } catch (err) {
        btn.disabled = false;
        btn.textContent = '重试';
        alert('记录失败：' + (err.message || '未知错误'));
      }
      return;
    }

    // dismiss / mute / false_positive
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = '处理中…';
    try {
      const payload = action === 'mute' ? { hours: 168 } : {};
      await api(`/api/cognition/clusters/${clusterId}/feedback`, {
        method: 'POST',
        body: { action, ...payload }
      });
      // 操作成功，移除卡片
      card?.classList.add('ac-done');
      setTimeout(() => card?.remove(), 300);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = originalText;
      alert('操作失败：' + (err.message || '未知错误'));
    }
  });

  // 点击遮罩关闭弹窗
  root.addEventListener('click', (e) => {
    if (e.target.matches('[data-role="modal-backdrop"]')) {
      e.target.remove();
    }
  });
}

// 空状态引导
function onboardingHtml(hasInterests, hasSources) {
  const steps = [];
  if (!hasInterests) {
    steps.push(`<li><strong>第一步：登记你的核心利益</strong> — 远见需要知道你关心什么（现金流、工作、健康等），才能把外部信号映射到你的个人影响。<a href="#/tell">立即登记 →</a></li>`);
  }
  if (!hasSources) {
    steps.push(`<li><strong>第二步：启用信息源</strong> — 在「源管理」中启用预置信源（政府官网、权威媒体等），远见开始后台监控。<a href="#/sources">去启用 →</a></li>`);
  }
  if (steps.length) {
    steps.push(`<li>完成后，等待1-2分钟首次研判完成，行动雷达就会开始工作。</li>`);
  }
  return steps.length ? `<div class="radar-onboarding">
    <h3>👋 欢迎使用远见</h3>
    <p>远见是你的个人风险雷达，帮你提前看到影响个人利益的外部变化。开始使用需要简单设置：</p>
    <ol>${steps.join('')}</ol>
  </div>` : '';
}

export async function render(root) {
  const [dashboard, progress, candidates] = await Promise.all([
    api('/api/risk-dashboard').catch(() => null),
    api('/api/forecasts/progress').catch(() => null),
    api('/api/cognition/candidates').catch(() => ({ candidates: [] }))
  ]);

  // 检查是否需要新手引导：没有任何impact项 + 没有pending候选
  const items = Array.isArray(dashboard?.items) ? dashboard.items : [];
  const pendingCandidates = Array.isArray(candidates?.candidates) ? candidates.candidates : [];
  const needsOnboarding = items.length === 0 && pendingCandidates.length === 0 && dashboard?.state !== 'coverage_gap';

  // 简单判断是否有利益/信源（通过dashboard的state和summary推断）
  const state = dashboard?.state || '';
  let sections = '';

  if (needsOnboarding) {
    // 检查是否有interests和sources
    const [interestsData, sourcesData] = await Promise.all([
      api('/api/interests').catch(() => ({ objects: [] })),
      api('/api/external/sources').catch(() => [])
    ]);
    const hasInterests = (interestsData?.objects || []).some(o => o.status === 'active');
    const hasSources = Array.isArray(sourcesData) && sourcesData.some(s => s.enabled);
    sections += onboardingHtml(hasInterests, hasSources);
  }

  if (!dashboard) {
    sections += `<div class="radar-empty">暂时读不到风险面板，请稍后重试。</div>`;
  } else if (state === 'stable' && !needsOnboarding) {
    sections += `<div class="radar-empty">目前没有需要你处理的高等级风险，系统仍在后台监控。</div>`;
  } else if (state === 'coverage_gap' && items.length === 0) {
    sections += `<div class="radar-empty">公开信息监控覆盖不足，系统正在重试。请确认已启用信源。</div>`;
  }

  if (dashboard?.summary && items.length > 0) {
    sections += `<div class="radar-summary">${escapeHtml(dashboard.summary)}</div>`;
  }

  const l4 = items.filter(i => i.mode === 'action' && i.alert_level === 'L4');
  const l3 = items.filter(i => (i.mode === 'action' && i.alert_level === 'L3') || i.mode === 'watch');

  // L4 立即行动区
  if (l4.length > 0 || state === 'action') {
    sections += `<section class="radar-section">
      <h2><span class="dot-l4"></span> L4 · 立即行动</h2>
      ${l4.length ? l4.map(i => actionCardHtml(i, true)).join('') : '<div class="radar-empty">今天没有 L4 等级的立即行动项。</div>'}
    </section>`;
  }

  // L3 准备观察区
  if (l3.length > 0 || state === 'watch') {
    sections += `<section class="radar-section">
      <h2><span class="dot-l3"></span> L3 · 准备观察</h2>
      ${l3.length ? l3.map(i => actionCardHtml(i, false)).join('') : '<div class="radar-empty">暂无需要继续观察的事项。</div>'}
    </section>`;
  }

  // 待确认预测提示
  if (pendingCandidates.length > 0) {
    sections += `<section class="radar-section">
      <h2><span class="dot-pending"></span> 待确认预测（${pendingCandidates.length}）</h2>
      <div class="radar-hint">有 ${pendingCandidates.length} 条候选预测等待你确认概率，确认后进入预测账本开始校准。<a href="#/calib">去校准面板 →</a></div>
    </section>`;
  }

  sections += safeListHtml();

  root.innerHTML = `<div class="u-max">
    ${sections}
    ${progressHtml(progress)}
  </div>`;

  // 告诉远见折叠框
  const tellSlot = document.createElement('div');
  tellSlot.className = 'u-max u-mt-md';
  tellSlot.innerHTML = `<details class="tell card"><summary class="section-title">告诉远见 · 有新情况要记录</summary><div class="tell-slot u-mt-sm"></div></details>`;
  root.appendChild(tellSlot);
  const inlineSlot = tellSlot.querySelector('.tell-slot');
  if (inlineSlot) {
    inlineSlot.innerHTML = tellBoxHtml('inline');
    bindTellBox(inlineSlot);
  }

  // 绑定卡片交互
  bindCardActions(root);
}
